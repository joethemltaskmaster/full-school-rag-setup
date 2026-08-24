"""
add_statement_versions_table.py

Adds the `statement_versions` table (STMT-003) to an existing
school.db, following the same idempotent CREATE TABLE IF NOT EXISTS
pattern as add_ml_feature_tables.py -- safe to run any number of times.

The actual DDL is NOT duplicated here: it's imported from
database/schema.py, which is the single source of truth for the
statement_versions schema (including its immutability triggers). This
script's job is narrower:

  1. Locate database/schema.py (see _find_database_dir below).
  2. If statement_versions already exists, verify via
     PRAGMA table_info() that it actually has the shape this codebase
     expects -- refuse loudly rather than silently trusting a
     same-named-but-different table.
  3. Call create_statement_versions_table() UNCONDITIONALLY (whether
     the table already existed or not). That function is fully
     idempotent (CREATE TABLE / TRIGGER IF NOT EXISTS), and it is the
     ONLY thing that guarantees the two immutability triggers exist.
     Skipping it just because the table already existed used to mean:
     a statement_versions table created before the triggers existed
     (by an older version of this script, by database/schema.py before
     they were added, or by hand) would stay silently, permanently
     mutable -- exactly the opposite of what this table is for. Always
     calling it closes that gap on every run.

Run:
    python add_statement_versions_table.py
"""

import sqlite3
import sys
from pathlib import Path

DB_NAME = "school.db"


def _find_database_dir(start_path: Path, max_levels: int = 6) -> Path:
    """
    Walk upward from `start_path` looking for a `database/schema.py`,
    instead of assuming a fixed number of `.parent` hops to the repo
    root. A hardcoded depth is exactly what broke previously: this
    script's actual location in the repo didn't match the assumption
    baked into a single `.parent`, and silently importing nothing (or
    the wrong module) is a bad failure mode for a migration. This
    walks up (bounded) until it actually finds the file, and raises
    clearly if it can't.
    """
    current = start_path
    for _ in range(max_levels):
        candidate = current / "database"
        if (candidate / "schema.py").is_file():
            return candidate
        if current.parent == current:  # reached filesystem root
            break
        current = current.parent

    raise RuntimeError(
        f"Could not locate 'database/schema.py' by walking up from "
        f"{start_path} (checked {max_levels} parent director(ies)). "
        "database/schema.py is the single source of truth for the "
        "statement_versions DDL -- fix the repo layout, or widen "
        "max_levels in _find_database_dir() if this script now lives "
        "deeper than expected."
    )


_DATABASE_DIR = _find_database_dir(Path(__file__).resolve().parent)
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
    codebase actually expects before treating "table exists" as safe
    to proceed against. Refuses loudly rather than silently operating
    against an incompatible table.
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
        table_already_existed = _table_exists(conn, "statement_versions")
        if table_already_existed:
            _validate_existing_schema(conn)

        # Always call this -- see the module docstring. It is fully
        # idempotent and is the only thing that guarantees the
        # immutability triggers exist, whether the table is brand new
        # or has existed since before the triggers were introduced.
        create_statement_versions_table(conn)

        if table_already_existed:
            print(
                "'statement_versions' already existed -- schema verified "
                "and immutability triggers (re)applied."
            )
        else:
            print("Created 'statement_versions' table (+ immutability triggers) in school.db.")

        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM statement_versions")
        print(f"  - {'statement_versions':<22} ({cur.fetchone()[0]} rows currently)")

        triggers = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='statement_versions'"
        ).fetchall()
        print(f"  - triggers present: {sorted(r[0] for r in triggers)}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()