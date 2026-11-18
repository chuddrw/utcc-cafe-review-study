from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any

from src.geo import area_bucket, classify_area, haversine_m, infer_format, university_flag
from src.logging_util import setup_logging

logger = setup_logging()

BRAND_PATTERNS = {
    "Second Cup": re.compile(r"\bsecond\s*cup\b", re.I),
    "Tim Hortons": re.compile(r"\btim\s*hortons?\b", re.I),
    "Starbucks": re.compile(r"\bstarbucks\b", re.I),
}


def _stable_store_id(place: dict[str, Any]) -> str:
    base = place.get("place_id") or place.get("google_id") or place.get("full_address") or place.get("name")
    return hashlib.sha1(str(base).encode("utf-8")).hexdigest()[:16]


def _is_operating(place: dict[str, Any]) -> bool:
    status = str(place.get("business_status") or place.get("status") or "").lower()
    if status in {"closed", "permanently_closed", "temporarily_closed"}:
        return False
    if place.get("closed") is True:
        return False
    return True


def _brand_match(name: str, brand: str) -> bool:
    return bool(BRAND_PATTERNS[brand].search(name or ""))


def _tie_key(place: dict[str, Any], seed: int) -> str:
    pid = place.get("place_id") or place.get("google_id") or place.get("name")
    return hashlib.sha1(f"{seed}:{pid}".encode()).hexdigest()


def normalize_place(place: dict[str, Any], brand: str, config: dict[str, Any]) -> dict[str, Any] | None:
    name = place.get("name") or ""
    if not _brand_match(name, brand):
        return None
    if not _is_operating(place):
        return None
    lat = place.get("latitude")
    lon = place.get("longitude")
    if lat is None or lon is None:
        return None
    lat, lon = float(lat), float(lon)
    geography = config["geography"]
    area = classify_area(lat, lon, geography)
    if area == "outside_toronto":
        return None
    uni, uni_name, uni_d = university_flag(
        lat, lon, geography, float(config["study"]["university_radius_m"])
    )
    maps_url = place.get("location_link") or place.get("location_url")
    if not maps_url and place.get("place_id"):
        maps_url = f"https://www.google.com/maps/place/?q=place_id:{place['place_id']}"
    return {
        "store_id": _stable_store_id(place),
        "brand": brand,
        "name": name,
        "address": place.get("full_address") or place.get("address"),
        "lat": lat,
        "lon": lon,
        "format": infer_format(place),
        "area": area,
        "university_adjacent": 1 if uni else 0,
        "university_name": uni_name,
        "university_distance_m": uni_d,
        "maps_url": maps_url,
        "place_id": place.get("place_id"),
        "google_id": place.get("google_id"),
        "lifetime_rating": place.get("rating"),
        "lifetime_review_count": place.get("reviews"),
        "raw_json": None,
        "_raw": place,
    }


def prefer_cafe_format(fmt: str) -> int:
    order = {"cafe": 0, "unknown": 1, "mall_food_court": 2, "drive_through": 3, "kiosk": 4, "hospital": 5, "airport": 6}
    return order.get(fmt, 9)


def discover_second_cup(client: Any, config: dict[str, Any]) -> list[dict[str, Any]]:
    geo = config["geography"]
    queries = list(geo["downtown_search_queries"])
    for area in geo["suburban_areas"]:
        queries.append(geo["suburban_search_query_template"].format(name=area["name"]))
    # Bound discovery
    limit = int(config["outscraper"]["search_limit_per_query"])
    places = client.maps_search(queries, limit=limit)
    seen = {}
    for p in places:
        norm = normalize_place(p, "Second Cup", config)
        if not norm:
            continue
        seen[norm["store_id"]] = norm
    return list(seen.values())


