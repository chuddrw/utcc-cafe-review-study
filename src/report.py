from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import folium
import matplotlib.pyplot as plt

from src.budget import BudgetLedger
from src.db import connect, get_meta
from src.geo import area_bucket
from src.paths import EXPORTS_DIR, ensure_dirs
from src.safe_io import escape_html, write_csv
from src.stats import compute_all

BRAND_COLORS = {"Second Cup": "#8B4513", "Tim Hortons": "#C8102E", "Starbucks": "#00704A"}


def _footer(ax, text: str) -> None:
    ax.figure.text(0.01, 0.01, text, fontsize=7, ha="left", va="bottom")


def chart_brand_ratings(stats: dict[str, Any], window: dict[str, Any], out_base: Path) -> None:
    stores = stats["stores"]
    recent = stats["recent_by_store"]
    brands = ["Second Cup", "Tim Hortons", "Starbucks"]
    fig, ax = plt.subplots(figsize=(8, 5))
    xs = []
    ys = []
    colors = []
    for i, brand in enumerate(brands):
        vals = []
        for s in stores:
            if s["brand"] != brand:
                continue
            m = (recent.get(s["store_id"]) or {}).get("recent_mean")
            if m is None:
                continue
            vals.append(m)
            xs.append(i)
            ys.append(m)
            colors.append(BRAND_COLORS.get(brand, "#666"))
        if vals:
            ax.scatter([i] * len(vals), vals, c=BRAND_COLORS.get(brand, "#666"), alpha=0.7, label=None)
            ax.hlines(sum(vals) / len(vals), i - 0.3, i + 0.3, colors="black", linewidths=2)
    ax.set_xticks(range(len(brands)))
    ax.set_xticklabels(brands)
    ax.set_ylim(1, 5)
    ax.set_ylabel("Recent mean star rating (collected window)")
    ax.set_title("Brand ratings: store means and equal-store averages")
    n_stores = len([s for s in stores if (recent.get(s["store_id"]) or {}).get("recent_mean") is not None])
    n_reviews = sum(int((recent.get(s["store_id"]) or {}).get("n_ratings") or 0) for s in stores)
    _footer(
        ax,
        f"Equal weight per store. Lifetime Google ratings excluded from series. "
        f"Stores={n_stores}, reviews={n_reviews}. Window ends {window.get('end_local', '')}.",
    )
    fig.tight_layout(rect=[0, 0.06, 1, 1])
    fig.savefig(out_base.with_suffix(".png"), dpi=160)
    fig.savefig(out_base.with_suffix(".svg"))
    plt.close(fig)


def chart_topic_comparison(stats: dict[str, Any], config: dict[str, Any], window: dict[str, Any], out_base: Path) -> None:
    topics = config["topics"]
    brands = ["Second Cup", "Tim Hortons", "Starbucks"]
    complaint = []
    praise = []
    for brand in brands:
        crow, prow = [], []
        for topic in topics:
            row = next((r for r in stats["brand_topics"] if r["brand"] == brand and r["topic"] == topic), None)
            crow.append(row["complaint_prevalence"] if row else None)
            prow.append(row["praise_prevalence"] if row else None)
        complaint.append(crow)
        praise.append(prow)

    fig, axes = plt.subplots(1, 2, figsize=(12, 4.5))
    for ax, mat, title in (
        (axes[0], complaint, "Complaint prevalence (neg+mixed)"),
        (axes[1], praise, "Praise prevalence (pos+mixed)"),
    ):
        data = [[(0.0 if v is None else v) for v in row] for row in mat]
        im = ax.imshow(data, cmap="YlOrRd", vmin=0, vmax=1, aspect="auto")
        # grey unavailable
        for i, row in enumerate(mat):
            for j, v in enumerate(row):
                if v is None:
                    ax.add_patch(plt.Rectangle((j - 0.5, i - 0.5), 1, 1, color="#cccccc"))
                else:
                    ax.text(j, i, f"{v:.2f}", ha="center", va="center", fontsize=8)
        ax.set_xticks(range(len(topics)))
        ax.set_xticklabels(topics, rotation=30, ha="right")
        ax.set_yticks(range(len(brands)))
        ax.set_yticklabels(brands)
        ax.set_title(title)
        fig.colorbar(im, ax=ax, fraction=0.046)
    thin = [r for r in stats["brand_topics"] if r.get("thin_evidence")]
    _footer(
        axes[0],
        f"Store equal-weight means among stores with ≥{config['study']['min_analyzed_reviews_for_topic']} analyzed text reviews. "
        f"Mixed counts in both. Grey=unavailable. Thin-evidence flags={len(set((t['brand'] for t in thin)))}. "
        f"Window ends {window.get('end_local', '')}.",
    )
    fig.tight_layout(rect=[0, 0.08, 1, 1])
    fig.savefig(out_base.with_suffix(".png"), dpi=160)
    fig.savefig(out_base.with_suffix(".svg"))
    plt.close(fig)


