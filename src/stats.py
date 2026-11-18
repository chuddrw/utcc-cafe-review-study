from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

from src.db import connect, rows_to_dicts
from src.geo import area_bucket


def load_frames() -> dict[str, Any]:
    with connect() as conn:
        stores = rows_to_dicts(conn.execute("SELECT * FROM stores").fetchall())
        reviews = rows_to_dicts(conn.execute("SELECT * FROM reviews").fetchall())
        analysis = rows_to_dicts(conn.execute("SELECT * FROM analysis").fetchall())
    return {"stores": stores, "reviews": reviews, "analysis": analysis}


def store_recent_stats(reviews: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    by_store: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in reviews:
        by_store[r["store_id"]].append(r)
    out = {}
    for sid, rows in by_store.items():
        ratings = [float(r["rating"]) for r in rows if r.get("rating") is not None]
        low = sum(1 for x in ratings if x <= 2)
        ts = [r["review_ts"] for r in rows if r.get("review_ts") is not None]
        out[sid] = {
            "n_ratings": len(ratings),
            "recent_mean": (sum(ratings) / len(ratings)) if ratings else None,
            "low_rating_share": (low / len(ratings)) if ratings else None,
            "oldest_ts": min(ts) if ts else None,
            "newest_ts": max(ts) if ts else None,
            "n_text": sum(1 for r in rows if (r.get("original_text") or "").strip()),
        }
    return out


def topic_store_prevalence(
    stores: list[dict[str, Any]],
    reviews: list[dict[str, Any]],
    analysis: list[dict[str, Any]],
    topics: list[str],
    min_analyzed: int,
) -> list[dict[str, Any]]:
    analysis_ok = {a["review_id"]: a for a in analysis if a.get("status") == "ok"}
    text_by_store: dict[str, list[str]] = defaultdict(list)
    for r in reviews:
        if (r.get("original_text") or "").strip() and r["review_id"] in analysis_ok:
            text_by_store[r["store_id"]].append(r["review_id"])

    rows = []
    for store in stores:
        sid = store["store_id"]
        ids = text_by_store.get(sid, [])
        n = len(ids)
        eligible = n >= min_analyzed
        topic_stats = {}
        for topic in topics:
            neg = 0
            pos = 0
            for rid in ids:
                topics_list = json.loads(analysis_ok[rid].get("topics_json") or "[]")
                for t in topics_list:
                    if t.get("topic") != topic:
                        continue
                    sent = t.get("sentiment")
                    if sent in {"negative", "mixed"}:
                        neg += 1
                    if sent in {"positive", "mixed"}:
                        pos += 1
            topic_stats[topic] = {
                "complaint_prevalence": (neg / n) if n else None,
                "praise_prevalence": (pos / n) if n else None,
            }
        rows.append(
            {
                "store_id": sid,
                "brand": store["brand"],
                "area": store.get("area"),
                "area_bucket": area_bucket(store.get("area") or "other"),
                "university_adjacent": bool(store.get("university_adjacent")),
                "n_analyzed_text": n,
                "eligible_for_topic": eligible,
                "topics": topic_stats,
            }
        )
    return rows


def brand_equal_weight_means(store_rows: list[dict[str, Any]], recent: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    by_brand: dict[str, list[float]] = defaultdict(list)
    counts: dict[str, dict[str, int]] = defaultdict(lambda: {"stores": 0, "reviews": 0})
    for s in store_rows:
        stats = recent.get(s["store_id"]) or {}
        if stats.get("recent_mean") is None:
            continue
        by_brand[s["brand"]].append(float(stats["recent_mean"]))
        counts[s["brand"]]["stores"] += 1
        counts[s["brand"]]["reviews"] += int(stats.get("n_ratings") or 0)
    out = []
    for brand, vals in by_brand.items():
        out.append(
            {
                "brand": brand,
                "equal_store_mean_recent_rating": sum(vals) / len(vals),
                "n_stores": counts[brand]["stores"],
                "n_reviews": counts[brand]["reviews"],
            }
        )
    return out


def brand_topic_prevalence(store_topic_rows: list[dict[str, Any]], topics: list[str], min_stores: int) -> list[dict[str, Any]]:
    out = []
    brands = sorted({r["brand"] for r in store_topic_rows})
    for brand in brands:
        eligible = [r for r in store_topic_rows if r["brand"] == brand and r["eligible_for_topic"]]
        thin = len(eligible) < min_stores
        for topic in topics:
            comps = [r["topics"][topic]["complaint_prevalence"] for r in eligible if r["topics"][topic]["complaint_prevalence"] is not None]
            prai = [r["topics"][topic]["praise_prevalence"] for r in eligible if r["topics"][topic]["praise_prevalence"] is not None]
            out.append(
                {
                    "brand": brand,
                    "topic": topic,
                    "complaint_prevalence": (sum(comps) / len(comps)) if comps else None,
                    "praise_prevalence": (sum(prai) / len(prai)) if prai else None,
                    "n_eligible_stores": len(eligible),
                    "thin_evidence": thin,
                }
            )
    return out


def area_brand_ratings(stores: list[dict[str, Any]], recent: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[float]] = defaultdict(list)
    for s in stores:
        bucket = area_bucket(s.get("area") or "other")
        mean = (recent.get(s["store_id"]) or {}).get("recent_mean")
        if mean is None:
            continue
        groups[(s["brand"], bucket)].append(float(mean))
    out = []
    for (brand, bucket), vals in sorted(groups.items()):
        out.append(
            {
                "brand": brand,
                "group": bucket,
                "group_type": "area",
                "equal_store_mean_recent_rating": sum(vals) / len(vals),
                "n_stores": len(vals),
            }
        )
    # university panel (overlapping)
    for brand in sorted({s["brand"] for s in stores}):
        vals = []
        for s in stores:
            if s["brand"] != brand or not s.get("university_adjacent"):
                continue
            mean = (recent.get(s["store_id"]) or {}).get("recent_mean")
            if mean is not None:
                vals.append(float(mean))
        out.append(
            {
                "brand": brand,
                "group": "university_adjacent",
                "group_type": "university",
                "equal_store_mean_recent_rating": (sum(vals) / len(vals)) if vals else None,
                "n_stores": len(vals),
            }
        )
    return out


def compute_all(config: dict[str, Any]) -> dict[str, Any]:
    data = load_frames()
    recent = store_recent_stats(data["reviews"])
    store_topics = topic_store_prevalence(
        data["stores"],
        data["reviews"],
        data["analysis"],
        config["topics"],
        int(config["study"]["min_analyzed_reviews_for_topic"]),
    )
    return {
        "stores": data["stores"],
        "reviews": data["reviews"],
        "analysis": data["analysis"],
        "recent_by_store": recent,
        "store_topics": store_topics,
        "brand_ratings": brand_equal_weight_means(data["stores"], recent),
        "brand_topics": brand_topic_prevalence(
            store_topics, config["topics"], int(config["study"]["min_eligible_stores_for_group"])
        ),
        "area_ratings": area_brand_ratings(data["stores"], recent),
    }
