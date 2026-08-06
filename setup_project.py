"""
setup_project.py

The single entrypoint to bring the project from "just cloned" to
"ready to run" — and, just as importantly, to tell you clearly what's
still missing when it can't finish a step for you (like training a
model, which only you can decide how to do properly).

What it does, in order:
    1. Builds school.db's core schema (if missing)
    2. Loads the sample operational CSVs (students, classes, etc.) if
       the tables are still empty
    3. Adds the 4 ML feature tables (if missing)
    4. Loads the ML feature CSVs into them, if the source CSVs are
       found locally and the tables are still empty
    5. Checks whether a trained model exists for the prediction agent
    6. Checks whether a knowledge base file exists for the retrieval agent
    7. Checks whether GEMINI_API_KEY / GOOGLE_API_KEY is set
    8. Prints a final readiness report for all three agents

Safe to re-run: every step checks current state first and skips work
that's already done, rather than duplicating rows or re-downloading
anything.

Run:
    python setup_project.py
"""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(dotenv_path=".env")

DB_NAME = "school.db"
MODEL_DIR = Path("model")               # what prediction_agent.py's MODEL_REGISTRY expects
LEGACY_MODEL_DIR = Path("models")        # the earlier prototype model, kept for reference
KNOWLEDGE_BASE_PATH = Path("agents/school_knowledge_base.txt")

# Where to look for the 4 ML feature CSVs, in priority order. The
# sandbox path is specific to this Claude environment — on your own
# machine, drop the CSVs in ./ml_data/ instead.
ML_CSV_SEARCH_DIRS = [
    Path("/mnt/user-data/uploads"),
    Path("ml_data"),
    Path("."),
]
ML_CSV_FILES = [
    "teacher_profiles.csv",
    "fee_default_synthetic.csv",
    "lesson_schedule.csv",
    "nigerian_students_synthetic-1.csv",
]


def _print_header(title: str) -> None:
    print(f"\n{'=' * 60}\n{title}\n{'=' * 60}")


def _run_script(script_name: str) -> bool:
    """Runs a helper script as a subprocess, streaming its output. Returns True on success."""
    if not Path(script_name).exists():
        print(f"  ! {script_name} not found — skipping this step.")
        return False
    result = subprocess.run([sys.executable, script_name], capture_output=True, text=True)
    print(result.stdout.strip())
    if result.returncode != 0:
        print(f"  ! {script_name} failed:\n{result.stderr.strip()}")
        return False
    return True


def _table_row_count(table: str) -> int:
    if not Path(DB_NAME).exists():
        return 0
    try:
        conn = sqlite3.connect(DB_NAME)
        cur = conn.execute(f"SELECT COUNT(*) FROM {table}")
        count = cur.fetchone()[0]
        conn.close()
        return count
    except sqlite3.OperationalError:
        return 0  # table doesn't exist yet


def _find_ml_csv_dir() -> Path | None:
    for directory in ML_CSV_SEARCH_DIRS:
        if all((directory / f).exists() for f in ML_CSV_FILES):
            return directory
    return None


# =====================================================================
# STEP 1-2: core schema + sample operational data
# =====================================================================
def setup_core_database() -> None:
    _print_header("STEP 1-2: Core database schema + sample data")

    if not Path(DB_NAME).exists() or _table_row_count("students") == 0:
        print("Building core schema and loading sample CSVs...")
        _run_script("create_school_db.py")
        if Path("csv").exists():
            _run_script("load_csvs_to_db.py")
        else:
            print("  ! ./csv/ folder not found — run generate_sample_csvs.py first if you need sample data.")
    else:
        print(f"Already set up — 'students' table has {_table_row_count('students')} rows. Skipping.")


# =====================================================================
# STEP 3-4: ML feature tables + their CSVs
# =====================================================================
def setup_ml_feature_tables() -> None:
    _print_header("STEP 3-4: ML feature tables (teacher_profiles, lesson_schedule, "
                   "fee_default_records, student_risk_records)")

    if _table_row_count("student_risk_records") > 0:
        print(f"Already loaded — 'student_risk_records' has "
              f"{_table_row_count('student_risk_records')} rows. Skipping.")
        return

    _run_script("add_ml_feature_tables.py")

    csv_dir = _find_ml_csv_dir()
    if csv_dir is None:
        print("  ! Could not find all 4 ML feature CSVs in any known location "
              f"({', '.join(str(d) for d in ML_CSV_SEARCH_DIRS)}).")
        print("    Place them in ./ml_data/ and re-run this script.")
        return

    print(f"Found ML feature CSVs in {csv_dir} — loading...")
    _run_script("load_ml_feature_csvs.py")


