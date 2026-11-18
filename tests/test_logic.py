from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.budget import BudgetError, BudgetLedger
from src.collect import freeze_window
from src.db import connect, init_db, upsert_review, upsert_store
from src.geo import classify_area, haversine_m, university_flag
from src.safe_io import escape_html, neutralize_csv_cell
from src.settings import load_config
from src.stats import store_recent_stats, topic_store_prevalence


class LogicTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "t.sqlite"
        # patch DB path via init with explicit path
        init_db(self.db)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_haversine_and_uni(self) -> None:
        # ~0 m
        self.assertLess(haversine_m(43.66, -79.39, 43.66, -79.39), 1)
        # U of T point should be university adjacent to itself
        flag, name, d = university_flag(43.6629, -79.3957, self.config["geography"], 800)
        self.assertTrue(flag)
        self.assertIsNotNone(name)
        self.assertIsNotNone(d)
        self.assertLessEqual(d, 1)

    def test_downtown_classification(self) -> None:
        area = classify_area(43.65, -79.38, self.config["geography"])
        self.assertEqual(area, "downtown")

    def test_review_window(self) -> None:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        now = datetime(2026, 11, 18, 12, 0, tzinfo=ZoneInfo("America/Toronto"))
        window = freeze_window(self.config, now=now)
        self.assertIn("cutoff_ts", window)
        self.assertLess(window["cutoff_ts"], window["end_ts"])

    def test_textless_ratings_and_topics(self) -> None:
        reviews = [
            {"store_id": "s1", "review_id": "r1", "rating": 5, "original_text": "great coffee", "review_ts": 1},
            {"store_id": "s1", "review_id": "r2", "rating": 1, "original_text": "", "review_ts": 2},
            {"store_id": "s1", "review_id": "r3", "rating": 2, "original_text": "dirty tables", "review_ts": 3},
        ]
        recent = store_recent_stats(reviews)
        self.assertEqual(recent["s1"]["n_ratings"], 3)
        self.assertAlmostEqual(recent["s1"]["recent_mean"], (5 + 1 + 2) / 3)
        self.assertAlmostEqual(recent["s1"]["low_rating_share"], 2 / 3)

        analysis = [
            {
                "review_id": "r1",
                "status": "ok",
                "topics_json": json.dumps(
                    [{"topic": "product_quality", "sentiment": "positive", "evidence": "great coffee"}]
                ),
            },
            {
                "review_id": "r3",
                "status": "ok",
                "topics_json": json.dumps(
                    [
                        {"topic": "cleanliness", "sentiment": "negative", "evidence": "dirty tables"},
                        {"topic": "service", "sentiment": "mixed", "evidence": "dirty tables"},
                    ]
                ),
            },
        ]
        stores = [{"store_id": "s1", "brand": "Second Cup", "area": "downtown", "university_adjacent": 0}]
        # pad to 10 analyzed for eligibility? for unit test check denominator with lower threshold
        rows = topic_store_prevalence(stores, reviews, analysis, self.config["topics"], min_analyzed=2)
        self.assertTrue(rows[0]["eligible_for_topic"])
        self.assertAlmostEqual(rows[0]["topics"]["cleanliness"]["complaint_prevalence"], 0.5)
        # mixed counts in both for service
        self.assertAlmostEqual(rows[0]["topics"]["service"]["complaint_prevalence"], 0.5)
        self.assertAlmostEqual(rows[0]["topics"]["service"]["praise_prevalence"], 0.5)

    def test_budget_refusal_with_pending(self) -> None:
        cfg = json.loads(json.dumps(self.config))
        cfg["budget"]["cap_cad"] = 0.01
        cfg["budget"]["reserve_padding_cad"] = 0
        cfg["budget"]["outscraper_business_free"] = 0
        cfg["budget"]["outscraper_review_free"] = 0
        ledger = BudgetLedger(cfg, db_path=self.db)
        rid = ledger.reserve(kind="outscraper_review", provider="outscraper", units=100, estimated_cad=0.009, note="t")
        with self.assertRaises(BudgetError):
            ledger.reserve(kind="outscraper_review", provider="outscraper", units=100, estimated_cad=0.009, note="t2")
        ledger.release(rid)
        # after release, should allow again
        ledger.reserve(kind="outscraper_review", provider="outscraper", units=1, estimated_cad=0.005, note="ok")

    def test_safe_export(self) -> None:
        self.assertEqual(neutralize_csv_cell("=CMD()"), "'=CMD()")
        self.assertIn("&lt;", escape_html("<script>"))

    def test_duplicate_review_ids(self) -> None:
        with connect(self.db) as conn:
            upsert_store(
                conn,
                {
                    "store_id": "abc",
                    "brand": "Second Cup",
                    "name": "Test",
                    "format": "cafe",
                    "area": "downtown",
                    "university_adjacent": 0,
                    "pilot": 0,
                },
            )
            upsert_review(
                conn,
                {
                    "review_id": "dup1",
                    "store_id": "abc",
                    "rating": 5,
                    "original_text": "a",
                    "source": "test",
                    "within_window": 1,
                },
            )
            upsert_review(
                conn,
                {
                    "review_id": "dup1",
                    "store_id": "abc",
                    "rating": 4,
                    "original_text": "b",
                    "source": "test",
                    "within_window": 1,
                },
            )
            n = conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
            rating = conn.execute("SELECT rating FROM reviews WHERE review_id='dup1'").fetchone()[0]
        self.assertEqual(n, 1)
        self.assertEqual(rating, 4)


if __name__ == "__main__":
    unittest.main()
