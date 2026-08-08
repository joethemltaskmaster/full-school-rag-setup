"""
planner_agent.py

Planner Agent -- the "front door" of the multi-agent school system.

Where data_agent / prediction_agent / retrieval_agent each answer ONE
kind of question, and the orchestrator composes them into workflows, the
planner is what decides which of those to reach for in the first place,
given a free-text request. It never talks to a database, a model file,
or a vector index directly -- it only ever talks to an LLM (to understand
the request) and to the Orchestrator instance it's handed (to discover
what's possible and to actually run the plan).

    user message
        -> identify_intent()        LLM call: classify against a controlled
                                     vocabulary built from real agent capabilities
        -> extract_entities()       LLM call: pull structured slots (student_id,
                                     class_id, term, date, query text, ...)
        -> create_execution_plan()  rule-based: known intent + entities -> an
                                     ordered list of concrete agent calls
        -> execute_plan()           runs each step through the SAME orchestrator
                                     instance passed in at construction time

Responsibilities kept deliberately narrow:
    - Intent recognition                         (LLM)
    - Entity extraction                          (LLM)
    - Agent selection, ordering, parameter wiring (deterministic, rule-based)
    - Producing + running a structured execution plan

Note on "timetable": timetable requests (a student's timetable, a class
timetable, a teacher's schedule) are not a separate agent -- data_agent
already owns that capability via get_student_timetable /
get_class_timetable / get_teacher_schedule. The planner treats
"timetable" as one of its recognized intent categories and routes it to
the data agent like any other read, same as attendance, scores, or fees.

Compatible with orchestrator.py as provided: every step the planner
emits maps onto Orchestrator's actual interface --
execute({"intent"/"model"/"query": ...}) and run_workflow(name, **params) --
and every result is read back in Orchestrator's real
{"status", "agent", "result"/"error"} shape, not an "ok"/"data" shape.

LLM backend: NVIDIA-hosted inference (ChatNVIDIA / NIM), not Gemini.
Both LLM calls (identify_intent, extract_entities) internally stream the
response and join the chunks, rather than blocking on .invoke() -- same
fix applied in retrieval_agent.py: a blocking call waits for the ENTIRE
response before anything comes back, which is what was producing 60s
read-timeout errors. Streaming keeps the connection active token by
token instead of one long silent wait. The public methods still return
one complete parsed dict, since JSON needs to be fully received before
it can be parsed anyway -- only the underlying HTTP behavior changed.

Usage:
    from orchestrator import Orchestrator
    from planner_agent import PlannerAgent

    orch = Orchestrator(db_path="school.db")
    planner = PlannerAgent(orchestrator=orch)  # reads NVIDIA_API_KEY from env

    result = planner.run("Is student 14's attendance okay? Notify the guardian if not.")
    # result = {"status": "success"/"error", "plan": {...}, "steps": [...], "final_result": {...}}
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any
from dotenv import load_dotenv

# No hardcoded path: python-dotenv's default load_dotenv() walks up from the
# current working directory looking for a .env file, which works the same
# on any machine/deployment target instead of only on one person's desktop.
load_dotenv(dotenv_path=r"C:\Users\Joseph\Desktop\Database\agents\.env")

from langchain_nvidia_ai_endpoints import ChatNVIDIA

logger = logging.getLogger("planner_agent")
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

DEFAULT_NVIDIA_MODEL = "nvidia/nemotron-3-ultra-550b-a55b"
DEFAULT_RISK_MODEL = "student_risk_engine"


class PlannerAgentError(Exception):
    """Raised for planner-specific setup or LLM-parsing failures."""


# --------------------------------------------------------------------------- #
# Controlled vocabulary the LLM classifies against. Grouping data-agent's
# 25+ raw intents into a handful of semantic categories makes classification
# reliable; create_execution_plan() below maps each category back down to
# concrete agent/intent names the orchestrator actually understands.
# --------------------------------------------------------------------------- #

INTENT_CATEGORIES: dict[str, dict[str, Any]] = {
    "student_lookup": {
        "description": "Looking up a specific student's basic info or full profile.",
        "agent": "data",
    },
    "student_search": {
        "description": "Searching or listing students by name, class, or status.",
        "agent": "data",
    },
    "attendance": {
        "description": "Attendance records, attendance rate, or attendance on a specific date.",
        "agent": "data",
    },
    "scores": {
        "description": "Exam/test scores, grades, or class score averages.",
        "agent": "data",
    },
    "fees": {
        "description": "Fee balance, fee/payment history, or outstanding fees.",
        "agent": "data",
    },
    "timetable": {
        "description": "A class timetable, a student's timetable, or a teacher's schedule.",
        "agent": "data",
    },
    "guardian_communication": {
        "description": "Sending a message to a guardian, or reading past guardian messages.",
        "agent": "data",
    },
    "class_overview": {
        "description": "A class-level overview or roster snapshot.",
        "agent": "data",
    },
    "risk_prediction": {
        "description": "Predicting a student's dropout/fee-default risk, or any ML model score.",
        "agent": "workflow",
        "workflow": "predict_dropout_risk_for_student",
    },
    "policy_question": {
        "description": "A question about school policy, rules, or guidance documents (not row-level data).",
        "agent": "retrieval",
    },
    "attendance_notification": {
        "description": "Checking a student's attendance against a threshold and notifying the guardian if it's low.",
        "agent": "workflow",
        "workflow": "attendance_check_and_notify",
    },
    "combined_briefing": {
        "description": (
            "A request that needs the student's data AND a risk prediction AND policy "
            "guidance together, e.g. 'give me a full risk briefing on student 5'."
        ),
        "agent": "workflow",
        "workflow": "at_risk_briefing",
    },
    "full_student_report": {
        "description": "A full report on one student: attendance, scores, fees, and timetable together.",
        "agent": "workflow",
        "workflow": "student_report",
    },
    "full_class_report": {
        "description": "A full report on one class: roster, averages, outstanding fees, and timetable together.",
        "agent": "workflow",
        "workflow": "class_report",
    },
    "unknown": {
        "description": "None of the above, or not enough information in the message to tell.",
        "agent": None,
    },
}


@dataclass
class ExecutionStep:
    step: int
    agent: str  # "data" | "predictor" | "retrieval" | "workflow" | "clarification"
    action: str  # data-agent intent name / model key / "ask" / workflow name
    params: dict[str, Any] = field(default_factory=dict)
    note: str = ""


@dataclass
class ExecutionPlan:
    intent: str
    confidence: float
    reasoning: str
    entities: dict[str, Any]
    steps: list[ExecutionStep]

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "confidence": self.confidence,
            "reasoning": self.reasoning,
            "entities": self.entities,
            "steps": [vars(s) for s in self.steps],
        }


def _clean(params: dict[str, Any]) -> dict[str, Any]:
    """Drops None-valued kwargs so optional params fall back to each
    method's own defaults instead of being explicitly overridden with None."""
    return {k: v for k, v in params.items() if v is not None}


