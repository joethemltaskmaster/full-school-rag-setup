"""
prediction_agent.py
--------------------
Predictor Agent for the multi-model school management ML platform.

This module is a self-contained agent: it has no API/network dependency
of its own. It is *called by* the orchestrator whenever a prediction task
comes in, does its work locally against already-trained artifacts
(model.joblib / label_encoder.joblib / feature_schema.json, produced by
training.py / train_pipeline.py), and always hands back a single
JSON-safe dict. Nothing here fits models -- that stays in the training
scripts, keeping training and serving cleanly decoupled.

Pipeline per request:
    raw student record
        -> FeatureBuilder          raw dict/row -> model-ready feature frame
        -> ModelLoader             loads + caches the correct trained pipeline
        -> Predictor               pipeline.predict / predict_proba
        -> ExplanationGenerator    structures the "why", tied to student_id
        -> PredictorAgent.run()    returns one JSON-safe dict

MODEL_REGISTRY below is wired to the Student Dropout Risk Engine artifacts.
Adding a second entry (e.g. "fee_default_predictor") with its own paths and
feature lists lets the same four classes serve any of the other models in
the 107-table schema without touching the class bodies.

Orchestrator contract:
    handler = PredictorAgentHandler()
    result = handler.handle({"model": "student_risk_engine", "record": {...}})
    # result is always a JSON-serializable dict with a "status" key.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd

logger = logging.getLogger("predictor_agent")
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")


# --------------------------------------------------------------------------- #
# Registry: one entry per trained model this agent can serve.
# To plug in another model (e.g. Fee Default Predictor), add an entry here --
# no changes needed to FeatureBuilder / ModelLoader / Predictor / ExplanationGenerator.
# --------------------------------------------------------------------------- #

MODEL_REGISTRY: dict[str, dict[str, Any]] = {
    "student_risk_engine": {
        "model_path": Path("model/model.joblib"),
        "label_encoder_path": Path("model/label_encoder.joblib"),
        "feature_schema_path": Path("model/feature_schema.json"),
        "numerical_features": [
            "fee_payment_days_late",
            "attendance_rate",
            "consecutive_absences",
            "score_avg",
            "subject_failure_count",
            "paid_before_resumption",
        ],
        "ordinal_features": ["school_type"],
        "ordinal_categories": {
            "school_type": ["elite_private", "public_state", "low_cost_private"],
        },
        "id_columns": ["student_id", "tenant_id", "academic_year", "term"],
    },
    # "fee_default_predictor": { ... same shape, different paths/features ... },
}


# --------------------------------------------------------------------------- #
# 1. Feature Builder
# --------------------------------------------------------------------------- #

class FeatureBuilder:
    """Converts a raw student record (dict or pandas Series) into the exact
    feature frame the trained pipeline expects: correct columns, correct
    order, defensive handling of missing fields so one incomplete record
    never crashes inference for the whole batch.

    Identity columns (student_id, tenant_id, ...) are pulled off and
    returned separately -- they're never fed into the model, but they
    travel alongside the prediction so results can be re-attached to the
    right student downstream.
    """

    def __init__(self, model_key: str, registry: dict[str, dict[str, Any]] = MODEL_REGISTRY):
        if model_key not in registry:
            raise KeyError(f"No feature config registered for model '{model_key}'")
        self.model_key = model_key
        self.config = registry[model_key]
        self.numerical_features: list[str] = list(self.config["numerical_features"])
        self.ordinal_features: list[str] = list(self.config["ordinal_features"])
        self.id_columns: list[str] = list(self.config.get("id_columns", []))

    def build(self, raw_record: dict[str, Any] | pd.Series) -> tuple[pd.DataFrame, dict[str, Any]]:
        """Returns (feature_frame, identity)."""
        record = dict(raw_record)

        identity = {col: record.get(col) for col in self.id_columns if col in record}

        row: dict[str, Any] = {}
        missing_fields: list[str] = []

        for col in self.numerical_features:
            value = record.get(col)
            row[col] = value if value is not None else np.nan
            if value is None:
                missing_fields.append(col)

        for col in self.ordinal_features:
            value = record.get(col)
            row[col] = value if value is not None else None
            if value is None:
                missing_fields.append(col)

        if missing_fields:
            logger.warning(
                "Missing fields filled with NaN/None for student_id=%s: %s",
                identity.get("student_id", "<unknown>"), missing_fields,
            )

        ordered_columns = self.numerical_features + self.ordinal_features
        feature_frame = pd.DataFrame([row], columns=ordered_columns)
        return feature_frame, identity


# --------------------------------------------------------------------------- #
# 2. Model Loader
# --------------------------------------------------------------------------- #

class ModelLoader:
    """Loads and in-process caches the trained pipeline + label encoder
    (+ feature schema, if present) for a given model key. Disk I/O only --
    no external calls. Cached per model_key so repeated predictions across
    a batch, or repeated dispatches from the orchestrator, don't re-read
    joblib files from disk each time.
    """

    _cache: dict[str, dict[str, Any]] = {}

    def __init__(self, model_key: str, registry: dict[str, dict[str, Any]] = MODEL_REGISTRY):
        if model_key not in registry:
            raise KeyError(f"No model registered under '{model_key}'")
        self.model_key = model_key
        self.config = registry[model_key]

    def load(self) -> dict[str, Any]:
        if self.model_key in ModelLoader._cache:
            return ModelLoader._cache[self.model_key]

        model_path = Path(self.config["model_path"])
        encoder_path = Path(self.config["label_encoder_path"])
        schema_path = Path(self.config["feature_schema_path"]) if self.config.get("feature_schema_path") else None

        if not model_path.exists():
            raise FileNotFoundError(f"Model artifact not found: {model_path}")
        if not encoder_path.exists():
            raise FileNotFoundError(f"Label encoder not found: {encoder_path}")

        pipeline = joblib.load(model_path)
        label_encoder = joblib.load(encoder_path)

        schema = None
        if schema_path and schema_path.exists():
            with open(schema_path) as f:
                schema = json.load(f)

        bundle = {
            "pipeline": pipeline,
            "label_encoder": label_encoder,
            "schema": schema,
            "class_names": list(label_encoder.classes_),
        }
        ModelLoader._cache[self.model_key] = bundle
        logger.info("Loaded and cached model bundle '%s'", self.model_key)
        return bundle

    @classmethod
    def clear_cache(cls, model_key: str | None = None) -> None:
        """Useful after retraining, so the agent picks up a fresh artifact
        instead of serving a stale cached pipeline."""
        if model_key is None:
            cls._cache.clear()
        else:
            cls._cache.pop(model_key, None)


# --------------------------------------------------------------------------- #
# 3. Predictor
# --------------------------------------------------------------------------- #

@dataclass
class PredictionResult:
    predicted_label: str
    predicted_class_index: int
    class_probabilities: dict[str, float]
    confidence: float


class Predictor:
    """Pure inference: runs the trained pipeline against a prepared feature
    frame. No feature engineering and no explanation logic here -- those
    are the other two components' jobs."""

    def __init__(self, model_bundle: dict[str, Any]):
        self.pipeline = model_bundle["pipeline"]
        self.label_encoder = model_bundle["label_encoder"]
        self.class_names: list[str] = model_bundle["class_names"]

    def predict(self, feature_frame: pd.DataFrame) -> PredictionResult:
        pred_encoded = self.pipeline.predict(feature_frame)[0]
        predicted_label = self.label_encoder.inverse_transform([pred_encoded])[0]

        probabilities: dict[str, float] = {}
        confidence = 1.0
        if hasattr(self.pipeline, "predict_proba"):
            proba = self.pipeline.predict_proba(feature_frame)[0]
            probabilities = {cls: float(p) for cls, p in zip(self.class_names, proba)}
            confidence = float(proba[int(pred_encoded)])

        return PredictionResult(
            predicted_label=str(predicted_label),
            predicted_class_index=int(pred_encoded),
            class_probabilities=probabilities,
            confidence=confidence,
        )


