from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config.json"
ENV_PATH = ROOT / ".env"
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
DB_PATH = DATA_DIR / "research.sqlite"
EXPORTS_DIR = ROOT / "exports"
LOGS_DIR = ROOT / "logs"
LOG_PATH = LOGS_DIR / "run.log"
LOCK_PATH = DATA_DIR / "run.lock"
FIXTURES_DIR = ROOT / "fixtures"


def ensure_dirs() -> None:
    for path in (DATA_DIR, RAW_DIR, EXPORTS_DIR, LOGS_DIR):
        path.mkdir(parents=True, exist_ok=True)