# Params that MUST be present (non-None) for a given data intent to
# succeed. Checked BEFORE calling the orchestrator, so a missing required
# value produces one clear, actionable message ("I couldn't find a
# student_id...") instead of letting _clean() silently drop it and having
# it surface several layers down as a raw Python TypeError from
# data_agent's argument binding.
REQUIRED_DATA_PARAMS: dict[str, list[str]] = {
    "get_student_full_profile": ["student_id"],
    "get_student": ["student_id"],
    "get_attendance_summary": ["student_id"],
    "get_score_summary": ["student_id"],
    "get_fee_balance": ["student_id"],
    "get_student_timetable": ["student_id"],
    "get_student_messages": ["student_id"],
    "get_class_overview": ["class_id"],
    "get_class": ["class_id"],
    "get_class_students": ["class_id"],
    "get_class_timetable": ["class_id"],
    "get_class_average_scores": ["class_id"],
    "get_teacher": ["teacher_id"],
    "get_teacher_schedule": ["teacher_id"],
    "get_guardian": ["guardian_id"],
    "get_guardian_messages": ["guardian_id"],
    "search_students": ["name_query"],
}

REQUIRED_WORKFLOW_PARAMS: dict[str, list[str]] = {
    "student_report": ["student_id"],
    "class_report": ["class_id"],
    "attendance_check_and_notify": ["student_id"],
    "predict_dropout_risk_for_student": ["student_id"],
    "predict_dropout_risk_for_reference_student": ["student_id"],
    # at_risk_briefing is handled separately below -- it needs EITHER
    # student_id OR reference_student_id, not a fixed required list.
}


