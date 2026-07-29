"""
agent.py

A rule-based data agent for the school system.

No API, no LLM, no external calls of any kind. This class simply knows
*which* db_service.py method answers a given named request, and how to
call it safely. It sits between the orchestrator and the database layer:

    orchestrator.py  -->  agent.py (SchoolAgent)  -->  db_service.py (SchoolDB)  -->  school.db

The agent never talks SQL directly — it only ever calls methods on
SchoolDB. The orchestrator never talks to SchoolDB directly either — it
only ever calls SchoolAgent.handle(...).

Usage:
    from agent import SchoolAgent

    agent = SchoolAgent("school.db")
    result = agent.handle("get_student", student_id=1)
    print(result)   # {"ok": True, "data": {...}}
"""

from typing import Any, Callable

from db_service import SchoolDB, RecordNotFoundError


class UnknownIntentError(Exception):
    """Raised when the orchestrator asks for a capability the agent doesn't have."""


class SchoolAgent:
    """
    A capability registry over SchoolDB.

    Each "intent" is a named capability (e.g. "get_student",
    "record_payment") mapped to a bound method on this class. The
    orchestrator calls `handle(intent, **params)` and never needs to
    know which db_service method actually answers it.
    """

    def __init__(self, db_path: str = "school.db"):
        self.db_path = db_path

        # ---- read-only capabilities: safe to run without confirmation ----
        self._read_intents: dict[str, Callable] = {
            "get_student": self._get_student,
            "get_all_students": self._get_all_students,
            "get_class_students": self._get_class_students,
            "search_students": self._search_students,
            "get_guardian": self._get_guardian,
            "get_students_by_guardian": self._get_students_by_guardian,
            "get_teacher": self._get_teacher,
            "get_all_teachers": self._get_all_teachers,
            "get_teacher_schedule": self._get_teacher_schedule,
            "get_class": self._get_class,
            "get_all_classes": self._get_all_classes,
            "get_class_timetable": self._get_class_timetable,
            "get_class_average_scores": self._get_class_average_scores,
            "get_class_attendance_for_date": self._get_class_attendance_for_date,
            "get_subject": self._get_subject,
            "get_subjects_by_class": self._get_subjects_by_class,
            "get_attendance": self._get_attendance,
            "get_attendance_summary": self._get_attendance_summary,
            "get_scores": self._get_scores,
            "get_score_summary": self._get_score_summary,
            "get_fee_history": self._get_fee_history,
            "get_fee_balance": self._get_fee_balance,
            "get_outstanding_fees": self._get_outstanding_fees,
            "get_guardian_messages": self._get_guardian_messages,
            "get_student_messages": self._get_student_messages,
            "get_student_timetable": self._get_student_timetable,
            "get_student_full_profile": self._get_student_full_profile,
            "get_class_overview": self._get_class_overview,
        }

        # ---- write capabilities: mutate real data, require confirm=True ----
        self._write_intents: dict[str, Callable] = {
            "record_attendance": self._record_attendance,
            "record_score": self._record_score,
            "record_payment": self._record_payment,
            "send_guardian_message": self._send_guardian_message,
        }

    # =================================================================
    # PUBLIC ENTRYPOINT — this is the only method the orchestrator calls
    # =================================================================
    def handle(self, intent: str, **params) -> dict[str, Any]:
        """
        Run a named capability with the given parameters.

        Always returns a dict shaped like:
            {"ok": True,  "data": <result>}
            {"ok": False, "error": <message>}

        Never raises — every failure mode is turned into a structured
        error so the orchestrator can rely on a single response shape.
        """
        is_write = intent in self._write_intents
        fn = self._read_intents.get(intent) or self._write_intents.get(intent)

        if fn is None:
            return {
                "ok": False,
                "error": f"Unknown intent '{intent}'",
                "available_intents": self.list_intents(),
            }

        if is_write and not params.pop("confirm", False):
            return {
                "ok": False,
                "error": (
                    f"'{intent}' modifies data and requires confirm=True. "
                    f"Re-run with confirm=True to proceed."
                ),
            }

        try:
            result = fn(**params)
            return {"ok": True, "data": result}
        except RecordNotFoundError as e:
            return {"ok": False, "error": str(e)}
        except TypeError as e:
            return {"ok": False, "error": f"Invalid parameters for '{intent}': {e}"}
        except Exception as e:  # noqa: BLE001 - agent boundary must never raise
            return {"ok": False, "error": f"Unexpected error in '{intent}': {e}"}

    def list_intents(self) -> dict[str, list[str]]:
        """Lets the orchestrator (or anything else) discover what this agent can do."""
        return {
            "read": sorted(self._read_intents.keys()),
            "write": sorted(self._write_intents.keys()),
        }

    # =================================================================
    # INTERNAL: one small wrapper per db_service method.
    # Each opens its own short-lived connection and closes it immediately.
    # =================================================================
    def _get_student(self, student_id: int):
        with SchoolDB(self.db_path) as db:
            return db.get_student(student_id)

    def _get_all_students(self, class_id: int = None, status: str = None):
        with SchoolDB(self.db_path) as db:
            return db.get_all_students(class_id=class_id, status=status)

    def _get_class_students(self, class_id: int, status: str = "active"):
        with SchoolDB(self.db_path) as db:
            return db.get_class_students(class_id, status=status)

    def _search_students(self, name_query: str):
        with SchoolDB(self.db_path) as db:
            return db.search_students(name_query)

    def _get_guardian(self, guardian_id: int):
        with SchoolDB(self.db_path) as db:
            return db.get_guardian(guardian_id)

    def _get_students_by_guardian(self, guardian_id: int):
        with SchoolDB(self.db_path) as db:
            return db.get_students_by_guardian(guardian_id)

    def _get_teacher(self, teacher_id: int):
        with SchoolDB(self.db_path) as db:
            return db.get_teacher(teacher_id)

    def _get_all_teachers(self):
        with SchoolDB(self.db_path) as db:
            return db.get_all_teachers()

    def _get_teacher_schedule(self, teacher_id: int, day_of_week: str = None):
        with SchoolDB(self.db_path) as db:
            return db.get_teacher_schedule(teacher_id, day_of_week=day_of_week)

    def _get_class(self, class_id: int):
        with SchoolDB(self.db_path) as db:
            return db.get_class(class_id)

    def _get_all_classes(self):
        with SchoolDB(self.db_path) as db:
            return db.get_all_classes()

    def _get_class_timetable(self, class_id: int, day_of_week: str = None):
        with SchoolDB(self.db_path) as db:
            return db.get_class_timetable(class_id, day_of_week=day_of_week)

    def _get_class_average_scores(self, class_id: int, subject_id: int = None):
        with SchoolDB(self.db_path) as db:
            return db.get_class_average_scores(class_id, subject_id=subject_id)

    def _get_class_attendance_for_date(self, class_id: int, date: str):
        with SchoolDB(self.db_path) as db:
            return db.get_class_attendance_for_date(class_id, date)

    def _get_subject(self, subject_id: int):
        with SchoolDB(self.db_path) as db:
            return db.get_subject(subject_id)

    def _get_subjects_by_class(self, class_id: int):
        with SchoolDB(self.db_path) as db:
            return db.get_subjects_by_class(class_id)

    def _get_attendance(self, student_id: int, start_date: str = None, end_date: str = None):
        with SchoolDB(self.db_path) as db:
            return db.get_attendance(student_id, start_date=start_date, end_date=end_date)

    def _get_attendance_summary(self, student_id: int, start_date: str = None, end_date: str = None):
        with SchoolDB(self.db_path) as db:
            return db.get_attendance_summary(student_id, start_date=start_date, end_date=end_date)

    def _get_scores(self, student_id: int, term: str = None, subject_id: int = None):
        with SchoolDB(self.db_path) as db:
            return db.get_scores(student_id, term=term, subject_id=subject_id)

    def _get_score_summary(self, student_id: int, term: str = None):
        with SchoolDB(self.db_path) as db:
            return db.get_score_summary(student_id, term=term)

    def _get_fee_history(self, student_id: int):
        with SchoolDB(self.db_path) as db:
            return db.get_fee_history(student_id)

    def _get_fee_balance(self, student_id: int):
        with SchoolDB(self.db_path) as db:
            return db.get_fee_balance(student_id)

    def _get_outstanding_fees(self, class_id: int = None):
        with SchoolDB(self.db_path) as db:
            return db.get_outstanding_fees(class_id=class_id)

    def _get_guardian_messages(self, guardian_id: int):
        with SchoolDB(self.db_path) as db:
            return db.get_guardian_messages(guardian_id)

    def _get_student_messages(self, student_id: int):
        with SchoolDB(self.db_path) as db:
            return db.get_student_messages(student_id)

    def _get_student_timetable(self, student_id: int, day_of_week: str = None):
        with SchoolDB(self.db_path) as db:
            return db.get_student_timetable(student_id, day_of_week=day_of_week)

    def _get_student_full_profile(self, student_id: int):
        with SchoolDB(self.db_path) as db:
            return db.get_student_full_profile(student_id)

    def _get_class_overview(self, class_id: int):
        with SchoolDB(self.db_path) as db:
            return db.get_class_overview(class_id)

    # ---- write wrappers ----
    def _record_attendance(self, student_id: int, date: str, status: str, recorded_by: int = None):
        with SchoolDB(self.db_path) as db:
            return {"attendance_id": db.record_attendance(student_id, date, status, recorded_by)}

    def _record_score(self, student_id: int, subject_id: int, term: str, exam_type: str,
                       score: float, max_score: float = 100, date_recorded: str = None):
        with SchoolDB(self.db_path) as db:
            return {"score_id": db.record_score(student_id, subject_id, term, exam_type,
                                                 score, max_score, date_recorded)}

    def _record_payment(self, student_id: int, term: str, amount_due: float,
                         amount_paid: float, payment_date: str = None, payment_method: str = None):
        with SchoolDB(self.db_path) as db:
            return {"payment_id": db.record_payment(student_id, term, amount_due, amount_paid,
                                                      payment_date, payment_method)}

    def _send_guardian_message(self, guardian_id: int, message_text: str, student_id: int = None,
                                sender_teacher_id: int = None, subject: str = None):
        with SchoolDB(self.db_path) as db:
            return {"message_id": db.send_guardian_message(guardian_id, message_text, student_id,
                                                             sender_teacher_id, subject)}


if __name__ == "__main__":
    agent = SchoolAgent("school.db")
    print(agent.list_intents())
    print(agent.handle("get_student", student_id=1))
