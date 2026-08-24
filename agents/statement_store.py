"""
agents/statement_store.py

Narrow persistence/versioning layer for STMT-003. Owns exactly one
concern -- the `statement_versions` table -- and answers only the
questions versioning needs:

    Do I already have this version?
    Did the relevant ledger change (fingerprint match)?
    What is the latest version for student + period?
    Should a new version be created?
    Can I retrieve a specific version?

This module knows nothing about *how* a statement is generated or
rendered -- that stays entirely in fee_statement.py. It only stores and
retrieves whatever content/fingerprint it is given.

Table DDL is NOT declared here. database/schema.py is the single
source of truth for the statement_versions schema (table + immutability
triggers); this module imports and calls create_statement_versions_table()
so that even a caller who never ran the full schema/migration (e.g. a
test using a throwaway sqlite file) still gets the real table shape --
there is exactly one CREATE TABLE for statement_versions in the whole
codebase.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from date_utils import is_strict_iso_date

# database/schema.py lives one directory above agents/, under database/.
_DATABASE_DIR = Path(__file__).resolve().parent.parent / "database"
if str(_DATABASE_DIR) not in sys.path:
    sys.path.insert(0, str(_DATABASE_DIR))

from schema import create_statement_versions_table  # noqa: E402

# Bumped only when a change to the STMT-001 rendering/classification
# contract could make an OLD fingerprint no longer trustworthy (e.g. a
# change to what counts as a "conflicting" duplicate). Included in the
# fingerprint so a future contract change can force new versions
# everywhere, rather than silently reusing content generated under a
# since-changed contract.
RENDERER_CONTRACT_VERSION = "STMT-001.v1"


class PeriodFormatError(ValueError):
    """Raised when a caller passes a non-ISO or inverted period to this
    module directly (defense in depth -- fee_statement.py already
    validates before calling in, but this module must not silently
    trust its caller)."""


def _validate_period(start: str, end: str) -> None:
    if not is_strict_iso_date(start) or not is_strict_iso_date(end):
        raise PeriodFormatError(
            f"start ({start!r}) and end ({end!r}) must both be strict ISO dates (YYYY-MM-DD)."
        )
    if start > end:
        raise PeriodFormatError("Start date is after end date")


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    # Autocommit mode: we manage transactions explicitly (BEGIN
    # IMMEDIATE / COMMIT / ROLLBACK) in get_or_create_version() below,
    # so sqlite3's own implicit-transaction handling must stay out of
    # the way.
    conn.isolation_level = None
    conn.execute("PRAGMA foreign_keys = ON;")
    # A concurrent writer that loses the race for the IMMEDIATE lock
    # waits (up to 5s) instead of failing immediately with
    # "database is locked".
    conn.execute("PRAGMA busy_timeout = 5000;")
    conn.row_factory = sqlite3.Row
    create_statement_versions_table(conn)
    return conn


def compute_fingerprint(
    student_id: Any, start: str, end: str, relevant_rows: list[dict]
) -> str:
    """
    Deterministic fingerprint of the ledger state relevant to
    `student_id` over [start, end].

    The caller is responsible for scoping `relevant_rows` to exactly
    the rows that can influence the generated statement (placeable
    rows within the period, plus unplaceable rows whose date can't
    rule them out of the period) -- rows with a *valid* date outside
    [start, end] must never be passed in here, which is what keeps
    out-of-period ledger activity from ever changing the fingerprint
    (STMT-003 Scenario C).

    Narrowing: `student_id` is dropped from each row before hashing --
    it's already a fixed, outer fingerprint input (every row was
    fetched for this same student), so keeping it per-row too would be
    redundant, not "rendered content". No other column is dropped:
    STMT-001's duplicate classification (_classify_by_payment_id /
    _rows_identical in fee_statement.py) compares FULL rows, so a
    change in any other column -- even one that isn't itself printed,
    e.g. `term` -- can flip a group from "identical" to "conflicting"
    and change RECONCILED -> BLOCKED, which is very much rendered
    content. Dropping any of those columns here would make the
    fingerprint blind to a real, contract-relevant change.

    Determinism is guaranteed by:
      - serializing each row with sort_keys=True, so column order
        never matters
      - sorting the resulting list of serialized rows, so raw
        retrieval/dict ordering never matters
      - never including timestamps, random ids, or object identity
    """
    _validate_period(start, end)
    serialized_rows = sorted(
        json.dumps(
            {k: v for k, v in row.items() if k != "student_id"},
            sort_keys=True,
            default=str,
        )
        for row in relevant_rows
    )
    payload = json.dumps(
        {
            "renderer_contract_version": RENDERER_CONTRACT_VERSION,
            "student_id": student_id,
            "start": start,
            "end": end,
            "rows": serialized_rows,
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def get_latest_version(
    db_path: str, student_id: Any, start: str, end: str
) -> dict | None:
    """Latest stored version row for student+period, or None if no
    version has ever been created."""
    _validate_period(start, end)
    conn = _connect(db_path)
    try:
        row = conn.execute(
            """SELECT * FROM statement_versions
               WHERE student_id = ? AND period_start = ? AND period_end = ?
               ORDER BY version DESC LIMIT 1""",
            (student_id, start, end),
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        conn.close()


def get_version(
    db_path: str, student_id: Any, start: str, end: str, version: int
) -> dict | None:
    """A specific stored version row for student+period, or None if
    that version was never issued."""
    _validate_period(start, end)
    conn = _connect(db_path)
    try:
        row = conn.execute(
            """SELECT * FROM statement_versions
               WHERE student_id = ? AND period_start = ? AND period_end = ?
                 AND version = ?""",
            (student_id, start, end, version),
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        conn.close()


def get_or_create_version(
    db_path: str,
    student_id: Any,
    start: str,
    end: str,
    fingerprint: str,
    statement_content: str,
) -> dict:
    """
    Atomically decides between "reuse the existing version" and
    "create version + 1", and returns the version that should be used.

    Runs the read-compare-and-maybe-insert sequence inside a single
    BEGIN IMMEDIATE transaction, so two concurrent requests for the
    same student+period can never both observe "no matching version
    yet" and both insert -- IMMEDIATE acquires the write lock before
    the SELECT even runs, so a second concurrent caller simply waits
    for the first to finish and then sees its result.

    Only ever INSERTs -- an existing version row is never UPDATEd, so
    version 1 remains a permanent, immutable historical snapshot once
    version 2 (or later) is created. The UNIQUE constraint on
    (student_id, period_start, period_end, version), plus the
    immutability triggers on the table itself, make an accidental
    overwrite structurally impossible even if application logic here
    had a bug.

    Returns:
        {"version": int, "fingerprint": str, "statement_content": str,
         "generated_at": str, "created": bool}
    """
    _validate_period(start, end)
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            latest = conn.execute(
                """SELECT * FROM statement_versions
                   WHERE student_id = ? AND period_start = ? AND period_end = ?
                   ORDER BY version DESC LIMIT 1""",
                (student_id, start, end),
            ).fetchone()

            if latest is not None and latest["fingerprint"] == fingerprint:
                conn.execute("COMMIT")
                return {
                    "version": latest["version"],
                    "fingerprint": latest["fingerprint"],
                    "statement_content": latest["statement_content"],
                    "generated_at": latest["generated_at"],
                    "created": False,
                }

            next_version = (latest["version"] if latest is not None else 0) + 1
            conn.execute(
                """INSERT INTO statement_versions
                   (student_id, period_start, period_end, version, fingerprint, statement_content)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (student_id, start, end, next_version, fingerprint, statement_content),
            )
            row = conn.execute(
                """SELECT * FROM statement_versions
                   WHERE student_id = ? AND period_start = ? AND period_end = ?
                     AND version = ?""",
                (student_id, start, end, next_version),
            ).fetchone()
            conn.execute("COMMIT")
            return {
                "version": row["version"],
                "fingerprint": row["fingerprint"],
                "statement_content": row["statement_content"],
                "generated_at": row["generated_at"],
                "created": True,
            }
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()