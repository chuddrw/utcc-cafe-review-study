from __future__ import annotations

import atexit
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.analyze import GeminiAnalyzer, analyze_pending
from src.budget import BudgetError, BudgetLedger
from src.collect import (
    collect_reviews_for_store,
    expand_and_persist_stores,
    freeze_window,
    select_and_persist_stores,
)
from src.db import connect, get_meta, init_db, rows_to_dicts, set_meta, set_state
from src.logging_util import setup_logging
from src.outscraper_client import OutscraperClient
from src.paths import LOCK_PATH, ensure_dirs
from src.report import export_all
from src.settings import load_secrets

logger = setup_logging()


class RunLock:
    def __init__(self, path: Path = LOCK_PATH):
        self.path = path
        self.held = False

    def acquire(self) -> None:
        ensure_dirs()
        if self.path.exists():
            try:
                pid = int(self.path.read_text().strip().splitlines()[0])
                os.kill(pid, 0)
                raise RuntimeError(f"Another run holds the lock (pid={pid}).")
            except (ValueError, ProcessLookupError, PermissionError):
                pass
        self.path.write_text(f"{os.getpid()}\n{datetime.now(timezone.utc).isoformat()}\n", encoding="utf-8")
        self.held = True
        atexit.register(self.release)

    def release(self) -> None:
        if self.held and self.path.exists():
            try:
                if self.path.read_text().startswith(str(os.getpid())):
                    self.path.unlink(missing_ok=True)
            except Exception:
                pass
        self.held = False


def _deadline(config: dict[str, Any]) -> float:
    return time.time() + float(config["runtime"]["max_hours"]) * 3600


def _export_reserve_hit(config: dict[str, Any], deadline: float) -> bool:
    return time.time() >= deadline - float(config["runtime"]["export_reserve_minutes"]) * 60


def run_pilot(config: dict[str, Any]) -> dict[str, Any]:
    init_db()
    secrets = load_secrets()
    lock = RunLock()
    lock.acquire()
    try:
        print("[pilot] Starting. Outscraper jobs are async and can sit quiet for 1–5 minutes each.", flush=True)
        budget = BudgetLedger(config)
        with connect() as conn:
            window = get_meta(conn, "review_window")
            if not window:
                window = freeze_window(config)
                set_meta(conn, "review_window", window)
                set_meta(conn, "run_end_frozen", window["end_local"])

        client = OutscraperClient(secrets.outscraper_api_key, config, budget)
        try:
            with connect() as conn:
                stores = rows_to_dicts(conn.execute("SELECT * FROM stores").fetchall())
            if len(stores) < 3:
                print("[pilot] Discovering and matching stores...", flush=True)
                stores = select_and_persist_stores(client, config)
                print(f"[pilot] Selected {len(stores)} stores", flush=True)

            pilot_stores = []
            for brand in config["study"]["brands"]:
                for s in stores:
                    if s["brand"] == brand and all(s["store_id"] != p["store_id"] for p in pilot_stores):
                        pilot_stores.append(s)
                        break
            pilot_stores = pilot_stores[: int(config["study"]["pilot_stores"])]
            with connect() as conn:
                for s in pilot_stores:
                    conn.execute("UPDATE stores SET pilot=1 WHERE store_id=?", (s["store_id"],))
                    s["pilot"] = 1

            limit = int(config["study"]["pilot_reviews_per_store"])
            results = []
            for s in pilot_stores:
                print(f"[pilot] Collecting up to {limit} reviews for {s.get('brand')} / {s.get('name')}...", flush=True)
                results.append(
                    collect_reviews_for_store(client, s, config, window, limit=limit, resume=False)
                )
                print(f"[pilot] Collected {results[-1].get('collected')} reviews", flush=True)

            print("[pilot] Running Gemini topic analysis...", flush=True)
            analyzer = GeminiAnalyzer(secrets.gemini_api_key, config, budget)
            with connect() as conn:
                model = get_meta(conn, "gemini_model")
            if model:
                analyzer.model = model
            else:
                try:
                    analyzer.choose_model()
                    with connect() as conn:
                        set_meta(conn, "gemini_model", analyzer.model)
                except Exception as e:
                    logger.error("Gemini unavailable during pilot: %s", e)

            analyze_pending(analyzer, config)
            print("[pilot] Exporting charts/map/report...", flush=True)
            details = export_all(config, window, status="pilot_complete")
            details["pilot_collection"] = results
            print("[pilot] Done.", flush=True)
            return details
        finally:
            client.close()
    finally:
        lock.release()


