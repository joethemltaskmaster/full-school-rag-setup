# Update Summary

## orchestrator.py Changes
1. Added narrate flag handling in the `execute()` method:
   - Extracts `narrate` boolean from task parameters
   - For successful data agent results with narrate=True, calls `_attach_data_narration()`

2. Added `_attach_data_narration()` method:
   - Best-effort narration that never blocks original data delivery
   - On success: adds "narrative" key alongside original result
   - On failure: adds "narrative_error" key but preserves original data and returns success status
   - Includes proper exception handling and logging

3. Updated `capabilities()` method to include narration agent capabilities
4. Added lazy initialization of narration agent via property

## planner_agent.py Changes
1. Added `NARRATE_BY_DEFAULT_INTENTS` constant:
   - get_student_full_profile
   - get_class_overview
   - get_risk_briefing
   - get_school_overview

2. Added `REQUIRES_STUDENT_ID` constant:
   - get_student_full_profile
   - get_student
   - get_attendance_rate
   - get_fee_status
   - get_risk_briefing

3. Enhanced `create_execution_plan()` method:
   - Automatically sets `narrate: True` for intents in `NARRATE_BY_DEFAULT_INTENTS`
   - Preserves existing logic for other intents (narrate: False)

4. Enhanced `execute_plan()` method:
   - Fail-fast validation: checks for missing student_id in REQUIRES_STUDENT_ID intents
   - Returns clear error message: "I couldn't find student_id in your message for '{action}' -- could you specify it and try again?"
   - Error is returned at top level so CLI's render_result() finds it immediately
   - Prevents None values from being silently stripped and causing confusing downstream errors

## Behavior
- Narration is enrichment only: original data is always preserved and returned
- Narration failures (missing API key, network issues, etc.) result in narrative_error field but don't fail the overall request
- Complex multi-section reads automatically get narration enabled
- Simple single-value lookups (like attendance rate) remain JSON-only as requested
- Missing required student_id triggers immediate, clear user guidance instead of confusing Python errors