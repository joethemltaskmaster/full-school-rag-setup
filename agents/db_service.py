"""
db_service.py

A database service layer for school.db.

Purpose:
    The rest of the application should never write raw SQL. Every table
    and every meaningful query is exposed here as a plain Python method
    that takes normal arguments and returns dicts / lists of dicts.

Usage:
    from db_service import SchoolDB

    db = SchoolDB("school.db")
    student = db.get_student(1)
    history = db.get_fee_history(1)
    db.close()

    # or, as a context manager:
    with SchoolDB("school.db") as db:
        students = db.get_class_students(class_id=2)
"""

import sqlite3
from typing import Any, Optional


class RecordNotFoundError(Exception):
    """Raised when a get_x(id) lookup finds nothing."""


class SchoolDB:
    def __init__(self, db_path: str = "school.db"):
        self.db_path = db_path
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON;")

    # -- context manager support -----------------------------------
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def close(self):
        self.conn.close()

    # -- internal helpers --------------------------------------------
    def _fetchone(self, sql: str, params: tuple = ()) -> Optional[dict]:
        cur = self.conn.execute(sql, params)
        row = cur.fetchone()
        return dict(row) if row else None

    def _fetchall(self, sql: str, params: tuple = ()) -> list[dict]:
        cur = self.conn.execute(sql, params)
        return [dict(row) for row in cur.fetchall()]

    def _execute(self, sql: str, params: tuple = ()) -> int:
        """For INSERT/UPDATE/DELETE. Returns lastrowid (for inserts)."""
        cur = self.conn.execute(sql, params)
        self.conn.commit()
        return cur.lastrowid

    # =================================================================
    # GUARDIANS
    # =================================================================
    def get_guardian(self, guardian_id: int) -> Optional[dict]:
        return self._fetchone(
            "SELECT * FROM guardians WHERE guardian_id = ?", (guardian_id,)
        )

    def get_all_guardians(self) -> list[dict]:
        return self._fetchall("SELECT * FROM guardians ORDER BY full_name")

    def get_students_by_guardian(self, guardian_id: int) -> list[dict]:
        """All students linked to a given guardian."""
        return self._fetchall(
            "SELECT * FROM students WHERE guardian_id = ? ORDER BY full_name",
            (guardian_id,),
        )

    def add_guardian(self, full_name: str, relationship: str = None,
                      phone: str = None, email: str = None,
                      address: str = None) -> int:
        return self._execute(
            """INSERT INTO guardians (full_name, relationship, phone, email, address)
               VALUES (?, ?, ?, ?, ?)""",
            (full_name, relationship, phone, email, address),
        )

    def update_guardian(self, guardian_id: int, **fields) -> None:
        self._update_row("guardians", "guardian_id", guardian_id, fields)

    # =================================================================
    # TEACHERS
    # =================================================================
    def get_teacher(self, teacher_id: int) -> Optional[dict]:
        return self._fetchone(
            "SELECT * FROM teachers WHERE teacher_id = ?", (teacher_id,)
        )

    def get_all_teachers(self) -> list[dict]:
        return self._fetchall("SELECT * FROM teachers ORDER BY full_name")

    def add_teacher(self, full_name: str, email: str = None, phone: str = None,
                     specialty: str = None, date_hired: str = None) -> int:
        return self._execute(
            """INSERT INTO teachers (full_name, email, phone, specialty, date_hired)
               VALUES (?, ?, ?, ?, ?)""",
            (full_name, email, phone, specialty, date_hired),
        )

    def update_teacher(self, teacher_id: int, **fields) -> None:
        self._update_row("teachers", "teacher_id", teacher_id, fields)

    def get_teacher_schedule(self, teacher_id: int, day_of_week: str = None) -> list[dict]:
        """Everything a teacher is scheduled to teach, optionally filtered by day."""
        sql = """
            SELECT tt.timetable_id, tt.day_of_week, tt.start_time, tt.end_time,
                   c.class_name, s.subject_name
            FROM timetable tt
            JOIN classes c ON tt.class_id = c.class_id
            JOIN subjects s ON tt.subject_id = s.subject_id
            WHERE tt.teacher_id = ?
        """
        params = [teacher_id]
        if day_of_week:
            sql += " AND tt.day_of_week = ?"
            params.append(day_of_week)
        sql += " ORDER BY CASE tt.day_of_week " \
               "WHEN 'Monday' THEN 1 WHEN 'Tuesday' THEN 2 WHEN 'Wednesday' THEN 3 " \
               "WHEN 'Thursday' THEN 4 WHEN 'Friday' THEN 5 ELSE 6 END, tt.start_time"
        return self._fetchall(sql, tuple(params))

    def get_subjects_by_teacher(self, teacher_id: int) -> list[dict]:
        return self._fetchall(
            """SELECT sub.*, c.class_name
               FROM subjects sub
               JOIN classes c ON sub.class_id = c.class_id
               WHERE sub.teacher_id = ?""",
            (teacher_id,),
        )

    # =================================================================
    # CLASSES
    # =================================================================
    def get_class(self, class_id: int) -> Optional[dict]:
        return self._fetchone(
            """SELECT c.*, t.full_name AS class_teacher_name
               FROM classes c
               LEFT JOIN teachers t ON c.class_teacher_id = t.teacher_id
               WHERE c.class_id = ?""",
            (class_id,),
        )

    def get_all_classes(self) -> list[dict]:
        return self._fetchall(
            """SELECT c.*, t.full_name AS class_teacher_name
               FROM classes c
               LEFT JOIN teachers t ON c.class_teacher_id = t.teacher_id
               ORDER BY c.class_name"""
        )

    def add_class(self, class_name: str, academic_year: str = None,
                   class_teacher_id: int = None) -> int:
        return self._execute(
            """INSERT INTO classes (class_name, academic_year, class_teacher_id)
               VALUES (?, ?, ?)""",
            (class_name, academic_year, class_teacher_id),
        )

    def get_class_students(self, class_id: int, status: str = "active") -> list[dict]:
        sql = "SELECT * FROM students WHERE class_id = ?"
        params = [class_id]
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY full_name"
        return self._fetchall(sql, tuple(params))

    def get_class_timetable(self, class_id: int, day_of_week: str = None) -> list[dict]:
        sql = """
            SELECT tt.timetable_id, tt.day_of_week, tt.start_time, tt.end_time,
                   s.subject_name, t.full_name AS teacher_name
            FROM timetable tt
            JOIN subjects s ON tt.subject_id = s.subject_id
            LEFT JOIN teachers t ON tt.teacher_id = t.teacher_id
            WHERE tt.class_id = ?
        """
        params = [class_id]
        if day_of_week:
            sql += " AND tt.day_of_week = ?"
            params.append(day_of_week)
        sql += " ORDER BY CASE tt.day_of_week " \
               "WHEN 'Monday' THEN 1 WHEN 'Tuesday' THEN 2 WHEN 'Wednesday' THEN 3 " \
               "WHEN 'Thursday' THEN 4 WHEN 'Friday' THEN 5 ELSE 6 END, tt.start_time"
        return self._fetchall(sql, tuple(params))

    def get_class_average_scores(self, class_id: int, subject_id: int = None) -> list[dict]:
        """Average score per subject for a class (or one subject if given)."""
        sql = """
            SELECT sub.subject_name, ROUND(AVG(sc.score), 2) AS average_score,
                   COUNT(sc.score_id) AS number_of_scores
            FROM scores sc
            JOIN students st ON sc.student_id = st.student_id
            JOIN subjects sub ON sc.subject_id = sub.subject_id
            WHERE st.class_id = ?
        """
        params = [class_id]
        if subject_id:
            sql += " AND sub.subject_id = ?"
            params.append(subject_id)
        sql += " GROUP BY sub.subject_name ORDER BY sub.subject_name"
        return self._fetchall(sql, tuple(params))

    def get_class_attendance_for_date(self, class_id: int, date: str) -> list[dict]:
        """Attendance roll call for a whole class on a given date."""
        return self._fetchall(
            """SELECT st.student_id, st.full_name, a.status
               FROM students st
               LEFT JOIN attendance a
                 ON a.student_id = st.student_id AND a.date = ?
               WHERE st.class_id = ?
               ORDER BY st.full_name""",
            (date, class_id),
        )

    # =================================================================
    # SUBJECTS
    # =================================================================
    def get_subject(self, subject_id: int) -> Optional[dict]:
        return self._fetchone(
            """SELECT sub.*, c.class_name, t.full_name AS teacher_name
               FROM subjects sub
               JOIN classes c ON sub.class_id = c.class_id
               LEFT JOIN teachers t ON sub.teacher_id = t.teacher_id
               WHERE sub.subject_id = ?""",
            (subject_id,),
        )

    def get_subjects_by_class(self, class_id: int) -> list[dict]:
        return self._fetchall(
            """SELECT sub.*, t.full_name AS teacher_name
               FROM subjects sub
               LEFT JOIN teachers t ON sub.teacher_id = t.teacher_id
               WHERE sub.class_id = ?
               ORDER BY sub.subject_name""",
            (class_id,),
        )

    def add_subject(self, subject_name: str, class_id: int, teacher_id: int = None) -> int:
        return self._execute(
            """INSERT INTO subjects (subject_name, class_id, teacher_id)
               VALUES (?, ?, ?)""",
            (subject_name, class_id, teacher_id),
        )

    # =================================================================
    # STUDENTS
    # =================================================================
    def get_student(self, student_id: int) -> Optional[dict]:
        """Core student record joined with class + guardian names."""
        return self._fetchone(
            """SELECT st.*, c.class_name, g.full_name AS guardian_name,
                      g.phone AS guardian_phone
               FROM students st
               LEFT JOIN classes c ON st.class_id = c.class_id
               LEFT JOIN guardians g ON st.guardian_id = g.guardian_id
               WHERE st.student_id = ?""",
            (student_id,),
        )

    def get_all_students(self, class_id: int = None, status: str = None) -> list[dict]:
        sql = """SELECT st.*, c.class_name
                  FROM students st
                  LEFT JOIN classes c ON st.class_id = c.class_id
                  WHERE 1 = 1"""
        params = []
        if class_id is not None:
            sql += " AND st.class_id = ?"
            params.append(class_id)
        if status:
            sql += " AND st.status = ?"
            params.append(status)
        sql += " ORDER BY st.full_name"
        return self._fetchall(sql, tuple(params))

    def search_students(self, name_query: str) -> list[dict]:
        return self._fetchall(
            """SELECT st.*, c.class_name
               FROM students st
               LEFT JOIN classes c ON st.class_id = c.class_id
               WHERE st.full_name LIKE ?
               ORDER BY st.full_name""",
            (f"%{name_query}%",),
        )

    def add_student(self, full_name: str, date_of_birth: str = None,
                     gender: str = None, admission_date: str = None,
                     class_id: int = None, guardian_id: int = None,
                     status: str = "active") -> int:
        return self._execute(
            """INSERT INTO students
               (full_name, date_of_birth, gender, admission_date, class_id, guardian_id, status)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (full_name, date_of_birth, gender, admission_date, class_id, guardian_id, status),
        )

    def update_student(self, student_id: int, **fields) -> None:
        """e.g. update_student(5, class_id=3, status='graduated')"""
        self._update_row("students", "student_id", student_id, fields)

    def transfer_student(self, student_id: int, new_class_id: int) -> None:
        self._execute(
            "UPDATE students SET class_id = ? WHERE student_id = ?",
            (new_class_id, student_id),
        )

    def withdraw_student(self, student_id: int) -> None:
        self._execute(
            "UPDATE students SET status = 'withdrawn' WHERE student_id = ?",
            (student_id,),
        )

    # =================================================================
    # ATTENDANCE
    # =================================================================
    def get_attendance(self, student_id: int, start_date: str = None,
                        end_date: str = None) -> list[dict]:
        sql = "SELECT * FROM attendance WHERE student_id = ?"
        params = [student_id]
        if start_date:
            sql += " AND date >= ?"
            params.append(start_date)
        if end_date:
            sql += " AND date <= ?"
            params.append(end_date)
        sql += " ORDER BY date"
        return self._fetchall(sql, tuple(params))

    def get_attendance_summary(self, student_id: int, start_date: str = None,
                                end_date: str = None) -> dict:
        """Counts of present/absent/late/excused plus an attendance rate."""
        rows = self.get_attendance(student_id, start_date, end_date)
        summary = {"present": 0, "absent": 0, "late": 0, "excused": 0, "total_days": len(rows)}
        for row in rows:
            key = row["status"].lower()
            if key in summary:
                summary[key] += 1
        summary["attendance_rate"] = (
            round((summary["present"] + summary["late"]) / summary["total_days"] * 100, 1)
            if summary["total_days"] else None
        )
        return summary

    def record_attendance(self, student_id: int, date: str, status: str,
                           recorded_by: int = None) -> int:
        """Insert or update (student, date) attendance — UNIQUE constraint safe."""
        return self._execute(
            """INSERT INTO attendance (student_id, date, status, recorded_by)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(student_id, date)
               DO UPDATE SET status = excluded.status, recorded_by = excluded.recorded_by""",
            (student_id, date, status, recorded_by),
        )

    # =================================================================
    # SCORES
    # =================================================================
    def get_scores(self, student_id: int, term: str = None,
                    subject_id: int = None) -> list[dict]:
        sql = """SELECT sc.*, sub.subject_name
                  FROM scores sc
                  JOIN subjects sub ON sc.subject_id = sub.subject_id
                  WHERE sc.student_id = ?"""
        params = [student_id]
        if term:
            sql += " AND sc.term = ?"
            params.append(term)
        if subject_id:
            sql += " AND sc.subject_id = ?"
            params.append(subject_id)
        sql += " ORDER BY sub.subject_name, sc.date_recorded"
        return self._fetchall(sql, tuple(params))

    def get_score_summary(self, student_id: int, term: str = None) -> list[dict]:
        """Average score per subject for one student."""
        sql = """SELECT sub.subject_name, ROUND(AVG(sc.score), 2) AS average_score,
                         COUNT(sc.score_id) AS number_of_assessments
                  FROM scores sc
                  JOIN subjects sub ON sc.subject_id = sub.subject_id
                  WHERE sc.student_id = ?"""
        params = [student_id]
        if term:
            sql += " AND sc.term = ?"
            params.append(term)
        sql += " GROUP BY sub.subject_name ORDER BY sub.subject_name"
        return self._fetchall(sql, tuple(params))

    def record_score(self, student_id: int, subject_id: int, term: str,
                      exam_type: str, score: float, max_score: float = 100,
                      date_recorded: str = None) -> int:
        return self._execute(
            """INSERT INTO scores
               (student_id, subject_id, term, exam_type, score, max_score, date_recorded)
               VALUES (?, ?, ?, ?, ?, ?, COALESCE(?, date('now')))""",
            (student_id, subject_id, term, exam_type, score, max_score, date_recorded),
        )

    # =================================================================
    # FEE PAYMENTS
    # =================================================================
    def get_fee_history(self, student_id: int) -> list[dict]:
        return self._fetchall(
            "SELECT * FROM fees_payment WHERE student_id = ? ORDER BY term",
            (student_id,),
        )

    def get_fee_balance(self, student_id: int) -> dict:
        row = self._fetchone(
            """SELECT COALESCE(SUM(amount_due), 0) AS total_due,
                      COALESCE(SUM(amount_paid), 0) AS total_paid
               FROM fees_payment WHERE student_id = ?""",
            (student_id,),
        )
        row["balance"] = round(row["total_due"] - row["total_paid"], 2)
        return row

    def get_outstanding_fees(self, class_id: int = None) -> list[dict]:
        """All students with a fee balance > 0, optionally filtered by class."""
        sql = """
            SELECT st.student_id, st.full_name, c.class_name,
                   SUM(fp.amount_due) AS total_due,
                   SUM(fp.amount_paid) AS total_paid,
                   SUM(fp.amount_due) - SUM(fp.amount_paid) AS balance
            FROM fees_payment fp
            JOIN students st ON fp.student_id = st.student_id
            LEFT JOIN classes c ON st.class_id = c.class_id
        """
        params = []
        if class_id is not None:
            sql += " WHERE st.class_id = ?"
            params.append(class_id)
        sql += " GROUP BY st.student_id HAVING balance > 0 ORDER BY balance DESC"
        return self._fetchall(sql, tuple(params))

    def record_payment(self, student_id: int, term: str, amount_due: float,
                        amount_paid: float, payment_date: str = None,
                        payment_method: str = None) -> int:
        status = "paid" if amount_paid >= amount_due else (
            "partial" if amount_paid > 0 else "pending"
        )
        return self._execute(
            """INSERT INTO fees_payment
               (student_id, term, amount_due, amount_paid, payment_date, payment_method, status)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (student_id, term, amount_due, amount_paid, payment_date, payment_method, status),
        )

    # =================================================================
    # GUARDIAN MESSAGES
    # =================================================================
    def get_guardian_messages(self, guardian_id: int) -> list[dict]:
        return self._fetchall(
            """SELECT gm.*, st.full_name AS student_name, t.full_name AS sender_name
               FROM guardian_messages gm
               LEFT JOIN students st ON gm.student_id = st.student_id
               LEFT JOIN teachers t ON gm.sender_teacher_id = t.teacher_id
               WHERE gm.guardian_id = ?
               ORDER BY gm.sent_at DESC""",
            (guardian_id,),
        )

    def get_student_messages(self, student_id: int) -> list[dict]:
        return self._fetchall(
            """SELECT gm.*, g.full_name AS guardian_name, t.full_name AS sender_name
               FROM guardian_messages gm
               JOIN guardians g ON gm.guardian_id = g.guardian_id
               LEFT JOIN teachers t ON gm.sender_teacher_id = t.teacher_id
               WHERE gm.student_id = ?
               ORDER BY gm.sent_at DESC""",
            (student_id,),
        )

    def send_guardian_message(self, guardian_id: int, message_text: str,
                               student_id: int = None, sender_teacher_id: int = None,
                               subject: str = None) -> int:
        return self._execute(
            """INSERT INTO guardian_messages
               (guardian_id, student_id, sender_teacher_id, subject, message_text)
               VALUES (?, ?, ?, ?, ?)""",
            (guardian_id, student_id, sender_teacher_id, subject, message_text),
        )

    def mark_message_read(self, message_id: int) -> None:
        self._execute(
            "UPDATE guardian_messages SET status = 'read' WHERE message_id = ?",
            (message_id,),
        )

    # =================================================================
    # TIMETABLE
    # =================================================================
    def get_student_timetable(self, student_id: int, day_of_week: str = None) -> list[dict]:
        """A student's weekly timetable, derived from their class."""
        student = self.get_student(student_id)
        if not student or not student.get("class_id"):
            return []
        return self.get_class_timetable(student["class_id"], day_of_week)

    def add_timetable_slot(self, class_id: int, subject_id: int, day_of_week: str,
                            start_time: str, end_time: str, teacher_id: int = None) -> int:
        return self._execute(
            """INSERT INTO timetable
               (class_id, subject_id, teacher_id, day_of_week, start_time, end_time)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (class_id, subject_id, teacher_id, day_of_week, start_time, end_time),
        )

    # =================================================================
    # CROSS-TABLE / AGGREGATE CONVENIENCE METHODS
    # =================================================================
    def get_student_full_profile(self, student_id: int) -> dict:
        """
        Everything about one student in a single call: bio, guardian, class,
        attendance summary, score summary, fee balance, and recent messages.
        Handy for a student profile page.
        """
        student = self.get_student(student_id)
        if not student:
            raise RecordNotFoundError(f"No student with id {student_id}")

        return {
            "student": student,
            "attendance_summary": self.get_attendance_summary(student_id),
            "score_summary": self.get_score_summary(student_id),
            "fee_balance": self.get_fee_balance(student_id),
            "timetable": self.get_student_timetable(student_id),
            "recent_messages": self.get_student_messages(student_id)[:5],
        }

    def get_class_overview(self, class_id: int) -> dict:
        """Roster + subjects + timetable + average scores for a whole class."""
        class_info = self.get_class(class_id)
        if not class_info:
            raise RecordNotFoundError(f"No class with id {class_id}")

        return {
            "class": class_info,
            "students": self.get_class_students(class_id),
            "subjects": self.get_subjects_by_class(class_id),
            "timetable": self.get_class_timetable(class_id),
            "average_scores": self.get_class_average_scores(class_id),
            "outstanding_fees": self.get_outstanding_fees(class_id),
        }

    # =================================================================
    # GENERIC UPDATE HELPER (used internally)
    # =================================================================
    def _update_row(self, table: str, id_column: str, id_value: Any,
                     fields: dict) -> None:
        if not fields:
            return
        set_clause = ", ".join(f"{col} = ?" for col in fields)
        params = list(fields.values()) + [id_value]
        self._execute(
            f"UPDATE {table} SET {set_clause} WHERE {id_column} = ?",
            tuple(params),
        )

    # =================================================================
    # TEACHER PROFILES  (scheduling personas for the Timetable Optimizer)
    #
    # Independent of the operational `teachers` table above — these use
    # their own TCH-xxxxx IDs from the synthetic scheduling dataset.
    # =================================================================
    def get_teacher_profile(self, teacher_id: str) -> Optional[dict]:
        return self._fetchone(
            "SELECT * FROM teacher_profiles WHERE teacher_id = ?", (teacher_id,)
        )

    def get_all_teacher_profiles(self, school_id: str = None,
                                  persona: str = None,
                                  employment_type: str = None) -> list[dict]:
        sql = "SELECT * FROM teacher_profiles WHERE 1 = 1"
        params = []
        if school_id:
            sql += " AND school_id = ?"
            params.append(school_id)
        if persona:
            sql += " AND persona = ?"
            params.append(persona)
        if employment_type:
            sql += " AND employment_type = ?"
            params.append(employment_type)
        sql += " ORDER BY teacher_name"
        return self._fetchall(sql, tuple(params))

    def get_teacher_profiles_by_subject(self, subject_name: str) -> list[dict]:
        """subjects_taught is a pipe-separated string (e.g. 'Physics|Chemistry')."""
        return self._fetchall(
            "SELECT * FROM teacher_profiles WHERE subjects_taught LIKE ? ORDER BY teacher_name",
            (f"%{subject_name}%",),
        )

    # =================================================================
    # LESSON SCHEDULE  (candidate/generated timetable slots)
    # =================================================================
    def get_lesson(self, lesson_id: str) -> Optional[dict]:
        return self._fetchone(
            "SELECT * FROM lesson_schedule WHERE lesson_id = ?", (lesson_id,)
        )

    def get_lesson_schedule_by_teacher(self, teacher_id: str,
                                        day_of_week: str = None) -> list[dict]:
        sql = "SELECT * FROM lesson_schedule WHERE teacher_id = ?"
        params = [teacher_id]
        if day_of_week:
            sql += " AND day_of_week = ?"
            params.append(day_of_week)
        sql += " ORDER BY day_of_week, start_time"
        return self._fetchall(sql, tuple(params))

    def get_lesson_schedule_by_class_arm(self, class_arm_id: str,
                                          day_of_week: str = None) -> list[dict]:
        sql = "SELECT * FROM lesson_schedule WHERE class_arm_id = ?"
        params = [class_arm_id]
        if day_of_week:
            sql += " AND day_of_week = ?"
            params.append(day_of_week)
        sql += " ORDER BY day_of_week, start_time"
        return self._fetchall(sql, tuple(params))

    def get_lesson_schedule_by_room(self, room_id: str) -> list[dict]:
        return self._fetchall(
            "SELECT * FROM lesson_schedule WHERE room_id = ? ORDER BY day_of_week, start_time",
            (room_id,),
        )

    def get_lesson_schedule_by_school(self, school_id: str) -> list[dict]:
        return self._fetchall(
            "SELECT * FROM lesson_schedule WHERE school_id = ? ORDER BY day_of_week, start_time",
            (school_id,),
        )

    def get_teacher_workload_summary(self, school_id: str = None) -> list[dict]:
        """Lesson count and average preference score per teacher — handy for
        spotting overloaded teachers when validating a generated timetable."""
        sql = """
            SELECT ls.teacher_id, tp.teacher_name, tp.persona,
                   COUNT(*) AS lesson_count,
                   ROUND(AVG(ls.soft_preference_score), 3) AS avg_preference_score
            FROM lesson_schedule ls
            LEFT JOIN teacher_profiles tp ON ls.teacher_id = tp.teacher_id
            WHERE 1 = 1
        """
        params = []
        if school_id:
            sql += " AND ls.school_id = ?"
            params.append(school_id)
        sql += " GROUP BY ls.teacher_id ORDER BY lesson_count DESC"
        return self._fetchall(sql, tuple(params))

    # =================================================================
    # FEE DEFAULT RECORDS  (training/feature data for the Fee Default Predictor)
    #
    # Independent of the operational `fee_payments` table — these use
    # their own STU-xxxxx IDs from the synthetic risk-modeling dataset.
    # =================================================================
    def get_fee_default_record(self, student_id: str) -> Optional[dict]:
        return self._fetchone(
            "SELECT * FROM fee_default_records WHERE student_id = ?", (student_id,)
        )

    def get_fee_default_records(self, risk_label: str = None,
                                 academic_year: int = None,
                                 term: str = None,
                                 limit: int = None) -> list[dict]:
        sql = "SELECT * FROM fee_default_records WHERE 1 = 1"
        params = []
        if risk_label:
            sql += " AND fee_default_risk_label = ?"
            params.append(risk_label.upper())
        if academic_year:
            sql += " AND academic_year = ?"
            params.append(academic_year)
        if term:
            sql += " AND term = ?"
            params.append(term)
        sql += " ORDER BY student_id"
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        return self._fetchall(sql, tuple(params))

    def get_fee_default_records_by_guardian(self, guardian_id: str) -> list[dict]:
        return self._fetchall(
            "SELECT * FROM fee_default_records WHERE guardian_id = ? ORDER BY student_id",
            (guardian_id,),
        )

    def get_fee_default_label_distribution(self) -> list[dict]:
        """Count of records per risk label — quick class-balance check before training."""
        return self._fetchall(
            """SELECT fee_default_risk_label, COUNT(*) AS count
               FROM fee_default_records
               GROUP BY fee_default_risk_label
               ORDER BY count DESC"""
        )

    def get_fee_default_training_data(self) -> list[dict]:
        """All fee-default records, unfiltered — the full training set for
        the Fee Default Predictor."""
        return self._fetchall("SELECT * FROM fee_default_records")

    # =================================================================
    # STUDENT RISK RECORDS  (training/feature data for the Dropout Risk Engine)
    #
    # Independent of the operational `students` table — these use their
    # own STU-xxxxx IDs from the synthetic risk-modeling dataset. Column
    # names match the feature schema used by the Student Risk Engine model.
    # =================================================================
    def get_student_risk_record(self, student_id: str) -> Optional[dict]:
        return self._fetchone(
            "SELECT * FROM student_risk_records WHERE student_id = ?", (student_id,)
        )

    def get_student_risk_records(self, risk_label: str = None,
                                  school_type: str = None,
                                  academic_year: int = None,
                                  term: str = None,
                                  limit: int = None) -> list[dict]:
        sql = "SELECT * FROM student_risk_records WHERE 1 = 1"
        params = []
        if risk_label:
            sql += " AND dropout_risk_label = ?"
            params.append(risk_label.upper())
        if school_type:
            sql += " AND school_type = ?"
            params.append(school_type)
        if academic_year:
            sql += " AND academic_year = ?"
            params.append(academic_year)
        if term:
            sql += " AND term = ?"
            params.append(term)
        sql += " ORDER BY student_id"
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        return self._fetchall(sql, tuple(params))

    def get_student_risk_label_distribution(self) -> list[dict]:
        """Count of records per risk label — quick class-balance check before training."""
        return self._fetchall(
            """SELECT dropout_risk_label, COUNT(*) AS count
               FROM student_risk_records
               GROUP BY dropout_risk_label
               ORDER BY count DESC"""
        )

    def get_student_risk_training_data(self) -> list[dict]:
        """All student-risk records, unfiltered — the full training set for
        the Student Dropout Risk Engine."""
        return self._fetchall("SELECT * FROM student_risk_records")


if __name__ == "__main__":
    # quick smoke test when run directly
    with SchoolDB("school.db") as db:
        print(db.get_student(1))
