"""
agents/reconcile_statement.py

STMT-005 -- independent fee statement reconciliation/proof.

This module answers exactly one question: does a stored
statement_versions row still agree with the LIVE fee ledger, right
now? It is deliberately NOT another way to generate a statement.

    Live Ledger (db_service.get_fees_payment_in_range)
        -> independent period/duplicate classification (this file)
        -> expected payment-ID set
        -> stored statement's own printed payment-ID set (parsed from
           the stored text, not re-derived from generation)
        -> compare
        -> verify the stored statement's printed total is internally
           honest (sum of its own printed lines)
        -> independently recompute the fingerprint
           (statement_store.compute_fingerprint, fed independently
           re-derived row groups)
        -> compare against the stored fingerprint
        -> RECONCILES / BLOCKED / FAILS

CRITICAL ARCHITECTURAL RULE (see STMT-005 spec section 2): this module
must never call fee_statement.generate_fee_statement() or otherwise
treat the generator's own output as the source of truth. Doing so
would make the verifier circular -- the same logic that produced a
statement would also be "proving" it correct. Every step above is
either a raw ledger read (db_service.SchoolDB.get_fees_payment_in_range
-- the same low-level access point the generator itself uses, but not
generation logic) or a fresh derivation performed in this file.

The one piece of logic intentionally reused rather than reimplemented
is statement_store.compute_fingerprint(): STMT-005 explicitly requires
"the same underlying data semantics established by STMT-003" for the
fingerprint, and that function already lives outside the generator (in
statement_store.py, the versioning module), taking independently-built
row groups as plain input. Reusing it is reusing a data-semantics
utility, not asking the generator for an answer.

Public entrypoint:
    reconcile_statement(student_id, start, end, version=None,
                         db_path="school.db") -> dict
        {"verdict": "RECONCILES" | "BLOCKED" | "FAILS",
         "explanation": str, ...diagnostic detail...}

Raises:
    statement_store.PeriodFormatError -- non-ISO or inverted period.
    StatementNotFoundError            -- the requested version (or, if
                                          version is omitted, any
                                          version at all) was never
                                          stored for this student +
                                          period.
"""

from __future__ import annotations

import re
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

# Matches this project's established convention for a script living
# inside agents/ that needs to import a sibling module (see
# statement_cli.py's identical sys.path.insert line).
sys.path.insert(0, str(Path(__file__).resolve().parent))

from date_utils import is_strict_iso_date  # noqa: E402
from db_service import SchoolDB  # noqa: E402
import statement_store  # noqa: E402
from statement_store import PeriodFormatError  # noqa: E402  (re-exported for callers)

RECONCILES = "RECONCILES"
BLOCKED = "BLOCKED"
FAILS = "FAILS"


class StatementNotFoundError(Exception):
    """Raised when the requested (student, period, version) -- or, if
    no version was given, ANY version for that student + period -- has
    never been stored. Not a verdict: there is nothing to reconcile
    against."""


# =========================================================================
# Parsing the STORED statement text. Deliberately narrow: only matches
# the exact StatementLine.render() format ("  payment_id=X |
# amount_paid=Y | payment_date=Z ..."), so it can never accidentally
# pick up entries from the "Unplaceable payments" section (format:
# "payment_id=X raw_payment_date=Y", no pipes) or the "CONFLICTING
# duplicates" section (format: raw Python dict repr, "{'payment_id': ...}",
# no bare "payment_id=" token) -- both by construction, not because this
# code specifically excludes them.
# =========================================================================
_LINE_RE = re.compile(
    r"^\s*payment_id=(?P<payment_id>[^\s|]+)\s*\|\s*amount_paid=(?P<amount>[^\s|]+)\s*\|\s*payment_date="
)
_TOTAL_RE = re.compile(r"^(?:Total|Subtotal[^:]*):\s*(?P<total>\S+)\s*$", re.MULTILINE)


def _extract_statement_lines(statement_content: str) -> list[tuple[str, Decimal]]:
    """Independently parses the stored statement text for its printed
    (payment_id, amount_paid) line pairs."""
    results: list[tuple[str, Decimal]] = []
    for line in statement_content.splitlines():
        match = _LINE_RE.match(line)
        if not match:
            continue
        payment_id = match.group("payment_id")
        try:
            amount = Decimal(match.group("amount"))
        except InvalidOperation:
            continue
        results.append((payment_id, amount))
    return results


def _extract_printed_total(statement_content: str) -> Decimal | None:
    """Independently parses the "Total: X" / "Subtotal (BLOCKED...): X"
    line. Returns None if no such line can be found (itself a FAILS
    condition -- a statement with no readable total can't be certified)."""
    match = _TOTAL_RE.search(statement_content)
    if match is None:
        return None
    try:
        return Decimal(match.group("total"))
    except InvalidOperation:
        return None


