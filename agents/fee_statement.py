"""
fee_statement.py

Generates an auditable, reconcilable fee statement for one student over
an explicit inclusive date period, exactly per the Reconcilable Fee
Statement Contract (STMT-001).

    orchestrator / data_agent
        --> fee_statement.generate_fee_statement()
                --> db_service.SchoolDB.get_fees_payment_in_range()  [only SQL call]
                --> statements/statement_{student_id}_{start}_{end}.txt

This module owns ALL contract logic and contains no SQL of its own:
strict-ISO date validation, period membership, the exact REAL ->
shortest-round-trip-string -> Decimal -> ROUND_HALF_UP rounding
pipeline, duplicate-row classification (identical vs conflicting),
total calculation from printed lines only, and generation status
(RECONCILED / BLOCKED).

Out of scope (per STMT-001 / STMT-002 sprint boundaries -- NOT
implemented here): statement versioning, idempotent regeneration, CLI
options for prior versions, automated reconciliation checks beyond
generation-time status, immutability after ledger changes, lenient/fuzzy
date parsing, native float rounding.

Public entrypoint:
    generate_fee_statement(student_id, start, end,
                            db_path="school.db", output_dir=None) -> dict
        {"ok": True,  "file_path": ..., "status": "RECONCILED"/"BLOCKED", "total": "123.45", ...}
        {"ok": False, "error": "..."}    -- caller-facing error; no file written
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from db_service import SchoolDB

# agents/fee_statement.py -> repo root is one level up from agents/
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STATEMENTS_DIR = REPO_ROOT / "statements"

ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

QUANTUM = Decimal("0.01")


class FeeStatementError(Exception):
    """Reserved for genuinely exceptional conditions. Not used for
    contract-defined statement content (malformed dates, duplicates,
    empty periods) -- those are valid statement outcomes, not errors."""


# =========================================================================
# Section 4 rounding pipeline -- the ONLY way a money value may be
# quantized anywhere in this module or its callers. Never round a float
# directly (no `round(value, 2)` on a float anywhere in this file).
# =========================================================================
def _quantize_amount(raw_value: Any) -> Decimal:
    """
    1. Read the stored REAL           -- raw_value, as returned by sqlite3
                                          (already a Python float for a
                                          REAL column).
    2. Shortest round-trip string      -- str(float(raw_value)). Python's
                                          float repr/str (equivalent since
                                          3.1) is guaranteed to be the
                                          shortest decimal string that
                                          round-trips to that exact float.
    3. Parse into Decimal              -- Decimal(that_string), which is
                                          then exact for the string.
                                          NEVER Decimal(float) directly --
                                          that imports the float's raw
                                          binary imprecision (e.g.
                                          Decimal(10.005) == Decimal(
                                          '10.00499999999999989...'))
                                          instead of the clean decimal a
                                          human intended.
    4. Quantize                        -- exactly 2 places, ROUND_HALF_UP.
    """
    if raw_value is None:
        raise FeeStatementError("Cannot quantize a null amount.")
    shortest_round_trip_string = str(float(raw_value))
    return Decimal(shortest_round_trip_string).quantize(QUANTUM, rounding=ROUND_HALF_UP)


def _is_strict_iso_date(value: Any) -> bool:
    """True only for a real calendar date in exactly YYYY-MM-DD form --
    no other separators, no two-digit years, no fuzzy/lenient parsing,
    and rejects shapes that pass the regex but aren't real dates
    (e.g. 2025-02-30)."""
    if not isinstance(value, str) or not ISO_DATE_RE.match(value):
        return False
    try:
        year, month, day = (int(part) for part in value.split("-"))
        date(year, month, day)
        return True
    except ValueError:
        return False


# =========================================================================
# Duplicate classification (Section 4): compare ENTIRE rows (all
# columns), not just the fields that end up printed.
# =========================================================================
def _rows_identical(rows: list[dict]) -> bool:
    first = rows[0]
    return all(row == first for row in rows[1:])


def _classify_by_payment_id(
    rows: list[dict],
) -> tuple[dict[Any, dict], dict[Any, list[dict]], list[Any]]:
    """
    Groups placeable rows by payment_id and returns:
      includable   -- payment_id -> the one row to render as a line
                       (unique rows, or identical-duplicate groups
                       deduplicated to their single shared row)
      conflicting   -- payment_id -> ALL rows verbatim, for any group
                       that disagrees on so much as one column. These
                       ids are excluded from includable/lines/total
                       entirely -- no row is ever picked as a winner.
      identical_ids -- payment_ids that were >1 row but all identical
                       (a ledger anomaly worth flagging even though the
                       statement can still be RECONCILED).
    """
    by_id: dict[Any, list[dict]] = {}
    for row in rows:
        by_id.setdefault(row["payment_id"], []).append(row)

    includable: dict[Any, dict] = {}
    conflicting: dict[Any, list[dict]] = {}
    identical_ids: list[Any] = []

    for payment_id, group in by_id.items():
        if len(group) == 1:
            includable[payment_id] = group[0]
        elif _rows_identical(group):
            includable[payment_id] = group[0]
            identical_ids.append(payment_id)
        else:
            conflicting[payment_id] = group

    return includable, conflicting, identical_ids


@dataclass
class StatementLine:
    payment_id: Any
    amount_paid: Decimal
    payment_date: str
    payment_method: Any = None
    status: Any = None

    def render(self) -> str:
        parts = [
            f"payment_id={self.payment_id}",
            f"amount_paid={self.amount_paid}",
            f"payment_date={self.payment_date}",
        ]
        if self.payment_method is not None:
            parts.append(f"payment_method={self.payment_method}")
        if self.status is not None:
            parts.append(f"status={self.status}")
        return "  " + " | ".join(parts)


@dataclass
class FeeStatementResult:
    student_id: Any
    start: str
    end: str
    lines: list[StatementLine]
    total: Decimal
    generation_status: str  # "RECONCILED" | "BLOCKED"
    unplaceable: list[dict] = field(default_factory=list)
    identical_duplicate_ids: list[Any] = field(default_factory=list)
    conflicting: dict[Any, list[dict]] = field(default_factory=dict)
    file_path: str | None = None

    def render_text(self) -> str:
        out: list[str] = []
        out.append(f"Fee Statement -- student_id={self.student_id}")
        out.append(f"Period: {self.start} to {self.end} (inclusive)")
        out.append(f"Status: {self.generation_status}")
        out.append("")

        if not self.lines:
            out.append("No payments in this period.")
        else:
            label = "Lines" if self.generation_status == "RECONCILED" \
                else "Includable lines (BLOCKED -- see conflicting duplicates below)"
            out.append(f"{label}:")
            out.extend(line.render() for line in self.lines)

        out.append("")
        total_label = "Total" if self.generation_status == "RECONCILED" \
            else "Subtotal (BLOCKED, excludes conflicting payment_id(s))"
        out.append(f"{total_label}: {self.total}")

        if self.identical_duplicate_ids:
            out.append("")
            out.append(
                "Ledger anomaly -- identical duplicate rows deduplicated to one line "
                "for payment_id(s): " + ", ".join(str(i) for i in self.identical_duplicate_ids)
            )

        if self.unplaceable:
            out.append("")
            out.append(
                "Unplaceable payments (excluded from lines and total -- payment_date is "
                "missing or not strict ISO YYYY-MM-DD, so period membership can't be "
                "determined; listed here so nothing is silently dropped):"
            )
            for row in self.unplaceable:
                out.append(f"  payment_id={row.get('payment_id')} raw_payment_date={row.get('payment_date')!r}")

        if self.conflicting:
            out.append("")
            out.append(
                "CONFLICTING duplicates (excluded from lines and total -- every "
                "disagreeing row is listed verbatim below; no row was picked as a winner):"
            )
            for payment_id, group in self.conflicting.items():
                out.append(f"  payment_id={payment_id}:")
                out.extend(f"    {row}" for row in group)

        return "\n".join(out) + "\n"


def _file_name(student_id: Any, start: str, end: str) -> str:
    return f"statement_{student_id}_{start}_{end}.txt"


def generate_fee_statement(
    student_id: Any,
    start: str,
    end: str,
    db_path: str = "school.db",
    output_dir: str | Path | None = None,
) -> dict[str, Any]:
    """
    Generate and write a fee statement for one student over the inclusive
    period [start, end]. Deterministic: the same ledger + same inputs
    always produce the same file content.

    Returns {"ok": False, "error": ...} with NO file written for a
    genuinely invalid request (non-ISO start/end, or start after end).
    Every other outcome -- including "no payments in range" and
    "BLOCKED due to conflicting duplicates" -- is a valid statement and
    always writes a file with {"ok": True, ...}.
    """
    if not _is_strict_iso_date(start) or not _is_strict_iso_date(end):
        return {"ok": False, "error": f"start ({start!r}) and end ({end!r}) must both be strict ISO dates (YYYY-MM-DD)."}

    if start > end:
        return {"ok": False, "error": "Start date is after end date"}

    with SchoolDB(db_path) as db:
        raw_rows = db.get_fees_payment_in_range(student_id, start, end)

    placeable_rows: list[dict] = []
    unplaceable_rows: list[dict] = []
    for row in raw_rows:
        payment_date = row.get("payment_date")
        if _is_strict_iso_date(payment_date) and start <= payment_date <= end:
            if row.get("amount_paid") is None:
                # Mandatory field missing -- can't render a valid line;
                # surface it the same way as a malformed date rather
                # than crashing or silently skipping it.
                unplaceable_rows.append(row)
            else:
                placeable_rows.append(row)
        elif not _is_strict_iso_date(payment_date):
            unplaceable_rows.append(row)
        # else: a valid ISO date outside [start, end] -- correctly not
        # part of this statement; not an anomaly, not surfaced.

    includable, conflicting, identical_duplicate_ids = _classify_by_payment_id(placeable_rows)

    lines = [
        StatementLine(
            payment_id=row["payment_id"],
            amount_paid=_quantize_amount(row["amount_paid"]),
            payment_date=row["payment_date"],
            payment_method=row.get("payment_method"),
            status=row.get("status"),
        )
        for row in sorted(includable.values(), key=lambda r: (r["payment_date"], r["payment_id"]))
    ]

    # Total is derived ONLY from the printed, already-quantized lines --
    # never recomputed from raw amounts.
    total = sum((line.amount_paid for line in lines), Decimal("0.00"))

    generation_status = "BLOCKED" if conflicting else "RECONCILED"

    result = FeeStatementResult(
        student_id=student_id, start=start, end=end,
        lines=lines, total=total, generation_status=generation_status,
        unplaceable=unplaceable_rows,
        identical_duplicate_ids=identical_duplicate_ids,
        conflicting=conflicting,
    )

    target_dir = Path(output_dir) if output_dir is not None else DEFAULT_STATEMENTS_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    file_path = target_dir / _file_name(student_id, start, end)
    file_path.write_text(result.render_text(), encoding="utf-8")
    result.file_path = str(file_path)

    return {
        "ok": True,
        "file_path": str(file_path),
        "status": generation_status,
        "total": str(total),
        "line_count": len(lines),
        "unplaceable_count": len(unplaceable_rows),
        "identical_duplicate_ids": identical_duplicate_ids,
        "conflicting_ids": list(conflicting.keys()),
    }


if __name__ == "__main__":
    import json
    print(json.dumps(generate_fee_statement(1, "2025-09-01", "2025-09-30"), indent=2, default=str))