def select_anchors(candidates: list[dict[str, Any]], config: dict[str, Any]) -> list[dict[str, Any]]:
    seed = int(config["study"]["selection_seed"])
    downtown_n = int(config["study"]["downtown_anchor_target"])
    suburban_n = int(config["study"]["suburban_anchor_target"])
    uni_target = int(config["study"]["university_adjacent_target"])

    def sort_key(c: dict[str, Any]) -> tuple:
        return (prefer_cafe_format(c["format"]), _tie_key(c, seed))

    downtown = sorted([c for c in candidates if area_bucket(c["area"]) == "downtown"], key=sort_key)
    suburban = sorted([c for c in candidates if area_bucket(c["area"]) == "suburban"], key=sort_key)
    other = sorted([c for c in candidates if area_bucket(c["area"]) == "other"], key=sort_key)

    selected: list[dict[str, Any]] = []
    selected.extend(downtown[:downtown_n])
    selected.extend(suburban[:suburban_n])
    # Fill shortfall without inventing
    if len(selected) < downtown_n + suburban_n:
        pool = [c for c in downtown[downtown_n:] + suburban[suburban_n:] + other if c not in selected]
        need = downtown_n + suburban_n - len(selected)
        selected.extend(pool[:need])

    # Prefer university-adjacent among already selected; if short, swap in uni candidates if available
    uni_selected = [s for s in selected if s["university_adjacent"]]
    if len(uni_selected) < uni_target:
        uni_pool = sorted(
            [c for c in candidates if c["university_adjacent"] and c not in selected],
            key=sort_key,
        )
        for u in uni_pool:
            if len([s for s in selected if s["university_adjacent"]]) >= uni_target:
                break
            # replace a non-uni of same bucket if possible
            for i, s in enumerate(selected):
                if not s["university_adjacent"] and area_bucket(s["area"]) == area_bucket(u["area"]):
                    selected[i] = u
                    break
            else:
                if len(selected) < downtown_n + suburban_n:
                    selected.append(u)

    for i, s in enumerate(selected):
        s["selection_reason"] = (
            f"Second Cup anchor; area={s['area']}; format={s['format']}; "
            f"uni={bool(s['university_adjacent'])}; deterministic seed={seed}; rank={i}"
        )
        s["pair_id"] = f"pair_{i+1:02d}"
        s["match_distance_m"] = 0.0
    logger.info(
        "Selected %s Second Cup anchors (downtown=%s suburban=%s uni=%s)",
        len(selected),
        sum(1 for s in selected if area_bucket(s["area"]) == "downtown"),
        sum(1 for s in selected if area_bucket(s["area"]) == "suburban"),
        sum(1 for s in selected if s["university_adjacent"]),
    )
    return selected[: downtown_n + suburban_n]


def _compatible_format(anchor_fmt: str, other_fmt: str) -> bool:
    if anchor_fmt == "unknown" or other_fmt == "unknown":
        return True
    if prefer_cafe_format(other_fmt) <= 1 and prefer_cafe_format(anchor_fmt) <= 1:
        return True
    return anchor_fmt == other_fmt


