"""
test_db_service.py

Exercises every method on SchoolDB against the real school.db to confirm
the service layer works end-to-end (read methods + write methods).
"""

from pprint import pprint
from db_service import SchoolDB, RecordNotFoundError

with SchoolDB("school.db") as db:
    print("=== get_student(1) ===")
    pprint(db.get_student(1))

    print("\n=== get_class_students(1) ===")
    pprint(db.get_class_students(1))

    print("\n=== get_attendance(1) ===")
    pprint(db.get_attendance(1))

    print("\n=== get_attendance_summary(1) ===")
    pprint(db.get_attendance_summary(1))

    print("\n=== get_scores(1) ===")
    pprint(db.get_scores(1))

    print("\n=== get_score_summary(1) ===")
    pprint(db.get_score_summary(1))

    print("\n=== get_fee_history(1) ===")
    pprint(db.get_fee_history(1))

    print("\n=== get_fee_balance(1) ===")
    pprint(db.get_fee_balance(1))

    print("\n=== get_teacher_schedule(1) ===")
    pprint(db.get_teacher_schedule(1))

    print("\n=== get_class_timetable(1) ===")
    pprint(db.get_class_timetable(1))

    print("\n=== get_student_timetable(1) ===")
    pprint(db.get_student_timetable(1))

    print("\n=== get_guardian(1) ===")
    pprint(db.get_guardian(1))

    print("\n=== get_students_by_guardian(1) ===")
    pprint(db.get_students_by_guardian(1))

    print("\n=== get_guardian_messages(1) ===")
    pprint(db.get_guardian_messages(1))

    print("\n=== search_students('Ojo') ===")
    pprint(db.search_students("Ojo"))

    print("\n=== get_outstanding_fees() ===")
    pprint(db.get_outstanding_fees())

    print("\n=== get_class_average_scores(1) ===")
    pprint(db.get_class_average_scores(1))

    print("\n=== get_class_attendance_for_date(1, '2026-01-12') ===")
    pprint(db.get_class_attendance_for_date(1, "2026-01-12"))

    print("\n=== get_student_full_profile(1) ===")
    pprint(db.get_student_full_profile(1))

    print("\n=== get_class_overview(1) ===")
    pprint(db.get_class_overview(1))

    # ---- write-path smoke tests ----
    print("\n=== write path: add_guardian + add_student + record_attendance + record_payment ===")
    new_guardian_id = db.add_guardian("Test Guardian", "Father", "08000000000")
    new_student_id = db.add_student("Test Student", "2013-01-01", "Male",
                                     "2026-01-01", class_id=1, guardian_id=new_guardian_id)
    db.record_attendance(new_student_id, "2026-01-20", "present", recorded_by=1)
    db.record_payment(new_student_id, "First Term", 75000, 20000, "2026-01-20", "cash")
    db.send_guardian_message(new_guardian_id, "Welcome to the new term!", student_id=new_student_id)
    pprint(db.get_student_full_profile(new_student_id))

    print("\n=== error path: get_student_full_profile(9999) ===")
    try:
        db.get_student_full_profile(9999)
    except RecordNotFoundError as e:
        print("Correctly raised:", e)

print("\nAll checks completed.")
