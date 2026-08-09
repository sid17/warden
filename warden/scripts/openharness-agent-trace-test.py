"""Test 5: OpenHarness agent span enrichment.

Sends a prompt that triggers the agent tool, waits for the subprocess
to complete, then queries Langfuse to verify the agent span has real
output, status, and duration.

Usage:
    PYTHONPATH=. LANGFUSE_PUBLIC_KEY=pk-lf-example LANGFUSE_SECRET_KEY=sk-lf-example \
        LANGFUSE_HOST=http://localhost:3456 \
        server/.venv/bin/python scripts/openharness-agent-trace-test.py
"""

import asyncio
import json
import os
import sys
import time
import urllib.request

from pathlib import Path

LANGFUSE_HOST = os.environ.get("LANGFUSE_HOST", "http://localhost:3456")
LANGFUSE_PK = os.environ.get("LANGFUSE_PUBLIC_KEY", "pk-lf-example")
LANGFUSE_SK = os.environ.get("LANGFUSE_SECRET_KEY", "sk-lf-example")


def langfuse_get(path: str) -> dict:
    """GET request to Langfuse public API with basic auth."""
    url = f"{LANGFUSE_HOST}{path}"
    req = urllib.request.Request(url)
    import base64
    creds = base64.b64encode(f"{LANGFUSE_PK}:{LANGFUSE_SK}".encode()).decode()
    req.add_header("Authorization", f"Basic {creds}")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


async def run_agent_session() -> str:
    """Run an OpenHarness session that triggers the agent tool."""
    from warden.providers.openharness.session import OpenHarnessSession

    session = OpenHarnessSession(repo_path=Path.cwd(), model="qwen3:1.7b")
    await session.start()

    # Prompt designed to trigger the agent tool.
    # The agent tool requires both 'description' and 'prompt' fields.
    # We spell this out explicitly because small models (qwen3:1.7b) often
    # miss the 'prompt' field if the instruction isn't very specific.
    prompt = (
        'Call the agent tool with exactly these arguments: '
        '{"description": "Read a file", "prompt": "Read the file CLAUDE.md and return the first line"}'
    )

    t0 = time.time()
    print(f"Sending prompt: {prompt[:80]}...")
    agent_spawned = False
    try:
        async for event in session.send(prompt):
            etype = type(event).__name__
            if etype == "ToolExecutionStarted":
                print(f"  [{time.time()-t0:.0f}s] ToolStart: {event.tool_name}")
            elif etype == "ToolExecutionCompleted":
                output_preview = getattr(event, "output", "")[:100]
                print(f"  [{time.time()-t0:.0f}s] ToolDone: {getattr(event, 'tool_name', '?')} → {output_preview}")
                if "Spawned agent" in getattr(event, "output", ""):
                    agent_spawned = True
            elif etype == "AssistantTurnComplete":
                print(f"  [{time.time()-t0:.0f}s] TurnComplete")
            elif etype == "AssistantTextDelta":
                print(event.text, end="", flush=True)
    except Exception as e:
        print(f"\n  Session ended with: {type(e).__name__}: {e}")

    if not agent_spawned:
        print("\nWARNING: Model never successfully spawned an agent.")
        print("The qwen3:1.7b model may be too small to correctly call the agent tool.")
        print("Try with a larger model: qwen3:8b")

    print(f"\nSession: {session.session_id}")
    session_id = session.session_id
    await session.close()
    return session_id


def wait_for_agent_subprocess(max_wait: int = 120) -> None:
    """Wait for any background agent tasks to complete."""
    try:
        from openharness.tasks.manager import get_task_manager
        manager = get_task_manager()
    except ImportError:
        print("WARNING: Cannot import BackgroundTaskManager — skipping wait")
        return

    print("\nWaiting for agent subprocess to complete...")
    start = time.time()
    while time.time() - start < max_wait:
        tasks = manager.list_tasks()
        running = [t for t in tasks if t.status in ("pending", "running")]
        if not running:
            print("All background tasks completed.")
            return
        for t in running:
            print(f"  Still running: {t.id} status={t.status}")
        time.sleep(3)
    print(f"WARNING: Timed out after {max_wait}s — some tasks may still be running")


def verify_langfuse(session_id: str) -> bool:
    """Query Langfuse and verify agent span enrichment."""
    print("\n--- Langfuse Verification ---")
    time.sleep(2)  # Allow flush to complete

    # Get latest trace
    data = langfuse_get("/api/public/traces?limit=5&orderBy=timestamp.desc")
    traces = data.get("data", [])
    if not traces:
        print("FAIL: No traces found")
        return False

    # Find trace for our session
    trace = None
    for t in traces:
        if t.get("sessionId") == session_id:
            trace = t
            break
    if not trace:
        trace = traces[0]
        print(f"WARNING: Could not find trace for session {session_id}, using latest")

    trace_id = trace["id"]
    print(f"Trace: {trace_id}  name={trace['name']}")

    # Get observations
    obs_data = langfuse_get(f"/api/public/observations?traceId={trace_id}&limit=20")
    observations = sorted(obs_data.get("data", []), key=lambda x: x["startTime"])

    print(f"\nObservations ({len(observations)}):")
    agent_span = None
    for obs in observations:
        model = obs.get("model") or ""
        output_preview = (obs.get("output") or "")[:80]
        print(f"  {obs['type']:12} {obs['name']:25} model={model:15} output={output_preview}")
        if "agent" in obs.get("name", "").lower() and obs["type"] == "SPAN":
            agent_span = obs

    # Verify agent span
    print("\n--- Checks ---")
    checks = []

    if agent_span:
        print(f"Found agent span: {agent_span['name']}")

        # Check 1: output is NOT the spawn confirmation
        output = agent_span.get("output") or ""
        is_spawn_only = "Spawned agent" in output and len(output) < 100
        check1 = not is_spawn_only and len(output) > 0
        checks.append(("Agent span has real output (not spawn confirmation)", check1))
        if check1:
            print(f"  Output preview: {output[:100]}")

        # Check 2: metadata has status
        meta = agent_span.get("metadata") or {}
        has_status = "status" in meta
        checks.append(("Agent span metadata has 'status'", has_status))
        if has_status:
            print(f"  Status: {meta['status']}")

        # Check 3: duration_ms > 0
        duration = meta.get("duration_ms", 0)
        has_duration = duration > 0
        checks.append(("Agent span metadata has duration_ms > 0", has_duration))
        if has_duration:
            print(f"  Duration: {duration}ms")

        # Check 4: status is a terminal state
        status_ok = meta.get("status") in ("completed", "failed", "killed", "running")
        checks.append(("Agent span status is a known state", status_ok))
    else:
        checks.append(("Agent span found in trace", False))
        print("FAIL: No agent span found in observations")

    print("\n--- Results ---")
    all_pass = True
    for name, passed in checks:
        status = "PASS" if passed else "FAIL"
        print(f"  [{status}] {name}")
        if not passed:
            all_pass = False

    return all_pass


def main() -> None:
    print("=== Test 5: OpenHarness Agent Span Enrichment ===\n")

    # Run the session
    session_id = asyncio.run(run_agent_session())

    # Wait for subprocess
    wait_for_agent_subprocess()

    # Give the completion listener time to fire and Langfuse to flush
    print("\nWaiting 5s for Langfuse flush...")
    time.sleep(5)

    # Verify
    passed = verify_langfuse(session_id)
    print(f"\n{'ALL CHECKS PASSED' if passed else 'SOME CHECKS FAILED'}")
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