def chart_area_comparison(stats: dict[str, Any], window: dict[str, Any], out_base: Path) -> None:
    rows = stats["area_ratings"]
    area_rows = [r for r in rows if r["group_type"] == "area" and r["group"] in {"downtown", "suburban", "other"}]
    uni_rows = [r for r in rows if r["group_type"] == "university"]
    brands = ["Second Cup", "Tim Hortons", "Starbucks"]
    areas = ["downtown", "suburban", "other"]
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.5))
    ax = axes[0]
    width = 0.25
    for i, brand in enumerate(brands):
        vals = []
        for a in areas:
            row = next((r for r in area_rows if r["brand"] == brand and r["group"] == a), None)
            vals.append(row["equal_store_mean_recent_rating"] if row and row["equal_store_mean_recent_rating"] is not None else float("nan"))
        xs = [x + (i - 1) * width for x in range(len(areas))]
        ax.bar(xs, vals, width=width, label=brand, color=BRAND_COLORS.get(brand))
    ax.set_xticks(range(len(areas)))
    ax.set_xticklabels(areas)
    ax.set_ylim(1, 5)
    ax.set_title("Recent ratings by area")
    ax.legend(fontsize=8)

    ax2 = axes[1]
    for i, brand in enumerate(brands):
        row = next((r for r in uni_rows if r["brand"] == brand), None)
        val = row["equal_store_mean_recent_rating"] if row else None
        n = row["n_stores"] if row else 0
        if val is None:
            continue
        ax2.bar(i, val, color=BRAND_COLORS.get(brand), label=f"{brand} (n={n})")
    ax2.set_xticks(range(len(brands)))
    ax2.set_xticklabels(brands)
    ax2.set_ylim(1, 5)
    ax2.set_title("University-adjacent panel (overlaps areas)")
    _footer(
        ax,
        "University stores also appear in downtown/suburban/other. Small samples are descriptive only. "
        f"Window ends {window.get('end_local', '')}.",
    )
    fig.tight_layout(rect=[0, 0.08, 1, 1])
    fig.savefig(out_base.with_suffix(".png"), dpi=160)
    fig.savefig(out_base.with_suffix(".svg"))
    plt.close(fig)


