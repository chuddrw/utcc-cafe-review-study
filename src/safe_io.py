from __future__ import annotations

import csv
import html
import re
from pathlib import Path
from typing import Any, Iterable


FORMULA_RE = re.compile(r"^[=+\-@]")


def escape_html(value: Any) -> str:
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def neutralize_csv_cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, (int, float, bool)):
        return value
    text = str(value)
    if FORMULA_RE.match(text):
        return "'" + text
    return text


def write_csv(path: Path, rows: Iterable[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: neutralize_csv_cell(row.get(k)) for k in fieldnames})


def evidence_is_substring(evidence: str, original: str) -> bool:
    if not evidence:
        return False
    return evidence in original