class PlannerAgent:
    """
    LLM-backed request planner.

    Given a free-text message, decides WHAT the user wants (intent), WHAT
    specific values are involved (entities), WHICH agent(s) the
    orchestrator should call and in WHAT order (execution plan), then
    (optionally) runs that plan through the orchestrator instance it was
    constructed with.
    """

    def __init__(
        self,
        orchestrator,
        nvidia_api_key: str | None = None,
        nvidia_model: str = DEFAULT_NVIDIA_MODEL,
        temperature: float = 1.0,
        request_timeout: int = 120,
    ):
        self.orchestrator = orchestrator
        self.nvidia_api_key = nvidia_api_key or os.environ.get("NVIDIA_API_KEY")
        self.nvidia_model_name = nvidia_model
        self.temperature = temperature
        self.request_timeout = request_timeout
        self._llm: ChatNVIDIA | None = None

        # Pulled from the live orchestrator so the planner never routes to an
        # intent/model/workflow the system can't actually serve.
        self.capabilities = self.orchestrator.capabilities()

    def _get_llm(self) -> ChatNVIDIA:
        if self._llm is None:
            if not self.nvidia_api_key:
                raise PlannerAgentError(
                    "No NVIDIA API key configured. Pass nvidia_api_key=... to "
                    "PlannerAgent(...) or set the NVIDIA_API_KEY environment variable."
                )
            self._llm = ChatNVIDIA(
                model=self.nvidia_model_name,
                temperature=self.temperature,
                api_key=self.nvidia_api_key,
                top_p=1,
                max_tokens=16384,
                seed=42,
                timeout=self.request_timeout,
            )
        return self._llm

    def _call_llm_json(self, system_prompt: str, user_message: str) -> dict[str, Any]:
        """Shared helper: call the NVIDIA-hosted LLM, expect JSON-only
        output, parse it defensively (models sometimes wrap JSON in
        ```json fences even when told not to).

        Streams the response internally and joins the chunks instead of
        a single blocking .invoke() call -- same fix as retrieval_agent.py.
        The full JSON string still has to be assembled before it can be
        parsed, so the public return type is unchanged; only the HTTP
        behavior underneath is different (keeps the connection active
        instead of one long wait that can trip a read timeout)."""
        llm = self._get_llm()
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]
        chunks = [chunk.content for chunk in llm.stream(messages) if chunk.content]
        raw = "".join(chunks)
        cleaned = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError as e:
            raise PlannerAgentError(f"LLM did not return valid JSON: {e}\nRaw output: {raw!r}")

    # =================================================================
    # 1. INTENT RECOGNITION (LLM)
    # =================================================================
    def identify_intent(self, user_message: str) -> dict[str, Any]:
        categories = "\n".join(f"- {name}: {cfg['description']}" for name, cfg in INTENT_CATEGORIES.items())
        system_prompt = (
            "You are the intent classifier for a school management assistant. "
            "Classify the user's message into EXACTLY ONE of these intent categories:\n\n"
            f"{categories}\n\n"
            "Respond with ONLY a JSON object, no markdown fences, no commentary:\n"
            '{"intent": "<one of the category names above>", '
            '"confidence": <number between 0.0 and 1.0>, '
            '"reasoning": "<one short sentence explaining the classification>"}'
        )
        parsed = self._call_llm_json(system_prompt, user_message)
        intent = parsed.get("intent", "unknown")
        if intent not in INTENT_CATEGORIES:
            logger.warning("LLM returned unrecognized intent '%s'; falling back to 'unknown'", intent)
            intent = "unknown"
        return {
            "intent": intent,
            "confidence": float(parsed.get("confidence", 0.0) or 0.0),
            "reasoning": parsed.get("reasoning", ""),
        }

    # =================================================================
    # 2. ENTITY EXTRACTION (LLM)
    # =================================================================
    def extract_entities(self, user_message: str) -> dict[str, Any]:
        system_prompt = (
            "Extract structured entities from the user's message for a school management "
            "assistant. Only include keys you find real evidence for -- omit anything not "
            "mentioned or implied. Respond with ONLY a JSON object, no markdown fences, no "
            "commentary, using this schema (every key optional):\n"
            "{\n"
            '  "student_id": <int, a REAL enrolled student\'s numeric ID, e.g. 1, 12, 214>,\n'
            '  "reference_student_id": <string, ONLY if the message literally contains an ID '
            'in the STU-xxxxx format, e.g. "STU-00001" -- this is a DIFFERENT id space from '
            'student_id and must not be converted to a number>,\n'
            '  "student_name": <string>,\n'
            '  "class_id": <int>,\n'
            '  "teacher_id": <int>,\n'
            '  "guardian_id": <int>,\n'
            '  "subject_id": <int>,\n'
            '  "term": <string>,\n'
            '  "date": <string, ISO format if possible>,\n'
            '  "start_date": <string>,\n'
            '  "end_date": <string>,\n'
            '  "day_of_week": <string>,\n'
            '  "threshold": <number>,\n'
            '  "subject": <string, message subject line>,\n'
            '  "message_text": <string, content of a message to send>,\n'
            '  "confirm": <true or false -- true only if the user explicitly authorized a write action>,\n'
            '  "model": <string, an ML model name if one was explicitly named>,\n'
            '  "query_text": <string, the free-text question, for policy/document questions>\n'
            "}"
        )
        try:
            return self._call_llm_json(system_prompt, user_message)
        except PlannerAgentError:
            logger.warning("Entity extraction failed to parse; continuing with no entities.")
            return {}

    # =================================================================
    # 3. AGENT SELECTION, ORDERING & PARAMETER WIRING (rule-based)
    # =================================================================
    def create_execution_plan(self, intent: str, entities: dict[str, Any],
                               confidence: float = 0.0, reasoning: str = "",
                               user_message: str = "") -> ExecutionPlan:
        """
        Deterministic by design: intent recognition and entity extraction
        already spent an LLM call each understanding the *request*. Turning
        a now-known intent into a now-known sequence of agent calls doesn't
        need a third LLM round-trip -- a rule-based mapping is faster,
        cheaper, and auditable in a way an LLM-generated plan wouldn't be.
        """
        config = INTENT_CATEGORIES.get(intent, INTENT_CATEGORIES["unknown"])
        steps: list[ExecutionStep] = []

        student_id = entities.get("student_id")
        class_id = entities.get("class_id")

        if config["agent"] == "data":
            data_intent, params = self._resolve_data_call(intent, entities)
            missing = [f for f in REQUIRED_DATA_PARAMS.get(data_intent, []) if params.get(f) is None]
            if missing:
                steps.append(ExecutionStep(
                    step=1, agent="clarification", action=data_intent,
                    params={"missing_fields": missing},
                    note=f"Required for '{data_intent}' but not found in the message: {', '.join(missing)}",
                ))
            else:
                steps.append(ExecutionStep(step=1, agent="data", action=data_intent, params=params))

        elif config["agent"] == "predictor":
            model_key = entities.get("model") or DEFAULT_RISK_MODEL
            steps.append(ExecutionStep(
                step=1, agent="data", action="get_student_full_profile",
                params={"student_id": student_id},
                note="Fetch the source record the model's features are built from.",
            ))
            steps.append(ExecutionStep(
                step=2, agent="predictor", action=model_key,
                params={"student_id": student_id},
                note="Score the record fetched in step 1.",
            ))

        elif config["agent"] == "retrieval":
            steps.append(ExecutionStep(
                step=1, agent="retrieval", action="ask",
                params={"query": entities.get("query_text") or entities.get("message_text") or user_message},
            ))

        elif config["agent"] == "workflow":
            workflow_name = config["workflow"]
            reference_id = entities.get("reference_student_id")
            # A STU-xxxxx id means "score the synthetic reference dataset", not
            # a real operational student -- route to the matching workflow
            # instead of silently coercing it into an integer student_id.
            if reference_id and workflow_name == "predict_dropout_risk_for_student":
                workflow_name = "predict_dropout_risk_for_reference_student"
            elif reference_id and workflow_name == "at_risk_briefing":
                pass  # at_risk_briefing itself branches on reference_student_id vs student_id
            wf_params = self._resolve_workflow_params(workflow_name, student_id, class_id, entities)

            if workflow_name == "at_risk_briefing":
                missing = [] if (wf_params.get("student_id") or wf_params.get("reference_student_id")) \
                    else ["student_id or reference_student_id"]
            else:
                missing = [f for f in REQUIRED_WORKFLOW_PARAMS.get(workflow_name, []) if wf_params.get(f) is None]

            if missing:
                steps.append(ExecutionStep(
                    step=1, agent="clarification", action=workflow_name,
                    params={"missing_fields": missing},
                    note=f"Required for '{workflow_name}' but not found in the message: {', '.join(missing)}",
                ))
            else:
                steps.append(ExecutionStep(step=1, agent="workflow", action=workflow_name, params=wf_params))

        else:  # unknown intent
            steps.append(ExecutionStep(
                step=1, agent="retrieval", action="ask",
                params={"query": entities.get("query_text") or user_message},
                note="Intent unrecognized -- falling back to a general RAG lookup.",
            ))

        return ExecutionPlan(
            intent=intent, confidence=confidence, reasoning=reasoning,
            entities=entities, steps=steps,
        )

    def _resolve_data_call(self, intent: str, entities: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """Maps a data-flavored intent category + extracted entities onto
        one concrete data_agent intent name and its keyword arguments."""
        student_id = entities.get("student_id")
        class_id = entities.get("class_id")
        teacher_id = entities.get("teacher_id")
        guardian_id = entities.get("guardian_id")

        if intent == "student_lookup":
            return "get_student_full_profile", {"student_id": student_id, "narrate": True}

        if intent == "student_search":
            if entities.get("student_name"):
                return "search_students", {"name_query": entities["student_name"]}
            return "get_class_students", {"class_id": class_id}

        if intent == "attendance":
            if entities.get("date") and class_id is not None:
                return "get_class_attendance_for_date", {"class_id": class_id, "date": entities["date"]}
            return "get_attendance_summary", {
                "student_id": student_id,
                "start_date": entities.get("start_date"),
                "end_date": entities.get("end_date"),
            }

        if intent == "scores":
            if class_id is not None and student_id is None:
                return "get_class_average_scores", {"class_id": class_id, "subject_id": entities.get("subject_id")}
            return "get_score_summary", {"student_id": student_id, "term": entities.get("term")}

        if intent == "fees":
            if class_id is not None and student_id is None:
                return "get_outstanding_fees", {"class_id": class_id}
            return "get_fee_balance", {"student_id": student_id}

        if intent == "timetable":
            if teacher_id is not None:
                return "get_teacher_schedule", {"teacher_id": teacher_id, "day_of_week": entities.get("day_of_week")}
            if class_id is not None and student_id is None:
                return "get_class_timetable", {"class_id": class_id, "day_of_week": entities.get("day_of_week")}
            return "get_student_timetable", {"student_id": student_id, "day_of_week": entities.get("day_of_week")}

        if intent == "guardian_communication":
            if entities.get("message_text"):
                return "send_guardian_message", {
                    "guardian_id": guardian_id,
                    "student_id": student_id,
                    "message_text": entities["message_text"],
                    "subject": entities.get("subject"),
                    "confirm": entities.get("confirm", False),
                }
            return "get_guardian_messages", {"guardian_id": guardian_id}

        if intent == "class_overview":
            return "get_class_overview", {"class_id": class_id, "narrate": True}

        return "get_student", {"student_id": student_id}

    def _resolve_workflow_params(self, workflow_name: str, student_id: Any, class_id: Any,
                                  entities: dict[str, Any]) -> dict[str, Any]:
        builders = {
            "student_report": lambda: {"student_id": student_id},
            "class_report": lambda: {"class_id": class_id},
            "attendance_check_and_notify": lambda: {
                "student_id": student_id,
                "low_attendance_threshold": entities.get("threshold"),
                "confirm": entities.get("confirm", False),
            },
            "at_risk_briefing": lambda: (
                {"reference_student_id": entities.get("reference_student_id"),
                 "model": entities.get("model") or DEFAULT_RISK_MODEL}
                if entities.get("reference_student_id")
                else {"student_id": student_id, "model": entities.get("model") or DEFAULT_RISK_MODEL}
            ),
            "predict_dropout_risk_for_student": lambda: {
                "student_id": student_id,
                "model": entities.get("model") or DEFAULT_RISK_MODEL,
                "narrate": True,
            },
            "predict_dropout_risk_for_reference_student": lambda: {
                "student_id": entities.get("reference_student_id"),
                "model": entities.get("model") or DEFAULT_RISK_MODEL,
                "narrate": True,
            },
        }
        return builders.get(workflow_name, lambda: {})()

    # =================================================================
    # analyze_request: the "understand it" phase in one call
    # =================================================================
    def analyze_request(self, user_message: str) -> ExecutionPlan:
        """Runs intent recognition + entity extraction (both LLM calls),
        then builds the execution plan (rule-based)."""
        intent_result = self.identify_intent(user_message)
        entities = self.extract_entities(user_message)
        return self.create_execution_plan(
            intent=intent_result["intent"],
            entities=entities,
            confidence=intent_result["confidence"],
            reasoning=intent_result["reasoning"],
            user_message=user_message,
        )

    # =================================================================
    # 4. EXECUTING THE PLAN THROUGH THE ORCHESTRATOR
    # =================================================================
    def execute_plan(self, plan: ExecutionPlan) -> dict[str, Any]:
        """Runs each step of the plan through self.orchestrator, in order,
        threading output from earlier steps into later ones where needed.
        Stops at the first failed step rather than cascading bad data
        forward into later steps.

        Every step result comes back in the orchestrator's actual shape:
        {"status": "success"/"error", "agent": ..., "result"/"error": ...}
        -- this method never assumes an "ok"/"data" shape that Orchestrator
        doesn't produce.
        """
        step_results: list[dict[str, Any]] = []
        last_data_result: Any = None

        for step in plan.steps:
            if step.agent == "data":
                task = {"intent": step.action, **_clean(step.params)}
                result = self.orchestrator.execute(task)
                if result.get("status") == "success":
                    last_data_result = result.get("result")

            elif step.agent == "predictor":
                record = last_data_result if isinstance(last_data_result, dict) else None
                if record is None:
                    result = {
                        "status": "error", "agent": "predictor_agent",
                        "error": "No upstream record available to build features from.",
                    }
                else:
                    result = self.orchestrator.execute({"model": step.action, "record": record})

            elif step.agent == "retrieval":
                result = self.orchestrator.execute({"query": step.params.get("query", "")})

            elif step.agent == "workflow":
                result = self.orchestrator.run_workflow(step.action, **_clean(step.params))

            elif step.agent == "clarification":
                missing = step.params.get("missing_fields", [])
                result = {
                    "status": "error", "agent": "planner_agent",
                    "error": f"I couldn't find {', '.join(missing)} in your message for '{step.action}' -- "
                             f"could you specify it and try again?",
                }

            else:
                result = {"status": "error", "agent": "planner_agent", "error": f"Unroutable step: {step}"}

            step_results.append({"step": step.step, "agent": step.agent, "action": step.action, "result": result})

            if result.get("status") != "success":
                break

        overall_ok = bool(step_results) and all(r["result"].get("status") == "success" for r in step_results)
        top_level_error = None
        if not overall_ok:
            top_level_error = (
                step_results[-1]["result"].get("error") if step_results else "No steps were executed"
            )
        return {
            "status": "success" if overall_ok else "error",
            "error": top_level_error,
            "plan": plan.to_dict(),
            "steps": step_results,
            "final_result": step_results[-1]["result"] if step_results else None,
        }

    # =================================================================
    # ONE-CALL ENTRYPOINT: analyze + execute
    # =================================================================
    def run(self, user_message: str) -> dict[str, Any]:
        try:
            plan = self.analyze_request(user_message)
        except PlannerAgentError as e:
            return {"status": "error", "agent": "planner_agent", "error": f"Planning failed: {e}"}
        return self.execute_plan(plan)


if __name__ == "__main__":
    from orchestrator import Orchestrator

    orch = Orchestrator(db_path="school.db")
    planner = PlannerAgent(orchestrator=orch, nvidia_api_key=os.environ.get("NVIDIA_API_KEY"))

    plan = planner.analyze_request("What's student 12's attendance rate this term?")
    print(json.dumps(plan.to_dict(), indent=2, default=str))

    print()
    print(json.dumps(planner.run("Give me a full risk briefing on student 12"), indent=2, default=str))