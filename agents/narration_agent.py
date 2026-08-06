"""
narration_agent.py
--------------------
Turns structured results into natural-language explanations.

This is deliberately NOT retrieval-augmented generation. The JSON handed
in already contains the complete, correct answer -- there's nothing to
search for. This module just hands that JSON to the LLM in a prompt and
asks it to narrate it in plain English, so the numbers can't drift the
way they could if the LLM were reconstructing them from a retrieved,
re-embedded text chunk.

Two narration paths, because "explain a risk prediction" and "summarize
a multi-section student profile / class overview" want different
prompts:

    prediction JSON --[narrate_prediction, one NVIDIA LLM call]--> prose
    data JSON        --[narrate_data,       one NVIDIA LLM call]--> prose

Matches the same OrchestratorAgentInterface/handle(task) convention as
the other agents, so the orchestrator can call it the same way:

    handler = NarrationAgentHandler()
    handler.handle({"prediction": prediction_dict})
    # -> {"status": "success"/"error", "agent": "narration_agent", "result": {"narrative": "..."}}

    handler.handle({"data": profile_dict, "context": "student profile"})
    # -> {"status": "success"/"error", "agent": "narration_agent", "result": {"narrative": "..."}}

Environment:
    NVIDIA_API_KEY must be set.
    Required package: langchain-nvidia-ai-endpoints
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from dotenv import load_dotenv

# No hardcoded path: python-dotenv's default load_dotenv() walks up from the
# current working directory looking for a .env file, which works the same
# on any machine/deployment target instead of only on one person's desktop.
load_dotenv(dotenv_path=r"C:\Users\Joseph\Desktop\Database\agents\.env")

logger = logging.getLogger("narration_agent")
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")


class OrchestratorAgentInterface:
    """Same minimal contract every agent exposes to the orchestrator."""

    def handle(self, task: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError


PREDICTION_NARRATION_SYSTEM_PROMPT = """You explain student risk predictions from a school \
management system to school staff in plain, natural English.

You will be given a JSON object containing a model's prediction. Write a short \
paragraph (3-5 sentences) that:
- States the predicted risk level plainly, in your own words
- Explains what's driving that prediction, referencing the top contributing \
features by name and their actual values (never invent a number that isn't \
in the JSON)
- Uses a calm, factual tone -- this is read by teachers and administrators, \
not the student or their family
- Does NOT recommend specific interventions unless the JSON explicitly \
contains guidance to draw from -- if intervention guidance is separately \
available, note that it should be added, don't invent your own

Never restate the raw JSON. Never invent values not present in the input."""


DATA_NARRATION_SYSTEM_PROMPT = """You summarize structured records from a school \
management system for school staff in plain, natural English.

You will be given a JSON object -- this could be a full student profile, a \
class-wide overview, or another multi-section record. Write a short summary \
(3-6 sentences, or a couple of short paragraphs for a class overview covering \
many students) that:
- Leads with the most important, actionable facts (e.g. attendance issues, \
overdue fees, flagged grades) rather than walking through every field in the \
JSON's own order
- Uses actual names, numbers, and dates exactly as given -- never invent or \
round a value that isn't in the JSON
- Groups related facts together instead of listing fields one by one, so it \
reads like something a colleague would say out loud, not a transcription of \
the record
- Uses a calm, factual tone -- this is read by teachers and administrators
- Does NOT recommend interventions or draw conclusions the data doesn't \
support

