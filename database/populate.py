"""
load_csvs_to_db.py

Loads the CSV files in ./csv/ into school.db, in an order that respects
foreign key dependencies. Assumes create_school_db.py has already been
run (or runs it automatically if school.db / tables don't exist yet).

Usage:
    python load_csvs_to_db.py
"""

import csv
import sqlite3
from pathlib import Path

DB_NAME = "school.db"
CSV_DIR = Path("csv")

# Order matters: parents before children
LOAD_ORDER = [
    # "guardians",
    # "teachers",
    # "classes",
    # "students",
    # "subjects",
    # "scores",
    # "attendance",
    # "fee_payments",
    # "guardian_messages",
    # "timetable",
    'fee_default_synthetic',
    'lesson_schedule',
    'nigerian_students_synthetic-1',
    'teacher_profiles'
]


def load_csv_into_table(conn: sqlite3.Connection, table: str) -> int:
    csv_path = CSV_DIR / f"{table}.csv"
    if not csv_path.exists():
        print(f"  ! Skipped {table}: {csv_path} not found")
        return 0

    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        if not rows:
            return 0
        columns = reader.fieldnames
        placeholders = ", ".join("?" for _ in columns)
        col_names = ", ".join(columns)
        sql = f"INSERT INTO {table} ({col_names}) VALUES ({placeholders})"

        cur = conn.cursor()
        for row in rows:
            # convert empty strings to None so blanks don't get stored as ""
            values = [row[c] if row[c] != "" else None for c in columns]
            cur.execute(sql, values)

    conn.commit()
    return len(rows)


def main():
    conn = sqlite3.connect(DB_NAME)
    conn.execute("PRAGMA foreign_keys = ON;")

    print(f"Loading CSVs from '{CSV_DIR}/' into '{DB_NAME}'...\n")
    for table in LOAD_ORDER:
        count = load_csv_into_table(conn, table)
        print(f"  {table:<20} -> {count} rows inserted")

    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
