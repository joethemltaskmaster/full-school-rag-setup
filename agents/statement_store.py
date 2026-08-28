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
triggers). Only the writer path (get_or_create_version) ensures the
table/triggers exist -- see _connect()'s `ensure_table` parameter and
the note on the two read-only functions below.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from date_utils import is_strict_iso_date

WAT = timezone(timedelta(hours=1))  # West Africa Time, UTC+1, no DST


def _now_wat_iso() -> str:
    """
    Current time in West Africa Time, millisecond precision, explicit
    offset -- e.g. '2026-08-24T14:03:07.481+01:00'. Computed in Python
    (not left to SQLite's DEFAULT) specifically so this exact value can
    be embedded in a version's rendered header BEFORE the row is
    inserted, guaranteeing the header and the stored/persisted
    timestamp can never drift apart.
    """
    now = datetime.now(timezone.utc).astimezone(WAT)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}+01:00"


def _find_database_dir(start_path: Path, max_levels: int = 6) -> Path:
    """
    Walk upward from `start_path` looking for a `database/schema.py`,
    instead of assuming a fixed number of `.parent` hops to the repo
    root. See add_statement_versions_table.py's copy of this same
    helper for the full rationale -- a hardcoded depth broke as soon
    as a file's actual location didn't match the assumption; walking
    up (bounded) is self-correcting.
    """
    current = start_path
    for _ in range(max_levels):
        candidate = current / "database"
        if (candidate / "schema.py").is_file():
            return candidate
        if current.parent == current:
            break
        current = current.parent

    raise RuntimeError(
        f"Could not locate 'database/schema.py' by walking up from "
        f"{start_path} (checked {max_levels} parent director(ies))."
    )


_DATABASE_DIR = _find_database_dir(Path(__file__).resolve().parent)
if str(_DATABASE_DIR) not in sys.path:
    sys.path.insert(0, str(_DATABASE_DIR))

from schema import create_statement_versions_table  # noqa: E402

# Bumped only when a change to the STMT-001 rendering/classification
# contract could make an OLD fingerprint no longer trustworthy (e.g. a
# change to what counts as a "conflicting" duplicate). Included in the
# fingerprint so a future contract change can force new versions
# everywhere, rather than silently reusing content generated under a
# since-changed contract.
RENDERER_CONTRACT_VERSION = "STMT-001.v2"  # bumped: unplaceable-row hashing narrowed


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


def _connect(db_path: str, ensure_table: bool = False) -> sqlite3.Connection:
    """
    `ensure_table` defaults to False: opening a connection is not, by
    itself, allowed to run DDL. Only get_or_create_version() (the
    writer) passes ensure_table=True -- it's the one path that can
    legitimately need the table to exist before it can do its job, and
    it's fully idempotent (CREATE TABLE / TRIGGER IF NOT EXISTS) so
    calling it on every write is cheap and safe.

    get_latest_version() and get_version() are pure readers: they pass
    ensure_table=False and simply SELECT. If the table doesn't exist
    yet (no statement has ever been generated for this database), they
    catch the resulting "no such table" error and treat it as "no
    matching row" rather than creating the table as a side effect of a
    read.
    """
    conn = sqlite3.connect(db_path)
    # Autocommit mode: get_or_create_version() manages transactions
    # explicitly (BEGIN IMMEDIATE / COMMIT / ROLLBACK), so sqlite3's own
    # implicit-transaction handling must stay out of the way.
    conn.isolation_level = None
    conn.execute("PRAGMA foreign_keys = ON;")
    # A concurrent writer that loses the race for the IMMEDIATE lock
    # waits (up to 5s) instead of failing immediately with
    # "database is locked".
    conn.execute("PRAGMA busy_timeout = 5000;")
    conn.row_factory = sqlite3.Row
    if ensure_table:
        create_statement_versions_table(conn)
    return conn


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    # version_id is the table's own AUTOINCREMENT primary key: a
    # single, global, strictly-increasing integer across ALL students
    # and ALL periods -- unlike `version`, which restarts at 1 for
    # every new (student, period). Exposed explicitly as `sequence` so
    # callers have an unambiguous, clock-independent tiebreaker/label
    # for a specific stored artifact, on top of the human-facing
    # per-period `version` number and the (now sub-second, WAT)
    # `generated_at` timestamp.
    d["sequence"] = d["version_id"]
    return d


