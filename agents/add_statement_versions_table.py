"""
add_statement_versions_table.py

Adds the `statement_versions` table (STMT-003) to an existing
school.db, following the exact same pattern as add_ml_feature_tables.py:
CREATE TABLE IF NOT EXISTS, so running this script is idempotent --
safe to run any number of times against a database that may or may
not already have the table, without destroying data, duplicating
schema objects, or failing.

    First execution:  statement_versions does not exist -> CREATE
    Any later run:     statement_versions already exists -> no-op

Run:
    python add_statement_versions_table.py
"""

import sqlite3

DB_NAME = "school.db"


def get_connection(db_name: str = DB_NAME) -> sqlite3.Connection:
    conn = sqlite3.connect(db_name)
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def create_tables(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()

    # ---------------------------------------------------------------
    # STATEMENT_VERSIONS  (immutable history of generated fee
    # statements, keyed by student + requested period + version)
    # ---------------------------------------------------------------
    cur.execute("""
    CREATE TABLE IF NOT EXISTS statement_versions (
        version_id           INTEGER PRIMARY KEY AUTOINCREMENT,
        student_id           INTEGER NOT NULL,
        period_start         TEXT NOT NULL,
        period_end           TEXT NOT NULL,
        version              INTEGER NOT NULL,
        fingerprint          TEXT NOT NULL,
        statement_content    TEXT NOT NULL,
        generated_at         TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (student_id) REFERENCES students (student_id)
            ON DELETE CASCADE,
        UNIQUE (student_id, period_start, period_end, version)
    );
    """)

    conn.commit()


def main():
    conn = get_connection()
    try:
        create_tables(conn)
        print("Created/verified statement_versions table in school.db:")
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM statement_versions")
        print(f"  - {'statement_versions':<22} ({cur.fetchone()[0]} rows currently)")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