# --------------------------------------------------------------------------- #
# 4. Explanation Generator
# --------------------------------------------------------------------------- #

class ExplanationGenerator:
    """Builds a structured, student-attributable explanation for a single
    prediction: the label, confidence, class probabilities, and the top
    contributing features (with direction where the model supports it),
    all keyed to the student's identity so it can be logged, displayed on
    a dashboard, or written back to the student's record.

    Supports linear models (coef_) and tree-based models
    (feature_importances_); falls back to a schema-only view for any other
    estimator type so the agent never hard-fails on an unsupported model.
    """

    def __init__(self, feature_names: list[str], top_n: int = 5):
        self.feature_names = feature_names
        self.top_n = top_n

    def explain(
        self,
        model_bundle: dict[str, Any],
        feature_frame: pd.DataFrame,
        prediction: PredictionResult,
        identity: dict[str, Any],
    ) -> dict[str, Any]:
        pipeline = model_bundle["pipeline"]
        estimator = pipeline.named_steps.get("model") if hasattr(pipeline, "named_steps") else pipeline

        return {
            "student_id": identity.get("student_id"),
            "tenant_id": identity.get("tenant_id"),
            "academic_year": identity.get("academic_year"),
            "term": identity.get("term"),
            "prediction": prediction.predicted_label,
            "confidence": round(prediction.confidence, 4),
            "class_probabilities": {
                k: round(v, 4) for k, v in prediction.class_probabilities.items()
            },
            "top_contributing_features": self._rank_features(estimator, feature_frame),
            "input_snapshot": {k: _json_safe(v) for k, v in feature_frame.iloc[0].to_dict().items()},
        }

    def _rank_features(self, estimator: Any, feature_frame: pd.DataFrame) -> list[dict[str, Any]]:
        values = feature_frame.iloc[0]

        if hasattr(estimator, "feature_importances_"):
            importances = estimator.feature_importances_
            ranked = sorted(
                zip(self.feature_names, importances), key=lambda kv: abs(kv[1]), reverse=True
            )[: self.top_n]
            return [
                {"feature": name, "importance": round(float(imp), 4), "value": _json_safe(values.get(name))}
                for name, imp in ranked
            ]

        if hasattr(estimator, "coef_"):
            coefs = np.ravel(estimator.coef_[0]) if np.ndim(estimator.coef_) > 1 else np.ravel(estimator.coef_)
            ranked = sorted(
                zip(self.feature_names, coefs), key=lambda kv: abs(kv[1]), reverse=True
            )[: self.top_n]
            return [
                {"feature": name, "weight": round(float(w), 4), "value": _json_safe(values.get(name))}
                for name, w in ranked
            ]

        # Estimator has no interpretable weights -- return a schema-only view.
        return [{"feature": name, "value": _json_safe(values.get(name))} for name in self.feature_names[: self.top_n]]


