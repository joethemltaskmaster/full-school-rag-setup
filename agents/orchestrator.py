"""
orchestrator.py

The execution driver for the whole platform. Holds all three agents and
decides which one answers a given task, in what order (for multi-step
workflows), and how their differing native responses get normalized
into one predictable shape.

    orchestrator.py
        --> data_agent.py       (SchoolAgent, via DataAgentAdapter)  --> db_service.py --> school.db
        --> prediction_agent.py (PredictorAgentHandler)              --> trained model artifacts
        --> retrieval_agent.py  (RetrievalAgentHandler)              --> FAISS + Gemini

Why an adapter for data_agent specifically:
    data_agent.SchoolAgent.handle() uses the signature
    handle(intent: str, **params) -> {"ok": bool, "data"/"error": ...}
    prediction_agent.PredictorAgentHandler and retrieval_agent.RetrievalAgentHandler
    both use handle(task: dict) -> {"status": "success"/"error", "agent": ..., "result"/"error": ...}
    DataAgentAdapter below wraps SchoolAgent so all three agents can be
    called and normalized the same way, without modifying data_agent.py.

Shared response shape from every agent (after normalization):
    {"status": "success", "agent": <name>, "result": <data>}
    {"status": "error",   "agent": <name>, "error": <message>, ...}

Task-routing convention used by execute():
    {"intent": "get_student", "student_id": 1}                        -> data agent
    {"model": "student_risk_engine", "record": {...}}                 -> prediction agent
    {"query": "What are the fee payment policies?"}                   -> retrieval agent

Usage:
    from orchestrator import Orchestrator

    orch = Orchestrator(db_path="school.db")

    orch.execute({"intent": "get_student", "student_id": 1})
    orch.execute({"model": "student_risk_engine", "record": {...}})
    orch.execute({"query": "What are the fee payment policies?"})

    orch.run_workflow("student_report", student_id=1)
"""

from typing import Any

from data_agent import SchoolAgent
from prediction_agent import PredictorAgentHandler, MODEL_REGISTRY
from retrieval_agent import RetrievalAgentHandler, OrchestratorAgentInterface


# =====================================================================
# Adapter: makes SchoolAgent speak the same task-dict contract as the
# other two agents, without touching data_agent.py itself.
# =====================================================================
class DataAgentAdapter(OrchestratorAgentInterface):
    """
    Expected task shape:
        {"intent": "get_student", "student_id": 1}
        {"intent": "record_payment", "student_id": 1, ..., "confirm": True}
    """

    def __init__(self, db_path: str = "school.db"):
        self.agent = SchoolAgent(db_path)

    def handle(self, task: dict[str, Any]) -> dict[str, Any]:
        intent = task.get("intent")
        if not intent:
            return {"status": "error", "agent": "data_agent", "error": "Task must include 'intent'."}

        params = {k: v for k, v in task.items() if k != "intent"}
        raw = self.agent.handle(intent, **params)

        if raw.get("ok"):
            return {"status": "success", "agent": "data_agent", "result": raw["data"]}
        return {
            "status": "error",
            "agent": "data_agent",
            "error": raw.get("error"),
            **({"available_intents": raw["available_intents"]} if "available_intents" in raw else {}),
        }

    def list_intents(self) -> dict[str, list[str]]:
        return self.agent.list_intents()


