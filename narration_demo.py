#!/usr/bin/env python3
"""
Demonstration script showing the narrate functionality working
This script imports and uses the orchestrator the same way the main block does
"""

import json
import sys
import os

# Add current directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agents.orchestrator import Orchestrator

def main():
    print("=== Narration Functionality Demo ===\n")

    # Create orchestrator (same as main block)
    orch = Orchestrator(db_path="school.db")

    print("1. Testing data agent task WITHOUT narration:")
    result = orch.execute({"intent": "get_student", "student_id": 1})
    print(f"   Status: {result['status']}")
    if 'result' in result:
        print(f"   Student: {result['result'].get('full_name', 'Unknown')}")
        print(f"   Has narrative data: {'narrative' in result['result']}")
        print(f"   Has narrative error: {'narrative_error' in result['result']}")
    print()

    print("2. Testing data agent task WITH narration:")
    result = orch.execute({"intent": "get_student", "student_id": 1, "narrate": True})
    print(f"   Status: {result['status']}")
    if 'result' in result:
        print(f"   Student: {result['result'].get('full_name', 'Unknown')}")
        print(f"   Has narrative data: {'narrative' in result['result']}")
        print(f"   Has narrative error: {'narrative_error' in result['result']}")
        if 'narrative_error' in result['result']:
            error = result['result']['narrative_error']
            print(f"   Narrative error (expected): {error[:100]}{'...' if len(error) > 100 else ''}")
    print()

    print("3. Testing that other functionality still works:")
    print("   Workflow execution:")
    workflow_result = orch.run_workflow("student_report", student_id=1)
    print(f"   Status: {workflow_result['status']}")
    if 'result' in workflow_result and 'student' in workflow_result['result']:
        student = workflow_result['result']['student']
        print(f"   Student report for: {student.get('full_name', 'Unknown')}")
    print()

    print("=== Demo Complete ===")
    print("\\nNote: Narrative errors are expected when NVIDIA_API_KEY is not set.")
    print("The orchestrator correctly handles this by preserving the original data")
    print("and adding a narrative_error field rather than failing the request.")

if __name__ == "__main__":
    main()