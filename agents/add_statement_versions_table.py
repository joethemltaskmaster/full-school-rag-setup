"""
add_statement_versions_table.py

Adds the `statement_versions` table (STMT-003) to an existing
school.db, following the same idempotent CREATE TABLE IF NOT EXISTS
pattern as add_ml_feature_tables.py -- safe to run any number of times.

Unlike the first version of this migration, the actual DDL is NOT
duplicated here: it's imported from database/schema.py, which is the
single source of truth for the statement_versions schema (including
its immutability triggers). This script only decides *whether* to
create it, and -- if the table already exists -- verifies via
PRAGMA table_info() that it actually has the shape this codebase
expects, instead of silently trusting a same-named-but-different table.

    First execution:  statement_versions does not exist -> CREATE
    Any later run:     statement_versions exists & matches -> no-op
    Any later run:     statement_versions exists & DOESN'T match
                       -> refuse to proceed (raise), so a stale/foreign
                          table is never mistaken for this one

Run:
    python add_statement_versions_table.py
"""

import sqlite3
import sys
from pathlib import Path

DB_NAME = "school.db"

# database/schema.py lives one directory below the repo root that this
# script sits in.
_DATABASE_DIR = Path(__file__).resolve().parent / "database"
if str(_DATABASE_DIR) not in sys.path:
    sys.path.insert(0, str(_DATABASE_DIR))

from schema import create_statement_versions_table  # noqa: E402

EXPECTED_COLUMNS = {
    "version_id", "student_id", "period_start", "period_end",
    "version", "fingerprint", "statement_content", "generated_at",
}


def get_connection(db_name: str = DB_NAME) -> sqlite3.Connection:
    conn = sqlite3.connect(db_name)
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def _validate_existing_schema(conn: sqlite3.Connection) -> None:
    """
    PRAGMA table_info() sanity check. If statement_versions already
    exists (e.g. created by hand, by an older version of this script,
    or by another process entirely), verify it has the columns this
    codebase actually expects before treating "table exists" as
    "migration already applied". Refuses loudly rather than silently
    operating against an incompatible table.
    """
    info = conn.execute("PRAGMA table_info(statement_versions)").fetchall()
    actual_columns = {row[1] for row in info}  # row[1] is the column name
    missing = EXPECTED_COLUMNS - actual_columns
    if missing:
        raise RuntimeError(
            "Existing 'statement_versions' table is missing expected "
            f"column(s): {sorted(missing)}. Refusing to proceed -- this "
            "looks like an incompatible or hand-modified table, not the "
            "schema database/schema.py defines. Resolve manually before "
            "re-running this migration."
        )


def main():
    conn = get_connection()
    try:
        if _table_exists(conn, "statement_versions"):
            _validate_existing_schema(conn)
            print("'statement_versions' already exists and matches the expected schema -- no changes made.")
        else:
            create_statement_versions_table(conn)
            print("Created 'statement_versions' table (+ immutability triggers) in school.db.")

        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM statement_versions")
        print(f"  - {'statement_versions':<22} ({cur.fetchone()[0]} rows currently)")
    finally:
        conn.close()


if __name__ == "__main__":
    main()