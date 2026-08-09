#!/usr/bin/env python3
"""OTel waterfall test — exercises tool calls, agent spawning, and multi-turn
in a single session so you can inspect the trace waterfall in Tempo/Grafana
and the session timeline in Langfuse.

Usage:
    PYTHONPATH=. LANGFUSE_PUBLIC_KEY=pk-lf-example LANGFUSE_SECRET_KEY=sk-lf-example \
        LANGFUSE_HOST=http://localhost:3456 \
        server/.venv/bin/python scripts/otel-waterfall-test.py

What it does (5 turns in one session):
    1. Tool call  — Read a file (exercises Read tool)
    2. Web search — Search the web (exercises WebSearch tool if available)
    3. Multi-tool — Count files + read one (exercises Glob + Read)
    4. Agent call — Spawn a sub-agent to search the codebase
    5. Summary    — Ask for a summary of everything done (multi-turn context)

After running, check:
    - Tempo: http://localhost:3030 → Explore → Tempo → {service.name="claude-agent"}
    - Langfuse: http://localhost:3456 → Sessions → look for the session ID printed below
    - Prometheus: http://localhost:9090 → query example_traces_span_metrics_calls_total

Phase 3c additions:
    - Verbose logging of sub-agent lifecycle events (TaskStarted/Progress/Notification)
    - Langfuse trace URL printed after completion
"""

import asyncio
import os

from warden import (
    ChatAPI,
    ErrorEvent,
    HarnessConfig,
    MessageEvent,
    ToolAccessNotificationEvent,
)

TURNS = [
    {
        "label": "1/5 Tool call (Read)",
        "prompt": (
            "Read the file orchestrator/telemetry.py and tell me what it does "
            "in one sentence. Use the Read tool."
        ),
    },
    {
        "label": "2/5 Web search",
        "prompt": (
            "Search the web for 'OpenTelemetry gen_ai semantic conventions' "
            "and give me a one-sentence summary of what gen_ai conventions are. "
            "Use the WebSearch tool."
        ),
    },
    {
        "label": "3/5 Multi-tool (Glob + Read)",
        "prompt": (
            "How many Python files are in the warden/providers/ directory? "
            "Use Glob to list them, then Read the first one and tell me the class name. "
            "Be concise."
        ),
    },
    {
        "label": "4/5 Agent call (sub-agent)",
        "prompt": (
            "Use the Agent tool to search this codebase for all files that contain "
            "'LANGFUSE'. Have the agent return a simple list of file paths. "
            "Keep the agent prompt short."
        ),
    },
    {
        "label": "5/5 Summary (multi-turn context)",
        "prompt": (
            "Summarize everything you did in the previous 4 turns in a bullet list. "
            "Do NOT use any tools — just answer from memory."
        ),
    },
]


async def main():
    api = ChatAPI(HarnessConfig(), repo_path=".")
    await api.init()

    print("=" * 60)
    print("OTel Waterfall Test — 5 turns, 1 session")
    print("=" * 60)

    for i, turn in enumerate(TURNS):
        print(f"\n--- {turn['label']} ---")
        print(f"Prompt: {turn['prompt'][:80]}...")
        print()

        text_parts = []
        async for event in api.send(
            turn["prompt"], workflow=None,
        ):
            if isinstance(event, MessageEvent):
                if event.kind == "text":
                    chunk = event.content.get("text", "")
                    text_parts.append(chunk)
                    print(chunk, end="", flush=True)
                elif event.kind == "tool_use":
                    name = event.content.get("name", "?")
                    print(f"\n  [tool_use] {name}", flush=True)
                elif event.kind == "tool_result":
                    output = str(event.content.get("output", ""))
                    preview = output[:100] + "..." if len(output) > 100 else output
                    print(f"  [tool_result] {preview}", flush=True)
            elif isinstance(event, ToolAccessNotificationEvent):
                print(
                    f"  [{event.action}] {event.tool_name}: {event.reason}",
                    flush=True,
                )
            elif isinstance(event, ErrorEvent):
                print(f"  [ERROR] {event.text}", flush=True)
        print()

    # Print session info for verification
    sid = getattr(api, "_current_session_id", None)
    langfuse_host = os.environ.get("LANGFUSE_HOST", "http://localhost:3456")
    print("\n" + "=" * 60)
    print(f"Session ID: {sid}")
    print(f"Langfuse:   {langfuse_host}")
    if sid:
        print(f"  Session:  {langfuse_host}/sessions/{sid}")
    print("Tempo:      http://localhost:3030 → Explore → Tempo")
    print('  TraceQL:  {service.name="claude-agent"}')
    print("=" * 60)

    await api.close()


if __name__ == "__main__":
    asyncio.run(main())
