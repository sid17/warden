"""OpenHarness permission + custom-tool free-lane smoke (Ollama, no spend).

Runs REAL OpenHarness turns through the REAL orchestrator against a local Ollama
model and proves the finalized-contract legs:

  (1) NAME-LEVEL enforcement — a can_use_tool that denies the mutating tool by
      NAME blocks it (the file is NOT written): fail-closed.
  (2) ARG/PATH-LEVEL enforcement (B15 CLOSED) — deny a SPECIFIC PATH (not just a
      tool name); the PRE_TOOL_USE hook now sees the REAL {path, content} at the
      seam and BLOCKS the write to that path. An allow-path control writes.
  (3) CUSTOM TOOL — register a trivial custom tool and force the model to call it;
      assert the handler executed.

Requires a reachable Ollama. Configure via env:
    OPENHARNESS_BASE_URL (default http://localhost:11434)
    OPENHARNESS_MODEL    (default qwen3:8b — better tool-calling than 1.7b)

    python -m warden.tests.e2e.openharness_perm_smoke

Exit 0 iff every conclusive leg passes.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

from warden.orchestrator.orchestrator import Orchestrator
from warden.orchestrator.session.manager import SessionManager
from warden.schemas.events import MessageEvent
from warden.seams.custom_tools import CustomTool

WRITE_TARGET = "oh_should_not_exist.txt"
DENIED_PATH = "denied_dir/blocked.txt"
ALLOWED_PATH = "allowed_dir/ok.txt"

_WRITE_TOOLS = ("write_file", "Write")


def _path_of(tool_input: dict) -> str:
    """Best-effort path field across write-tool shapes."""
    return str(tool_input.get("path") or tool_input.get("file_path") or "")


async def _run(policy, run_dir: Path, prompt: str, custom_tools=None):
    """Run one OpenHarness turn under ``policy(tool_name, tool_input)->bool``.

    Returns (received_inputs, events). ``policy`` returns True to ALLOW.
    """
    sm = SessionManager()
    await sm.init()
    orch = Orchestrator(
        session_manager=sm, repo_path=run_dir, custom_tools=custom_tools
    )

    received_inputs: list[tuple[str, dict]] = []

    async def observing_cb(tool_name, tool_input, context):
        received_inputs.append((tool_name, dict(tool_input)))
        if policy(tool_name, dict(tool_input)):
            return PermissionResultAllow(behavior="allow")
        return PermissionResultDeny(behavior="deny", message="denied by policy")

    orch._can_use_tool = observing_cb  # type: ignore[assignment]

    events: list = []
    try:
        async for ev in orch.send_message(prompt, provider="openharness"):
            events.append(ev)
            if isinstance(ev, MessageEvent) and ev.kind in ("tool_use",):
                print(f"[OH][tool_use] {ev.content}", flush=True)
    finally:
        await orch.close()
        await sm.close_all()
        await sm.close_index()

    return received_inputs, events


async def main() -> int:
    os.environ.setdefault("OPENHARNESS_MODEL", "qwen3:8b")
    run_dir = Path("/tmp/oh_perm_smoke")
    run_dir.mkdir(parents=True, exist_ok=True)
    for rel in (WRITE_TARGET, DENIED_PATH, ALLOWED_PATH):
        p = run_dir / rel
        if p.exists():
            p.unlink()

    base = os.environ.get("OPENHARNESS_BASE_URL", "http://localhost:11434")
    print("=" * 70)
    print(" OpenHarness perm+tools free-lane smoke — model=%s base=%s" % (
        os.environ.get("OPENHARNESS_MODEL"), base,
    ))
    print("=" * 70)

    ok = True

    # --- (1) NAME-LEVEL deny: block write_file by name → file must NOT exist ---
    print("\n[1] NAME-LEVEL deny of write_file (should block; file absent)")
    prompt1 = (
        f"Create a file named {WRITE_TARGET} containing the text 'x' using the "
        f"write_file tool. Just do it, no explanation."
    )
    inputs1, _ = await _run(
        lambda name, _inp: name not in _WRITE_TOOLS, run_dir, prompt1
    )
    saw_write1 = any(n in _WRITE_TOOLS for n, _ in inputs1)
    written1 = (run_dir / WRITE_TARGET).exists()
    print(f"    tool calls seen: {[t for t, _ in inputs1]}  file written: {written1}")
    if not saw_write1:
        print("    [1] INCONCLUSIVE — model never called write_file (retune/bigger model).")
    elif written1:
        ok = False
        print("    [1] FAIL — file written despite name-level deny!")
    else:
        print("    [1] PASS — write_file denied by name, file absent.")

    # --- (2) ARG/PATH-LEVEL (B15): deny a SPECIFIC PATH, see REAL tool_input ---
    print("\n[2] ARG/PATH-LEVEL deny of a specific path (B15 — seam sees {path})")
    prompt2 = (
        f"Write the text 'blocked' to the file {DENIED_PATH} using the write_file "
        f"tool. Just do it, no explanation."
    )

    def deny_path(name, tool_input):
        # Allow everything EXCEPT a write whose path is the denied path.
        if name in _WRITE_TOOLS and DENIED_PATH in _path_of(tool_input):
            return False
        return True

    inputs2, _ = await _run(deny_path, run_dir, prompt2)
    write_inputs2 = [inp for n, inp in inputs2 if n in _WRITE_TOOLS]
    saw_real_path = any(DENIED_PATH in _path_of(inp) for inp in write_inputs2)
    denied_written = (run_dir / DENIED_PATH).exists()
    print(f"    write tool_input seen at seam: {write_inputs2}")
    print(f"    denied path written: {denied_written}  (want False)")
    if not write_inputs2:
        print("    [2] INCONCLUSIVE — model never called write_file.")
    elif not saw_real_path:
        ok = False
        print("    [2] FAIL/B15-REGRESSION — seam did NOT see the real path "
              f"{DENIED_PATH!r} (got {write_inputs2}).")
    elif denied_written:
        ok = False
        print("    [2] FAIL — denied path was written despite arg-level deny!")
    else:
        print("    [2] PASS — real {path} at the seam AND blocked (B15 closed).")

    # --- (2b) allow-path control: same policy, a DIFFERENT path must write -----
    print("\n[2b] ALLOW-PATH control (a non-denied path should write)")
    prompt2b = (
        f"Write the text 'fine' to the file {ALLOWED_PATH} using the write_file "
        f"tool. Just do it, no explanation."
    )
    inputs2b, _ = await _run(deny_path, run_dir, prompt2b)
    saw_write2b = any(n in _WRITE_TOOLS for n, _ in inputs2b)
    allowed_written = (run_dir / ALLOWED_PATH).exists()
    print(f"    allowed path written: {allowed_written}  (want True)")
    if not saw_write2b:
        print("    [2b] INCONCLUSIVE — model never called write_file.")
    elif not allowed_written:
        ok = False
        print("    [2b] FAIL — allowed path was NOT written (over-blocking?).")
    else:
        print("    [2b] PASS — allowed path written under the same arg policy.")

    # --- (3) CUSTOM TOOL: register + force a trivial tool, assert executed -----
    print("\n[3] CUSTOM TOOL — register + force a trivial tool")
    calls: list[str] = []

    def _ping(text: str = "") -> str:
        calls.append(text)
        return f"pong:{text}"

    ping_tool = CustomTool(
        name="ping_tool",
        description="Return pong for the given text. Call this to answer.",
        input_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        handler=_ping,
    )
    prompt3 = (
        "Call the ping_tool with text set to 'hi' and report its result. "
        "You MUST use the ping_tool tool."
    )
    inputs3, _ = await _run(
        lambda *_: True, run_dir, prompt3, custom_tools=[ping_tool]
    )
    saw_tool_call = any(n == "ping_tool" for n, _ in inputs3)
    executed = len(calls) > 0
    print(f"    tool calls seen: {[t for t, _ in inputs3]}  handler runs: {calls}")
    if not saw_tool_call and not executed:
        print("    [3] INCONCLUSIVE — model never called ping_tool (retune/bigger model).")
    elif not executed:
        ok = False
        print("    [3] FAIL — ping_tool was invoked but the handler did not run.")
    else:
        print("    [3] PASS — custom tool registered and executed.")

    print("\n" + "=" * 70)
    print(" RESULT:", "PASS" if ok else "FAIL")
    print("=" * 70)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