# =========================================================================
# Independent ledger extraction + classification. Reproduces the
# STMT-001 period-membership and duplicate-payment_id rules from raw
# ledger rows, derived here from first principles rather than by
# calling fee_statement.py -- see module docstring.
# =========================================================================
def _fetch_ledger_rows(db_path: str, student_id: Any, start: str, end: str) -> list[dict]:
    """The only SQL-adjacent call this module makes: the same raw
    ledger read fee_statement.py itself uses (db_service is the data
    access layer, not generation logic), fetched fresh here rather than
    obtained via the generator."""
    with SchoolDB(db_path) as db:
        return db.get_fees_payment_in_range(student_id, start, end)


def _classify_ledger(
    start: str, end: str, raw_rows: list[dict]
) -> tuple[list[dict], list[dict], dict[Any, dict], dict[Any, list[dict]], list[Any]]:
    """
    Returns (placeable_rows, unplaceable_rows, includable, conflicting,
    identical_duplicate_ids):

      placeable_rows    -- in-period, date-valid, amount present.
      unplaceable_rows  -- date can't rule the row out of the period
                            (missing/malformed date), OR a valid
                            in-period date but a missing amount.
      includable        -- payment_id -> single row: unique rows, or
                            identical-duplicate groups deduplicated to
                            their one shared row.
      conflicting        -- payment_id -> ALL rows verbatim, for any
                            group that disagrees on any column. Excluded
                            from includable entirely.
      identical_duplicate_ids -- payment_ids that were >1 row but all
                            identical.
    """
    placeable_rows: list[dict] = []
    unplaceable_rows: list[dict] = []
    for row in raw_rows:
        payment_date = row.get("payment_date")
        if is_strict_iso_date(payment_date) and start <= payment_date <= end:
            if row.get("amount_paid") is None:
                unplaceable_rows.append(row)
            else:
                placeable_rows.append(row)
        elif not is_strict_iso_date(payment_date):
            unplaceable_rows.append(row)
        # else: a valid ISO date outside [start, end] -- correctly out
        # of scope for this statement's ledger; not an anomaly.

    by_id: dict[Any, list[dict]] = {}
    for row in placeable_rows:
        by_id.setdefault(row["payment_id"], []).append(row)

    includable: dict[Any, dict] = {}
    conflicting: dict[Any, list[dict]] = {}
    identical_duplicate_ids: list[Any] = []
    for payment_id, group in by_id.items():
        if len(group) == 1:
            includable[payment_id] = group[0]
        elif all(row == group[0] for row in group[1:]):
            includable[payment_id] = group[0]
            identical_duplicate_ids.append(payment_id)
        else:
            conflicting[payment_id] = group

    return placeable_rows, unplaceable_rows, includable, conflicting, identical_duplicate_ids


def _get_stored_version(
    db_path: str, student_id: Any, start: str, end: str, version: int | None
) -> dict:
    """Retrieval only -- delegates to statement_store.py, the existing
    STMT-003 storage layer, exactly as statement_cli.py already does.
    Never inserts, never generates."""
    if version is None:
        stored = statement_store.get_latest_version(db_path, student_id, start, end)
        if stored is None:
            raise StatementNotFoundError(
                f"No stored statement versions exist for student {student_id}, "
                f"period {start} to {end}."
            )
        return stored

    stored = statement_store.get_version(db_path, student_id, start, end, version)
    if stored is None:
        raise StatementNotFoundError(
            f"Version {version} has not been issued for student {student_id}, "
            f"period {start} to {end}."
        )
    return stored


