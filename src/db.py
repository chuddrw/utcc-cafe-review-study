from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from src.paths import DB_PATH, ensure_dirs

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS budget_ledger (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  kind TEXT NOT NULL,
  provider TEXT NOT NULL,
  units REAL NOT NULL,
  estimated_cad REAL NOT NULL,
  actual_cad REAL,
  reserved INTEGER NOT NULL DEFAULT 0,
  note TEXT,
  job_id TEXT
);

CREATE TABLE IF NOT EXISTS provider_jobs (
  job_id TEXT PRIMARY KEY,
  provider TEXT NOT NULL,
  kind TEXT NOT NULL,
  status TEXT NOT NULL,
  request_json TEXT,
  result_path TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  error TEXT
);

CREATE TABLE IF NOT EXISTS stores (
  store_id TEXT PRIMARY KEY,
  pair_id TEXT,
  brand TEXT NOT NULL,
  name TEXT,
  address TEXT,
  lat REAL,
  lon REAL,
  format TEXT,
  area TEXT,
  university_adjacent INTEGER,
  university_name TEXT,
  match_distance_m REAL,
  selection_reason TEXT,
  maps_url TEXT,
  place_id TEXT,
  google_id TEXT,
  lifetime_rating REAL,
  lifetime_review_count INTEGER,
  collected_at TEXT,
  pagination_id TEXT,
  pilot INTEGER DEFAULT 0,
  raw_json TEXT
);

CREATE TABLE IF NOT EXISTS reviews (
  review_id TEXT PRIMARY KEY,
  store_id TEXT NOT NULL,
  rating REAL,
  review_date TEXT,
  review_ts INTEGER,
  original_text TEXT,
  language TEXT,
  review_url TEXT,
  source TEXT,
  fetched_at TEXT,
  within_window INTEGER,
  text_hash TEXT,
  FOREIGN KEY(store_id) REFERENCES stores(store_id)
);

CREATE TABLE IF NOT EXISTS analysis (
  review_id TEXT PRIMARY KEY,
  model TEXT,
  prompt_version TEXT,
  language TEXT,
  english_translation TEXT,
  topics_json TEXT,
  keywords_json TEXT,
  uncertain INTEGER,
  status TEXT,
  error TEXT,
  analyzed_at TEXT,
  FOREIGN KEY(review_id) REFERENCES reviews(review_id)
);

CREATE TABLE IF NOT EXISTS analysis_cache (
  cache_key TEXT PRIMARY KEY,
  response_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS run_state (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


@contextmanager
def connect(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    ensure_dirs()
    path = db_path or DB_PATH
    # timeout waits on locks instead of failing immediately during long Gemini calls
    conn = sqlite3.connect(path, timeout=60.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 60000")
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.Error:
        pass
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: Path | None = None) -> None:
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 60000")


def set_meta(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value) if not isinstance(value, str) else value),
    )


def get_meta(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    if not row:
        return default
    try:
        return json.loads(row["value"])
    except json.JSONDecodeError:
        return row["value"]


def set_state(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO run_state(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value)),
    )


def get_state(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = conn.execute("SELECT value FROM run_state WHERE key=?", (key,)).fetchone()
    if not row:
        return default
    return json.loads(row["value"])


def upsert_store(conn: sqlite3.Connection, store: dict[str, Any]) -> None:
    cols = [
        "store_id", "pair_id", "brand", "name", "address", "lat", "lon", "format", "area",
        "university_adjacent", "university_name", "match_distance_m", "selection_reason",
        "maps_url", "place_id", "google_id", "lifetime_rating", "lifetime_review_count",
        "collected_at", "pagination_id", "pilot", "raw_json",
    ]
    values = [store.get(c) for c in cols]
    placeholders = ",".join("?" for _ in cols)
    updates = ",".join(f"{c}=excluded.{c}" for c in cols if c != "store_id")
    conn.execute(
        f"INSERT INTO stores({','.join(cols)}) VALUES({placeholders}) ON CONFLICT(store_id) DO UPDATE SET {updates}",
        values,
    )


def upsert_review(conn: sqlite3.Connection, review: dict[str, Any]) -> None:
    cols = [
        "review_id", "store_id", "rating", "review_date", "review_ts", "original_text",
        "language", "review_url", "source", "fetched_at", "within_window", "text_hash",
    ]
    values = [review.get(c) for c in cols]
    placeholders = ",".join("?" for _ in cols)
    updates = ",".join(f"{c}=excluded.{c}" for c in cols if c != "review_id")
    conn.execute(
        f"INSERT INTO reviews({','.join(cols)}) VALUES({placeholders}) ON CONFLICT(review_id) DO UPDATE SET {updates}",
        values,
    )


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]
