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