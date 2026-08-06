"""
demo_predict_synthetic.py

Proves the core pipeline works: pull rows from student_risk_records
(the synthetic dataset the model was trained on) and score them through
student_risk_engine directly -- no feature bridge, no placeholders, no
real-student involvement. This is the "does the prototype work" check.

Run from the agents/ directory (or wherever school.db / model/ live):
    python demo_predict_synthetic.py
"""

import json

from orchestrator import Orchestrator


def main():
    orch = Orchestrator(db_path="school.db")

    sample = []
    for label in ["LOW", "MEDIUM", "HIGH"]:
        result = orch.execute({"intent": "get_student_risk_records", "risk_label": label})
        if result["status"] == "success":
            sample.extend(result["result"][:5])  # a handful per class

    if not sample:
        print("No rows found in student_risk_records -- has load_ml_feature_csvs.py been run?")
        return

    print(f"Testing {len(sample)} synthetic records across LOW/MEDIUM/HIGH...\n")

    correct = 0
    rows = []
    for row in sample:
        true_label = row.pop("dropout_risk_label", None)  # the answer key -- don't feed it to the model

        prediction = orch.execute({"model": "student_risk_engine", "record": row})
        if prediction["status"] != "success":
            print(f"{row.get('student_id')}: PREDICTION FAILED -> {prediction.get('error')}")
            continue

        predicted = prediction["result"]["prediction"]
        confidence = prediction["result"]["confidence"]
        is_correct = predicted == true_label
        correct += is_correct

        rows.append({
            "student_id": row.get("student_id"),
            "true_label": true_label,
            "predicted": predicted,
            "confidence": confidence,
            "correct": is_correct,
        })
        print(f"{row.get('student_id'):<12} true={true_label:<8} predicted={predicted:<8} "
              f"confidence={confidence:.2f}  [{'OK' if is_correct else 'MISS'}]")

    print(f"\n{correct}/{len(sample)} correct on this sample")
    print(json.dumps(rows, indent=2))


if __name__ == "__main__":
    main()
