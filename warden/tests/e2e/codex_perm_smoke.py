"""Codex SDK T4 — permission fail-closed smoke (the load-bearing C3 leg).

The open question: does the Codex SDK actually consult our approval handler and
BLOCK a denied exec/patch, or does the auto-reviewer approve it first?

VERIFIED FINDING (2026-07-18, openai-codex 0.144.4): ``ApprovalMode.auto_review``
AUTO-APPROVES exec/patch BEFORE our handler (handler fired 0x, file written). The
adapter therefore starts the thread under ``approval_policy=untrusted`` +
``approvals_reviewer=None`` so the handler is load-bearing. This driver proves it
end-to-end against the REAL CodexSdkSession:

  DENY case (a can_use_tool that DENIES the exec):
    (a) the approval handler FIRED for the exec (counter > 0),
    (b) the side effect did NOT happen (the target file was NOT written),
  ALLOW control (same tool allowed): the file IS written.

It LOGS the raw approval ``params`` so we nail the exact schema each run.

    python -m warden.tests.e2e.codex_perm_smoke

Exit 0 = T4 PASS (handler fires AND blocks). Non-zero = FAIL / auto-approved.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from warden.providers.codex.sdk_session import CodexSdkSession

DENY_TARGET = "codex_should_not_exist.txt"
ALLOW_TARGET = "codex_allow_control.txt"


class _Allow:
    behavior = "allow"


class _Deny:
    behavior = "deny"
    message = "denied by T4 policy"


async def _run_case(*, run_dir: Path, deny: bool, target: str, prompt: str):
    """Run one codex turn under a deny/allow policy. Returns
    (approval_fire_count, file_written, raw_params_seen)."""
    tgt = run_dir / target
    if tgt.exists():
        tgt.unlink()

    fires = {"n": 0}
    raw_params: list = []

    async def policy_cb(tool_name, tool_input, context):
        fires["n"] += 1
        raw_params.append((tool_name, dict(tool_input)))
        print(
            f"[codex-T4][callback] tool={tool_name} "
            f"input_keys={sorted(tool_input.keys())} "
            f"command={str(tool_input.get('command'))[:80]!r}",
            flush=True,
        )
        return _Allow() if not deny else _Deny()

    codex_home = os.environ.get("CODEX_HOME")
    api_key = os.environ.get("OPENAI_API_KEY")
    session = CodexSdkSession(
        repo_path=run_dir,
        can_use_tool=policy_cb,
        codex_home=Path(codex_home) if codex_home else None,
        auth_env={"OPENAI_API_KEY": api_key} if api_key else None,
    )
    try:
        await session.start()
        async for ev in session.send(prompt):
            kind = ev.get("kind") if isinstance(ev, dict) else None
            if kind == "text":
                t = ev.get("text", "").strip()
                if t:
                    print(f"[codex-T4][text] {t[:160]}", flush=True)
            elif kind == "error":
                print(f"[codex-T4][ERROR] {ev.get('text')}", flush=True)
    finally:
        await session.close()

    return fires["n"], tgt.exists(), raw_params


async def main() -> int:
    run_dir = Path("/tmp/codex_perm_smoke")
    run_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 66)
    print(" Codex SDK T4 — DENY an exec via the approval handler")
    print(" (adapter uses approval_policy=untrusted so the handler is consulted)")
    print("=" * 66)
    deny_prompt = (
        f"Run the shell command: echo written > {DENY_TARGET}\n"
        f"You MUST use your shell/exec tool to create that file. Do it now."
    )
    deny_fires, deny_written, deny_params = await _run_case(
        run_dir=run_dir, deny=True, target=DENY_TARGET, prompt=deny_prompt,
    )

    print("\n--- DENY case results ---")
    print(f"  (a) approval handler fired : {deny_fires} time(s)")
    print(f"  (b) file written           : {deny_written}  (want False)")
    print(f"  raw params seen            : {deny_params}")
    deny_ok = (deny_fires > 0) and (not deny_written)

    print("\n" + "=" * 66)
    print(" Codex SDK T4 ALLOW control — same exec allowed; file should be WRITTEN")
    print("=" * 66)
    allow_prompt = (
        f"Run the shell command: echo written > {ALLOW_TARGET}\n"
        f"You MUST use your shell/exec tool to create that file. Do it now."
    )
    allow_fires, allow_written, _ = await _run_case(
        run_dir=run_dir, deny=False, target=ALLOW_TARGET, prompt=allow_prompt,
    )
    print("\n--- ALLOW control results ---")
    print(f"  approval handler fired : {allow_fires} time(s)")
    print(f"  file written           : {allow_written}  (want True)")
    allow_ok = (allow_fires > 0) and allow_written

    print("\n" + "=" * 66)
    if deny_ok and allow_ok:
        print(" CODEX T4 RESULT: PASS — the SDK consults can_use_tool AND blocks a")
        print(" denied exec (file absent), and allows it when permitted. The Codex")
        print(" approval bridge FIRES and is LOAD-BEARING (untrusted policy).")
        print("=" * 66)
        return 0

    print(" CODEX T4 RESULT: FAIL")
    if deny_fires == 0:
        print("   - handler NEVER fired → auto-approved BEFORE our handler."
              " Re-check approval_policy (must be untrusted, no auto-reviewer).")
    if deny_written:
        print("   - SIDE EFFECT HAPPENED: file written despite deny.")
    if not allow_ok:
        print("   - ALLOW control did not write (model may not have used exec;"
              " retune the prompt).")
    print("=" * 66)
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