def build_map(stats: dict[str, Any], window: dict[str, Any], out_path: Path) -> None:
    stores = stats["stores"]
    recent = stats["recent_by_store"]
    if not stores:
        out_path.write_text("<html><body>No stores</body></html>", encoding="utf-8")
        return
    m = folium.Map(location=[43.70, -79.40], zoom_start=11, tiles="OpenStreetMap")
    lifetime_group = folium.FeatureGroup(name="Lifetime rating", show=True)
    low_group = folium.FeatureGroup(name="Recent low-rating share", show=False)

    def color_for(val: float | None, lo: float, hi: float) -> str:
        if val is None:
            return "#999999"
        t = (val - lo) / (hi - lo) if hi > lo else 0.5
        t = max(0, min(1, t))
        # green-yellow-red-ish
        r = int(255 * t)
        g = int(255 * (1 - t))
        return f"#{r:02x}{g:02x}40"

    for s in stores:
        lat, lon = s.get("lat"), s.get("lon")
        if lat is None or lon is None:
            continue
        st = recent.get(s["store_id"]) or {}
        life = s.get("lifetime_rating")
        low = st.get("low_rating_share")
        popup = (
            f"<b>{escape_html(s.get('brand'))}</b><br>"
            f"{escape_html(s.get('name'))}<br>"
            f"Area: {escape_html(s.get('area'))}<br>"
            f"Format: {escape_html(s.get('format'))}<br>"
            f"Uni: {escape_html(s.get('university_adjacent'))}<br>"
            f"Lifetime rating: {escape_html(life)} (n={escape_html(s.get('lifetime_review_count'))})<br>"
            f"Recent mean: {escape_html(st.get('recent_mean'))} (n={escape_html(st.get('n_ratings'))})<br>"
            f"Low-rating share: {escape_html(low)}<br>"
            f"Date range ts: {escape_html(st.get('oldest_ts'))}–{escape_html(st.get('newest_ts'))}<br>"
            f"<a href=\"{escape_html(s.get('maps_url'))}\" target=\"_blank\" rel=\"noopener\">Maps link</a>"
        )
        folium.CircleMarker(
            location=[lat, lon],
            radius=8,
            color=BRAND_COLORS.get(s["brand"], "#333"),
            fill=True,
            fill_color=color_for(float(life) if life is not None else None, 1, 5),
            fill_opacity=0.85,
            popup=folium.Popup(popup, max_width=300),
            tooltip=f"{s.get('brand')}: lifetime {life}",
        ).add_to(lifetime_group)
        folium.CircleMarker(
            location=[lat, lon],
            radius=8,
            color=BRAND_COLORS.get(s["brand"], "#333"),
            fill=True,
            fill_color=color_for(float(low) if low is not None else None, 0, 0.5),
            fill_opacity=0.85,
            popup=folium.Popup(popup, max_width=300),
            tooltip=f"{s.get('brand')}: low share {low}",
        ).add_to(low_group)
    lifetime_group.add_to(m)
    low_group.add_to(m)
    folium.LayerControl(collapsed=False).add_to(m)
    m.get_root().html.add_child(
        folium.Element(
            f"<div style='position:fixed;bottom:10px;left:10px;background:white;padding:6px;z-index:9999;font-size:12px'>"
            f"Grey = unavailable. Window ends {escape_html(window.get('end_local'))}. Sample stores={len(stores)}.</div>"
        )
    )
    m.save(str(out_path))


def _pick_quotes(stats: dict[str, Any], limit: int = 8) -> list[dict[str, Any]]:
    analysis_ok = [a for a in stats["analysis"] if a.get("status") == "ok"]
    reviews = {r["review_id"]: r for r in stats["reviews"]}
    stores = {s["store_id"]: s for s in stats["stores"]}
    theme_counts: dict[tuple[str, str], int] = {}
    candidates = []
    for a in analysis_ok:
        topics = json.loads(a.get("topics_json") or "[]")
        rev = reviews.get(a["review_id"])
        if not rev:
            continue
        store = stores.get(rev["store_id"], {})
        for t in topics:
            key = (t.get("topic"), t.get("sentiment"))
            theme_counts[key] = theme_counts.get(key, 0) + 1
            candidates.append(
                {
                    "review_id": a["review_id"],
                    "topic": t.get("topic"),
                    "sentiment": t.get("sentiment"),
                    "evidence": t.get("evidence"),
                    "brand": store.get("brand"),
                    "store_name": store.get("name"),
                    "date": rev.get("review_date"),
                    "url": rev.get("review_url") or store.get("maps_url"),
                    "url_fallback": not bool(rev.get("review_url")),
                    "translation": a.get("english_translation"),
                    "language": a.get("language"),
                }
            )
    candidates.sort(key=lambda c: (-theme_counts.get((c["topic"], c["sentiment"]), 0), c["review_id"]))
    picked = []
    seen_text = set()
    for c in candidates:
        if c["evidence"] in seen_text:
            continue
        if c["sentiment"] not in {"positive", "negative", "mixed"}:
            continue
        seen_text.add(c["evidence"])
        picked.append(c)
        if len(picked) >= limit:
            break
    return picked


