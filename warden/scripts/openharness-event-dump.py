#!/usr/bin/env python3
"""Capture OpenHarness StreamEvent lifecycle as JSON for observability analysis.

Runs two scenarios against a local Ollama model, dumping every StreamEvent
to JSON files for inspection — parallel to the Claude SDK dumps.

Usage:
    PYTHONPATH=. server/.venv/bin/python scripts/openharness-event-dump.py

Output:
    docs/observability-artifacts/lifecycle/openharness-events-dump.json       (tool call, no agent)
    docs/observability-artifacts/lifecycle/openharness-agent-dump.json        (agent call)
"""

import asyncio
import dataclasses
import json
import time
from pathlib import Path

from openharness.api.openai_client import OpenAICompatibleClient
from openharness.engine.query_engine import QueryEngine
from openharness.permissions.checker import (
    PermissionChecker,
    PermissionMode,
    PermissionSettings,
)
from openharness.tools import create_default_tool_registry

MODEL = "qwen3:1.7b"
BASE_URL = "http://localhost:11434/v1"
OUTPUT_DIR = Path("docs/observability-artifacts/lifecycle")

SCENARIOS = [
    {
        "name": "tool_call",
        "output_file": "openharness-events-dump.json",
        "prompt": (
            "Read the file orchestrator/telemetry.py and tell me "
            "the function names defined in it. Use the read_file tool."
        ),
        "description": (
            "Single tool call (read_file), no agents. "
            "Parallel to Claude's sdk-messages-dump.json."
        ),
    },
    {
        "name": "agent_call",
        "output_file": "openharness-agent-dump.json",
        "prompt": (
            "Use the agent tool to search this codebase for files "
            'containing "get_langfuse". Have the agent find them and '
            "return the file list. After the agent returns, tell me "
            "how many files were found."
        ),
        "description": (
            "Forces the agent tool to spawn a sub-agent. "
            "Parallel to Claude's sdk-subagent-dump.json."
        ),
    },
]


def event_to_dict(event, turn_label: str) -> dict:
    """Convert a StreamEvent dataclass to a serializable dict."""
    d = {
        "_type": type(event).__name__,
        "_timestamp": time.time(),
        "_scenario": turn_label,
    }
    if dataclasses.is_dataclass(event):
        for field in dataclasses.fields(event):
            val = getattr(event, field.name)
            if hasattr(val, "model_dump"):
                val = val.model_dump()
            elif hasattr(val, "dict"):
                val = val.dict()
            d[field.name] = val
    return d


def make_engine() -> QueryEngine:
    api_client = OpenAICompatibleClient(api_key="ollama", base_url=BASE_URL)
    tool_registry = create_default_tool_registry()
    permission_checker = PermissionChecker(
        PermissionSettings(mode=PermissionMode.FULL_AUTO)
    )
    return QueryEngine(
        api_client=api_client,
        tool_registry=tool_registry,
        permission_checker=permission_checker,
        cwd=Path.cwd(),
        model=MODEL,
        system_prompt=(
            "You are a helpful coding assistant. You have access to tools "
            "for reading files, listing directories, and spawning agents. "
            "Use them when asked."
        ),
        max_tokens=2048,
        max_turns=6,
    )


async def run_scenario(scenario: dict) -> None:
    name = scenario["name"]
    prompt = scenario["prompt"]
    output_file = OUTPUT_DIR / scenario["output_file"]

    print(f"\n{'='*60}", flush=True)
    print(f"Scenario: {name}", flush=True)
    print(f"Prompt: {prompt[:80]}...", flush=True)
    print(f"{'='*60}", flush=True)

    engine = make_engine()
    events = []
    t0 = time.time()

    async for event in engine.submit_message(prompt):
        event_dict = event_to_dict(event, name)
        events.append(event_dict)

        etype = type(event).__name__
        elapsed = time.time() - t0
        if etype == "AssistantTextDelta":
            print(event.text, end="", flush=True)
        elif etype == "ToolExecutionStarted":
            inp_preview = str(event.tool_input)[:100]
            print(f"\n  [{elapsed:.1f}s] [tool_start] {event.tool_name}({inp_preview})", flush=True)
        elif etype == "ToolExecutionCompleted":
            preview = event.output[:80] + "..." if len(event.output) > 80 else event.output
            print(f"  [{elapsed:.1f}s] [tool_done]  {event.tool_name} error={event.is_error} → {preview}", flush=True)
        elif etype == "AssistantTurnComplete":
            usage = event.usage
            print(f"\n  [{elapsed:.1f}s] [turn_complete] {usage.input_tokens}in/{usage.output_tokens}out", flush=True)
        elif etype == "ErrorEvent":
            print(f"\n  [{elapsed:.1f}s] [ERROR] {event.message}", flush=True)

    elapsed_total = time.time() - t0

    dump = {
        "prompt": prompt,
        "model": MODEL,
        "description": scenario["description"],
        "event_count": len(events),
        "duration_seconds": round(elapsed_total, 1),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "events": events,
    }

    output_file.parent.mkdir(parents=True, exist_ok=True)
    with open(output_file, "w") as f:
        json.dump(dump, f, indent=2, default=str)

    print(f"\n\nCaptured {len(events)} events in {elapsed_total:.1f}s → {output_file}", flush=True)


async def main():
    print(f"OpenHarness Event Dump — model={MODEL}", flush=True)

    for scenario in SCENARIOS:
        try:
            await run_scenario(scenario)
        except Exception as e:
            print(f"\n[ERROR] Scenario '{scenario['name']}' failed: {e}", flush=True)
            import traceback
            traceback.print_exc()

    print(f"\n{'='*60}", flush=True)
    print("All scenarios complete.", flush=True)
    print(f"{'='*60}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
