#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.collect import freeze_window, import_outscraper_export
from src.db import connect, init_db, set_meta
from src.logging_util import setup_logging
from src.pipeline import run_overnight, run_pilot
from src.preflight import run_preflight
from src.report import export_all
from src.settings import load_config

logger = setup_logging()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Toronto café review research pipeline")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_pre = sub.add_parser("preflight", help="Check environment and optionally probe APIs")
    p_pre.add_argument("--probe", action="store_true", help="Make small bounded paid/auth probes")

    sub.add_parser("pilot", help="Select stores if needed and run 3-store/5-review pilot")

    p_over = sub.add_parser("overnight", help="Unattended collection + analysis + export")
    p_over.add_argument("--resume", action="store_true", help="Resume from checkpoints / pagination")

    p_exp = sub.add_parser("expand", help="Add triplets up to stores_per_brand, then collect/analyze/export")
    p_exp.add_argument("--pairs", type=int, default=None, help="Max new triplets to add (default: fill to stores_per_brand)")
    p_exp.add_argument("--resume", action="store_true", help="Resume review pagination where possible")

    sub.add_parser("export", help="Export CSVs/charts/map/report with no paid calls")

    p_imp = sub.add_parser("import-export", help="Import Outscraper website CSV/JSON without re-collecting")
    p_imp.add_argument("path", type=Path)

    sub.add_parser("analyze", help="Retry Gemini analysis for pending/failed text reviews, then export")

    args = parser.parse_args(argv)
    config = load_config()
    init_db()

    if args.cmd == "preflight":
        report = run_preflight(config, probe_paid=args.probe)
        print(json.dumps(report, indent=2))
        return 0 if report.get("ready_for_pilot") and not report.get("blocker") else 1

    if args.cmd == "pilot":
        details = run_pilot(config)
        print(json.dumps({k: details.get(k) for k in ("status", "stores", "reviews", "analyzed_ok", "budget")}, indent=2))
        return 0

    if args.cmd == "overnight":
        details = run_overnight(config, resume=args.resume)
        print(json.dumps({k: details.get(k) for k in ("status", "stores", "reviews", "analyzed_ok", "budget", "missing")}, indent=2))
        return 0 if details.get("status") in {"complete", "pilot_complete", "partial"} else 1

    if args.cmd == "expand":
        from src.db import rows_to_dicts

        with connect() as conn:
            stores = rows_to_dicts(conn.execute("SELECT * FROM stores").fetchall())
        have_pairs = len({s.get("pair_id") for s in stores if s.get("pair_id")})
        add = int(args.pairs) if args.pairs is not None else max(0, int(config["study"]["stores_per_brand"]) - have_pairs)
        config["study"]["stores_per_brand"] = have_pairs + add
        print(f"[expand] Target triplets: {have_pairs} existing + up to {add} new = {config['study']['stores_per_brand']}", flush=True)
        details = run_overnight(config, resume=args.resume)
        print(json.dumps({k: details.get(k) for k in ("status", "stores", "reviews", "analyzed_ok", "budget", "missing")}, indent=2))
        return 0 if details.get("status") in {"complete", "pilot_complete", "partial"} else 1

    if args.cmd == "export":
        with connect() as conn:
            window = None
            from src.db import get_meta

            window = get_meta(conn, "review_window")
        details = export_all(config, window, status="export_only")
        print(json.dumps(details, indent=2, default=str))
        return 0

    if args.cmd == "analyze":
        from src.analyze import GeminiAnalyzer, analyze_pending
        from src.budget import BudgetLedger
        from src.db import get_meta
        from src.report import export_all
        from src.settings import load_secrets

        secrets = load_secrets()
        budget = BudgetLedger(config)
        analyzer = GeminiAnalyzer(secrets.gemini_api_key, config, budget)
        with connect() as conn:
            model = get_meta(conn, "gemini_model")
            window = get_meta(conn, "review_window")
        if model:
            analyzer.model = model
        else:
            analyzer.choose_model()
            with connect() as conn:
                set_meta(conn, "gemini_model", analyzer.model)
        result = analyze_pending(analyzer, config)
        details = export_all(config, window, status="analyze_complete")
        print(json.dumps({"analyze": result, "export": {k: details.get(k) for k in ("status", "stores", "reviews", "analyzed_ok", "budget")}}, indent=2))
        return 0

    if args.cmd == "import-export":
        with connect() as conn:
            from src.db import get_meta

            window = get_meta(conn, "review_window")
            if not window:
                window = freeze_window(config)
                set_meta(conn, "review_window", window)
        result = import_outscraper_export(args.path, config, window)
        print(json.dumps(result, indent=2))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
