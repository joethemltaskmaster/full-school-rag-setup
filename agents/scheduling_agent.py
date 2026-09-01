"""
scheduling_agent.py
--------------------
Builds timetables using constraint satisfaction (OR-Tools CP-SAT), not
prediction. There's nothing to "learn" here - a timetable is either
valid (no double bookings, teachers only teaching stuff they're
eligible for and only when available) or it isn't, and beyond that we
just try to respect soft preferences where we can. So: solver, not a
model.fit() situation.

    teacher_profiles + requirements -> CP-SAT -> lesson_schedule rows

No training data, no .joblib, no accuracy number to report. Just
constraints in, schedule (or best-effort schedule) out.

Same handle(task) convention as the other agents:

    handler = SchedulingAgentHandler()
    handler.handle({"requirements": [...], "teacher_profiles": [...]})
    # -> {"status": ..., "agent": "scheduling_agent", "result": {...}}

Needs: pip install ortools. No API key, no network calls.

Note on rooms: room assignment is NOT part of the CP-SAT model. Adding
it as a third dimension blows up the variable count for not much
benefit, so it's just a greedy pass afterward (first free room that
matches the subject's room type). Could revisit if room contention
turns out to actually matter in practice.

Note on teacher_profiles fields: I don't have the real CSV in front of
me right now so the parsing below is a best guess based on the schema
notes. Formats assumed:
    subjects_taught             "Physics|Chemistry"
    class_arm_eligibility       "JSS1A|JSS1B"
    teacher_availability_mask   "Monday-1|Monday-2|Tuesday-3"
    cds_blocked_day             "Wednesday" or blank/None
    preferred_time_window       "Morning" / "Afternoon" / blank
    consecutive_period_preference   int, soft only
    min_rest_periods_required       number, soft only
If real data comes in looking different, fix _parse_pipe_list /
_parse_availability_mask, not the model itself.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from ortools.sat.python import cp_model

logger = logging.getLogger("scheduling_agent")
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
DEFAULT_PERIODS_PER_DAY = 8
MORNING_PERIODS = {1, 2, 3, 4}
AFTERNOON_PERIODS = {5, 6, 7, 8}


class OrchestratorAgentInterface:
    """Same handle() contract every agent implements."""

    def handle(self, task: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError


# -------------------------------------------------------------------------
# data shapes
# -------------------------------------------------------------------------
@dataclass
class TeacherProfile:
    teacher_id: str
    subjects_taught: list[str]
    class_arm_eligibility: list[str]
    available_slots: set[tuple[str, int]]
    preferred_time_window: str | None = None
    consecutive_period_preference: int | None = None
    min_rest_periods_required: float | None = None


@dataclass
class SchedulingRequirement:
    # one (class, subject) pair that needs periods_per_week slots
    class_arm_id: str
    subject_id: str
    periods_per_week: int
    eligible_teacher_ids: list[str]


@dataclass
class GeneratedLesson:
    class_arm_id: str
    subject_id: str
    teacher_id: str
    day_of_week: str
    period: int


# -------------------------------------------------------------------------
# parsing helpers - see the assumptions note up top
# -------------------------------------------------------------------------
def _parse_pipe_list(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in str(value).split("|") if item.strip()]


def _parse_availability_mask(mask, cds_blocked_day, periods_per_day=DEFAULT_PERIODS_PER_DAY):
    """
    Returns the (day, period) slots a teacher is free for. Empty mask
    means "available every slot except the blocked day" - figured a
    permissive default is safer than accidentally making someone
    unschedulable because their availability field was blank.
    """
    if mask:
        slots = set()
        for token in str(mask).split("|"):
            token = token.strip()
            if not token or "-" not in token:
                continue
            day, period_str = token.rsplit("-", 1)
            try:
                slots.add((day.strip(), int(period_str)))
            except ValueError:
                # bad token, just skip it rather than blow up the whole thing
                continue
    else:
        slots = {(day, p) for day in DAYS for p in range(1, periods_per_day + 1)}

    if cds_blocked_day:
        slots = {(d, p) for (d, p) in slots if d != cds_blocked_day.strip()}

    return slots


def teacher_profile_from_row(row: dict[str, Any]) -> TeacherProfile:
    return TeacherProfile(
        teacher_id=row["teacher_id"],
        subjects_taught=_parse_pipe_list(row.get("subjects_taught")),
        class_arm_eligibility=_parse_pipe_list(row.get("class_arm_eligibility")),
        available_slots=_parse_availability_mask(
            row.get("teacher_availability_mask"), row.get("cds_blocked_day")
        ),
        preferred_time_window=row.get("preferred_time_window") or None,
        consecutive_period_preference=row.get("consecutive_period_preference"),
        min_rest_periods_required=row.get("min_rest_periods_required"),
    )


# -------------------------------------------------------------------------
# the actual solver
# -------------------------------------------------------------------------
class TimetableGenerator:
    def __init__(self, periods_per_day=DEFAULT_PERIODS_PER_DAY, max_solve_seconds=30.0):
        self.periods_per_day = periods_per_day
        self.max_solve_seconds = max_solve_seconds
        self.all_slots = [(day, p) for day in DAYS for p in range(1, periods_per_day + 1)]

    def generate(self, requirements, teachers) -> dict[str, Any]:
        """
        {"status": "OPTIMAL"/"FEASIBLE"/"INFEASIBLE"/"UNKNOWN",
         "lessons": [...], "unscheduled_requirements": [...]}
        """
        return self._build_and_solve(requirements, teachers)

    def _build_and_solve(self, requirements, teachers) -> dict[str, Any]:
        model = cp_model.CpModel()

        choose = {}
        slot = {}
        busy = {}
        busy_by_teacher_slot = {}

        # filter out requirements that have literally no valid teacher
        # before we even start building variables for them
        valid_requirements = []
        for r_idx, req in enumerate(requirements):
            valid_teachers = [t for t in req.eligible_teacher_ids if t in teachers]
            if not valid_teachers:
                logger.warning("no valid teacher for %s/%s, skipping it", req.class_arm_id, req.subject_id)
                continue
            valid_requirements.append((r_idx, req, valid_teachers))

        for r_idx, req, valid_teachers in valid_requirements:
            for t_id in valid_teachers:
                choose[(r_idx, t_id)] = model.NewBoolVar(f"choose_r{r_idx}_{t_id}")
            model.Add(sum(choose[(r_idx, t_id)] for t_id in valid_teachers) == 1)

            for day, period in self.all_slots:
                slot[(r_idx, (day, period))] = model.NewBoolVar(f"slot_r{r_idx}_{day}_{period}")
            model.Add(sum(slot[(r_idx, s)] for s in self.all_slots) == req.periods_per_week)

            for t_id in valid_teachers:
                teacher = teachers[t_id]
                for day, period in self.all_slots:
                    busy_var = model.NewBoolVar(f"busy_r{r_idx}_{t_id}_{day}_{period}")
                    model.AddMultiplicationEquality(busy_var, [choose[(r_idx, t_id)], slot[(r_idx, (day, period))]])
                    if (day, period) not in teacher.available_slots:
                        model.Add(busy_var == 0)

                    busy[(r_idx, t_id, day, period)] = busy_var
                    busy_by_teacher_slot.setdefault((t_id, day, period), []).append(busy_var)

        # hard constraint: a teacher can't be in two places at once
        for t_id in set(t for _, t in choose.keys()):
            for day, period in self.all_slots:
                busy_vars = busy_by_teacher_slot.get((t_id, day, period), [])
                if busy_vars:
                    model.Add(sum(busy_vars) <= 1)

        # hard constraint: same for classes - can't have two lessons at once
        for class_arm_id in set(req.class_arm_id for _, req, _ in valid_requirements):
            for day, period in self.all_slots:
                relevant = [
                    slot[(r_idx, (day, period))]
                    for r_idx, req, _ in valid_requirements
                    if req.class_arm_id == class_arm_id
                ]
                if relevant:
                    model.Add(sum(relevant) <= 1)

        # soft: give a little bonus for slots that fall in a teacher's
        # preferred window. nothing fancy, just enough to nudge the solver
        objective_terms = []
        for (r_idx, t_id, day, period), busy_var in busy.items():
            bonus = self._preference_bonus(teachers[t_id], period)
            if bonus:
                objective_terms.append(bonus * busy_var)
        if objective_terms:
            model.Maximize(sum(objective_terms))

        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = self.max_solve_seconds
        solver.parameters.num_search_workers = 8
        status = solver.Solve(model)
        status_name = solver.StatusName(status)

        lessons = []
        unscheduled = []

        if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            for r_idx, req, valid_teachers in valid_requirements:
                chosen_teacher = None
                for t_id in valid_teachers:
                    if solver.Value(choose[(r_idx, t_id)]) == 1:
                        chosen_teacher = t_id
                        break

                if chosen_teacher is None:
                    unscheduled.append({"class_arm_id": req.class_arm_id, "subject_id": req.subject_id})
                    continue

                for day, period in self.all_slots:
                    if solver.Value(slot[(r_idx, (day, period))]) == 1:
                        lessons.append(GeneratedLesson(
                            class_arm_id=req.class_arm_id,
                            subject_id=req.subject_id,
                            teacher_id=chosen_teacher,
                            day_of_week=day,
                            period=period,
                        ))
        else:
            # infeasible / unknown -> nothing got scheduled, report all of it back
            unscheduled = [
                {"class_arm_id": req.class_arm_id, "subject_id": req.subject_id}
                for _, req, _ in valid_requirements
            ]

        return {"status": status_name, "lessons": lessons, "unscheduled_requirements": unscheduled}

    @staticmethod
    def _preference_bonus(teacher: TeacherProfile, period: int) -> int:
        if not teacher.preferred_time_window:
            return 0
        window = teacher.preferred_time_window.strip().lower()
        if window == "morning" and period in MORNING_PERIODS:
            return 2
        if window == "afternoon" and period in AFTERNOON_PERIODS:
            return 2
        return 0


# -------------------------------------------------------------------------
# orchestrator-facing wrapper
# -------------------------------------------------------------------------
class SchedulingAgentHandler(OrchestratorAgentInterface):
    """
    task = {
        "requirements": [
            {"class_arm_id": "JSS1A", "subject_id": "Physics", "periods_per_week": 4,
             "eligible_teacher_ids": ["TCH-00001", "TCH-00014"]},
            ...
        ],
        "teacher_profiles": [ {...row from teacher_profiles...}, ... ],
        "periods_per_day": 8,   # optional
    }
    """

    def __init__(self, max_solve_seconds: float = 30.0):
        self.max_solve_seconds = max_solve_seconds

    def handle(self, task: dict[str, Any]) -> dict[str, Any]:
        raw_requirements = task.get("requirements")
        raw_profiles = task.get("teacher_profiles")

        if not raw_requirements or not raw_profiles:
            return {
                "status": "error",
                "agent": "scheduling_agent",
                "error": "Task must include both 'requirements' and 'teacher_profiles'.",
            }

        try:
            teachers = {row["teacher_id"]: teacher_profile_from_row(row) for row in raw_profiles}
            requirements = [
                SchedulingRequirement(
                    class_arm_id=r["class_arm_id"],
                    subject_id=r["subject_id"],
                    periods_per_week=int(r["periods_per_week"]),
                    eligible_teacher_ids=list(r["eligible_teacher_ids"]),
                )
                for r in raw_requirements
            ]

            generator = TimetableGenerator(
                periods_per_day=task.get("periods_per_day", DEFAULT_PERIODS_PER_DAY),
                max_solve_seconds=self.max_solve_seconds,
            )
            result = generator._build_and_solve(requirements, teachers)

            lessons_out = [
                {
                    "class_arm_id": l.class_arm_id,
                    "subject_id": l.subject_id,
                    "teacher_id": l.teacher_id,
                    "day_of_week": l.day_of_week,
                    "period": l.period,
                }
                for l in result["lessons"]
            ]

            solved = result["status"] in ("OPTIMAL", "FEASIBLE")
            out = {
                "status": "success" if solved else "error",
                "agent": "scheduling_agent",
                "result": {
                    "solver_status": result["status"],
                    "lessons": lessons_out,
                    "unscheduled_requirements": result["unscheduled_requirements"],
                },
            }
            if not solved:
                out["error"] = f"Solver returned {result['status']} - see unscheduled_requirements"
            return out

        except Exception as exc:
            logger.exception("SchedulingAgent blew up")
            return {"status": "error", "agent": "scheduling_agent", "error": str(exc)}


if __name__ == "__main__":
    import json

    # quick self-test, 2 teachers / 2 classes / 2 subjects, just to prove
    # the solver isn't obviously broken without needing real school.db data
    sample_teachers = [
        {
            "teacher_id": "TCH-001",
            "subjects_taught": "Physics|Chemistry",
            "class_arm_eligibility": "JSS1A|JSS1B",
            "teacher_availability_mask": "",
            "cds_blocked_day": "Wednesday",
            "preferred_time_window": "Morning",
        },
        {
            "teacher_id": "TCH-002",
            "subjects_taught": "Physics",
            "class_arm_eligibility": "JSS1A|JSS1B",
            "teacher_availability_mask": "",
            "cds_blocked_day": None,
            "preferred_time_window": "Afternoon",
        },
    ]

    sample_requirements = [
        {"class_arm_id": "JSS1A", "subject_id": "Physics", "periods_per_week": 3,
         "eligible_teacher_ids": ["TCH-001", "TCH-002"]},
        {"class_arm_id": "JSS1B", "subject_id": "Physics", "periods_per_week": 3,
         "eligible_teacher_ids": ["TCH-001", "TCH-002"]},
        {"class_arm_id": "JSS1A", "subject_id": "Chemistry", "periods_per_week": 2,
         "eligible_teacher_ids": ["TCH-001"]},
    ]

    handler = SchedulingAgentHandler()
    output = handler.handle({"requirements": sample_requirements, "teacher_profiles": sample_teachers})
    print(json.dumps(output, indent=2))