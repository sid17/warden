#!/usr/bin/env python3
"""Durable-HITL probe — checkpoint-and-inject (pre-07b): pause at a tool call,
resume by INJECTING the decision keyed by ``tool_use_id``. **No nudge.**

Sibling of ``scripts/permission-gating-probe.py``. Where that probe asks *is the
gate consulted?*, this asks the M6 question: **can we pause a run at a tool call,
capture its id, and inject an allow/deny decision into THAT EXACT call** — with
the tool actually running (allow) or staying absent (deny)?

This probe drives the real mechanic (:class:`warden.seams.defer.DeferRegistry`),
not the retired nudge. The mechanic is **warm-hold checkpoint-and-inject**: the
permission consult parks on an ``asyncio.Future`` keyed by ``tool_use_id`` (the
turn's provider blocks there — the tool does NOT run); a concurrent controller
watches for the pending id, persists it to ``pending.json``, then ``resolve()``s
it to inject the decision. Because the exact call is *held* (never denied and
re-driven), allow runs THAT call with no re-generation, and two concurrent calls
get two ids resolved independently — the case the nudge could never handle.

Warm hold works where the provider awaits the async seam without deadlock:
**Claude** (``can_use_tool`` / the pre-07 custom-tool ``PreToolUse`` gate) and
**OpenHarness** (``PRE_TOOL_USE`` hook). **Codex cannot warm-hold** (its approval
bridge is a sync reader-thread call with a hard timeout) → its honest analogue is
decline-to-end + ``thread_resume`` with a content-matched pre-seed (re-drive);
that + the cross-process restart variant are covered by ``DeferRegistry.preseed``
and its hermetic tests (``tests/seams/test_defer.py``), not this live probe.

Authoritative signals (mirror the perm-probe discipline — do NOT read gating off
the tool_use event stream):
  1. did the run CAPTURE a pending tool with a NON-NULL tool_use_id? (pending.json)
  2. did the controller INJECT the decision by that id? (resolved list)
  3. SIDE EFFECT: does out.txt exist afterwards? (allow ⇒ yes, deny ⇒ no)
  4. multi: two ids captured + resolved independently ⇒ exactly one file.

CLAUDE OAUTH NOTE: export the token AND strip the API key:
    OAUTH_TOKEN="$(security find-generic-password -s 'Claude Code-credentials' -w \
        | python3 -c 'import json,sys; print(json.load(sys.stdin)["claudeAiOauth"]["accessToken"])')"

Usage (from repo root):
    S=/tmp/hitlproof
    # Claude — warm inject, accept/reject
    env -u ANTHROPIC_API_KEY CLAUDE_CODE_OAUTH_TOKEN="$OAUTH_TOKEN" PYTHONPATH=. \
        uv run --no-sync python warden/scripts/hitl-defer-resume-probe.py \
        --provider claude --case accept --base $S
    # OpenHarness — free Ollama
    PYTHONPATH=. uv run --no-sync python warden/scripts/hitl-defer-resume-probe.py \
        --provider openharness --model qwen3:8b --case reject --base $S
    # Multi-approval (two calls → two ids → allow one / deny the other)
    env -u ANTHROPIC_API_KEY CLAUDE_CODE_OAUTH_TOKEN="$OAUTH_TOKEN" PYTHONPATH=. \
        uv run --no-sync python warden/scripts/hitl-defer-resume-probe.py \
        --provider claude --case multi --base $S

Cost discipline: Claude+Codex OAuth (prefix ``env -u ANTHROPIC_API_KEY`` /
``env -u OPENAI_API_KEY``); OpenHarness free Ollama qwen3:8b. Never the API-key
lane. Stray ``Langfuse 401`` lines are harmless telemetry noise.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from warden import (
    ChatAPI,
    CompletionEvent,
    ErrorEvent,
    SessionCreatedEvent,
)
from warden.config import get_harness_config
from warden.seams.defer import DeferRegistry, PendingCall


PROMPT_ONE = (
    "Create a file named out.txt in the current directory containing exactly the "
    "word hello. Use your file-writing tool. Do it now, no explanation."
)
PROMPT_TWO = (
    "Create two files in the current directory: a.txt containing the word alpha, "
    "and b.txt containing the word bravo. Use your file-writing tool for each. "
    "Do it now, no explanation."
)


def _build_config(provider, model, handler, run_dir):
    config = get_harness_config()
    config.provider.provider = provider
    config.provider.model = model
    # Leave allow/deny lists empty so the built-in write tool falls through to the
    # can_use_tool seam (an explicit allow would let the Claude SDK shadow it).
    config.permissions.allowed_tools = None
    config.permissions.denied_tools = None
    config.permissions.handler_instance = handler
    config.custom_tools.tools = []
    return config


async def _controller(reg: DeferRegistry, decide, done: asyncio.Event, resolved: list):
    """Watch for parked consults and INJECT decisions by id (no nudge).

    ``decide(pending_call) -> bool`` returns allow?. Deciding on the call's INPUT
    (not arrival order) is what makes multi-approval robust: a denied call the
    model RETRIES arrives with a NEW id but the same target, so a content-based
    decision denies it again deterministically.
    """
    seen: set[str] = set()

    def _sweep():
        for tuid in reg.pending_ids():
            if tuid in seen:
                continue
            pc = reg.get_pending(tuid)
            seen.add(tuid)
            allow = decide(pc)
            reg.resolve(tuid, allow=allow)
            resolved.append((tuid, pc.tool_name, allow))

    while not done.is_set():
        _sweep()
        await asyncio.sleep(0.02)
    _sweep()  # final sweep for a consult parked just before completion


async def run_warm(provider, model, run_dir, prompt, decide, pending_path):
    """Drive one turn; the DeferRegistry parks at each tool call; a controller
    injects the decision by id. Returns (captured_ids, resolved, errors)."""
    captured: list[dict] = []

    def _persist(pc: PendingCall) -> None:
        captured.append({
            "tool_use_id": pc.tool_use_id,
            "tool_name": pc.tool_name,
            "tool_input": pc.tool_input,
        })
        pending_path.write_text(json.dumps(captured, indent=2, default=str))

    reg = DeferRegistry(on_pending=_persist)
    config = _build_config(provider, model, reg, run_dir)
    api = ChatAPI(config, repo_path=str(run_dir), workflow=None)
    await api.init()

    done = asyncio.Event()
    resolved: list = []
    controller = asyncio.create_task(_controller(reg, decide, done, resolved))
    errors: list[str] = []
    try:
        async for event in api.send(prompt):
            if isinstance(event, SessionCreatedEvent):
                pass
            elif isinstance(event, ErrorEvent):
                errors.append(event.text)
            elif isinstance(event, CompletionEvent):
                pass
    finally:
        done.set()
        await controller
        await api.close()
    return captured, resolved, errors


def verdict(case, captured, resolved, files):
    ids = [c for c in captured if c.get("tool_use_id")]
    if not ids:
        return "INCONCLUSIVE (no tool consult parked — model never called the tool)"
    if not resolved:
        return "PARTIAL (captured a pending call but never injected a decision)"
    if case == "accept":
        return ("PASS (warm inject): allow → tool ran, out.txt written"
                if files.get("out.txt") else
                "FAIL: injected ALLOW but out.txt absent")
    if case == "reject":
        return ("PASS (warm inject): deny → tool did NOT run, out.txt absent"
                if not files.get("out.txt") else
                "FAIL: injected DENY but out.txt present")
    # multi: allow a.txt (order 0), deny b.txt (order 1) — exactly one file.
    a, b = files.get("a.txt"), files.get("b.txt")
    if len(ids) < 2:
        return f"PARTIAL (multi needs 2 parked calls; captured {len(ids)})"
    return ("PASS (multi warm inject): 2 ids resolved independently → a.txt present, b.txt absent"
            if (a and not b) else
            f"CHECK: a.txt={a} b.txt={b} (expected a present, b absent) — ids={[i['tool_use_id'] for i in ids]}")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", required=True,
                    choices=["claude", "openharness", "codex"])
    ap.add_argument("--model", default=None)
    ap.add_argument("--base", required=True, help="base scratchpad dir")
    ap.add_argument("--case", required=True, choices=["accept", "reject", "multi"])
    args = ap.parse_args()

    if args.provider == "codex":
        print("NOTE: Codex cannot warm-hold (sync approval bridge). Its re-drive "
              "path (decline-to-end + thread_resume + content pre-seed) is covered "
              "by tests/seams/test_defer.py::test_preseed_short_circuits_redrive_by_content. "
              "Skipping the live warm probe for codex.")
        return

    run_dir = (Path(args.base).resolve()) / f"{args.provider}-{args.case}"
    import shutil
    shutil.rmtree(run_dir, ignore_errors=True)
    run_dir.mkdir(parents=True, exist_ok=True)
    pending_path = run_dir / "pending.json"

    if args.case == "multi":
        prompt = PROMPT_TWO
        # Per-TARGET decision (robust to the model retrying a denied call under a
        # new id): allow the write targeting a.txt, deny the one targeting b.txt.
        def decide(pc):
            blob = json.dumps(pc.tool_input, default=str)
            return "b.txt" not in blob  # allow a.txt, deny b.txt (and its retries)
        watch = ["a.txt", "b.txt"]
    else:
        prompt = PROMPT_ONE
        allow_all = args.case == "accept"
        def decide(pc):
            return allow_all
        watch = ["out.txt"]

    print(f"\n{'='*72}\nHITL CHECKPOINT-AND-INJECT PROBE  provider={args.provider} "
          f"case={args.case}\n  run_dir={run_dir}\n{'='*72}")

    captured, resolved, errors = await run_warm(
        args.provider, args.model, run_dir, prompt, decide, pending_path,
    )
    files = {name: (run_dir / name).exists() for name in watch}

    print(f"[capture] parked ids = {[c.get('tool_use_id') for c in captured]}")
    print(f"[inject]  resolved   = {resolved}")
    print(f"[effect]  files      = {files}  errors={errors[:1]}")
    print(f"\nVERDICT [{args.provider}/{args.case}]: {verdict(args.case, captured, resolved, files)}\n")


if __name__ == "__main__":
    asyncio.run(main())