Never restate the raw JSON. Never invent values not present in the input."""


class NarrationAgent:
    """
    Lazily-initialized NVIDIA-hosted LLM client, used only to turn
    structured data into prose. No vector store, no document loading, no
    retrieval -- this is the leanest possible wrapper around a single LLM
    call.
    """

    def __init__(
        self,
        nvidia_model_name: str = "nvidia/nemotron-3-ultra-550b-a55b",
        temperature: float = 1.0,
        request_timeout: int = 120,
    ):
        self.nvidia_model_name = nvidia_model_name
        self.temperature = temperature
        self.request_timeout = request_timeout
        self._llm = None

    def _ensure_ready(self) -> None:
        if self._llm is not None:
            return

        api_key = os.environ.get("NVIDIA_API_KEY")
        if not api_key:
            raise RuntimeError(
                "NVIDIA_API_KEY is not set. Export it in your environment "
                "before using the narration agent."
            )

        try:
            from langchain_nvidia_ai_endpoints import ChatNVIDIA
        except ImportError as exc:
            raise RuntimeError(
                "Missing dependency for the narration agent. Install with: "
                "pip install langchain-nvidia-ai-endpoints"
            ) from exc

        # Matches the target endpoint's constructor: model, temperature,
        # top_p, max_tokens, seed -- plus an explicit timeout, since the
        # library default was what produced 60s read-timeout errors.
        self._llm = ChatNVIDIA(
            model=self.nvidia_model_name,
            temperature=self.temperature,
            api_key=api_key,
            top_p=1,
            max_tokens=16384,
            seed=42,
            timeout=self.request_timeout,
        )

    def _stream_join(self, system_prompt: str, user_message: str) -> str:
        """Shared plumbing for both narration flavors. Streams the response
        internally and joins the chunks instead of a single blocking
        .invoke() call -- same fix as retrieval_agent.py and
        planner_agent.py: a blocking call waits for the ENTIRE response (up
        to 16384 tokens) before anything comes back, which is what produced
        the read-timeout errors. The public return type is unchanged (one
        complete string); only the HTTP behavior underneath is different."""
        self._ensure_ready()
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]
        chunks = [chunk.content for chunk in self._llm.stream(messages) if chunk.content]
        return "".join(chunks)

    def _stream_yield(self, system_prompt: str, user_message: str):
        self._ensure_ready()
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]
        for chunk in self._llm.stream(messages):
            if chunk.content:
                yield chunk.content

    # -- prediction narration ------------------------------------------------
    def narrate_prediction(self, prediction: dict[str, Any]) -> str:
        """
        prediction is the structured dict a prediction_agent call returns
        (student_id, prediction, confidence, class_probabilities,
        top_contributing_features, ...). Returns a plain-English paragraph.
        """
        user_message = (
            "Explain this prediction:\n\n" + json.dumps(prediction, indent=2, default=str)
        )
        return self._stream_join(PREDICTION_NARRATION_SYSTEM_PROMPT, user_message)

    def stream_narrate_prediction(self, prediction: dict[str, Any]):
        """Generator variant for a terminal/chat UI that wants to print the
        narration as it's generated instead of waiting for the full
        paragraph -- matches the target endpoint's
        `for chunk in client.stream(...): print(chunk.content, end="")` pattern
        directly."""
        user_message = (
            "Explain this prediction:\n\n" + json.dumps(prediction, indent=2, default=str)
        )
        yield from self._stream_yield(PREDICTION_NARRATION_SYSTEM_PROMPT, user_message)

    # -- general-purpose data narration --------------------------------------
    def narrate_data(self, data: dict[str, Any], context: str | None = None) -> str:
        """
        General-purpose counterpart to narrate_prediction(): summarizes any
        structured, multi-section record (a full student profile, a class
        overview, a risk briefing that stitches several sub-results
        together). `context` is an optional short label -- e.g. "student
        profile for Kelechi Ojo" or "class overview for Grade 10B" -- that
        gets folded into the prompt so the model knows what kind of record
        it's looking at instead of guessing from field names alone.
        """
        label = f" ({context})" if context else ""
        user_message = (
            f"Summarize this record{label}:\n\n" + json.dumps(data, indent=2, default=str)
        )
        return self._stream_join(DATA_NARRATION_SYSTEM_PROMPT, user_message)

    def stream_narrate_data(self, data: dict[str, Any], context: str | None = None):
        label = f" ({context})" if context else ""
        user_message = (
            f"Summarize this record{label}:\n\n" + json.dumps(data, indent=2, default=str)
        )
        yield from self._stream_yield(DATA_NARRATION_SYSTEM_PROMPT, user_message)


class NarrationAgentHandler(OrchestratorAgentInterface):
    """
    What the orchestrator instantiates and calls whenever a structured
    result needs to become prose.

    Routes on task shape, since "narrate a prediction" and "narrate a
    data record" use different prompts:

        {"prediction": <prediction dict from a prediction_agent call>}
            -> narrate_prediction path

        {"data": <any structured dict, e.g. a profile or class overview>,
         "context": <optional short label>}
            -> narrate_data path

    Exactly one of "prediction" / "data" is expected per call; if both or
    neither are present, that's a caller bug and comes back as a
    top-level "status": "error" instead of guessing which one was meant.
    """

    def __init__(self, **kwargs):
        self.agent = NarrationAgent(**kwargs)

    def handle(self, task: dict[str, Any]) -> dict[str, Any]:
        has_prediction = "prediction" in task and task.get("prediction")
        has_data = "data" in task and task.get("data")

        if has_prediction and has_data:
            return {
                "status": "error",
                "agent": "narration_agent",
                "error": "Task must include exactly one of 'prediction' or 'data', not both.",
            }

        try:
            if has_prediction:
                narrative = self.agent.narrate_prediction(task["prediction"])
            elif has_data:
                narrative = self.agent.narrate_data(task["data"], context=task.get("context"))
            else:
                return {
                    "status": "error",
                    "agent": "narration_agent",
                    "error": "Task must include 'prediction' or 'data'.",
                }
            return {"status": "success", "agent": "narration_agent", "result": {"narrative": narrative}}
        except Exception as exc:
            logger.exception("NarrationAgent failed")
            return {"status": "error", "agent": "narration_agent", "error": str(exc)}


if __name__ == "__main__":
    sample_prediction = {
        "student_id": "STU-00001",
        "prediction": "HIGH",
        "confidence": 1.0,
        "class_probabilities": {"LOW": 0.0, "MEDIUM": 0.0, "HIGH": 1.0},
        "top_contributing_features": [
            {"feature": "attendance_rate", "value": 42.1, "importance": 0.31},
            {"feature": "fee_payment_days_late", "value": 65.5, "importance": 0.24},
        ],
    }

    sample_profile = {
        "student_id": "STU-00001",
        "name": "Kelechi Ojo",
        "grade": "10B",
        "attendance": {"rate": 0.61, "days_absent_this_term": 19},
        "fees": {"balance_due": 45000, "days_late": 65},
        "grades": {"average": 58.4, "trend": "declining"},
    }

    # Live-streaming demo, matching the target endpoint's print pattern directly:
    agent = NarrationAgent()
    print("Streaming (prediction): ", end="")
    for token in agent.stream_narrate_prediction(sample_prediction):
        print(token, end="")
    print()

    print("Streaming (data): ", end="")
    for token in agent.stream_narrate_data(sample_profile, context="student profile"):
        print(token, end="")
    print()