def write_findings(stats: dict[str, Any], config: dict[str, Any], window: dict[str, Any], budget_summary: dict[str, Any], status: str) -> None:
    ensure_dirs()
    quotes = _pick_quotes(stats)
    brand_ratings = {r["brand"]: r for r in stats["brand_ratings"]}
    findings = []
    # Finding 1 ratings
    if brand_ratings:
        order = sorted(brand_ratings.values(), key=lambda r: r["equal_store_mean_recent_rating"], reverse=True)
        findings.append(
            {
                "title": "Recent ratings among sampled stores",
                "text": (
                    "Among the sampled stores, equal-store average recent ratings were "
                    + ", ".join(
                        f"{r['brand']} {r['equal_store_mean_recent_rating']:.2f} (n_stores={r['n_stores']}, n_reviews={r['n_reviews']})"
                        for r in order
                    )
                    + ". These are newest-first capped samples, not complete annual histories."
                ),
            }
        )
    # Topic findings
    for topic in config["topics"][:2]:
        rows = [r for r in stats["brand_topics"] if r["topic"] == topic and r["complaint_prevalence"] is not None]
        if not rows:
            continue
        rows = sorted(rows, key=lambda r: r["complaint_prevalence"], reverse=True)
        findings.append(
            {
                "title": f"Topic complaints: {topic}",
                "text": (
                    f"Among eligible stores (≥{config['study']['min_analyzed_reviews_for_topic']} analyzed text reviews), "
                    f"complaint prevalence for {topic} was "
                    + ", ".join(f"{r['brand']} {r['complaint_prevalence']:.0%} (eligible stores={r['n_eligible_stores']})" for r in rows)
                    + ". Mixed sentiment counts toward complaints and praise."
                ),
            }
        )
    # Area
    area_rows = [r for r in stats["area_ratings"] if r["group_type"] == "area" and r["n_stores"] > 0]
    if area_rows:
        findings.append(
            {
                "title": "Area patterns are descriptive",
                "text": (
                    "Downtown/suburban/other differences use this study's operational geography and small purposive samples. "
                    "University-adjacent stores overlap those area groups and cannot be summed as exclusive categories. "
                    + "; ".join(
                        f"{r['brand']} {r['group']} mean={r['equal_store_mean_recent_rating']:.2f} (n={r['n_stores']})"
                        for r in area_rows
                        if r["equal_store_mean_recent_rating"] is not None
                    )[:500]
                ),
            }
        )
    # Positive theme if available
    praise_rows = [r for r in stats["brand_topics"] if r.get("praise_prevalence") is not None]
    if praise_rows:
        best = max(praise_rows, key=lambda r: r["praise_prevalence"])
        findings.append(
            {
                "title": "Praise themes also appear",
                "text": (
                    f"Highest observed praise prevalence in the eligible-store summary was {best['brand']} on {best['topic']} "
                    f"at {best['praise_prevalence']:.0%} (eligible stores={best['n_eligible_stores']}). "
                    "This does not imply overall superiority."
                ),
            }
        )

    md_lines = [
        "# Toronto café review study findings",
        "",
        f"Status: **{status}**",
        "",
        "## Scope and limits",
        "",
        "- Toronto municipal study envelope only; surrounding municipalities excluded.",
        f"- Target {config['study']['stores_per_brand']} stores/brand; achieved {len(stats['stores'])} stores.",
        f"- Up to {config['study']['reviews_per_store']} newest reviews per store within 12 calendar months ending {window.get('end_local')}.",
        "- Reviewers are self-selected; store selection is purposive; newest-first caps cover different spans.",
        "- This study does not establish demand for a bar or predict financial returns.",
        "- Do not treat results as representative of all Toronto cafés or national brand performance.",
        "",
        "## Geography sources",
        "",
    ]
    for s in config["geography"]["sources"]:
        md_lines.append(f"- {s}")
    md_lines += ["", "## Findings", ""]
    for i, fnd in enumerate(findings[:5], 1):
        md_lines += [f"### {i}. {fnd['title']}", "", fnd["text"], ""]
    md_lines += ["## Illustrative excerpts", ""]
    for q in quotes:
        link = q["url"] or "unavailable"
        fb = " (store Maps URL fallback)" if q["url_fallback"] else ""
        trans = ""
        if q.get("translation"):
            trans = f" Machine translation available (language={q.get('language')})."
        md_lines.append(
            f"- **{q['brand']}** / {q['topic']} / {q['sentiment']}: “{q['evidence']}” "
            f"— {q['store_name']}, {q['date']}. [source]({link}){fb}. IDs: `{q['review_id']}`.{trans}"
        )
    md_lines += [
        "",
        "## Methods notes",
        "",
        "- Recent store rating = mean of collected ratings including textless ratings.",
        "- Topic prevalences exclude textless ratings and failed analysis; failed ≠ neutral.",
        "- Brand topic metrics average eligible store prevalences (equal store weight).",
        "- Lifetime Google ratings are reported separately from recent collected means.",
        "",
        "## Budget estimate",
        "",
        f"```json\n{json.dumps(budget_summary, indent=2)}\n```",
        "",
    ]
    md = "\n".join(md_lines)
    (EXPORTS_DIR / "findings.md").write_text(md, encoding="utf-8")
    html = "<html><head><meta charset='utf-8'><title>Findings</title></head><body>" + "".join(
        f"<pre>{escape_html(md)}</pre>"
    ) + "</body></html>"
    # Better simple HTML
    html_parts = ["<html><head><meta charset='utf-8'><title>Toronto café findings</title>",
                  "<style>body{font-family:Georgia,serif;max-width:860px;margin:2rem auto;line-height:1.45} code{font-size:0.9em}</style>",
                  "</head><body>"]
    for line in md_lines:
        if line.startswith("# "):
            html_parts.append(f"<h1>{escape_html(line[2:])}</h1>")
        elif line.startswith("## "):
            html_parts.append(f"<h2>{escape_html(line[3:])}</h2>")
        elif line.startswith("### "):
            html_parts.append(f"<h3>{escape_html(line[4:])}</h3>")
        elif line.startswith("- "):
            html_parts.append(f"<li>{escape_html(line[2:])}</li>")
        elif line.startswith("```"):
            continue
        elif line.strip() == "":
            html_parts.append("<br>")
        else:
            html_parts.append(f"<p>{escape_html(line)}</p>")
    html_parts.append("</body></html>")
    (EXPORTS_DIR / "findings.html").write_text("\n".join(html_parts), encoding="utf-8")