def reconcile_statement(
    student_id: Any,
    start: str,
    end: str,
    version: int | None = None,
    db_path: str = "school.db",
) -> dict[str, Any]:
    """
    Independently determines whether a stored fee statement version
    still agrees with the live fee ledger.

    version=None reconciles the latest stored version, matching
    statement_cli.py's existing "--version omitted -> latest" retrieval
    convention.

    Returns a dict always containing "verdict" (one of RECONCILES,
    BLOCKED, FAILS) and "explanation" (human-readable), plus diagnostic
    detail (payment-id sets, totals, fingerprints) -- see individual
    branches below for the exact shape.
    """
    stored = _get_stored_version(db_path, student_id, start, end, version)
    resolved_version = stored["version"]
    statement_content = stored["statement_content"]
    stored_fingerprint = stored["fingerprint"]

    base_result: dict[str, Any] = {
        "student_id": student_id,
        "start": start,
        "end": end,
        "version": resolved_version,
        "sequence": stored["sequence"],
        "generated_at": stored["generated_at"],
        "stored_fingerprint": stored_fingerprint,
    }

    raw_rows = _fetch_ledger_rows(db_path, student_id, start, end)
    placeable_rows, unplaceable_rows, includable, conflicting, _identical_ids = _classify_ledger(
        start, end, raw_rows
    )

    # ---- Duplicate conflicts short-circuit everything else: a dirty
    # ledger is not evidence the statement is wrong (STMT-005 section 6). ----
    if conflicting:
        conflicting_ids = sorted(str(pid) for pid in conflicting.keys())
        return {
            **base_result,
            "verdict": BLOCKED,
            "explanation": (
                "Cannot safely certify this statement: the live ledger currently "
                f"contains conflicting duplicate records for payment_id(s) "
                f"{', '.join(conflicting_ids)} (same payment_id, disagreeing "
                "column values). This is a ledger data-quality problem, not "
                "proof that the stored statement itself is wrong."
            ),
            "conflicting_payment_ids": conflicting_ids,
            "ledger_payment_ids": sorted(str(pid) for pid in includable.keys()),
            "statement_payment_ids": None,
            "missing_payment_ids": None,
            "extra_payment_ids": None,
            "printed_total": None,
            "computed_total": None,
            "recomputed_fingerprint": None,
        }

    # ---- Step 1: payment-ID set equality (exact -- no missing, no
    # extra, no duplicates; every relevant ledger payment appears
    # exactly once and everything on the statement is in the ledger). ----
    ledger_ids = {str(pid) for pid in includable.keys()}
    statement_lines = _extract_statement_lines(statement_content)
    statement_ids = {pid for pid, _amount in statement_lines}

    if ledger_ids != statement_ids:
        missing = sorted(ledger_ids - statement_ids)
        extra = sorted(statement_ids - ledger_ids)
        details = []
        if missing:
            details.append(f"missing from statement: {', '.join(missing)}")
        if extra:
            details.append(f"present on statement but not in ledger: {', '.join(extra)}")
        return {
            **base_result,
            "verdict": FAILS,
            "explanation": (
                "Payment-ID mismatch between the live ledger and the stored "
                "statement (" + "; ".join(details) + ")."
            ),
            "conflicting_payment_ids": [],
            "ledger_payment_ids": sorted(ledger_ids),
            "statement_payment_ids": sorted(statement_ids),
            "missing_payment_ids": missing,
            "extra_payment_ids": extra,
            "printed_total": None,
            "computed_total": None,
            "recomputed_fingerprint": None,
        }

    # ---- Step 2: printed total honesty (self-consistency of the
    # STORED TEXT, independent of the ledger) ----
    computed_total = sum((amount for _pid, amount in statement_lines), Decimal("0.00"))
    printed_total = _extract_printed_total(statement_content)

    if printed_total is None:
        return {
            **base_result,
            "verdict": FAILS,
            "explanation": "Could not locate a printed total/subtotal line in the stored statement.",
            "conflicting_payment_ids": [],
            "ledger_payment_ids": sorted(ledger_ids),
            "statement_payment_ids": sorted(statement_ids),
            "missing_payment_ids": [],
            "extra_payment_ids": [],
            "printed_total": None,
            "computed_total": str(computed_total),
            "recomputed_fingerprint": None,
        }

    if printed_total != computed_total:
        return {
            **base_result,
            "verdict": FAILS,
            "explanation": (
                f"Printed total ({printed_total}) does not equal the sum of the "
                f"statement's own printed line amounts ({computed_total})."
            ),
            "conflicting_payment_ids": [],
            "ledger_payment_ids": sorted(ledger_ids),
            "statement_payment_ids": sorted(statement_ids),
            "missing_payment_ids": [],
            "extra_payment_ids": [],
            "printed_total": str(printed_total),
            "computed_total": str(computed_total),
            "recomputed_fingerprint": None,
        }

    # ---- Step 3: fingerprint, independently recomputed from the
    # freshly-fetched, freshly-classified ledger rows above -- never
    # taken from the generator's own result. ----
    recomputed_fingerprint = statement_store.compute_fingerprint(
        student_id, start, end, placeable_rows, unplaceable_rows
    )

    if recomputed_fingerprint != stored_fingerprint:
        return {
            **base_result,
            "verdict": FAILS,
            "explanation": (
                "Fingerprint mismatch: the independently recomputed fingerprint "
                "of the current live ledger no longer matches the fingerprint "
                "stored with this version -- the relevant ledger data has "
                "changed since this version was generated."
            ),
            "conflicting_payment_ids": [],
            "ledger_payment_ids": sorted(ledger_ids),
            "statement_payment_ids": sorted(statement_ids),
            "missing_payment_ids": [],
            "extra_payment_ids": [],
            "printed_total": str(printed_total),
            "computed_total": str(computed_total),
            "recomputed_fingerprint": recomputed_fingerprint,
        }

    return {
        **base_result,
        "verdict": RECONCILES,
        "explanation": (
            "Statement reconciles: the ledger and statement payment-ID sets "
            "match exactly, the printed total is mathematically honest, and "
            "the independently recomputed fingerprint matches the stored "
            "fingerprint."
        ),
        "conflicting_payment_ids": [],
        "ledger_payment_ids": sorted(ledger_ids),
        "statement_payment_ids": sorted(statement_ids),
        "missing_payment_ids": [],
        "extra_payment_ids": [],
        "printed_total": str(printed_total),
        "computed_total": str(computed_total),
        "recomputed_fingerprint": recomputed_fingerprint,
    }


if __name__ == "__main__":
    import json

    print(json.dumps(reconcile_statement(1, "2025-09-01", "2025-09-30"), indent=2, default=str))
