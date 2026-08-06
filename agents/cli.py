"""
cli.py

Terminal front end for the whole system: connects PlannerAgent to
Orchestrator (which in turn owns data_agent, prediction_agent,
retrieval_agent, and narration_agent) and reads free-text queries from
the user, one line at a time.

    user input (terminal)
        -> PlannerAgent.run(query)
             -> identify_intent()          [LLM]
             -> extract_entities()         [LLM]
             -> create_execution_plan()    [rule-based]
             -> execute_plan()             [calls Orchestrator]
                   -> data_agent / prediction_agent / retrieval_agent / narration_agent
        -> printed back to the terminal (narrated prose if the plan
           produced one, structured JSON otherwise)

Run from the project root (wherever school.db, model/, and
school_knowledge_base.txt live):

    python cli.py

Requires GEMINI_API_KEY (or GOOGLE_API_KEY) to be set -- the planner,
retrieval agent, and narration agent all need it. A missing/invalid key
will surface as a normal per-turn error instead of crashing the REPL.

Debug escape hatch: prefix a line with ":raw " followed by a JSON task
dict to call the orchestrator directly, bypassing the planner's LLM
calls entirely -- useful for testing one agent in isolation without
spending an intent-classification + entity-extraction call first.

    :raw {"intent": "get_student", "student_id": 1}
    :raw {"model": "student_risk_engine", "record": {...}}
    :raw {"query": "What is the attendance policy?"}
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "agents"))
sys.path.insert(0, str(ROOT / "database"))

from orchestrator import Orchestrator      # noqa: E402
from planner_agent import PlannerAgent      # noqa: E402

DB_PATH = str(ROOT / "school.db")
KNOWLEDGE_BASE_PATH = str(ROOT / "school_knowledge_base.txt")

HELP_TEXT = """
Commands:
  :help               show this message
  :capabilities        show what the orchestrator can currently do
  :raw <json task>     call the orchestrator directly, skipping the planner
                        e.g. :raw {"intent": "get_student", "student_id": 1}
  :quit / :exit         leave

Anything else is treated as a natural-language query for the planner,
e.g.:
  What is student 1's attendance rate?
  Give me a full risk briefing on student 3
  What is the school's fee payment policy?
"""


def print_json(data) -> None:
    print(json.dumps(data, indent=2, default=str))


def render_result(response: dict) -> None:
    """Planner responses are {"status", "plan", "steps", "final_result"}.
    If the final step produced a narrative, show that front and center;
    otherwise fall back to the raw structured result so nothing important
    gets hidden."""
    if response.get("status") != "success":
        error_message = response.get("error")
        if not error_message and response.get("steps"):
            error_message = response["steps"][-1].get("result", {}).get("error")
        print(f"\n[error] {error_message or 'Unknown error'}")
        if "steps" in response:
            print("\nSteps attempted:")
            print_json(response["steps"])
        return

    final = response.get("final_result", {}) or {}
    result_data = final.get("result", {}) or {}

    narrative = _find_narrative(result_data)
    if narrative:
        print(f"\n{narrative}")
    else:
        print("\n[no narration available for this result -- showing raw data]")
        print_json(result_data)

    print(f"\n(intent: {response['plan']['intent']}, confidence: {response['plan']['confidence']:.2f})")


def _find_narrative(data) -> str | None:
    """Narratives can show up at different nesting depths depending on
    which workflow ran (a plain prediction vs. a briefing that wraps a
    prediction). Check the common spots rather than assuming one shape."""
    if not isinstance(data, dict):
        return None
    if "narrative" in data:
        return data["narrative"]
    if isinstance(data.get("prediction"), dict) and "narrative" in data["prediction"]:
        return data["prediction"]["narrative"]
    risk = data.get("risk_assessment")
    if isinstance(risk, dict):
        return _find_narrative(risk)
    return None


def main() -> None:
    print("Connecting to the school system...")
    try:
        orch = Orchestrator(db_path=DB_PATH, knowledge_base_path=KNOWLEDGE_BASE_PATH)
        planner = PlannerAgent(orchestrator=orch)
    except Exception as exc:
        print(f"Failed to start up: {exc}")
        sys.exit(1)

    print("Ready. Type :help for commands, :quit to leave.\n")

    while True:
        try:
            user_input = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye.")
            break

        if not user_input:
            continue

        if user_input in (":quit", ":exit"):
            print("Goodbye.")
            break

        if user_input == ":help":
            print(HELP_TEXT)
            continue

        if user_input == ":capabilities":
            print_json(orch.capabilities())
            continue

        if user_input.startswith(":raw "):
            raw_json = user_input[len(":raw "):].strip()
            try:
                task = json.loads(raw_json)
            except json.JSONDecodeError as e:
                print(f"[error] Could not parse task JSON: {e}")
                continue
            try:
                print_json(orch.execute(task))
            except Exception as exc:
                print(f"[error] {exc}")
            continue

        # Normal path: hand the free-text query to the planner.
        try:
            response = planner.run(user_input)
            render_result(response)
        except Exception as exc:
            # Network errors, invalid API keys, etc. land here -- keep the
            # REPL alive instead of crashing on a single bad turn.
            print(f"\n[error] {exc}")

        print()


if __name__ == "__main__":
    main()