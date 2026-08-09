#!/usr/bin/env python3
"""Durable HITL probe — EJECT then REHYDRATE across processes (pre-07b durable).

Where the warm probe (`hitl-defer-resume-probe.py`) holds the pending call on an
in-memory future, this proves the DURABLE path: pause the tool call, **persist it
+ eject from memory + end the process**, then LATER (a fresh process) inject the
decision and let the exact call run. Nothing is held in memory between phases —
the two processes share only the on-disk `FileDeferStore` + the session DB.

Three phases, each a SEPARATE process invocation (that IS the proof):
  * --phase pass1  : drive a turn → the durable path records the pending call +
    ejects (Claude native `defer` → deferred_tool_use; OH/Codex deny-to-end) →
    the tool does NOT run, the turn ends, the process exits. Captures session_id.
  * --phase approve: the out-of-band approver writes a decision (allow/deny) into
    the store keyed by the recorded tool_use_id. (Simulates the human / an HTTP
    POST /tool_confirmation arriving much later, in a different process.)
  * --phase pass2  : a FRESH process resumes the session and the durable path
    injects the stored decision → the exact call runs (allow) or stays blocked
    (deny). Claude = native-defer exact-id re-fire; OH/Codex = re-drive + content
    pre-seed.

Per-cell mechanism (the honest mix):
  * claude    : native `defer` PreToolUse hook (config.safety.durable_defer) —
                exact-id inject. Covers built-in AND custom (one gate).
  * openharness/codex: DurableDeferHandler on can_use_tool — deny-to-end + resume
                (continue_pending / thread_resume) + content pre-seed (re-drive).
  * codex custom: N/A (pre-07 shipped the ungated fallback — no gate to defer at).

CLAUDE OAUTH: export the token AND strip the API key (see hitl-defer-resume-probe).

Usage (three SEPARATE processes — the cross-process proof):
    S=/tmp/durableproof
    OAUTH=... ; base="env -u ANTHROPIC_API_KEY CLAUDE_CODE_OAUTH_TOKEN=$OAUTH PYTHONPATH=. uv run --no-sync python warden/scripts/durable-defer-probe.py"
    $base --provider claude --case accept --base $S --phase pass1
    $base --provider claude --case accept --base $S --phase approve
    $base --provider claude --case accept --base $S --phase pass2
  # OpenHarness (free Ollama): --provider openharness --model qwen3:8b
  # Codex: env -u OPENAI_API_KEY ... --provider codex

Cost discipline: Claude/Codex OAuth, OpenHarness free Ollama. Never the API-key lane.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import stat
from pathlib import Path

from warden import (
    ChatAPI,
    CompletionEvent,
    ErrorEvent,
    SessionCreatedEvent,
)
from warden.config import get_harness_config
from warden.config.models import DurableDeferConfig
from warden.seams.defer import DurableDeferHandler
from warden.seams.defer_store import FileDeferStore

PROMPT = (
    "Create a file named out.txt in the current directory containing exactly the "
    "word hello. Use your file-writing tool. Do it now, no explanation."
)
RESUME_PROMPT = "Continue where we left off — proceed with the pending action now."


def _build_config(provider, model, store_root, session_db, base_task_dir, task_id):
    config = get_harness_config()
    config.provider.provider = provider
    config.provider.model = model
    config.permissions.allowed_tools = None
    config.permissions.denied_tools = None
    config.custom_tools.tools = []
    config.provider.codex_allow_ungated_custom_tools = True
    # Durable wiring differs per provider:
    if provider == "claude":
        # Native-defer PreToolUse hook is the gate (config-threaded, claude-only).
        config.safety.durable_defer = DurableDeferConfig(
            enabled=True, store_root=str(store_root),
        )
    else:
        # OH/Codex: the DurableDeferHandler on can_use_tool (deny-to-end + resume).
        config.permissions.handler_instance = DurableDeferHandler(
            FileDeferStore(store_root)
        )
    # Cross-process resume needs a shared session index + a stable workspace.
    config.persistence.session_db_path = str(session_db)
    config.workspace.base_dir = str(base_task_dir)
    config.workspace.user_id = "probe"
    config.workspace.task_id = task_id
    return config


def _make_writable(task_dir: Path) -> None:
    """Codex pins CODEX_HOME/.codex whose bundled git pack files are 0444; the
    persistence restore re-extracts over them and fails. Make the tree writable."""
    if not task_dir.exists():
        return
    for root, dirs, files in os.walk(task_dir):
        for name in dirs + files:
            p = Path(root) / name
            try:
                os.chmod(p, os.stat(p).st_mode | stat.S_IWUSR)
            except OSError:
                pass


async def _drive(config, run_dir, prompt, handler, session_id=None):
    """Run one turn; return (captured_session_id, errors)."""
    api = ChatAPI(config, repo_path=str(run_dir), workflow=None)
    await api.init()
    sid, errors = session_id, []
    try:
        async for event in api.send(prompt, session_id=session_id):
            if isinstance(event, SessionCreatedEvent):
                sid = event.session_id
                if handler is not None:
                    handler.session_id = sid
            elif isinstance(event, ErrorEvent):
                errors.append(event.text)
            elif isinstance(event, CompletionEvent):
                pass
    finally:
        await api.close()
    return sid, errors


async def run_pass1(provider, model, store_root, session_db, base_task, task_id,
                    run_dir, session_file):
    store = FileDeferStore(store_root)
    handler = None
    config = _build_config(provider, model, store_root, session_db, base_task, task_id)
    if provider != "claude":
        handler = config.permissions.handler_instance
    sid, errors = await _drive(config, base_task, PROMPT, handler)
    if sid:
        session_file.write_text(sid)
    pending = store.read_pending()
    print(f"[pass1] session={sid} pending={[p.tool_use_id for p in pending]} "
          f"ejected_action={getattr(handler, 'last_action', 'defer(hook)')} errors={errors[:1]}")
    return sid, pending


def run_approve(store_root, allow, session_file):
    store = FileDeferStore(store_root)
    pending = [p for p in store.read_pending() if p.status == "pending"]
    if not pending:
        print("[approve] no pending record — run pass1 first."); return False
    for rec in pending:
        store.resolve(rec.tool_use_id, allow=allow,
                      reason="" if allow else "denied by durable approver")
    print(f"[approve] resolved {[p.tool_use_id for p in pending]} allow={allow}")
    return True


async def run_pass2(provider, model, store_root, session_db, base_task, task_id,
                    run_dir, session_file, out_file):
    if not session_file.exists():
        print("[pass2] no session handle — run pass1 first."); return False, False
    session_id = session_file.read_text().strip()
    handler = None
    _make_writable(base_task)
    config = _build_config(provider, model, store_root, session_db, base_task, task_id)
    if provider != "claude":
        handler = config.permissions.handler_instance
    out_file.unlink(missing_ok=True)
    sid, errors = await _drive(config, base_task, RESUME_PROMPT, handler,
                               session_id=session_id)
    written = out_file.exists()
    print(f"[pass2] resumed_session={sid} inject_action="
          f"{getattr(handler, 'last_action', 'hook')} out.txt written={written} "
          f"errors={errors[:1]}")
    return True, written


def verdict(case, written):
    if case == "accept":
        return ("PASS (durable): approval injected across processes → out.txt written"
                if written else "FAIL: approved but out.txt absent")
    return ("PASS (durable): denial injected across processes → out.txt absent"
            if not written else "FAIL: denied but out.txt present")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", required=True, choices=["claude", "openharness", "codex"])
    ap.add_argument("--model", default=None)
    ap.add_argument("--base", required=True)
    ap.add_argument("--case", required=True, choices=["accept", "reject"])
    ap.add_argument("--phase", required=True, choices=["pass1", "approve", "pass2"])
    args = ap.parse_args()

    run_dir = (Path(args.base).resolve()) / f"{args.provider}-{args.case}"
    store_root = run_dir / "store"
    session_db = run_dir / "sessions.db"
    session_file = run_dir / "session.txt"
    base_task = run_dir / "ws"
    task_id = f"{args.provider}_{args.case}"
    out_file = base_task / "probe" / task_id / "out.txt"
    allow = args.case == "accept"

    if args.phase == "pass1":
        import shutil
        shutil.rmtree(run_dir, ignore_errors=True)
        run_dir.mkdir(parents=True, exist_ok=True)
        store_root.mkdir(parents=True, exist_ok=True)
        print(f"\n{'='*72}\nDURABLE DEFER PROBE  provider={args.provider} case={args.case} "
              f"phase=pass1 (eject)\n{'='*72}")
        await run_pass1(args.provider, args.model, store_root, session_db, base_task,
                        task_id, run_dir, session_file)
    elif args.phase == "approve":
        print(f"\n--- approve ({'ALLOW' if allow else 'DENY'}) ---")
        run_approve(store_root, allow, session_file)
    else:  # pass2
        print("\n--- pass2 (rehydrate + inject) ---")
        _, written = await run_pass2(args.provider, args.model, store_root, session_db,
                                     base_task, task_id, run_dir, session_file, out_file)
        print(f"\nVERDICT [{args.provider}/{args.case}]: {verdict(args.case, written)}\n")


if __name__ == "__main__":
    asyncio.run(main())
