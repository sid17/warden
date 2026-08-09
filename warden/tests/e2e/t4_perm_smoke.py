"""T4 — Claude SDK permission-bridge e2e smoke (the load-bearing D7 leg).

The open question this answers: does the Claude Agent SDK actually INVOKE
``can_use_tool`` and BLOCK a denied tool end-to-end, or is it a silent no-op?

Every existing deny test (``tests/core/test_orchestrator_errors.py:241-337``)
calls ``Orchestrator._can_use_tool(...)`` DIRECTLY — a unit invocation. None of
them prove the SDK *reaches* the callback for a real turn. This driver closes
that gap by running the REAL ``ClaudeSDKClient`` through the REAL orchestrator
and instrumenting the callback with a live counter.

Isolation of the callback from ``disallowed_tools``:
  The orchestrator normally passes BOTH a ``disallowed_tools`` list (SDK-native
  block) AND the ``can_use_tool`` callback. To prove the *callback* is
  load-bearing (not the native list masking it), we force ``disallowed_tools``
  to STAY EMPTY and let the checker/callback be the ONLY thing that can block.
  If Write is blocked with an empty disallowed_tools list, the block can ONLY
  have come from the callback.

Three assertions for the DENY case:
  (a) callback FIRED for the denied tool (counter > 0, real, not a stub),
  (b) the side effect did NOT happen (the target file was NOT written),
  (c) the stream carried the denial (a ToolAccessNotificationEvent action=denied
      and/or the model re-planned / mentioned it could not write).

Plus the ALLOW control (same tool, allowed → file IS written).

Run inside the Docker bed with a real Claude credential:
    python -m warden.tests.e2e.t4_perm_smoke

Exit 0 = T4 PASS (SDK enforces via the callback). Non-zero = FAIL / no-op.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from warden.orchestrator.orchestrator import Orchestrator
from warden.orchestrator.session.manager import SessionManager
from warden.schemas.events import (
    CompletionEvent,
    ErrorEvent,
    MessageEvent,
    ToolAccessNotificationEvent,
)
from warden.safety.permissions.checker import (
    PermissionChecker,
)
from warden.workspace.workflow.permissions import (
    Permissions,
    ToolAccess,
)

TARGET_NAME = "t4_should_not_exist.txt"
ALLOW_NAME = "t4_allow_control.txt"


async def _run_case(
    *,
    repo_path: Path,
    deny_write: bool,
    target_file: Path,
    prompt: str,
) -> tuple[int, list, bool]:
    """Run one turn against the real SDK. Returns (callback_fire_count_for_write,
    events, file_written)."""
    sm = SessionManager()
    await sm.init()

    orch = Orchestrator(session_manager=sm, repo_path=repo_path)

    # Install the checker we want. For deny: a workflow that denies Write via
    # tool_access.deny + AUTO mode (so nothing else needs confirmation and the
    # ONLY block is the deny rule, reached through the callback). For allow: AUTO
    # mode with no deny (Write auto-allowed).
    if deny_write:
        perms = Permissions(mode="auto", tool_access=ToolAccess(deny=["Write"]))
        orch._permission_checker = PermissionChecker.from_workflow_permissions(perms)
    else:
        perms = Permissions(mode="auto")
        orch._permission_checker = PermissionChecker.from_workflow_permissions(perms)

    # CRITICAL ISOLATION: keep the SDK-native disallowed_tools EMPTY, so a block
    # can ONLY come from the callback. compute_deny_baseline() is [] here anyway
    # (no .workflows dir), but force it to be certain.
    orch._deny_baseline = []

    # Instrument the REAL _can_use_tool with a live counter (not a stub — we call
    # through to the genuine method).
    real_cb = orch._can_use_tool
    fire_counts: dict[str, int] = {}

    async def counting_cb(tool_name, tool_input, context):
        fire_counts[tool_name] = fire_counts.get(tool_name, 0) + 1
        result = await real_cb(tool_name, tool_input, context)
        print(
            f"[T4][callback] tool={tool_name} "
            f"behavior={getattr(result, 'behavior', '?')} "
            f"input_keys={sorted(tool_input.keys())}",
            flush=True,
        )
        return result

    orch._can_use_tool = counting_cb  # type: ignore[assignment]

    events: list = []
    try:
        async for ev in orch.send_message(prompt, provider="claude"):
            events.append(ev)
            if isinstance(ev, ToolAccessNotificationEvent):
                print(
                    f"[T4][stream] ToolAccessNotification tool={ev.tool_name} "
                    f"action={ev.action} reason={ev.reason}",
                    flush=True,
                )
            elif isinstance(ev, MessageEvent) and ev.kind == "text":
                txt = ev.content.get("text", "")
                if txt.strip():
                    print(f"[T4][stream][text] {txt.strip()[:200]}", flush=True)
            elif isinstance(ev, ErrorEvent):
                print(f"[T4][stream][ERROR] {ev.text}", flush=True)
    finally:
        await orch.close()
        await sm.close_all()
        await sm.close_index()

    file_written = target_file.exists()
    return fire_counts.get("Write", 0), events, file_written


def _denied_in_stream(events: list) -> bool:
    for ev in events:
        if isinstance(ev, ToolAccessNotificationEvent) and ev.action == "denied":
            return True
    return False


async def main() -> int:
    run_dir = Path("/tmp/t4_perm_smoke")
    run_dir.mkdir(parents=True, exist_ok=True)

    # --- DENY case ---------------------------------------------------------
    deny_target = run_dir / TARGET_NAME
    if deny_target.exists():
        deny_target.unlink()
    deny_prompt = (
        f"Create a file named {TARGET_NAME} in the current directory "
        f"with the text 'written'. Use the Write tool. Do it now."
    )
    print("=" * 66)
    print(" T4 DENY case — workflow denies Write; disallowed_tools EMPTY")
    print(" A block here can ONLY come from the can_use_tool callback.")
    print("=" * 66)
    deny_fires, deny_events, deny_written = await _run_case(
        repo_path=run_dir,
        deny_write=True,
        target_file=deny_target,
        prompt=deny_prompt,
    )
    deny_denied_stream = _denied_in_stream(deny_events)
    has_completion = any(isinstance(e, CompletionEvent) for e in deny_events)

    print("\n--- DENY case results ---")
    print(f"  (a) callback fired for Write : {deny_fires} time(s)")
    print(f"  (b) file written (side effect): {deny_written}  (want False)")
    print(f"  (c) denial in stream          : {deny_denied_stream}")
    print(f"      turn completed            : {has_completion}")

    deny_ok = (deny_fires > 0) and (not deny_written) and deny_denied_stream

    # --- ALLOW control -----------------------------------------------------
    allow_target = run_dir / ALLOW_NAME
    if allow_target.exists():
        allow_target.unlink()
    allow_prompt = (
        f"Create a file named {ALLOW_NAME} in the current directory "
        f"with the text 'written'. Use the Write tool. Do it now."
    )
    print()
    print("=" * 66)
    print(" T4 ALLOW control — same tool allowed; Write should RUN")
    print("=" * 66)
    allow_fires, allow_events, allow_written = await _run_case(
        repo_path=run_dir,
        deny_write=False,
        target_file=allow_target,
        prompt=allow_prompt,
    )
    print("\n--- ALLOW control results ---")
    print(f"  callback fired for Write : {allow_fires} time(s)")
    print(f"  file written             : {allow_written}  (want True)")

    allow_ok = (allow_fires > 0) and allow_written

    # --- Verdict -----------------------------------------------------------
    print()
    print("=" * 66)
    if deny_ok and allow_ok:
        print(" T4 RESULT: PASS — SDK invokes can_use_tool AND blocks a denied")
        print(" tool (file absent), and allows it when permitted. The permission")
        print(" bridge FIRES and is LOAD-BEARING on the Claude SDK.")
        print("=" * 66)
        return 0

    print(" T4 RESULT: FAIL")
    if not deny_ok:
        if deny_fires == 0:
            print("   - callback NEVER fired for Write → SILENT NO-OP (SDK did not"
                  " reach can_use_tool).")
        if deny_written:
            print("   - SIDE EFFECT HAPPENED: file was written despite deny.")
        if not deny_denied_stream:
            print("   - no denial surfaced in the stream.")
    if not allow_ok:
        print("   - ALLOW control did not write the file (control invalid — the"
              " model may not have attempted Write; retune the prompt).")
    print("=" * 66)
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
