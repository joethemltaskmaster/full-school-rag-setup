#!/usr/bin/env python3
"""
Test script to demonstrate the narrate functionality in orchestrator.py
"""

import sys
import os

# Add the Database directory to the path so we can import agents
sys.path.insert(0, os.path.join(os.path.dirname(__file__)))

from agents.orchestrator import Orchestrator

def test_narration_functionality():
    """Test that the narrate flag works correctly"""
    print("=== Testing Orchestrator Narration Functionality ===\n")

    # Create orchestrator instance
    orch = Orchestrator()

    # Test 1: Data agent task without narration (should work as before)
    print("Test 1: Data agent task WITHOUT narration")
    result = orch.execute({"intent": "get_student", "student_id": 1})
    print(f"Status: {result['status']}")
    print(f"Agent: {result['agent']}")
    if 'result' in result:
        print(f"Result keys: {list(result['result'].keys())}")
        has_narrative = 'narrative' in result['result']
        has_narrative_error = 'narrative_error' in result['result']
        print(f"Has narrative: {has_narrative}")
        print(f"Has narrative_error: {has_narrative_error}")
    print()

    # Test 2: Data agent task WITH narration (should add narrative_error since no API key)
    print("Test 2: Data agent task WITH narration")
    result = orch.execute({"intent": "get_student", "student_id": 1, "narrate": True})
    print(f"Status: {result['status']}")
    print(f"Agent: {result['agent']}")
    if 'result' in result:
        print(f"Result keys: {list(result['result'].keys())}")
        has_narrative = 'narrative' in result['result']
        has_narrative_error = 'narrative_error' in result['result']
        print(f"Has narrative: {has_narrative}")
        print(f"Has narrative_error: {has_narrative_error}")
        if has_narrative_error:
            print(f"Narrative error (expected): {result['result']['narrative_error'][:100]}...")
    print()

    # Test 3: Direct narration agent call (to show it works when API key is available)
    print("Test 3: Direct narration agent call (shows error handling)")
    result = orch.narration_agent.handle({"data": {"test": "data"}, "context": "test"})
    print(f"Status: {result['status']}")
    if result['status'] == 'error':
        print(f"Error: {result.get('error', 'Unknown error')}")
    print()

    # Test 4: Show that other agent types still work normally
    print("Test 4: Prediction agent task (should work normally)")
    result = orch.execute({"model": "student_risk_engine", "record": {"test": "data"}})
    print(f"Status: {result['status']}")
    print(f"Agent: {result.get('agent', 'N/A')}")
    print()

    print("=== Test Complete ===")

if __name__ == "__main__":
    test_narration_functionality()