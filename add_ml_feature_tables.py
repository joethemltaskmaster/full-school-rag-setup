"""
add_ml_feature_tables.py

Adds 4 new tables to school.db to hold the ML training/feature data
supplied as CSVs:

    teacher_profiles       <- teacher_profiles.csv
    lesson_schedule        <- lesson_schedule.csv       (FK -> teacher_profiles)
    fee_default_records    <- fee_default_synthetic.csv
    student_risk_records   <- nigerian_students_synthetic-1.csv

These are independent of the operational students/teachers/classes
tables already in school.db (they use their own STU-xxxxx / TCH-xxxxx
style IDs from a separate synthetic dataset used to train the
Fee Default Predictor, Student Risk Engine, and Timetable Optimizer
models) — they are NOT foreign-keyed to the operational `students` /
`teachers` tables.

Run:
    python add_ml_feature_tables.py
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
    # TEACHER_PROFILES  (scheduling personas for the Timetable Optimizer)
    # ---------------------------------------------------------------
    cur.execute("""
    CREATE TABLE IF NOT EXISTS teacher_profiles (
        teacher_id                      TEXT PRIMARY KEY,
        school_id                       TEXT,
        teacher_name                    TEXT NOT NULL,
        persona                         TEXT,
        teacher_availability_mask       TEXT,
        preferred_time_window           TEXT,
        consecutive_period_preference   INTEGER,
        subjects_taught                 TEXT,     -- pipe-separated list, e.g. 'Physics|Chemistry'
        class_arm_eligibility           TEXT,
        room_strictness                 TEXT,
        years_of_experience              INTEGER,
        employment_type                 TEXT,
        cds_blocked_day                  TEXT,     -- nullable
        min_rest_periods_required        REAL      -- nullable
    );
    """)

    # ---------------------------------------------------------------
    # LESSON_SCHEDULE  (generated/candidate timetable slots)
    # ---------------------------------------------------------------
    cur.execute("""
    CREATE TABLE IF NOT EXISTS lesson_schedule (
        lesson_id                    TEXT PRIMARY KEY,
        school_id                    TEXT,
        teacher_id                   TEXT,
        persona                      TEXT,
        subject_id                   TEXT,
        class_arm_id                 TEXT,
        room_id                      TEXT,
        room_type_matches_subject    INTEGER,   -- 0/1
        day_of_week                  TEXT,
        start_time                   TEXT,
        end_time                     TEXT,
        consecutive_period_count     INTEGER,
        time_slot_hour                TEXT,
        soft_preference_score        REAL,
        FOREIGN KEY (teacher_id) REFERENCES teacher_profiles (teacher_id)
            ON DELETE SET NULL
    );
    """)

    # ---------------------------------------------------------------
    # FEE_DEFAULT_RECORDS  (training data for the Fee Default Predictor)
    # ---------------------------------------------------------------
    cur.execute("""
    CREATE TABLE IF NOT EXISTS fee_default_records (
        student_id                            TEXT PRIMARY KEY,
        guardian_id                           TEXT,
        academic_year                         INTEGER,
        term                                  TEXT,
        student_fee_payment_days_late         INTEGER,
        student_fee_default_count_lifetime    INTEGER,
        session_start_paid_flag               INTEGER,   -- 0/1
        guardian_message_open_rate            REAL,
        guardian_avg_response_time_hrs        REAL,      -- nullable
        fee_default_risk_label                TEXT       -- LOW/MEDIUM/HIGH
    );
    """)

    # ---------------------------------------------------------------
    # STUDENT_RISK_RECORDS  (training data for the Student Dropout Risk Engine)
    # ---------------------------------------------------------------
    cur.execute("""
    CREATE TABLE IF NOT EXISTS student_risk_records (
        student_id                TEXT PRIMARY KEY,
        tenant_id                 INTEGER,
        school_type               TEXT,
        academic_year             INTEGER,
        term                      TEXT,
        fee_payment_days_late     REAL,
        paid_before_resumption    INTEGER,   -- 0/1
        attendance_rate           REAL,      -- nullable
        consecutive_absences      REAL,      -- nullable
        score_avg                 REAL,      -- nullable
        subject_failure_count     REAL,      -- nullable
        dropout_risk_label        TEXT       -- LOW/MEDIUM/HIGH
    );
    """)

    conn.commit()


def main():
    conn = get_connection()
    try:
        create_tables(conn)
        print("Created/verified 4 ML feature tables in school.db:")
        cur = conn.cursor()
        for table in ["teacher_profiles", "lesson_schedule", "fee_default_records", "student_risk_records"]:
            cur.execute(f"SELECT COUNT(*) FROM {table}")
            print(f"  - {table:<22} ({cur.fetchone()[0]} rows currently)")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