def export_csvs(stats: dict[str, Any], window: dict[str, Any]) -> None:
    ensure_dirs()
    stores_rows = []
    for s in stats["stores"]:
        st = stats["recent_by_store"].get(s["store_id"], {})
        stores_rows.append(
            {
                **{k: s.get(k) for k in (
                    "store_id", "pair_id", "brand", "name", "address", "lat", "lon", "format", "area",
                    "university_adjacent", "university_name", "match_distance_m", "selection_reason",
                    "maps_url", "place_id", "lifetime_rating", "lifetime_review_count", "collected_at", "pilot",
                )},
                "recent_mean_rating": st.get("recent_mean"),
                "low_rating_share": st.get("low_rating_share"),
                "n_collected_ratings": st.get("n_ratings"),
                "oldest_review_ts": st.get("oldest_ts"),
                "newest_review_ts": st.get("newest_ts"),
                "area_bucket": area_bucket(s.get("area") or "other"),
            }
        )
    write_csv(
        EXPORTS_DIR / "stores.csv",
        stores_rows,
        list(stores_rows[0].keys()) if stores_rows else ["store_id", "brand"],
    )

    store_maps = {s["store_id"]: s.get("maps_url") for s in stats["stores"]}
    analysis_map = {a["review_id"]: a for a in stats["analysis"]}
    review_rows = []
    for r in stats["reviews"]:
        a = analysis_map.get(r["review_id"])
        url = r.get("review_url")
        fallback = False
        if not url:
            url = store_maps.get(r["store_id"])
            fallback = True
        review_rows.append(
            {
                "review_id": r["review_id"],
                "store_id": r["store_id"],
                "rating": r.get("rating"),
                "review_date": r.get("review_date"),
                "review_ts": r.get("review_ts"),
                "original_text": r.get("original_text"),
                "language": r.get("language"),
                "review_url": url,
                "review_url_is_store_fallback": int(fallback),
                "source": r.get("source"),
                "fetched_at": r.get("fetched_at"),
                "analysis_status": (a or {}).get("status") or "unavailable",
                "window_end": window.get("end_local"),
            }
        )
    write_csv(
        EXPORTS_DIR / "reviews.csv",
        review_rows,
        list(review_rows[0].keys()) if review_rows else ["review_id", "store_id"],
    )

    topic_rows = []
    for a in stats["analysis"]:
        if a.get("status") != "ok":
            continue
        for t in json.loads(a.get("topics_json") or "[]"):
            topic_rows.append(
                {
                    "review_id": a["review_id"],
                    "topic": t.get("topic"),
                    "sentiment": t.get("sentiment"),
                    "evidence": t.get("evidence"),
                    "language": a.get("language"),
                    "english_translation": a.get("english_translation"),
                    "uncertain": a.get("uncertain"),
                    "model": a.get("model"),
                }
            )
    write_csv(
        EXPORTS_DIR / "topics.csv",
        topic_rows,
        ["review_id", "topic", "sentiment", "evidence", "language", "english_translation", "uncertain", "model"],
    )

    summary_rows = []
    for r in stats["brand_ratings"]:
        summary_rows.append({"table": "brand_ratings", **r})
    for r in stats["brand_topics"]:
        summary_rows.append({"table": "brand_topics", **r})
    for r in stats["area_ratings"]:
        summary_rows.append({"table": "area_ratings", **r})
    # normalize keys
    keys = sorted({k for row in summary_rows for k in row.keys()}) or ["table"]
    write_csv(EXPORTS_DIR / "summary_tables.csv", summary_rows, keys)


