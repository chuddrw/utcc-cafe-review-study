from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.db import connect
from src.paths import DB_PATH


class BudgetError(RuntimeError):
    pass


class BudgetLedger:
    def __init__(self, config: dict[str, Any], db_path: Path | None = None):
        self.cfg = config["budget"]
        self.cap = float(self.cfg["cap_cad"])
        self.db_path = db_path or DB_PATH

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def spent_and_reserved(self) -> tuple[float, float]:
        with connect(self.db_path) as conn:
            spent = conn.execute(
                "SELECT COALESCE(SUM(COALESCE(actual_cad, estimated_cad)), 0) FROM budget_ledger WHERE reserved=0"
            ).fetchone()[0]
            reserved = conn.execute(
                "SELECT COALESCE(SUM(estimated_cad), 0) FROM budget_ledger WHERE reserved=1"
            ).fetchone()[0]
        return float(spent), float(reserved)

    def remaining(self) -> float:
        spent, reserved = self.spent_and_reserved()
        return self.cap - spent - reserved

    def estimate_outscraper_cad(self, business_units: float = 0, review_units: float = 0) -> float:
        free_b = float(self.cfg["outscraper_business_free"])
        free_r = float(self.cfg["outscraper_review_free"])
        used_b = self._used_units("outscraper_business")
        used_r = self._used_units("outscraper_review")
        billable_b = max(0.0, (used_b + business_units) - free_b) - max(0.0, used_b - free_b)
        billable_r = max(0.0, (used_r + review_units) - free_r) - max(0.0, used_r - free_r)
        usd = (billable_b + billable_r) / 1000.0 * float(self.cfg["outscraper_per_1000_usd"])
        return usd * float(self.cfg["usd_to_cad"]) * float(self.cfg["tax_buffer"])

    def estimate_gemini_cad(self, input_tokens: int, output_tokens: int) -> float:
        if self.cfg.get("gemini_assume_free_tier", True):
            return 0.0
        usd = (
            input_tokens / 1_000_000 * float(self.cfg["gemini_input_per_1m_usd"])
            + output_tokens / 1_000_000 * float(self.cfg["gemini_output_per_1m_usd"])
        )
        return usd * float(self.cfg["usd_to_cad"]) * float(self.cfg["tax_buffer"])

    def _used_units(self, kind: str) -> float:
        with connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(units), 0) FROM budget_ledger WHERE kind=? AND reserved=0",
                (kind,),
            ).fetchone()
        return float(row[0])

    def reserve(
        self,
        *,
        kind: str,
        provider: str,
        units: float,
        estimated_cad: float,
        note: str = "",
        job_id: str | None = None,
    ) -> int:
        estimated_cad = float(estimated_cad) + float(self.cfg.get("reserve_padding_cad", 0)) * 0.05
        if estimated_cad > self.remaining() + 1e-9:
            raise BudgetError(
                f"Budget refusal: need ~CAD {estimated_cad:.4f}, remaining CAD {self.remaining():.4f}"
            )
        with connect(self.db_path) as conn:
            cur = conn.execute(
                """INSERT INTO budget_ledger(ts, kind, provider, units, estimated_cad, reserved, note, job_id)
                   VALUES(?,?,?,?,?,1,?,?)""",
                (self._now(), kind, provider, units, estimated_cad, note, job_id),
            )
            return int(cur.lastrowid)

    def finalize(self, reserve_id: int, *, actual_cad: float | None = None, units: float | None = None) -> None:
        with connect(self.db_path) as conn:
            row = conn.execute("SELECT * FROM budget_ledger WHERE id=?", (reserve_id,)).fetchone()
            if not row:
                return
            conn.execute(
                """UPDATE budget_ledger SET reserved=0, actual_cad=?, units=COALESCE(?, units) WHERE id=?""",
                (actual_cad if actual_cad is not None else row["estimated_cad"], units, reserve_id),
            )

    def release(self, reserve_id: int) -> None:
        with connect(self.db_path) as conn:
            conn.execute("DELETE FROM budget_ledger WHERE id=? AND reserved=1", (reserve_id,))

    def record_direct(
        self,
        *,
        kind: str,
        provider: str,
        units: float,
        estimated_cad: float,
        actual_cad: float | None = None,
        note: str = "",
        job_id: str | None = None,
    ) -> None:
        cost = actual_cad if actual_cad is not None else estimated_cad
        if cost > self.remaining() + 1e-9:
            raise BudgetError("Budget refusal: direct spend exceeds remaining allowance")
        with connect(self.db_path) as conn:
            conn.execute(
                """INSERT INTO budget_ledger(ts, kind, provider, units, estimated_cad, actual_cad, reserved, note, job_id)
                   VALUES(?,?,?,?,?,?,0,?,?)""",
                (
                    self._now(),
                    kind,
                    provider,
                    units,
                    estimated_cad,
                    cost,
                    note,
                    job_id,
                ),
            )

    def summary(self) -> dict[str, Any]:
        spent, reserved = self.spent_and_reserved()
        return {
            "cap_cad": self.cap,
            "spent_cad": round(spent, 4),
            "reserved_cad": round(reserved, 4),
            "remaining_cad": round(self.cap - spent - reserved, 4),
            "outscraper_business_units": self._used_units("outscraper_business"),
            "outscraper_review_units": self._used_units("outscraper_review"),
        }
