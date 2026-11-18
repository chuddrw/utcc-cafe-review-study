from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from src.db import connect, init_db, set_meta, upsert_review, upsert_store
from src.logging_util import setup_logging
from src.outscraper_client import OutscraperClient
from src.select_stores import (
    discover_second_cup,
    expand_triplets,
    match_competitors,
    select_anchors,
    stamp_collection_time,
)

logger = setup_logging()


def freeze_window(config: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    tz = ZoneInfo(config["study"]["timezone"])
    end = now.astimezone(tz) if now else datetime.now(tz)
    # 12 calendar months back
    year = end.year
    month = end.month - int(config["study"]["review_window_months"])
    while month <= 0:
        month += 12
        year -= 1
    start = end.replace(year=year, month=month, day=min(end.day, 28), hour=0, minute=0, second=0, microsecond=0)
    return {
        "timezone": config["study"]["timezone"],
        "end_local": end.isoformat(),
        "start_local": start.isoformat(),
        "cutoff_ts": int(start.timestamp()),
        "end_ts": int(end.timestamp()),
    }


def _review_id(store_id: str, review: dict[str, Any]) -> str:
    native = review.get("review_id") or review.get("id") or review.get("reviews_id")
    ts = review.get("review_timestamp")
    text = review.get("review_text") or ""
    rating = review.get("review_rating")
    base = f"{store_id}|{native}|{ts}|{rating}|{text[:80]}"
    return hashlib.sha1(base.encode("utf-8")).hexdigest()


def _text_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def select_and_persist_stores(client: OutscraperClient, config: dict[str, Any]) -> list[dict[str, Any]]:
    init_db()
    candidates = discover_second_cup(client, config)
    anchors = select_anchors(candidates, config)
    stores = match_competitors(client, anchors, config)
    stores = stamp_collection_time(stores)
    with connect() as conn:
        for s in stores:
            raw = s.pop("_raw", None)
            if raw is not None:
                s["raw_json"] = json.dumps({k: raw.get(k) for k in ("name", "place_id", "google_id", "category", "subtypes", "type", "located_in") if k in raw})
            upsert_store(conn, s)
        set_meta(conn, "selection_complete", True)
        set_meta(conn, "geo_sources", config["geography"]["sources"])
        set_meta(conn, "achieved_pairs", len(stores) // 3)
    return stores


def expand_and_persist_stores(
    client: OutscraperClient,
    config: dict[str, Any],
    existing_stores: list[dict[str, Any]],
    *,
    need_pairs: int,
) -> list[dict[str, Any]]:
    """Add up to need_pairs new complete triplets; leave existing stores untouched."""
    init_db()
    added = expand_triplets(client, config, existing_stores, need_pairs=need_pairs)
    added = stamp_collection_time(added)
    with connect() as conn:
        for s in added:
            raw = s.pop("_raw", None)
            if raw is not None:
                s["raw_json"] = json.dumps(
                    {
                        k: raw.get(k)
                        for k in ("name", "place_id", "google_id", "category", "subtypes", "type", "located_in")
                        if k in raw
                    }
                )
            upsert_store(conn, s)
        total_pairs = (len(existing_stores) + len(added)) // 3
        set_meta(conn, "selection_complete", True)
        set_meta(conn, "geo_sources", config["geography"]["sources"])
        set_meta(conn, "achieved_pairs", total_pairs)
        set_meta(conn, "expansion_added_pairs", len(added) // 3)
    return added


def collect_reviews_for_store(
    client: OutscraperClient,
    store: dict[str, Any],
    config: dict[str, Any],
    window: dict[str, Any],
    *,
    limit: int,
    resume: bool = True,
) -> dict[str, Any]:
    query = store.get("place_id") or store.get("google_id") or f"{store.get('name')}, {store.get('address')}"
    pagination_id = store.get("pagination_id") if resume else None
    result = client.maps_reviews(
        str(query),
        reviews_limit=limit,
        cutoff_ts=int(window["cutoff_ts"]),
        last_pagination_id=pagination_id,
    )
    reviews = result.get("reviews") or []
    fetched_at = datetime.now(ZoneInfo("UTC")).isoformat()
    kept = 0
    newest = None
    oldest = None
    last_pag = pagination_id
    with connect() as conn:
        for rev in reviews:
            ts = rev.get("review_timestamp")
            if ts is not None and int(ts) < int(window["cutoff_ts"]):
                continue
            text = rev.get("review_text")
            rid = _review_id(store["store_id"], rev)
            url = rev.get("review_link")
            if not url:
                url = None  # fallback applied at export/report time
            row = {
                "review_id": rid,
                "store_id": store["store_id"],
                "rating": rev.get("review_rating"),
                "review_date": rev.get("review_datetime_utc"),
                "review_ts": int(ts) if ts is not None else None,
                "original_text": text,
                "language": rev.get("review_language") or rev.get("language"),
                "review_url": url,
                "source": "outscraper_api",
                "fetched_at": fetched_at,
                "within_window": 1,
                "text_hash": _text_hash(text or ""),
            }
            upsert_review(conn, row)
            kept += 1
            if ts is not None:
                newest = max(newest or int(ts), int(ts))
                oldest = min(oldest or int(ts), int(ts))
            if rev.get("review_pagination_id"):
                last_pag = rev.get("review_pagination_id")
        if last_pag:
            conn.execute("UPDATE stores SET pagination_id=? WHERE store_id=?", (last_pag, store["store_id"]))
    return {"store_id": store["store_id"], "collected": kept, "newest_ts": newest, "oldest_ts": oldest}


def import_outscraper_export(path: Path, config: dict[str, Any], window: dict[str, Any]) -> dict[str, Any]:
    """Import website CSV/JSON exports without triggering paid collection."""
    init_db()
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        payload = json.loads(text)
        if isinstance(payload, dict) and "data" in payload:
            data = payload["data"]
        else:
            data = payload
        places = OutscraperClient._flatten_places(data)
    else:
        import csv
        from io import StringIO

        reader = csv.DictReader(StringIO(text))
        places = list(reader)

    imported_reviews = 0
    imported_stores = 0
    with connect() as conn:
        for place in places:
            # CSV exports may already be review rows
            if "review_text" in place or "review_rating" in place:
                store_key = place.get("place_id") or place.get("google_id") or place.get("name") or "imported"
                store_id = hashlib.sha1(str(store_key).encode()).hexdigest()[:16]
                upsert_store(
                    conn,
                    {
                        "store_id": store_id,
                        "pair_id": place.get("pair_id"),
                        "brand": place.get("brand") or "Unknown",
                        "name": place.get("name"),
                        "address": place.get("full_address") or place.get("address"),
                        "lat": place.get("latitude") or place.get("lat"),
                        "lon": place.get("longitude") or place.get("lon"),
                        "format": place.get("format") or "unknown",
                        "area": place.get("area") or "other",
                        "university_adjacent": int(place.get("university_adjacent") or 0),
                        "selection_reason": "imported_export",
                        "maps_url": place.get("location_link"),
                        "place_id": place.get("place_id"),
                        "google_id": place.get("google_id"),
                        "lifetime_rating": place.get("rating"),
                        "lifetime_review_count": place.get("reviews"),
                        "collected_at": datetime.now(ZoneInfo("UTC")).isoformat(),
                        "pilot": 0,
                    },
                )
                imported_stores += 1
                ts = place.get("review_timestamp")
                if ts is not None and int(float(ts)) < int(window["cutoff_ts"]):
                    continue
                rid = _review_id(store_id, place)
                upsert_review(
                    conn,
                    {
                        "review_id": rid,
                        "store_id": store_id,
                        "rating": place.get("review_rating") or place.get("rating"),
                        "review_date": place.get("review_datetime_utc"),
                        "review_ts": int(float(ts)) if ts not in (None, "") else None,
                        "original_text": place.get("review_text"),
                        "language": place.get("language"),
                        "review_url": place.get("review_link"),
                        "source": f"import:{path.name}",
                        "fetched_at": datetime.now(ZoneInfo("UTC")).isoformat(),
                        "within_window": 1,
                        "text_hash": _text_hash(place.get("review_text") or ""),
                    },
                )
                imported_reviews += 1
                continue

            # place object with reviews_data
            store_id = hashlib.sha1(str(place.get("place_id") or place.get("name")).encode()).hexdigest()[:16]
            upsert_store(
                conn,
                {
                    "store_id": store_id,
                    "brand": place.get("brand") or "Unknown",
                    "name": place.get("name"),
                    "address": place.get("full_address"),
                    "lat": place.get("latitude"),
                    "lon": place.get("longitude"),
                    "format": "unknown",
                    "area": "other",
                    "university_adjacent": 0,
                    "selection_reason": "imported_export",
                    "maps_url": place.get("location_link"),
                    "place_id": place.get("place_id"),
                    "google_id": place.get("google_id"),
                    "lifetime_rating": place.get("rating"),
                    "lifetime_review_count": place.get("reviews"),
                    "collected_at": datetime.now(ZoneInfo("UTC")).isoformat(),
                    "pilot": 0,
                },
            )
            imported_stores += 1
            for rev in place.get("reviews_data") or []:
                ts = rev.get("review_timestamp")
                if ts is not None and int(ts) < int(window["cutoff_ts"]):
                    continue
                upsert_review(
                    conn,
                    {
                        "review_id": _review_id(store_id, rev),
                        "store_id": store_id,
                        "rating": rev.get("review_rating"),
                        "review_date": rev.get("review_datetime_utc"),
                        "review_ts": int(ts) if ts is not None else None,
                        "original_text": rev.get("review_text"),
                        "language": rev.get("language"),
                        "review_url": rev.get("review_link"),
                        "source": f"import:{path.name}",
                        "fetched_at": datetime.now(ZoneInfo("UTC")).isoformat(),
                        "within_window": 1,
                        "text_hash": _text_hash(rev.get("review_text") or ""),
                    },
                )
                imported_reviews += 1
        set_meta(conn, "imported_from", str(path))
    return {"stores": imported_stores, "reviews": imported_reviews}
