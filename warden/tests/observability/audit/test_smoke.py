"""Smoke test validation for audit JSONL output.

Usage:
    # Validate a single file
    PYTHONPATH=. python -c "
    from warden.tests.observability.audit.test_smoke import validate_jsonl
    errs = validate_jsonl('orchestrator/audit/logs/smoke-7.jsonl', {'event_types': ['PreToolUse', 'PostToolUse']})
    print('PASS' if not errs else errs)
    "

    # Run all validations
    PYTHONPATH=. python -m warden.tests.observability.audit.test_smoke
"""

from __future__ import annotations

import json
from pathlib import Path


def validate_jsonl(path: str, expected: dict) -> list[str]:
    """Validate a JSONL audit log against expected criteria.

    Args:
        path: Path to the JSONL file.
        expected: Dict with optional keys:
            - event_types: list[str] — required event types that must appear
            - expected_tools: list[str] — if set, tool_name must be one of these
            - no_subagent: bool — if True, no SubagentStart/SubagentStop expected
            - has_subagent: bool — if True, SubagentStart + SubagentStop required
            - has_agent_id: bool — if True, some events must have agent_id
            - no_agent_id: bool — if True, no events should have agent_id
            - min_events: int — minimum number of events
            - exact_events: int — exact number of events

    Returns:
        List of validation error strings. Empty list = pass.
    """
    errors: list[str] = []
    file_path = Path(path)

    if not file_path.exists():
        return [f"File not found: {path}"]

    text = file_path.read_text().strip()
    if not text:
        return [f"File is empty: {path}"]

    lines = text.split("\n")
    events: list[dict] = []

    for i, line in enumerate(lines, 1):
        try:
            event = json.loads(line)
            events.append(event)
        except json.JSONDecodeError as e:
            errors.append(f"Line {i}: invalid JSON — {e}")

    if errors:
        return errors  # Can't proceed with invalid JSON

    # Check min/exact event count
    if "min_events" in expected and len(events) < expected["min_events"]:
        errors.append(
            f"Expected at least {expected['min_events']} events, got {len(events)}"
        )
    if "exact_events" in expected and len(events) != expected["exact_events"]:
        errors.append(
            f"Expected exactly {expected['exact_events']} events, got {len(events)}"
        )

    # Check required event types present
    if "event_types" in expected:
        found_types = {e["event_type"] for e in events}
        for et in expected["event_types"]:
            if et not in found_types:
                errors.append(f"Missing expected event_type: {et}")

    # Check session_id consistency
    session_ids = {e.get("session_id") for e in events if e.get("session_id")}
    if len(session_ids) > 1:
        errors.append(f"Inconsistent session_ids: {session_ids}")

    # Check timestamps ascending
    timestamps = [e.get("timestamp", "") for e in events]
    for i in range(1, len(timestamps)):
        if timestamps[i] < timestamps[i - 1]:
            errors.append(
                f"Timestamps not ascending: event {i} ({timestamps[i-1]}) > event {i+1} ({timestamps[i]})"
            )

    # Check tool_name against expected_tools
    if "expected_tools" in expected:
        allowed = set(expected["expected_tools"])
        for e in events:
            tn = e.get("tool_name")
            if tn and tn not in allowed:
                errors.append(f"Unexpected tool_name: {tn} (expected one of {allowed})")

    # Sub-agent checks
    if expected.get("no_subagent"):
        for e in events:
            if e["event_type"] in ("SubagentStart", "SubagentStop"):
                errors.append(f"Unexpected sub-agent event: {e['event_type']}")

    if expected.get("has_subagent"):
        found_types = {e["event_type"] for e in events}
        if "SubagentStart" not in found_types:
            errors.append("Missing SubagentStart event")
        if "SubagentStop" not in found_types:
            errors.append("Missing SubagentStop event")

    # Agent ID checks
    if expected.get("has_agent_id"):
        has_any = any(e.get("agent_id") for e in events)
        if not has_any:
            errors.append("No events have agent_id populated")

    if expected.get("no_agent_id"):
        for e in events:
            aid = e.get("agent_id")
            if aid and e["event_type"] not in ("SubagentStart", "SubagentStop"):
                errors.append(f"Unexpected agent_id '{aid}' on {e['event_type']}")

    # Check tool_input_summary field presence on tool events
    for e in events:
        if e["event_type"] in ("PreToolUse", "PostToolUse", "PostToolUseFailure"):
            if "tool_name" not in e:
                errors.append(f"Tool event missing tool_name: {e['event_type']}")

    # Check OTel dot-notation keys
    for e in events:
        if "gen_ai.operation.name" not in e:
            errors.append(
                f"Event missing gen_ai.operation.name: {e['event_type']}"
            )

    return errors


# ---------------------------------------------------------------------------
# Per-case validation configs
# ---------------------------------------------------------------------------

SMOKE_CASES: dict[str, dict] = {
    "smoke-7": {
        "description": "Minimal Bash — fast sanity check",
        "event_types": ["PreToolUse", "PostToolUse"],
        "min_events": 2,
    },
    "smoke-1": {
        "description": "Basic read-only tool use",
        "event_types": ["PreToolUse", "PostToolUse"],
        "no_subagent": True,
        "min_events": 2,
    },
    "smoke-2": {
        "description": "Write tool with input summarization",
        "event_types": ["PreToolUse", "PostToolUse"],
        "min_events": 2,
    },
    "smoke-3": {
        "description": "Multi-tool single turn",
        "event_types": ["PreToolUse", "PostToolUse"],
        "min_events": 2,  # At least 1 Pre+Post pair (model may solve in 1 tool call)
    },
    "smoke-4": {
        "description": "Edit tool with input summarization",
        "event_types": ["PreToolUse", "PostToolUse"],
        "min_events": 2,
    },
    "smoke-5": {
        "description": "Sub-agent lifecycle",
        "event_types": ["PreToolUse", "PostToolUse", "SubagentStart", "SubagentStop"],
        "has_subagent": True,
        "has_agent_id": True,
        "min_events": 4,
    },
    "smoke-6": {
        "description": "Sub-agent + main-thread sequencing",
        "event_types": ["PreToolUse", "PostToolUse", "SubagentStart", "SubagentStop"],
        "has_subagent": True,
        "min_events": 4,
    },
}


def validate_all(log_dir: str = "orchestrator/audit/logs") -> dict[str, list[str]]:
    """Validate all smoke test JSONL files."""
    results: dict[str, list[str]] = {}
    for case_id, config in SMOKE_CASES.items():
        path = f"{log_dir}/{case_id}.jsonl"
        results[case_id] = validate_jsonl(path, config)
    return results


if __name__ == "__main__":
    results = validate_all()
    all_pass = True
    for case_id, errors in results.items():
        config = SMOKE_CASES[case_id]
        status = "PASS" if not errors else "FAIL"
        if errors:
            all_pass = False
        print(f"  {case_id} ({config['description']}): {status}")
        for err in errors:
            print(f"    - {err}")
    print()
    print("Overall:", "ALL PASS" if all_pass else "SOME FAILED")
