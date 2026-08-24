"""
create_school_db.py

Creates school.db (SQLite) with 10 interconnected tables for a school
management system:

    guardians -> students -> classes
    teachers -> classes / subjects / timetable
    students -> scores, attendance, fee_payments
    guardians -> guardian_messages

Run:
    python create_school_db.py
"""

import sqlite3
from pathlib import Path

DB_NAME = "school.db"

# =====================================================================
# STATEMENT_VERSIONS (STMT-003) -- single source of truth for this
# table's DDL. add_statement_versions_table.py (migration) and
# agents/statement_store.py (lazy table creation for callers that don't
# go through this module) both import create_statement_versions_table()
# from here rather than each declaring their own CREATE TABLE -- there
# is exactly one CREATE TABLE statement for statement_versions in the
# whole codebase.
#
# No FOREIGN KEY to `students` here, deliberately: this table follows
# the same "independent of the operational tables" precedent already
# established by the ML feature tables (see add_ml_feature_tables.py)
# -- a statement is a historical snapshot that should remain readable
# even if the student record it was generated for is later archived or
# removed, and it also has to work against throwaway/synthetic
# databases used in tests that never define a `students` table at all.
# =====================================================================
STATEMENT_VERSIONS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS statement_versions (
    version_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id           INTEGER NOT NULL,
    period_start         TEXT NOT NULL,      -- 'YYYY-MM-DD', strict ISO
    period_end           TEXT NOT NULL,      -- 'YYYY-MM-DD', strict ISO
    version              INTEGER NOT NULL,
    fingerprint          TEXT NOT NULL,       -- sha256 of relevant ledger state
    statement_content    TEXT NOT NULL,       -- full rendered statement text
    generated_at         TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (student_id, period_start, period_end, version)
);
"""

# Old versions are historical snapshots: once written, a row must never
# be changed or removed by anything (including a bug or a well-meaning
# manual `UPDATE`/`DELETE`) -- these triggers enforce that at the
# database layer itself, not just by convention in application code.
STATEMENT_VERSIONS_TRIGGERS_SQL = [
    """
    CREATE TRIGGER IF NOT EXISTS trg_statement_versions_no_update
    BEFORE UPDATE ON statement_versions
    BEGIN
        SELECT RAISE(ABORT, 'statement_versions is immutable: UPDATE is not allowed');
    END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_statement_versions_no_delete
    BEFORE DELETE ON statement_versions
    BEGIN
        SELECT RAISE(ABORT, 'statement_versions is immutable: DELETE is not allowed');
    END;
    """,
]


def create_statement_versions_table(conn: sqlite3.Connection) -> None:
    """Idempotent: CREATE TABLE/TRIGGER IF NOT EXISTS throughout, safe
    to call on every startup and from every caller that needs the table
    to exist."""
    conn.execute(STATEMENT_VERSIONS_TABLE_SQL)
    for trigger_sql in STATEMENT_VERSIONS_TRIGGERS_SQL:
        conn.execute(trigger_sql)
    conn.commit()


def get_connection(db_name: str = DB_NAME) -> sqlite3.Connection:
    """Open a connection with foreign key enforcement turned on."""
    conn = sqlite3.connect(db_name)
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def create_tables(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()

    # ---------------------------------------------------------------
    # GUARDIANS  (parents / next-of-kin)
    # ---------------------------------------------------------------
    cur.execute("""
    CREATE TABLE IF NOT EXISTS guardians (
        guardian_id     INTEGER PRIMARY KEY AUTOINCREMENT,
        full_name       TEXT NOT NULL,
        relationship    TEXT,               -- e.g. Father, Mother, Guardian
        phone           TEXT,
        email           TEXT,
        address         TEXT,
        created_at      TEXT DEFAULT (datetime('now'))
    );
    """)

    # ---------------------------------------------------------------
    # TEACHERS
    # ---------------------------------------------------------------
    cur.execute("""
    CREATE TABLE IF NOT EXISTS teachers (
        teacher_id      INTEGER PRIMARY KEY AUTOINCREMENT,
        full_name       TEXT NOT NULL,
        email           TEXT UNIQUE,
        phone           TEXT,
        specialty       TEXT,               -- main subject area
        date_hired      TEXT,
        created_at      TEXT DEFAULT (datetime('now'))
    );
    """)

    # ---------------------------------------------------------------
    # CLASSES  (e.g. JSS1A, Grade 5B)
    # ---------------------------------------------------------------
    cur.execute("""
    CREATE TABLE IF NOT EXISTS classes (
        class_id        INTEGER PRIMARY KEY AUTOINCREMENT,
        class_name      TEXT NOT NULL,
        academic_year   TEXT,               -- e.g. '2025/2026'
        class_teacher_id INTEGER,
        FOREIGN KEY (class_teacher_id) REFERENCES teachers (teacher_id)
            ON DELETE SET NULL
    );
    """)

    # ---------------------------------------------------------------
    # STUDENTS
    # ---------------------------------------------------------------
    cur.execute("""
    CREATE TABLE IF NOT EXISTS students (
        student_id      INTEGER PRIMARY KEY AUTOINCREMENT,
        full_name       TEXT NOT NULL,
        date_of_birth   TEXT,
        gender          TEXT,
        admission_date  TEXT DEFAULT (date('now')),
        class_id        INTEGER,
        guardian_id     INTEGER,            -- primary guardian
        status          TEXT DEFAULT 'active',  -- active/graduated/withdrawn
        FOREIGN KEY (class_id) REFERENCES classes (class_id)
            ON DELETE SET NULL,
        FOREIGN KEY (guardian_id) REFERENCES guardians (guardian_id)
            ON DELETE SET NULL
    );
    """)

    # ---------------------------------------------------------------
    # SUBJECTS  (tied to a class and the teacher who takes it)
    # ---------------------------------------------------------------
    cur.execute("""
    CREATE TABLE IF NOT EXISTS subjects (
        subject_id      INTEGER PRIMARY KEY AUTOINCREMENT,
        subject_name    TEXT NOT NULL,
        class_id        INTEGER,
        teacher_id      INTEGER,
        FOREIGN KEY (class_id) REFERENCES classes (class_id)
            ON DELETE CASCADE,
        FOREIGN KEY (teacher_id) REFERENCES teachers (teacher_id)
            ON DELETE SET NULL
    );
    """)

    # ---------------------------------------------------------------
    # SCORES  (exam / assessment results)
    # ---------------------------------------------------------------
    cur.execute("""
    CREATE TABLE IF NOT EXISTS scores (
        score_id        INTEGER PRIMARY KEY AUTOINCREMENT,
        student_id      INTEGER NOT NULL,
        subject_id      INTEGER NOT NULL,
        term            TEXT,               -- e.g. 'First Term'
        exam_type       TEXT,               -- e.g. 'Midterm', 'Final'
        score            REAL,
        max_score       REAL DEFAULT 100,
        date_recorded   TEXT DEFAULT (date('now')),
        FOREIGN KEY (student_id) REFERENCES students (student_id)
            ON DELETE CASCADE,
        FOREIGN KEY (subject_id) REFERENCES subjects (subject_id)
            ON DELETE CASCADE
    );
    """)

    # ---------------------------------------------------------------
    # ATTENDANCE
    # ---------------------------------------------------------------
    cur.execute("""
    CREATE TABLE IF NOT EXISTS attendance (
        attendance_id   INTEGER PRIMARY KEY AUTOINCREMENT,
        student_id      INTEGER NOT NULL,
        date            TEXT NOT NULL DEFAULT (date('now')),
        status          TEXT NOT NULL,      -- present/absent/late/excused
        recorded_by     INTEGER,            -- teacher_id
        FOREIGN KEY (student_id) REFERENCES students (student_id)
            ON DELETE CASCADE,
        FOREIGN KEY (recorded_by) REFERENCES teachers (teacher_id)
            ON DELETE SET NULL,
        UNIQUE (student_id, date)
    );
    """)

    # ---------------------------------------------------------------
    # FEE_PAYMENTS
    # ---------------------------------------------------------------
    cur.execute("""
    CREATE TABLE IF NOT EXISTS fee_payments (
        payment_id      INTEGER PRIMARY KEY AUTOINCREMENT,
        student_id      INTEGER NOT NULL,
        term            TEXT,
        amount_due      REAL NOT NULL,
        amount_paid     REAL DEFAULT 0,
        payment_date    TEXT,
        payment_method  TEXT,               -- cash/transfer/card
        status          TEXT DEFAULT 'pending',  -- pending/partial/paid
        FOREIGN KEY (student_id) REFERENCES students (student_id)
            ON DELETE CASCADE
    );
    """)

    # ---------------------------------------------------------------
    # STATEMENT_VERSIONS  (STMT-003 -- immutable history of generated
    # fee statements per student + requested period)
    # ---------------------------------------------------------------
    create_statement_versions_table(conn)

    # ---------------------------------------------------------------
    # GUARDIAN_MESSAGES  (school -> guardian communication log)
    # ---------------------------------------------------------------
    cur.execute("""
    CREATE TABLE IF NOT EXISTS guardian_messages (
        message_id      INTEGER PRIMARY KEY AUTOINCREMENT,
        guardian_id     INTEGER NOT NULL,
        student_id      INTEGER,            -- related student, if any
        sender_teacher_id INTEGER,          -- staff who sent it (nullable)
        subject         TEXT,
        message_text    TEXT NOT NULL,
        sent_at         TEXT DEFAULT (datetime('now')),
        status          TEXT DEFAULT 'sent', -- sent/delivered/read
        FOREIGN KEY (guardian_id) REFERENCES guardians (guardian_id)
            ON DELETE CASCADE,
        FOREIGN KEY (student_id) REFERENCES students (student_id)
            ON DELETE SET NULL,
        FOREIGN KEY (sender_teacher_id) REFERENCES teachers (teacher_id)
            ON DELETE SET NULL
    );
    """)

    # ---------------------------------------------------------------
    # TIMETABLE
    # ---------------------------------------------------------------
    cur.execute("""
    CREATE TABLE IF NOT EXISTS timetable (
        timetable_id    INTEGER PRIMARY KEY AUTOINCREMENT,
        class_id        INTEGER NOT NULL,
        subject_id      INTEGER NOT NULL,
        teacher_id      INTEGER,
        day_of_week     TEXT NOT NULL,      -- Monday..Friday
        start_time      TEXT NOT NULL,      -- 'HH:MM'
        end_time        TEXT NOT NULL,
        FOREIGN KEY (class_id) REFERENCES classes (class_id)
            ON DELETE CASCADE,
        FOREIGN KEY (subject_id) REFERENCES subjects (subject_id)
            ON DELETE CASCADE,
        FOREIGN KEY (teacher_id) REFERENCES teachers (teacher_id)
            ON DELETE SET NULL
    );
    """)

    conn.commit()


def main():
    db_path = Path(DB_NAME)
    conn = get_connection(db_path.as_posix())
    try:
        create_tables(conn)
        print(f"'{DB_NAME}' created/updated successfully with 10 tables:")
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;")
        for (table_name,) in cur.fetchall():
            print(f"  - {table_name}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()