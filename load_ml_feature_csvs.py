"""
load_ml_feature_csvs.py

Bulk-loads the 4 ML feature CSVs into their tables in school.db, using
executemany for speed (these files run into the thousands of rows).

Expects the CSVs at the paths below — update UPLOAD_DIR if needed.

Load order doesn't matter for fee_default_records / student_risk_records
(no FK dependencies), but teacher_profiles must load before
lesson_schedule (which references teacher_id).

Run:
    python load_ml_feature_csvs.py
"""

import csv
import sqlite3
from pathlib import Path

DB_NAME = "school.db"
UPLOAD_DIR = Path("/mnt/user-data/uploads")


def load_csv(conn: sqlite3.Connection, csv_path: Path, table: str, columns: list[str]) -> int:
    if not csv_path.exists():
        print(f"  ! Skipped {table}: {csv_path} not found")
        return 0

    placeholders = ", ".join("?" for _ in columns)
    col_names = ", ".join(columns)
    sql = f"INSERT OR REPLACE INTO {table} ({col_names}) VALUES ({placeholders})"

    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = [
            tuple((row[c] if row[c] != "" else None) for c in columns)
            for row in reader
        ]

    cur = conn.cursor()
    cur.executemany(sql, rows)
    conn.commit()
    return len(rows)


def main():
    conn = sqlite3.connect(DB_NAME)
    conn.execute("PRAGMA foreign_keys = ON;")

    jobs = [
        ("teacher_profiles.csv", "teacher_profiles", [
            "teacher_id", "school_id", "teacher_name", "persona",
            "teacher_availability_mask", "preferred_time_window",
            "consecutive_period_preference", "subjects_taught",
            "class_arm_eligibility", "room_strictness", "years_of_experience",
            "employment_type", "cds_blocked_day", "min_rest_periods_required",
        ]),
        ("lesson_schedule.csv", "lesson_schedule", [
            "lesson_id", "school_id", "teacher_id", "persona", "subject_id",
            "class_arm_id", "room_id", "room_type_matches_subject",
            "day_of_week", "start_time", "end_time", "consecutive_period_count",
            "time_slot_hour", "soft_preference_score",
        ]),
        ("fee_default_synthetic.csv", "fee_default_records", [
            "guardian_id", "student_id", "academic_year", "term",
            "student_fee_payment_days_late", "student_fee_default_count_lifetime",
            "session_start_paid_flag", "guardian_message_open_rate",
            "guardian_avg_response_time_hrs", "fee_default_risk_label",
        ]),
        ("nigerian_students_synthetic-1.csv", "student_risk_records", [
            "student_id", "tenant_id", "school_type", "academic_year", "term",
            "fee_payment_days_late", "paid_before_resumption", "attendance_rate",
            "consecutive_absences", "score_avg", "subject_failure_count",
            "dropout_risk_label",
        ]),
    ]

    print(f"Loading ML feature CSVs into '{DB_NAME}'...\n")
    for filename, table, columns in jobs:
        count = load_csv(conn, UPLOAD_DIR / filename, table, columns)
        print(f"  {table:<22} -> {count} rows loaded")

    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