class Orchestrator:
    def __init__(
        self,
        db_path: str = "school.db",
        knowledge_base_path: str = "school_knowledge_base.txt",
        predictor_registry: dict[str, dict[str, Any]] = None,
    ):
        self.data_agent = DataAgentAdapter(db_path)
        self.prediction_agent = PredictorAgentHandler(predictor_registry or MODEL_REGISTRY)
        self.retrieval_agent = RetrievalAgentHandler(knowledge_base_path)

    # =================================================================
    # SINGLE-STEP EXECUTION — routes a task dict to the right agent
    # =================================================================
    def execute(self, task: dict[str, Any]) -> dict[str, Any]:
        """
        Routes a task to whichever agent owns it, based on which key is
        present ("intent", "model", or "query"), and returns that
        agent's response as-is — all three already share the same
        {"status", "agent", "result"/"error"} shape.
        """
        if "intent" in task:
            return self.data_agent.handle(task)
        if "model" in task:
            return self.prediction_agent.handle(task)
        if "query" in task:
            return self.retrieval_agent.handle(task)

        return {
            "status": "error",
            "agent": "orchestrator",
            "error": "Could not determine target agent — include 'intent', 'model', or 'query' in the task.",
            "capabilities": self.capabilities(),
        }

    def capabilities(self) -> dict[str, Any]:
        """What can this orchestrator ask its three agents to do?"""
        return {
            "data_agent": self.data_agent.list_intents(),
            "prediction_agent": {
                "registered_models": list(self.prediction_agent.registry.keys()),
                "task_shape": {"model": "<model_key>", "record": {"...": "..."}},
            },
            "retrieval_agent": {
                "description": "Ask natural-language questions against the school knowledge base.",
                "task_shape": {"query": "<question>"},
            },
        }

    # -- convenience wrapper for internal workflow use --
    def _data(self, intent: str, **params) -> dict[str, Any]:
        return self.execute({"intent": intent, **params})

    # =================================================================
    # MULTI-STEP WORKFLOWS
    # The orchestrator composes several agent calls — the agents
    # themselves have no notion of a "workflow."
    # =================================================================
    def run_workflow(self, workflow_name: str, **params) -> dict[str, Any]:
        workflows = {
            "student_report": self._workflow_student_report,
            "class_report": self._workflow_class_report,
            "attendance_check_and_notify": self._workflow_attendance_check_and_notify,
            "ask_knowledge_base": self._workflow_ask_knowledge_base,
            "predict_dropout_risk": self._workflow_predict_dropout_risk,
        }
        fn = workflows.get(workflow_name)
        if fn is None:
            return {
                "status": "error",
                "agent": "orchestrator",
                "error": f"Unknown workflow '{workflow_name}'",
                "available_workflows": sorted(workflows.keys()),
            }
        return fn(**params)

    def _workflow_student_report(self, student_id: int) -> dict[str, Any]:
        """Pulls together a full picture of one student from several data-agent intents."""
        student = self._data("get_student", student_id=student_id)
        if student["status"] != "success" or not student["result"]:
            return {"status": "error", "agent": "orchestrator", "error": f"No student with id {student_id}"}

        attendance = self._data("get_attendance_summary", student_id=student_id)
        scores = self._data("get_score_summary", student_id=student_id)
        fees = self._data("get_fee_balance", student_id=student_id)
        timetable = self._data("get_student_timetable", student_id=student_id)

        return {
            "status": "success",
            "agent": "orchestrator",
            "result": {
                "student": student["result"],
                "attendance_summary": attendance.get("result"),
                "score_summary": scores.get("result"),
                "fee_balance": fees.get("result"),
                "timetable": timetable.get("result"),
            },
        }

    def _workflow_class_report(self, class_id: int) -> dict[str, Any]:
        class_info = self._data("get_class", class_id=class_id)
        if class_info["status"] != "success" or not class_info["result"]:
            return {"status": "error", "agent": "orchestrator", "error": f"No class with id {class_id}"}

        students = self._data("get_class_students", class_id=class_id)
        averages = self._data("get_class_average_scores", class_id=class_id)
        outstanding = self._data("get_outstanding_fees", class_id=class_id)
        timetable = self._data("get_class_timetable", class_id=class_id)

        return {
            "status": "success",
            "agent": "orchestrator",
            "result": {
                "class": class_info["result"],
                "students": students.get("result"),
                "average_scores": averages.get("result"),
                "outstanding_fees": outstanding.get("result"),
                "timetable": timetable.get("result"),
            },
        }

    def _workflow_attendance_check_and_notify(
        self, student_id: int, low_attendance_threshold: float = 75.0, confirm: bool = False
    ) -> dict[str, Any]:
        """Checks attendance, and only sends a guardian message if confirm=True and it's actually low."""
        summary = self._data("get_attendance_summary", student_id=student_id)
        if summary["status"] != "success":
            return summary

        rate = (summary["result"] or {}).get("attendance_rate")
        if rate is None:
            return {"status": "success", "agent": "orchestrator",
                     "result": {"message": "No attendance records yet", "summary": summary["result"]}}

        if rate >= low_attendance_threshold:
            return {"status": "success", "agent": "orchestrator",
                     "result": {"message": "Attendance is healthy", "summary": summary["result"]}}

        student = self._data("get_student", student_id=student_id)
        guardian_id = (student.get("result") or {}).get("guardian_id") if student["status"] == "success" else None

        if not guardian_id:
            return {"status": "success", "agent": "orchestrator", "result": {
                "message": f"Attendance rate {rate}% is below threshold, but no guardian on file to notify",
                "summary": summary["result"],
            }}

        if not confirm:
            return {"status": "error", "agent": "orchestrator", "error": (
                f"Attendance rate {rate}% is below {low_attendance_threshold}%. "
                f"Re-run with confirm=True to send a guardian notification."
            )}

        notification = self._data(
            "send_guardian_message",
            guardian_id=guardian_id,
            student_id=student_id,
            subject="Attendance notice",
            message_text=(
                f"Your ward's attendance rate is currently {rate}%, "
                f"below the school's {low_attendance_threshold}% threshold."
            ),
            confirm=True,
        )
        return {"status": "success", "agent": "orchestrator",
                 "result": {"summary": summary["result"], "notification": notification}}

    def _workflow_ask_knowledge_base(self, query: str) -> dict[str, Any]:
        """Thin pass-through to the retrieval agent, kept as a workflow for symmetry with the others."""
        return self.execute({"query": query})

    def _workflow_predict_dropout_risk(self, model: str, record: dict[str, Any]) -> dict[str, Any]:
        """
        Thin pass-through to the prediction agent. Note: the record shape
        must match the feature schema registered for `model` in
        prediction_agent.MODEL_REGISTRY — it does NOT come from
        db_service.py automatically. Mapping SchoolDB fields onto a given
        model's expected feature schema is a deliberate step you'd add
        here once the two schemas are aligned.
        """
        return self.execute({"model": model, "record": record})


if __name__ == "__main__":
    import json

    orch = Orchestrator(db_path="school.db")
    print("Capabilities:")
    print(json.dumps(orch.capabilities(), indent=2))

    print("\nData agent -> get_student:")
    print(json.dumps(orch.execute({"intent": "get_student", "student_id": 1}), indent=2))

    print("\nWorkflow -> student_report:")
    print(json.dumps(orch.run_workflow("student_report", student_id=1), indent=2))
