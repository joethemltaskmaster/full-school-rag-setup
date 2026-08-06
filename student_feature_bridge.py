"""
student_feature_bridge.py

Translates the NESTED output of SchoolDB.get_student_full_profile() (plus
raw attendance rows) into the FLAT feature row the trained
student_risk_engine model expects: fee_payment_days_late, attendance_rate,
consecutive_absences, score_avg, subject_failure_count,
paid_before_resumption, school_type.

Why this exists: student_risk_engine was trained on a separate synthetic
dataset (nigerian_students_synthetic-1.csv, now sitting in the
student_risk_records table) that already had those six columns flat. The
real school.db schema is relational -- get_student_full_profile() returns
attendance/score/fee data as summaries and per-subject lists, not a
single flat row, and three of the six training features have no honest
equivalent in the operational schema at all:

    fee_payment_days_late / paid_before_resumption
        -- both need a term "due date" to measure a payment against.
           There is no term-calendar table in school.db, so these can't
           be computed for real, only guessed.
    school_type
        -- this schema represents a single school; there's no
           school_type or multi-tenant concept anywhere in it.

This module is deliberately explicit about that rather than silently
guessing. Every derived field is a real computation; every field that
can't be derived is documented as a PLACEHOLDER, defaulted to a neutral
value, and named in the returned dict's "_assumptions" list, so nothing
downstream (a Streamlit page, a workflow result, a log line) can present
a placeholder as if it were measured data.

Real fix, when you're ready for it: retrain student_risk_engine on
features this schema can actually produce (drop the three fields above,
or replace them with real proxies like a fee outstanding_balance_ratio),
using students.status == 'withdrawn' as a proxy label. That removes the
need for this bridge's placeholders entirely -- until then, this lets
the existing model run against real students with its limitations made
visible instead of hidden.
"""

from __future__ import annotations

from typing import Any

# Nigerian secondary-school pass mark used to derive subject_failure_count.
# Adjust to your school's actual policy.
PASS_MARK = 40.0

# Neutral placeholder values, used ONLY when the real value can't be
# derived from this schema. Chosen to be the least risk-distorting
# default rather than an extreme value -- but still placeholders, not
# measurements.
PLACEHOLDER_SCHOOL_TYPE = "public_state"
PLACEHOLDER_FEE_DAYS_LATE = 0
PLACEHOLDER_PAID_BEFORE_RESUMPTION = 1


def _consecutive_absences_from_rows(attendance_rows: list[dict[str, Any]]) -> int:
    """Longest run of consecutive 'absent' days at the END of the record
    (the student's current absence streak), from raw day-by-day
    attendance rows. get_attendance_summary() only gives counts, not
    order, so this needs the raw rows from get_attendance()."""
    if not attendance_rows:
        return 0
    rows_sorted = sorted(attendance_rows, key=lambda r: r["date"])
    streak = 0
    for row in reversed(rows_sorted):
        if (row.get("status") or "").lower() == "absent":
            streak += 1
        else:
            break
    return streak


def _score_avg_and_failures(score_summary: list[dict[str, Any]]) -> tuple[float | None, int]:
    """score_summary is one row per subject: {"subject_name",
    "average_score", "number_of_assessments"}. Derives an overall average
    and a count of subjects currently below the pass mark."""
    if not score_summary:
        return None, 0
    averages = [row["average_score"] for row in score_summary if row.get("average_score") is not None]
    if not averages:
        return None, 0
    overall_avg = round(sum(averages) / len(averages), 2)
    failure_count = sum(1 for avg in averages if avg < PASS_MARK)
    return overall_avg, failure_count


def build_risk_features(
    profile: dict[str, Any],
    raw_attendance_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """
    Build the flat feature dict student_risk_engine expects, from the
    nested output of SchoolDB.get_student_full_profile() (plus raw
    attendance rows, which the profile alone doesn't include).

    Returns the model's expected keys PLUS an "_assumptions" key listing
    which fields are real derivations vs. placeholders.
    """
    student = profile.get("student", {}) or {}
    attendance_summary = profile.get("attendance_summary", {}) or {}
    score_summary = profile.get("score_summary", []) or []

    attendance_rate = attendance_summary.get("attendance_rate")
    consecutive_absences = _consecutive_absences_from_rows(raw_attendance_rows or [])
    score_avg, subject_failure_count = _score_avg_and_failures(score_summary)

    assumptions: list[str] = []
    if attendance_rate is None:
        assumptions.append("attendance_rate: no attendance records yet, defaulted to None")
    if not raw_attendance_rows:
        assumptions.append("consecutive_absences: raw attendance rows not supplied, defaulted to 0")
    if score_avg is None:
        assumptions.append("score_avg: no scores recorded yet, defaulted to None")

    assumptions.append(
        "fee_payment_days_late: PLACEHOLDER -- school.db has no term-calendar/due-date table "
        "to measure lateness against; using a neutral default."
    )
    assumptions.append("paid_before_resumption: PLACEHOLDER -- same reason as above.")
    assumptions.append(
        "school_type: PLACEHOLDER -- school.db represents a single school with no school_type "
        "concept; using a neutral default."
    )

    return {
        "student_id": student.get("student_id"),
        "attendance_rate": attendance_rate,
        "consecutive_absences": consecutive_absences,
        "score_avg": score_avg,
        "subject_failure_count": subject_failure_count,
        "fee_payment_days_late": PLACEHOLDER_FEE_DAYS_LATE,
        "paid_before_resumption": PLACEHOLDER_PAID_BEFORE_RESUMPTION,
        "school_type": PLACEHOLDER_SCHOOL_TYPE,
        "_assumptions": assumptions,
    }
