"""Smoke tests for OpenHarness audit hooks — integration tests requiring Ollama.

Usage:
    # Run all smokes (needs Ollama + qwen3:1.7b):
    PYTHONPATH=. server/.venv/bin/python -m warden.tests.observability.audit.test_openharness_smoke

    # Run a single smoke:
    PYTHONPATH=. server/.venv/bin/python -m warden.tests.observability.audit.test_openharness_smoke 1

Each smoke test:
1. Runs a prompt through OpenHarnessSession with AUDIT_ENABLED=1
2. Validates the resulting JSONL file using validate_jsonl()
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from warden.tests.observability.audit.test_smoke import validate_jsonl


def _run_prompt(run_id: str, prompt: str, log_dir: Path) -> Path:
    """Run a prompt through OpenHarnessSession with audit enabled, return JSONL path."""

    async def _inner():
        # Set env vars for the audit hook handler subprocess
        os.environ["AUDIT_ENABLED"] = "1"
        os.environ["AUDIT_RUN_ID"] = run_id
        os.environ["AUDIT_LOG_DIR"] = str(log_dir)

        from warden.providers.openharness.session import OpenHarnessSession

        session = OpenHarnessSession(
            repo_path=Path.cwd(),
            model="qwen3:1.7b",
        )
        await session.start()

        try:
            async for _event in session.send(prompt):
                pass  # Consume all events
        finally:
            await session.close()

    asyncio.run(_inner())
    return log_dir / f"{run_id}.jsonl"


# ---------------------------------------------------------------------------
# Smoke test definitions
# ---------------------------------------------------------------------------

SMOKE_TESTS: dict[str, dict] = {
    "1": {
        "name": "Minimal shell command",
        "prompt": "Run the shell command 'echo hello_audit_test'. Do not explain, just run it.",
        "expected": {
            "event_types": ["PreToolUse", "PostToolUse"],
            "min_events": 2,
        },
    },
    "2": {
        "name": "File read tool",
        "prompt": "Read the file CLAUDE.md and tell me the project name in one word.",
        "expected": {
            "event_types": ["PreToolUse", "PostToolUse"],
            "min_events": 2,
        },
    },
    "3": {
        "name": "Multi-tool turn",
        "prompt": "Read the file CLAUDE.md, then run the command 'wc -l CLAUDE.md' and tell me the line count.",
        "expected": {
            "event_types": ["PreToolUse", "PostToolUse"],
            "min_events": 4,  # 2 tool calls × 2 events each
        },
    },
    "4": {
        "name": "Write tool with input summarization",
        "prompt": "Create a file at /tmp/oh-audit-test.md with the text 'audit test content'",
        "expected": {
            "event_types": ["PreToolUse", "PostToolUse"],
            "min_events": 2,
        },
    },
    "5": {
        "name": "Stop event",
        "prompt": "Run the shell command 'echo smoke5_done'.",
        "expected": {
            "event_types": ["PreToolUse", "PostToolUse", "Stop"],
            "min_events": 3,
        },
    },
    "6": {
        "name": "Agent tool + SubagentStop (best-effort)",
        "prompt": (
            'Call the agent tool with exactly these arguments: '
            '{"description": "Read a file", "prompt": "Read CLAUDE.md and return the first line"}'
        ),
        "expected": {
            "event_types": ["SubagentStop"],
            "min_events": 1,
        },
        "best_effort": True,
    },
}


def run_smoke(smoke_id: str, log_dir: Path) -> tuple[str, list[str]]:
    """Run a single smoke test and return (name, errors)."""
    test = SMOKE_TESTS[smoke_id]
    run_id = f"oh-smoke-{smoke_id}"
    name = test["name"]

    print(f"\n--- Smoke {smoke_id}: {name} ---")
    print(f"  Prompt: {test['prompt'][:80]}...")

    try:
        jsonl_path = _run_prompt(run_id, test["prompt"], log_dir)
        errors = validate_jsonl(str(jsonl_path), test["expected"])

        # Smoke 4: extra check — content should not be in tool_input_summary
        if smoke_id == "4" and jsonl_path.exists():
            for line in jsonl_path.read_text().strip().split("\n"):
                evt = json.loads(line)
                summary = evt.get("tool_input_summary", {})
                if isinstance(summary, dict) and "content" in summary:
                    errors.append("tool_input_summary should not contain 'content' field for write tool")

    except Exception as e:
        errors = [f"Runtime error: {e}"]

    status = "PASS" if not errors else ("BEST-EFFORT FAIL" if test.get("best_effort") else "FAIL")
    print(f"  Result: {status}")
    for err in errors:
        print(f"    - {err}")

    return name, errors


def main():
    log_dir = Path("orchestrator/audit/logs")
    log_dir.mkdir(parents=True, exist_ok=True)

    # Determine which smokes to run
    if len(sys.argv) > 1:
        smoke_ids = sys.argv[1:]
    else:
        smoke_ids = list(SMOKE_TESTS.keys())

    results: dict[str, list[str]] = {}
    for sid in smoke_ids:
        if sid not in SMOKE_TESTS:
            print(f"Unknown smoke test: {sid}")
            continue
        name, errors = run_smoke(sid, log_dir)
        results[sid] = errors

    # Summary
    print("\n" + "=" * 50)
    print("OpenHarness Audit Smoke Test Summary")
    print("=" * 50)

    hard_fail = False
    for sid, errors in results.items():
        test = SMOKE_TESTS[sid]
        is_best_effort = test.get("best_effort", False)
        status = "PASS" if not errors else ("BEST-EFFORT FAIL" if is_best_effort else "FAIL")
        print(f"  Smoke {sid} ({test['name']}): {status}")
        if errors and not is_best_effort:
            hard_fail = True

    print()
    if hard_fail:
        print("RESULT: SOME HARD GATES FAILED")
        sys.exit(1)
    else:
        print("RESULT: ALL HARD GATES PASS")


if __name__ == "__main__":
    main()