def write_status(status: str, details: dict[str, Any]) -> None:
    ensure_dirs()
    lines = [
        f"# STATUS: {status}",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        "",
        "```json",
        json.dumps(details, indent=2, default=str),
        "```",
        "",
        "Resume command:",
        "",
        "```bash",
        "python run.py overnight --resume",
        "```",
        "",
    ]
    (EXPORTS_DIR / "STATUS.md").write_text("\n".join(lines), encoding="utf-8")


def export_all(config: dict[str, Any], window: dict[str, Any] | None, status: str = "partial") -> dict[str, Any]:
    ensure_dirs()
    stats = compute_all(config)
    with connect() as conn:
        window = window or get_meta(conn, "review_window") or {}
    budget = BudgetLedger(config).summary()
    chart_brand_ratings(stats, window, EXPORTS_DIR / "brand_ratings")
    chart_topic_comparison(stats, config, window, EXPORTS_DIR / "topic_comparison")
    chart_area_comparison(stats, window, EXPORTS_DIR / "area_comparison")
    build_map(stats, window, EXPORTS_DIR / "map.html")
    export_csvs(stats, window)
    write_findings(stats, config, window, budget, status)
    details = {
        "status": status,
        "stores": len(stats["stores"]),
        "reviews": len(stats["reviews"]),
        "analyzed_ok": sum(1 for a in stats["analysis"] if a.get("status") == "ok"),
        "budget": budget,
        "window": window,
        "outputs": [p.name for p in EXPORTS_DIR.iterdir() if p.is_file()],
    }
    write_status(status, details)
    return details
