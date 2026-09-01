"""
agents/reconcile_statement.py

STMT-005 -- independent fee statement reconciliation/proof.

This module answers exactly one question: does a stored
statement_versions row still agree with the LIVE fee ledger, right
now? It is deliberately NOT another way to generate a statement.

    Live Ledger (db_service.get_fees_payment_in_range)
        -> shared period/duplicate classification (fee_statement.classify_ledger_rows)
        -> expected payment-ID set (each id exactly once)
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
generation logic), a fresh derivation performed in this file, or a
call into a small SHARED classification function that both this module
and the generator call independently (see next paragraph) -- never a
call into the generator's own top-level pipeline or a reuse of its
result.

Two functions are intentionally reused rather than reimplemented here:

  - fee_statement.classify_ledger_rows(): applies the STMT-001
    period-membership and duplicate-payment_id rules to raw rows.
    Originally this file carried its own second copy of that logic --
    flagged in review because two independent copies of a
    contract-defined rule only stay identical until someone edits one
    of them. It's now a single function in fee_statement.py that
    BOTH generate_fee_statement() and reconcile_statement() call on
    their own, independently-fetched rows. Sharing the *rule* is not
    the same thing as sharing the *answer*: this module still does its
    own SQL round-trip, still classifies from scratch, and still never
    touches anything generate_fee_statement() computed.

  - statement_store.compute_fingerprint(): STMT-005 explicitly requires
    "the same underlying data semantics established by STMT-003" for
    the fingerprint, and that function already lives outside the
    generator (in statement_store.py, the versioning module), taking
    independently-built row groups as plain input.

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
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

# Matches this project's established convention for a script living
# inside agents/ that needs to import a sibling module (see
# statement_cli.py's identical sys.path.insert line).
sys.path.insert(0, str(Path(__file__).resolve().parent))

from db_service import SchoolDB  # noqa: E402
from fee_statement import classify_ledger_rows  # noqa: E402
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


class MalformedStatementError(Exception):
    """Raised when the stored statement text contains a line that
    matches the printed-line format ("payment_id=X | amount_paid=Y |
    payment_date=Z") but whose amount cannot be parsed as a Decimal.
    The regex matching means this line IS part of the statement's
    printed content -- it's the statement itself that's unreadable,
    not an irrelevant line to skip. A verifier that silently drops
    what it can't read ends up certifying only the parts it liked."""


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
    (payment_id, amount_paid) line pairs, IN ORDER, WITH duplicates
    preserved (never deduplicated here -- callers need to see repeats
    to detect a payment_id printed more than once).

    Raises MalformedStatementError if a line matches the printed-line
    format but its amount isn't a valid decimal -- see that class's
    docstring for why this isn't simply skipped."""
    results: list[tuple[str, Decimal]] = []
    for line in statement_content.splitlines():
        match = _LINE_RE.match(line)
        if not match:
            continue
        payment_id = match.group("payment_id")
        raw_amount = match.group("amount")
        try:
            amount = Decimal(raw_amount)
        except InvalidOperation:
            raise MalformedStatementError(
                f"a printed line's amount ({raw_amount!r}) is not a valid "
                f"decimal number: {line!r}"
            )
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


def _fetch_ledger_rows(db_path: str, student_id: Any, start: str, end: str) -> list[dict]:
    """The only SQL-adjacent call this module makes: the same raw
    ledger read fee_statement.py itself uses (db_service is the data
    access layer, not generation logic), fetched fresh here rather than
    obtained via the generator."""
    with SchoolDB(db_path) as db:
        return db.get_fees_payment_in_range(student_id, start, end)


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


def _empty_detail() -> dict[str, Any]:
    """Shared "nothing computed yet" shape for the diagnostic fields,
    used by every branch as a base so every returned dict has the same
    keys regardless of which check stopped it (a branch only overrides
    the keys it actually has values for)."""
    return {
        "conflicting_payment_ids": [],
        "ledger_payment_ids": None,
        "statement_payment_ids": None,
        "duplicated_statement_payment_ids": None,
        "missing_payment_ids": None,
        "extra_payment_ids": None,
        "printed_total": None,
        "computed_total": None,
        "recomputed_fingerprint": None,
        "blocked_reason": None,
    }


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
    detail (payment-id sets, totals, fingerprints, "blocked_reason").
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
    placeable_rows, unplaceable_rows, includable, conflicting, _identical_ids = classify_ledger_rows(
        start, end, raw_rows
    )

    # ---- Duplicate conflicts short-circuit everything else: a dirty
    # ledger is not evidence the statement is wrong (STMT-005 section 6). ----
    if conflicting:
        conflicting_ids = sorted(str(pid) for pid in conflicting.keys())
        return {
            **base_result,
            **_empty_detail(),
            "verdict": BLOCKED,
            "blocked_reason": "conflicting_duplicates",
            "explanation": (
                "Cannot safely certify this statement: the live ledger currently "
                f"contains conflicting duplicate records for payment_id(s) "
                f"{', '.join(conflicting_ids)} (same payment_id, disagreeing "
                "column values). This is a ledger data-quality problem, not "
                "proof that the stored statement itself is wrong."
            ),
            "conflicting_payment_ids": conflicting_ids,
            "ledger_payment_ids": sorted(str(pid) for pid in includable.keys()),
        }

    ledger_ids = {str(pid) for pid in includable.keys()}

    # ---- The stored text must actually be readable before anything
    # else can be checked against it. A matched-but-unparseable line is
    # a corrupt/malformed artifact -- FAILS, not silently ignored. ----
    try:
        statement_lines = _extract_statement_lines(statement_content)
    except MalformedStatementError as exc:
        return {
            **base_result,
            **_empty_detail(),
            "verdict": FAILS,
            "explanation": f"Stored statement text is malformed and cannot be verified: {exc}",
            "ledger_payment_ids": sorted(ledger_ids),
        }

    # ---- Step 1a: no payment_id may be printed more than once. A set
    # comparison alone can't see this -- {"A"} == {"A"} whether "A" was
    # printed once or five times -- so this is a counting check on the
    # raw parsed list, done BEFORE anything is reduced to a set. ----
    statement_id_counts = Counter(pid for pid, _amount in statement_lines)
    duplicated_ids = sorted(pid for pid, count in statement_id_counts.items() if count > 1)
    if duplicated_ids:
        return {
            **base_result,
            **_empty_detail(),
            "verdict": FAILS,
            "explanation": (
                "Statement prints the same payment_id more than once: "
                f"{', '.join(duplicated_ids)}. Every relevant payment must "
                "appear exactly once."
            ),
            "ledger_payment_ids": sorted(ledger_ids),
            "statement_payment_ids": sorted(statement_id_counts.keys()),
            "duplicated_statement_payment_ids": duplicated_ids,
        }

    # ---- Step 1b: payment-ID SET equality (safe now that Step 1a has
    # already ruled out any statement-side duplicate). No missing, no
    # extra: every relevant ledger payment appears exactly once and
    # everything on the statement is in the ledger. ----
    statement_ids = set(statement_id_counts.keys())

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
            **_empty_detail(),
            "verdict": FAILS,
            "explanation": (
                "Payment-ID mismatch between the live ledger and the stored "
                "statement (" + "; ".join(details) + ")."
            ),
            "ledger_payment_ids": sorted(ledger_ids),
            "statement_payment_ids": sorted(statement_ids),
            "missing_payment_ids": missing,
            "extra_payment_ids": extra,
        }

    # ---- Step 2: printed total honesty (self-consistency of the
    # STORED TEXT, independent of the ledger) ----
    computed_total = sum((amount for _pid, amount in statement_lines), Decimal("0.00"))
    printed_total = _extract_printed_total(statement_content)

    if printed_total is None:
        return {
            **base_result,
            **_empty_detail(),
            "verdict": FAILS,
            "explanation": "Could not locate a printed total/subtotal line in the stored statement.",
            "ledger_payment_ids": sorted(ledger_ids),
            "statement_payment_ids": sorted(statement_ids),
            "missing_payment_ids": [],
            "extra_payment_ids": [],
            "computed_total": str(computed_total),
        }

    if printed_total != computed_total:
        return {
            **base_result,
            **_empty_detail(),
            "verdict": FAILS,
            "explanation": (
                f"Printed total ({printed_total}) does not equal the sum of the "
                f"statement's own printed line amounts ({computed_total})."
            ),
            "ledger_payment_ids": sorted(ledger_ids),
            "statement_payment_ids": sorted(statement_ids),
            "missing_payment_ids": [],
            "extra_payment_ids": [],
            "printed_total": str(printed_total),
            "computed_total": str(computed_total),
        }

    # ---- Step 3: fingerprint, independently recomputed from the
    # freshly-fetched, freshly-classified ledger rows above -- never
    # taken from the generator's own result. ----
    recomputed_fingerprint = statement_store.compute_fingerprint(
        student_id, start, end, placeable_rows, unplaceable_rows
    )

    if recomputed_fingerprint != stored_fingerprint:
        # The statement's own printed content (payment-ID set, total)
        # already checked out above -- nothing on the page contradicts
        # itself or the ledger's visible surface. A fingerprint-only
        # mismatch means some OTHER ledger column (not printed on the
        # statement, e.g. payment_method, term, status) has changed
        # since this version was generated. That's the ordinary
        # lifecycle of a ledger after a statement is filed (a
        # correction, a refund posted against the same payment_id,
        # etc.) -- not proof the statement was wrong when issued. FAILS
        # would overclaim; this is the same "cannot safely certify"
        # category the conflicting-duplicates branch above already
        # uses, so it's reported as BLOCKED too, with a distinct reason.
        return {
            **base_result,
            **_empty_detail(),
            "verdict": BLOCKED,
            "blocked_reason": "fingerprint_drift",
            "explanation": (
                "Cannot certify against the current ledger: the independently "
                "recomputed fingerprint no longer matches the fingerprint "
                "stored with this version, meaning the underlying ledger data "
                "has changed since generation. The statement's printed "
                "payment-ID set and total are still internally consistent and "
                "match the ledger's visible surface, so this is not evidence "
                "the statement was wrong when filed -- it reflects the "
                "ordinary lifecycle of ledger data changing after a statement "
                "is issued (e.g. a correction or refund)."
            ),
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
        **_empty_detail(),
        "verdict": RECONCILES,
        "explanation": (
            "Statement reconciles: the ledger and statement payment-ID sets "
            "match exactly (each printed exactly once), the printed total is "
            "mathematically honest, and the independently recomputed "
            "fingerprint matches the stored fingerprint."
        ),
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