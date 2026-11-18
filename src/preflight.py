from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Any

from src.budget import BudgetLedger
from src.db import connect, init_db, set_meta
from src.logging_util import setup_logging
from src.paths import EXPORTS_DIR, RAW_DIR, ROOT, ensure_dirs
from src.settings import load_secrets

logger = setup_logging()


def check_disk(min_gb: float) -> tuple[bool, str]:
    usage = shutil.disk_usage(ROOT)
    free_gb = usage.free / (1024 ** 3)
    ok = free_gb >= min_gb
    return ok, f"free_disk_gb={free_gb:.2f} (need >={min_gb})"


def check_imports() -> tuple[bool, list[str]]:
    missing = []
    for mod in ("pandas", "httpx", "dotenv", "google.genai", "matplotlib", "folium", "shapely", "pydantic"):
        try:
            __import__(mod if mod != "dotenv" else "dotenv")
        except Exception:
            missing.append(mod)
    return len(missing) == 0, missing


def run_preflight(config: dict[str, Any], *, probe_paid: bool = False) -> dict[str, Any]:
    ensure_dirs()
    init_db()
    secrets = load_secrets()
    ok_imports, missing = check_imports()
    ok_disk, disk_msg = check_disk(float(config["runtime"]["disk_free_gb_min"]))
    writable = all(p.exists() and p.is_dir() for p in (EXPORTS_DIR, RAW_DIR))

    report: dict[str, Any] = {
        "python": sys.version,
        "imports_ok": ok_imports,
        "missing_imports": missing,
        "disk": disk_msg,
        "disk_ok": ok_disk,
        "folders_writable": writable,
        "outscraper_key_present": secrets.outscraper_present,
        "gemini_key_present": secrets.gemini_present,
        "config_city": config["study"]["city"],
        "stores_per_brand": config["study"]["stores_per_brand"],
        "reviews_per_store": config["study"]["reviews_per_store"],
        "budget_cap_cad": config["budget"]["cap_cad"],
        "runtime_hours": config["runtime"]["max_hours"],
        "gemini_candidates": config["gemini"]["model_candidates"],
        "price_assumptions": {
            "outscraper_free_business": config["budget"]["outscraper_business_free"],
            "outscraper_free_reviews": config["budget"]["outscraper_review_free"],
            "outscraper_per_1000_usd": config["budget"]["outscraper_per_1000_usd"],
            "usd_to_cad": config["budget"]["usd_to_cad"],
            "tax_buffer": config["budget"]["tax_buffer"],
            "gemini_assume_free_tier": config["budget"]["gemini_assume_free_tier"],
            "note": "Verify live provider pricing/quotas before overnight. Deposits are separate from the CAD usage cap.",
        },
        "probes": {},
        "ready_for_pilot": False,
    }

    if not secrets.outscraper_present or not secrets.gemini_present:
        report["blocker"] = "Missing API keys in .env (values must be non-empty). Do not paste keys into chat."
        logger.error(report["blocker"])
        return report

    if not ok_imports:
        report["blocker"] = f"Missing packages: {missing}. Run: python -m pip install -r requirements.txt"
        return report

    if not ok_disk:
        report["blocker"] = disk_msg
        return report

    # Optional bounded auth probes
    if probe_paid:
        from src.analyze import GeminiAnalyzer
        from src.outscraper_client import OutscraperClient

        budget = BudgetLedger(config)
        client = OutscraperClient(secrets.outscraper_api_key, config, budget)
        try:
            places = client.maps_search(["Second Cup, Toronto, ON"], limit=1)
            report["probes"]["outscraper"] = {"ok": True, "places": len(places)}
        except Exception as e:
            report["probes"]["outscraper"] = {"ok": False, "error": str(e)[:300]}
        finally:
            client.close()

        analyzer = GeminiAnalyzer(secrets.gemini_api_key, config, budget)
        try:
            model = analyzer.choose_model()
            report["probes"]["gemini"] = {"ok": True, "model": model}
            with connect() as conn:
                set_meta(conn, "gemini_model", model)
        except Exception as e:
            report["probes"]["gemini"] = {"ok": False, "error": str(e)[:300]}
        report["budget"] = budget.summary()

        report["ready_for_pilot"] = bool(
            report["probes"].get("outscraper", {}).get("ok")
            and report["probes"].get("gemini", {}).get("ok")
        )
    else:
        report["ready_for_pilot"] = True
        report["note"] = "Skipped paid probes. Re-run with probe to validate providers."

    logger.info("Preflight complete ready=%s", report["ready_for_pilot"])
    return report