def compute_fingerprint(
    student_id: Any,
    start: str,
    end: str,
    placeable_rows: list[dict],
    unplaceable_rows: list[dict],
) -> str:
    """
    Deterministic fingerprint of the ledger state relevant to
    `student_id` over [start, end].

    Callers must pass the SAME two row groups fee_statement.py itself
    computed: placeable_rows (in-period, date-valid) and
    unplaceable_rows (date can't rule them out of the period). Rows
    with a *valid* date outside [start, end] must never be passed in
    at all, which is what keeps out-of-period ledger activity from
    ever changing the fingerprint (STMT-003 Scenario C).

    The two groups are hashed differently, because they're RENDERED
    differently (see fee_statement.py's FeeStatementResult.render_text()):

      - placeable_rows: hashed as full rows (minus `student_id`, which
        is redundant -- see below). STMT-001's duplicate classification
        (_classify_by_payment_id / _rows_identical) compares FULL rows,
        so a change in any column here -- even one that isn't itself
        printed, e.g. `term` -- can flip a group from "identical" to
        "conflicting" and change RECONCILED -> BLOCKED. That's rendered
        content, so nothing here can be dropped.

      - unplaceable_rows: hashed as ONLY {payment_id, payment_date}.
        These rows never go through duplicate classification at all --
        they're rendered as a flat list showing exactly
        `payment_id=... raw_payment_date=...` and nothing else. Hashing
        the rest of their columns (amount_paid, term, status, ...)
        would make the fingerprint -- and therefore the version --
        change even though the rendered statement is byte-for-byte
        identical, causing spurious version bumps for edits that are
        genuinely invisible to this statement.

    `student_id` is dropped from every row before hashing in both
    groups: it's already a fixed, outer fingerprint input (every row
    was fetched for this same student), so keeping it per-row too
    would be redundant, not "rendered content".

    Determinism is guaranteed by:
      - serializing each row with sort_keys=True, so column order
        never matters
      - sorting each group's list of serialized rows, so raw
        retrieval/dict ordering never matters
      - never including timestamps, random ids, or object identity
    """
    _validate_period(start, end)

    placeable_serialized = sorted(
        json.dumps(
            {k: v for k, v in row.items() if k != "student_id"},
            sort_keys=True,
            default=str,
        )
        for row in placeable_rows
    )
    unplaceable_serialized = sorted(
        json.dumps(
            {"payment_id": row.get("payment_id"), "payment_date": row.get("payment_date")},
            sort_keys=True,
            default=str,
        )
        for row in unplaceable_rows
    )

    payload = json.dumps(
        {
            "renderer_contract_version": RENDERER_CONTRACT_VERSION,
            "student_id": student_id,
            "start": start,
            "end": end,
            "placeable_rows": placeable_serialized,
            "unplaceable_rows": unplaceable_serialized,
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def get_latest_version(
    db_path: str, student_id: Any, start: str, end: str
) -> dict | None:
    """Latest stored version row for student+period, or None if no
    version has ever been created (including the case where
    statement_versions doesn't exist in this database yet at all --
    this is a pure reader and never creates it)."""
    _validate_period(start, end)
    conn = _connect(db_path)
    try:
        try:
            row = conn.execute(
                """SELECT * FROM statement_versions
                   WHERE student_id = ? AND period_start = ? AND period_end = ?
                   ORDER BY version DESC LIMIT 1""",
                (student_id, start, end),
            ).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                return None
            raise
        return _row_to_dict(row) if row is not None else None
    finally:
        conn.close()


def get_version(
    db_path: str, student_id: Any, start: str, end: str, version: int
) -> dict | None:
    """A specific stored version row for student+period, or None if
    that version was never issued (including the case where
    statement_versions doesn't exist in this database yet at all --
    this is a pure reader and never creates it)."""
    _validate_period(start, end)
    conn = _connect(db_path)
    try:
        try:
            row = conn.execute(
                """SELECT * FROM statement_versions
                   WHERE student_id = ? AND period_start = ? AND period_end = ?
                     AND version = ?""",
                (student_id, start, end, version),
            ).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                return None
            raise
        return _row_to_dict(row) if row is not None else None
    finally:
        conn.close()


def list_versions(db_path: str, student_id: Any, start: str, end: str) -> list[int]:
    """
    All stored version numbers for student+period, ascending, or an
    empty list if none exist (including the case where
    statement_versions doesn't exist in this database yet at all --
    same pure-reader contract as get_latest_version()/get_version():
    never creates the table).

    Added for STMT-004 (Section 9): the CLI needs to report available
    versions when a requested one doesn't exist (e.g. "Available
    versions: 1-3."), and this is the smallest addition that lets it do
    that without duplicating a query the CLI has no business running
    directly against statement_versions itself.
    """
    _validate_period(start, end)
    conn = _connect(db_path)
    try:
        try:
            rows = conn.execute(
                """SELECT version FROM statement_versions
                   WHERE student_id = ? AND period_start = ? AND period_end = ?
                   ORDER BY version ASC""",
                (student_id, start, end),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                return []
            raise
        return [row["version"] for row in rows]
    finally:
        conn.close()


def get_or_create_version(
    db_path: str,
    student_id: Any,
    start: str,
    end: str,
    fingerprint: str,
    body_text: str,
    finalize_content,
) -> dict:
    """
    Atomically decides between "reuse the existing version" and
    "create version + 1", and returns the version that should be used.
    This is the WRITER path -- the only statement_store function
    allowed to run DDL (via _connect(..., ensure_table=True)).

    `body_text` is the rendered statement body WITHOUT any version/
    timestamp header (fee_statement.py's render_text() output,
    unchanged) -- it's what get compared/fingerprinted upstream, and
    it's independent of which version number this ends up being.

    `finalize_content(version: int, generated_at: str) -> str` is
    called ONLY when a brand-new version is being created, after this
    function has already determined what that version number and
    timestamp will be but BEFORE the row is inserted. This is what lets
    the caller embed "Version: N (generated ...)" directly into the
    text that gets stored/written -- the artifact a person actually
    opens, the DB row, and get_statement_version()'s "content" are
    therefore always the exact same bytes; there's no separate
    "logged" timestamp that could ever drift from what the file says.

    generated_at is computed once, in Python (see _now_wat_iso), and
    inserted as an explicit value rather than left to the column's SQL
    DEFAULT -- so the exact same timestamp used in the header is the
    one persisted, with no risk of the two disagreeing by a few
    milliseconds.

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
        {"version": int, "sequence": int, "fingerprint": str,
         "statement_content": str, "generated_at": str, "created": bool}
    `statement_content` is always the FINAL text (header + body) --
    exactly what was/should be written to disk.
    """
    _validate_period(start, end)
    conn = _connect(db_path, ensure_table=True)
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
                result = _row_to_dict(latest)
                result["created"] = False
                return result

            next_version = (latest["version"] if latest is not None else 0) + 1
            generated_at = _now_wat_iso()
            final_content = finalize_content(next_version, generated_at)
            conn.execute(
                """INSERT INTO statement_versions
                   (student_id, period_start, period_end, version, fingerprint,
                    statement_content, generated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (student_id, start, end, next_version, fingerprint, final_content, generated_at),
            )
            row = conn.execute(
                """SELECT * FROM statement_versions
                   WHERE student_id = ? AND period_start = ? AND period_end = ?
                     AND version = ?""",
                (student_id, start, end, next_version),
            ).fetchone()
            conn.execute("COMMIT")
            result = _row_to_dict(row)
            result["created"] = True
            return result
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()