def match_competitors(
    client: Any,
    anchors: list[dict[str, Any]],
    config: dict[str, Any],
    *,
    used_store_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    seed = int(config["study"]["selection_seed"])
    pref_m = float(config["study"]["match_preferred_km"]) * 1000
    max_m = float(config["study"]["match_max_km"]) * 1000
    used: set[str] = set(used_store_ids or ())
    selected = list(anchors)

    for brand in ("Tim Hortons", "Starbucks"):
        # One bounded nearby search per brand using anchor coordinates
        queries = []
        for a in anchors:
            queries.append(f"{brand} near {a['lat']},{a['lon']}, Toronto, ON")
        logger.info("Searching nearby %s for %s anchors (Outscraper async; may take a few minutes)...", brand, len(anchors))
        print(f"[select] Searching nearby {brand} for {len(anchors)} anchors...", flush=True)
        places = client.maps_search(queries, limit=int(config["outscraper"]["search_limit_per_query"]))
        logger.info("Received %s place records for %s", len(places), brand)
        print(f"[select] Got {len(places)} {brand} place records", flush=True)
        candidates = []
        seen = set()
        for p in places:
            norm = normalize_place(p, brand, config)
            if not norm or norm["store_id"] in seen:
                continue
            seen.add(norm["store_id"])
            candidates.append(norm)

        for anchor in anchors:
            best = None
            best_d = float("inf")
            # Prefer <=1km cafe-compatible; else expand to 3km
            for radius in (pref_m, max_m):
                pool = []
                for c in candidates:
                    if c["store_id"] in used:
                        continue
                    if not _compatible_format(anchor["format"], c["format"]):
                        # still allow if both unknown-ish later; skip noncompatible when better exist
                        if prefer_cafe_format(c["format"]) > 2 and prefer_cafe_format(anchor["format"]) <= 1:
                            continue
                    d = haversine_m(anchor["lat"], anchor["lon"], c["lat"], c["lon"])
                    if d <= radius:
                        pool.append((d, c))
                if pool:
                    pool.sort(key=lambda t: (prefer_cafe_format(t[1]["format"]), t[0], _tie_key(t[1], seed)))
                    best_d, best = pool[0]
                    weak = best_d > pref_m
                    best = dict(best)
                    best["pair_id"] = anchor["pair_id"]
                    best["match_distance_m"] = best_d
                    best["selection_reason"] = (
                        f"Matched to Second Cup {anchor['store_id']} at {best_d:.0f} m; "
                        f"{'weaker match >1km' if weak else 'within 1km'}; seed={seed}"
                    )
                    break
            if best is None:
                logger.warning("No %s match for pair %s", brand, anchor["pair_id"])
                continue
            used.add(best["store_id"])
            selected.append(best)

    # Keep only complete pairs
    by_pair: dict[str, list[dict[str, Any]]] = {}
    for s in selected:
        by_pair.setdefault(s["pair_id"], []).append(s)
    complete = []
    for pair_id, stores in sorted(by_pair.items()):
        brands = {s["brand"] for s in stores}
        if brands >= {"Second Cup", "Tim Hortons", "Starbucks"}:
            # one per brand
            picked = {}
            for s in stores:
                if s["brand"] not in picked:
                    picked[s["brand"]] = s
            if len(picked) == 3:
                complete.extend(picked.values())
            else:
                logger.warning("Incomplete brand set for %s", pair_id)
        else:
            logger.warning("Dropping incomplete pair %s brands=%s", pair_id, brands)
    logger.info("Achieved comparison stores: %s (%s pairs)", len(complete), len(complete) // 3)
    return complete


def _next_pair_ids(existing_pair_ids: set[str], n: int) -> list[str]:
    used_nums = set()
    for pid in existing_pair_ids:
        try:
            used_nums.add(int(str(pid).split("_")[-1]))
        except ValueError:
            continue
    out: list[str] = []
    i = 1
    while len(out) < n:
        if i not in used_nums:
            out.append(f"pair_{i:02d}")
        i += 1
    return out


def select_expansion_anchors(
    candidates: list[dict[str, Any]],
    config: dict[str, Any],
    *,
    exclude_store_ids: set[str],
    need_pairs: int,
    prefer_suburban: bool = True,
) -> list[dict[str, Any]]:
    """Pick additional Second Cup anchors not already in the sample."""
    seed = int(config["study"]["selection_seed"])
    pool = [c for c in candidates if c["store_id"] not in exclude_store_ids]

    def sort_key(c: dict[str, Any]) -> tuple:
        bucket = area_bucket(c["area"])
        # Prefer suburban when expanding from a downtown-heavy sample
        suburban_rank = 0 if (prefer_suburban and bucket == "suburban") else (1 if bucket == "downtown" else 2)
        return (suburban_rank, prefer_cafe_format(c["format"]), _tie_key(c, seed))

    ranked = sorted(pool, key=sort_key)
    # Over-select: matching will drop incomplete triplets
    oversample = max(need_pairs * 3, need_pairs + 8)
    selected = ranked[:oversample]
    pair_ids = _next_pair_ids(set(), len(selected))  # temporary; caller renumbers against DB
    for i, s in enumerate(selected):
        s["selection_reason"] = (
            f"Expansion Second Cup anchor; area={s['area']}; format={s['format']}; "
            f"uni={bool(s['university_adjacent'])}; deterministic seed={seed}; expand_rank={i}"
        )
        s["pair_id"] = pair_ids[i]
        s["match_distance_m"] = 0.0
    logger.info(
        "Expansion candidate anchors: %s (need_pairs=%s suburban=%s downtown=%s)",
        len(selected),
        need_pairs,
        sum(1 for s in selected if area_bucket(s["area"]) == "suburban"),
        sum(1 for s in selected if area_bucket(s["area"]) == "downtown"),
    )
    return selected


def expand_triplets(
    client: Any,
    config: dict[str, Any],
    existing_stores: list[dict[str, Any]],
    *,
    need_pairs: int,
) -> list[dict[str, Any]]:
    """Discover and match additional complete brand triplets without replacing existing stores."""
    if need_pairs <= 0:
        return []
    exclude_ids = {s["store_id"] for s in existing_stores}
    existing_pairs = {s["pair_id"] for s in existing_stores if s.get("pair_id")}
    print(f"[expand] Discovering Second Cup candidates for +{need_pairs} triplets...", flush=True)
    candidates = discover_second_cup(client, config)
    anchors = select_expansion_anchors(
        candidates,
        config,
        exclude_store_ids=exclude_ids,
        need_pairs=need_pairs,
        prefer_suburban=True,
    )
    if not anchors:
        logger.warning("No expansion anchors available")
        return []
    # Assign pair ids that do not collide with existing
    new_pair_ids = _next_pair_ids(existing_pairs, len(anchors))
    for a, pid in zip(anchors, new_pair_ids):
        a["pair_id"] = pid
    matched = match_competitors(client, anchors, config, used_store_ids=exclude_ids)
    by_pair: dict[str, list[dict[str, Any]]] = {}
    for s in matched:
        by_pair.setdefault(s["pair_id"], []).append(s)
    added: list[dict[str, Any]] = []
    taken_ids = set(exclude_ids)
    for pair_id in sorted(by_pair.keys()):
        if len(added) // 3 >= need_pairs:
            break
        picked: dict[str, dict[str, Any]] = {}
        for s in by_pair[pair_id]:
            if s["store_id"] in taken_ids:
                continue
            if s["brand"] not in picked:
                picked[s["brand"]] = s
        if set(picked) >= {"Second Cup", "Tim Hortons", "Starbucks"}:
            for brand in ("Second Cup", "Tim Hortons", "Starbucks"):
                row = dict(picked[brand])
                row["pair_id"] = pair_id
                added.append(row)
                taken_ids.add(row["store_id"])
    print(f"[expand] Matched {len(added) // 3} new complete triplets ({len(added)} stores)", flush=True)
    return added


def stamp_collection_time(stores: list[dict[str, Any]]) -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc).isoformat()
    out = []
    for s in stores:
        row = {k: v for k, v in s.items() if not k.startswith("_")}
        row["collected_at"] = now
        row["university_adjacent"] = int(bool(row.get("university_adjacent")))
        out.append(row)
    return out
