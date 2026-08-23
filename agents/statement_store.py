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

Table creation here is a deliberate, idempotent CREATE TABLE IF NOT
EXISTS (same pattern as add_ml_feature_tables.py /
add_statement_versions_table.py) so this module is safe to use even
against a database that hasn't been migrated yet -- it never destroys
data and never fails because the table already exists.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.row_factory = sqlite3.Row
    _ensure_table(conn)
    return conn


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
    CREATE TABLE IF NOT EXISTS statement_versions (
        version_id           INTEGER PRIMARY KEY AUTOINCREMENT,
        student_id           INTEGER NOT NULL,
        period_start         TEXT NOT NULL,
        period_end           TEXT NOT NULL,
        version              INTEGER NOT NULL,
        fingerprint          TEXT NOT NULL,
        statement_content    TEXT NOT NULL,
        generated_at         TEXT DEFAULT (datetime('now')),
        UNIQUE (student_id, period_start, period_end, version)
    );
    """)
    conn.commit()


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

    Determinism is guaranteed by:
      - serializing each row with sort_keys=True, so column order
        never matters
      - sorting the resulting list of serialized rows, so raw
        retrieval/dict ordering never matters
      - never including timestamps, random ids, or object identity
    """
    serialized_rows = sorted(
        json.dumps(row, sort_keys=True, default=str) for row in relevant_rows
    )
    payload = json.dumps(
        {
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


def create_version(
    db_path: str,
    student_id: Any,
    start: str,
    end: str,
    fingerprint: str,
    statement_content: str,
) -> int:
    """
    Stores a brand-new version row (latest existing version + 1, or 1
    if none exists yet) and returns its version number.

    Only ever INSERTs -- an existing version row is never touched, so
    version 1 remains a permanent, immutable historical snapshot once
    version 2 (or later) is created. The UNIQUE constraint on
    (student_id, period_start, period_end, version) makes an
    accidental overwrite structurally impossible.
    """
    conn = _connect(db_path)
    try:
        latest = conn.execute(
            """SELECT MAX(version) AS v FROM statement_versions
               WHERE student_id = ? AND period_start = ? AND period_end = ?""",
            (student_id, start, end),
        ).fetchone()
        next_version = (latest["v"] or 0) + 1
        conn.execute(
            """INSERT INTO statement_versions
               (student_id, period_start, period_end, version, fingerprint, statement_content)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (student_id, start, end, next_version, fingerprint, statement_content),
        )
        conn.commit()
        return next_version
    finally:
        conn.close()