# =====================================================================
# STEP 5: trained model check (can't be done automatically — reports only)
# =====================================================================
def check_trained_models() -> dict:
    _print_header("STEP 5: Trained model artifacts (prediction agent)")

    expected = {
        "model.joblib": MODEL_DIR / "model.joblib",
        "label_encoder.joblib": MODEL_DIR / "label_encoder.joblib",
        "feature_schema.json": MODEL_DIR / "feature_schema.json",
    }
    missing = [name for name, path in expected.items() if not path.exists()]

    if not missing:
        print(f"All expected files found in {MODEL_DIR}/ — prediction agent is ready.")
        return {"ready": True}

    print(f"Missing from {MODEL_DIR}/: {', '.join(missing)}")
    print("The prediction agent's MODEL_REGISTRY expects these — they are not")
    print("generated automatically. Train and save them with your own training")
    print("script (e.g. train_student_risk_engine.py) before using the prediction agent.")

    if (LEGACY_MODEL_DIR / "student_risk_model.joblib").exists():
        print(f"\n(Note: a prototype model exists at {LEGACY_MODEL_DIR}/ from earlier "
              f"testing, trained on synthetic placeholder data — not wired into "
              f"prediction_agent.py's registry, and not a substitute for training "
              f"on the real student_risk_records data now loaded in school.db.)")

    return {"ready": False, "missing": missing}


# =====================================================================
# STEP 6: knowledge base check (retrieval agent)
# =====================================================================
def check_knowledge_base() -> dict:
    _print_header("STEP 6: Knowledge base file (retrieval agent)")

    if KNOWLEDGE_BASE_PATH.exists():
        print(f"Found {KNOWLEDGE_BASE_PATH} — retrieval agent has something to index.")
        return {"ready": True}

    print(f"Missing: {KNOWLEDGE_BASE_PATH}")
    print("Create this file with the school policy/FAQ text you want the retrieval")
    print("agent to answer questions from, then it will build (and cache) a FAISS")
    print("index from it automatically on first use.")
    return {"ready": False}


# =====================================================================
# STEP 7: API key check (retrieval agent)
# =====================================================================
def check_api_key() -> dict:
    _print_header("STEP 7: GEMINI_API_KEY / GOOGLE_API_KEY (retrieval agent)")

    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("NVIDIA_API_KEY")
    if key:
        print("Found an API key in the environment — retrieval agent can call Gemini.")
        return {"ready": True}

    print("Not set. Export one before using the retrieval agent, e.g.:")
    print("    export GEMINI_API_KEY=your-key-here")
    print("(The data agent and prediction agent need no API key at all.)")
    return {"ready": False}


# =====================================================================
# STEP 8: final readiness report
# =====================================================================
def print_readiness_report(model_status: dict, kb_status: dict, api_key_status: dict) -> None:
    _print_header("READINESS REPORT")

    data_agent_ready = _table_row_count("students") > 0
    print(f"  data_agent        : {'READY' if data_agent_ready else 'NOT READY (run core schema step)'}")

    prediction_ready = model_status.get("ready", False)
    print(f"  prediction_agent  : {'READY' if prediction_ready else 'NOT READY (train + save model artifacts)'}")

    retrieval_ready = kb_status.get("ready", False) and api_key_status.get("ready", False)
    reasons = []
    if not kb_status.get("ready"):
        reasons.append("no knowledge base file")
    if not api_key_status.get("ready"):
        reasons.append("no API key")
    reason_text = f" ({', '.join(reasons)})" if reasons else ""
    print(f"  retrieval_agent   : {'READY' if retrieval_ready else 'NOT READY' + reason_text}")

    print()
    if data_agent_ready and prediction_ready and retrieval_ready:
        print("All three agents are ready. Try:")
        print('    from orchestrator import Orchestrator')
        print('    orch = Orchestrator(db_path="school.db")')
        print('    orch.execute({"intent": "get_student", "student_id": 1})')
    else:
        print("The orchestrator will still run — agents that aren't ready will")
        print("just return a structured {'status': 'error', ...} for their own")
        print("intents until the missing pieces above are in place.")


def main():
    setup_core_database()
    setup_ml_feature_tables()
    model_status = check_trained_models()
    kb_status = check_knowledge_base()
    api_key_status = check_api_key()
    print_readiness_report(model_status, kb_status, api_key_status)


if __name__ == "__main__":
    main()