def run_overnight(config: dict[str, Any], resume: bool = True) -> dict[str, Any]:
    init_db()
    secrets = load_secrets()
    lock = RunLock()
    lock.acquire()
    deadline = _deadline(config)
    status = "complete"
    missing: list[str] = []
    window: dict[str, Any] = {}
    try:
        budget = BudgetLedger(config)
        with connect() as conn:
            window = get_meta(conn, "review_window")
            if not window:
                window = freeze_window(config)
                set_meta(conn, "review_window", window)

        client = OutscraperClient(secrets.outscraper_api_key, config, budget)
        analyzer = GeminiAnalyzer(secrets.gemini_api_key, config, budget)
        try:
            with connect() as conn:
                model = get_meta(conn, "gemini_model")
                stores = rows_to_dicts(conn.execute("SELECT * FROM stores").fetchall())
            if model:
                analyzer.model = model
            else:
                analyzer.choose_model()
                with connect() as conn:
                    set_meta(conn, "gemini_model", analyzer.model)

            if not stores:
                stores = select_and_persist_stores(client, config)

            target = int(config["study"]["reviews_per_store"])
            pilot_extra = target - int(config["study"]["pilot_reviews_per_store"])

            def _collect_pass(store_rows: list[dict[str, Any]], *, label: str) -> None:
                nonlocal status
                # Lowest review counts first so zero/partial stores get budget before deepeners.
                ranked: list[tuple[int, dict[str, Any]]] = []
                with connect() as conn:
                    for s in store_rows:
                        have = conn.execute(
                            "SELECT COUNT(*) AS n FROM reviews WHERE store_id=?", (s["store_id"],)
                        ).fetchone()["n"]
                        ranked.append((int(have), s))
                ranked.sort(key=lambda t: (t[0], str(t[1].get("brand")), str(t[1].get("pair_id"))))
                for have, s in ranked:
                    if _export_reserve_hit(config, deadline):
                        status = "partial"
                        missing.append("runtime_export_reserve")
                        return
                    if budget.remaining() <= 0:
                        status = "partial"
                        missing.append("budget_exhausted")
                        return
                    if have >= target:
                        continue
                    need = target - have
                    if s.get("pilot") and resume and have > 0:
                        need = min(need, max(pilot_extra, need))
                    with connect() as conn:
                        pagination_id = conn.execute(
                            "SELECT pagination_id FROM stores WHERE store_id=?", (s["store_id"],)
                        ).fetchone()
                    store = dict(s)
                    if pagination_id:
                        store["pagination_id"] = pagination_id["pagination_id"]
                    print(
                        f"[{label}] Collecting up to {need} reviews for {s.get('brand')} / {s.get('name')} (have {have})",
                        flush=True,
                    )
                    try:
                        collect_reviews_for_store(
                            client,
                            store,
                            config,
                            window,
                            limit=need,
                            resume=resume and bool(store.get("pagination_id")),
                        )
                        with connect() as conn:
                            set_state(conn, f"collected:{s['store_id']}", True)
                    except BudgetError:
                        status = "partial"
                        missing.append("budget_exhausted")
                        return
                    except Exception as e:
                        logger.exception("Collect failed for %s: %s", s["store_id"], e)
                        missing.append(f"collect:{s['store_id']}")
                        continue

            # 1) Fill existing shortfalls first (protects incomplete triplets).
            _collect_pass(stores, label="fill")

            # 2) Expand with remaining budget (suburban-preferring), then collect new stores.
            if (
                "budget_exhausted" not in missing
                and "runtime_export_reserve" not in missing
                and not _export_reserve_hit(config, deadline)
            ):
                with connect() as conn:
                    stores = rows_to_dicts(conn.execute("SELECT * FROM stores").fetchall())
                have_pairs = len({s.get("pair_id") for s in stores if s.get("pair_id")})
                target_pairs = int(config["study"]["stores_per_brand"])
                need_pairs = max(0, target_pairs - have_pairs)
                if need_pairs > 0:
                    print(
                        f"[overnight] Expanding sample: have {have_pairs} triplets, need +{need_pairs} to reach {target_pairs}",
                        flush=True,
                    )
                    try:
                        added = expand_and_persist_stores(client, config, stores, need_pairs=need_pairs)
                        if added:
                            with connect() as conn:
                                stores = rows_to_dicts(conn.execute("SELECT * FROM stores").fetchall())
                            print(
                                f"[overnight] Sample now {len(stores)} stores / {len(stores)//3} triplets",
                                flush=True,
                            )
                            _collect_pass(added, label="expand-collect")
                        else:
                            missing.append("expansion_no_new_triplets")
                    except BudgetError:
                        status = "partial"
                        missing.append("budget_exhausted_expansion")
                    except Exception as e:
                        logger.exception("Expansion failed: %s", e)
                        missing.append("expansion_error")

            if "budget_exhausted" not in missing and not _export_reserve_hit(config, deadline):
                try:
                    analyze_pending(analyzer, config)
                except BudgetError:
                    status = "partial"
                    missing.append("budget_exhausted_analysis")
                except Exception as e:
                    logger.exception("Analysis failed: %s", e)
                    status = "partial"
                    missing.append("analysis_error")

            details = export_all(config, window, status=status)
            details["missing"] = missing
            return details
        finally:
            client.close()
    except Exception as e:
        logger.exception("Overnight failed: %s", e)
        try:
            with connect() as conn:
                window = get_meta(conn, "review_window") or window
            details = export_all(config, window, status="failed")
            details["error"] = str(e)[:500]
            return details
        except Exception:
            raise
    finally:
        lock.release()
