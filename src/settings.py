from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from src.paths import CONFIG_PATH, ENV_PATH, ROOT


@dataclass
class Secrets:
    outscraper_api_key: str
    gemini_api_key: str

    @property
    def outscraper_present(self) -> bool:
        return bool(self.outscraper_api_key.strip())

    @property
    def gemini_present(self) -> bool:
        return bool(self.gemini_api_key.strip())


def load_config(path: Path | None = None) -> dict[str, Any]:
    cfg_path = path or CONFIG_PATH
    with cfg_path.open(encoding="utf-8") as f:
        return json.load(f)


def load_secrets(env_path: Path | None = None) -> Secrets:
    load_dotenv(env_path or ENV_PATH, override=False)
    return Secrets(
        outscraper_api_key=os.getenv("OUTSCRAPER_API_KEY", "").strip(),
        gemini_api_key=os.getenv("GEMINI_API_KEY", "").strip(),
    )


def project_root() -> Path:
    return ROOT