def _json_safe(value: Any) -> Any:
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return None if np.isnan(value) else float(value)
    if isinstance(value, float) and pd.isna(value):
        return None
    return value


# --------------------------------------------------------------------------- #
# Predictor Agent -- orchestrates the four components above
# --------------------------------------------------------------------------- #

class PredictorAgent:
    """The agent itself. One record in, one JSON-safe dict out:
    receive data -> prepare features -> load model -> run prediction -> explain.

    Instantiate once per model_key; the orchestrator's handler (below)
    manages a small pool of these so each registered model gets its own
    agent instance with its own cached artifacts.
    """

    def __init__(self, model_key: str, registry: dict[str, dict[str, Any]] = MODEL_REGISTRY):
        self.model_key = model_key
        self.feature_builder = FeatureBuilder(model_key, registry)
        self.model_loader = ModelLoader(model_key, registry)
        feature_names = self.feature_builder.numerical_features + self.feature_builder.ordinal_features
        self.explanation_generator = ExplanationGenerator(feature_names)

    def run(self, raw_record: dict[str, Any] | pd.Series) -> dict[str, Any]:
        """Never raises for ordinary failure modes (missing artifact, bad
        input) -- always returns a structured dict with a 'status' field so
        the orchestrator can route successes and failures the same way."""
        try:
            feature_frame, identity = self.feature_builder.build(raw_record)
            model_bundle = self.model_loader.load()
            prediction = Predictor(model_bundle).predict(feature_frame)
            explanation = self.explanation_generator.explain(
                model_bundle, feature_frame, prediction, identity
            )
            return {
                "status": "success",
                "agent": "predictor_agent",
                "model": self.model_key,
                "result": explanation,
            }
        except Exception as exc:
            logger.exception("PredictorAgent failed for model '%s'", self.model_key)
            identity = {
                col: (raw_record.get(col) if isinstance(raw_record, dict) else None)
                for col in self.feature_builder.id_columns
            }
            return {
                "status": "error",
                "agent": "predictor_agent",
                "model": self.model_key,
                "error": str(exc),
                "identity": identity,
            }

    def run_batch(self, raw_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [self.run(record) for record in raw_records]


# --------------------------------------------------------------------------- #
# Orchestrator-facing handler
# --------------------------------------------------------------------------- #

class OrchestratorAgentInterface:
    """Minimal contract every agent exposes to the orchestrator: a task
    dict in, a JSON-safe result dict out. Kept separate from PredictorAgent
    itself so the orchestrator's calling convention doesn't leak into the
    agent's own, more specific public API (run / run_batch)."""

    def handle(self, task: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError


class PredictorAgentHandler(OrchestratorAgentInterface):
    """What the orchestrator actually instantiates and calls whenever a
    prediction task is required.

    Expected task shape:
        {"model": "student_risk_engine", "record": {...}}
        or
        {"model": "student_risk_engine", "records": [{...}, {...}]}
    """

    def __init__(self, registry: dict[str, dict[str, Any]] = MODEL_REGISTRY):
        self.registry = registry
        self._agents: dict[str, PredictorAgent] = {}

    def _get_agent(self, model_key: str) -> PredictorAgent:
        if model_key not in self._agents:
            self._agents[model_key] = PredictorAgent(model_key, self.registry)
        return self._agents[model_key]

    def handle(self, task: dict[str, Any]) -> dict[str, Any]:
        model_key = task.get("model")
        if not model_key:
            return {"status": "error", "agent": "predictor_agent", "error": "Task must include 'model'."}
        if model_key not in self.registry:
            return {"status": "error", "agent": "predictor_agent", "error": f"Unknown model '{model_key}'."}

        agent = self._get_agent(model_key)

        if "records" in task:
            return {
                "status": "success",
                "agent": "predictor_agent",
                "model": model_key,
                "results": agent.run_batch(task["records"]),
            }

        record = task.get("record")
        if record is None:
            return {"status": "error", "agent": "predictor_agent", "error": "Task must include 'record' or 'records'."}
        return agent.run(record)


# --------------------------------------------------------------------------- #
# Example (only runs if artifacts exist -- safe to leave in / delete)
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    sample_record = {
        "student_id": "STU-00214",
        "tenant_id": "TEN-07",
        "academic_year": "2025/2026",
        "term": "Term 2",
        "fee_payment_days_late": 12,
        "attendance_rate": 0.81,
        "consecutive_absences": 3,
        "score_avg": 58.4,
        "subject_failure_count": 1,
        "paid_before_resumption": 0,
        "school_type": "public_state",
    }

    handler = PredictorAgentHandler()
    response = handler.handle({"model": "student_risk_engine", "record": sample_record})
    print(json.dumps(response, indent=2, default=str))
