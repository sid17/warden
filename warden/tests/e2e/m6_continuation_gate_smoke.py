"""Repro cell — the durable landscape gate under the B1 CONTINUATION hook, over
MULTIPLE gate cycles (the untested interaction behind the live course failure).

Live bug (run ``44622262…``): a course run with ``LEARNING_DURABLE_GATE=1`` gated
``confirm_landscape`` under the B1 continuation hook (``until_tool=course_complete``).
The model proposed the landscape THREE times (a wandering loop the continuation hook
re-prompts into); each ``confirm_landscape`` was approved. Gates 1 & 2 resumed fine —
gate 3's resume returned ``result:""`` with ``input_tokens:0`` (NO model call), so the
run ended before ``course_complete`` → no ``draft_manifest`` → the product surfaced
"Creation failed — result event but no draft_manifest to bridge".

The existing ``--m6-gate`` cells prove the gate transport but NEVER run the continuation
hook, so this interaction is untested. This cell rebuilds exactly it, minimally:

- durable_http + a ``mode:auto, confirm:[confirm_landscape]`` gate manifest;
- the B1 continuation hook ON (``until_tool=course_complete``);
- TWO recording custom tools — ``confirm_landscape`` (the gate) + ``course_complete``
  (the completion tool the continuation hook waits for);
- a prompt that makes the model call ``confirm_landscape`` several times (each pausing)
  then ``course_complete``;
- the driver APPROVES every pause, then asserts the run converges (``course_complete``
  fired, no empty resume) — it FAILS (reproduces the bug) when an Nth resume goes empty.

Real Claude subprocess, OAuth / free lane (never the API-key lane). Claude-only (07b).
Run in-image or on host:
``python -m warden.tests.e2e.m6_continuation_gate_smoke [n_proposals]`` → 0=PASS, 1=FAIL(repro).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
from pathlib import Path

import httpx

from warden import CustomTool
from warden.harness_api.app import create_app
from warden.harness_api.config import HarnessApiConfig
from warden.harness_api.runner import Runner

USER = "probe"
GATE_TOOL = "confirm_landscape"
DONE_TOOL = "course_complete"

# mode:auto ⇒ auto-allow everything EXCEPT the confirm-listed gate tool.
_GATE_MANIFEST = """\
name: gate
description: landscape gate — confirm_landscape only
permissions:
  mode: auto
  tool_access:
    confirm: [confirm_landscape]
"""


def _prompt(n: int) -> str:
    # The live-bug trigger (per claude-code #77313 / #76807 and SDK #1190): background
    # ``Task``/``Agent`` SUBAGENTS in flight + interrupt() on the gate defer + resume.
    # Force delegated subagents between gates (NOT bare WebSearch) so the resume lands
    # in the known interrupt-mid-deferred-tool + delegated-task-settles-before-Result
    # window — the state that produces the zero-message empty resume.
    return (
        "You are authoring a short course. Follow these steps EXACTLY, in order, and "
        "call the tools — do NOT answer in prose:\n"
        f"1. Use the Task tool to launch TWO research subagents IN THE BACKGROUND to "
        "research the course topic (one per angle). Then, while/after they run, call "
        f"{GATE_TOOL} with a 'concepts' array of 2-3 short concept strings (your "
        "proposed landscape). Wait for the operator to approve.\n"
        f"2. After approval, launch ANOTHER background Task research subagent to "
        f"refine, then call {GATE_TOOL} AGAIN with a DIFFERENT 'concepts' array. "
        f"Repeat so that you call {GATE_TOOL} a total of {n} times, each preceded by a "
        "fresh background Task subagent and each waiting for approval.\n"
        f"3. ONLY after the {n}th {GATE_TOOL} is approved, call {DONE_TOOL} with a "
        f"'title' string to finish. {DONE_TOOL} MUST be the final tool you call.\n"
        f"Do not stop until you have called {DONE_TOOL}."
    )


def _place_manifest(base_dir: Path, task_id: str) -> None:
    wf_dir = base_dir / USER / task_id / ".workflows"
    wf_dir.mkdir(parents=True, exist_ok=True)
    (wf_dir / "gate.yaml").write_text(_GATE_MANIFEST)


def _recording_tool(name: str, record: Path, key: str, schema: dict) -> CustomTool:
    """A custom tool that appends one JSON line per call (so the driver can count
    calls and confirm the completion tool fired)."""

    def handler(**kwargs) -> str:
        with record.open("a") as fh:
            fh.write(json.dumps({"tool": name, key: kwargs.get(key)}) + "\n")
        return f"{name} recorded"

    return CustomTool(
        name=name,
        description=f"Record a {name} call.",
        input_schema=schema,
        handler=handler,
    )


def _tools(record: Path) -> list[CustomTool]:
    return [
        _recording_tool(
            GATE_TOOL, record, "concepts",
            {"type": "object",
             "properties": {"concepts": {"type": "array",
                                         "items": {"type": "string"}}},
             "required": ["concepts"]},
        ),
        _recording_tool(
            DONE_TOOL, record, "title",
            {"type": "object",
             "properties": {"title": {"type": "string"}},
             "required": ["title"]},
        ),
    ]


def _build_runner(run_dir: Path, record: Path) -> Runner:
    cfg = HarnessApiConfig()
    cfg.engine.permissions.handler = "durable_http"
    cfg.engine.provider.provider = "claude"
    cfg.engine.persistence.session_db_path = str(run_dir / "sessions.db")
    cfg.engine.workspace.base_dir = str(run_dir / "ws")
    cfg.engine.workspace.user_id = USER
    cfg.engine.custom_tools.tools = _tools(record)
    # THE INGREDIENT the existing gate cells lack: the B1 continuation hook, waiting
    # for course_complete — exactly the live course config.
    cfg.engine.continuation.enabled = True
    cfg.engine.continuation.until_tool = DONE_TOOL
    cfg.governance.enabled = False  # ungoverned ⇒ auth inherits the process cred
    cfg.hitl.sla_seconds = 3600.0
    return Runner(cfg)


async def _poll(client, run_id, targets, tries=800):
    status = None
    for _ in range(tries):
        status = (await client.get(f"/runs/{run_id}")).json()["status"]
        if status in targets:
            return status
        await asyncio.sleep(0.25)
    return status


async def _history(client, run_id) -> list[dict]:
    return (await client.get(f"/runs/{run_id}/history")).json()


async def _first_pending(client, run_id, seen: set[str]):
    for _ in range(800):
        status = await _poll(client, run_id, {"requires_action", "succeeded", "error"})
        if status != "requires_action":
            return None, status
        asks = [e for e in await _history(client, run_id)
                if e["type"] == "permission_request"]
        fresh = [a for a in asks if a["data"]["tool_use_id"] not in seen]
        if fresh:
            return fresh[-1], status
        await asyncio.sleep(0.25)
    return None, "timeout"


def _turn_input_tokens(hist: list[dict]) -> list[int]:
    """Per-resume input-token counts from the run's result/token events — a 0 on a
    non-first turn is the empty-resume signature (no model call happened)."""
    toks: list[int] = []
    for e in hist:
        if e["type"] in ("result", "token"):
            usage = e["data"].get("usage") or {}
            if "input" in usage:
                toks.append(usage["input"])
    return toks


async def run_repro(run_dir: Path, n_proposals: int) -> dict:
    task_id = "cont_gate"
    record = run_dir / "tool_calls.jsonl"
    _place_manifest(run_dir / "ws", task_id)
    runner = _build_runner(run_dir, record)
    app = create_app(runner)
    transport = httpx.ASGITransport(app=app)
    trace: list[str] = []
    seen: set[str] = set()
    paused_tools: list[str] = []

    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://cont",
                                     timeout=240.0) as client:
            resp = await client.post("/runs", json={
                "user_id": USER, "task_id": task_id, "provider": "claude",
                "input": {"prompt": _prompt(n_proposals), "workflow": "gate"},
                "sink": {"type": "sse"},
            })
            if resp.status_code != 202:
                return {"trace": [f"POST /runs {resp.status_code}"], "ok": False}
            run_id = resp.json()["run_id"]
            trace.append(f"run_id={run_id}")

            # Approve EVERY confirm_landscape pause until the run is terminal.
            to_approve = None
            for i in range(n_proposals + 6):  # headroom for wandering re-proposals
                if to_approve is None:
                    ask, status = await _first_pending(client, run_id, seen)
                    if ask is None:
                        trace.append(f"terminal before pause #{i} (status={status})")
                        break
                    to_approve = ask["data"]["tool_use_id"]
                    paused_tools.append(ask["data"]["tool_name"])
                    trace.append(f"pause[{ask['data']['tool_name']}] "
                                 f"round={ask['data'].get('revise_round')}")
                await client.post(f"/runs/{run_id}/tool_confirmation", json={
                    "tool_use_id": to_approve, "decision": "approve",
                })
                seen.add(to_approve)
                to_approve = None
                nxt, status = await _first_pending(client, run_id, seen)
                if nxt is None:
                    trace.append(f"reached terminal status={status}")
                    break
                to_approve = nxt["data"]["tool_use_id"]
                paused_tools.append(nxt["data"]["tool_name"])
                trace.append(f"pause[{nxt['data']['tool_name']}] "
                             f"round={nxt['data'].get('revise_round')}")

            final = await _poll(client, run_id, {"succeeded", "error"})
            hist = await _history(client, run_id)
            input_tokens = _turn_input_tokens(hist)
            # The terminal result text (empty on the empty-resume bug).
            results = [e for e in hist if e["type"] == "result"]
            final_result = results[-1]["data"].get("result", "") if results else "(none)"
            errs = [e for e in hist if e["type"] in ("error", "stopped")]
            err_reason = errs[-1]["data"].get("reason") if errs else None

        calls = ([json.loads(ln) for ln in record.read_text().splitlines() if ln]
                 if record.exists() else [])
        gate_calls = [c for c in calls if c["tool"] == GATE_TOOL]
        done_calls = [c for c in calls if c["tool"] == DONE_TOOL]
        # An empty-resume = a non-first turn whose input_tokens == 0.
        empty_resume = any(t == 0 for t in input_tokens[1:]) if input_tokens else False
        trace.append(f"final={final} gate_calls={len(gate_calls)} "
                     f"done_calls={len(done_calls)} input_tokens/turn={input_tokens} "
                     f"final_result={final_result!r} err={err_reason!r}")
        return {
            "trace": trace, "final": final, "paused_tools": paused_tools,
            "gate_calls": len(gate_calls), "done_calls": len(done_calls),
            "input_tokens": input_tokens, "empty_resume": empty_resume,
            "final_result": final_result, "err_reason": err_reason,
            # PASS = the completion tool fired and the run converged with no empty resume.
            "ok": len(done_calls) >= 1 and final == "succeeded" and not empty_resume,
        }
    finally:
        await runner.aclose()


async def main(n_proposals: int, base: str) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    print("=" * 72)
    print(f" M6 CONTINUATION-GATE REPRO — {n_proposals} confirm_landscape cycles "
          f"under the B1 continuation hook (until={DONE_TOOL})")
    print("=" * 72)
    run_dir = Path(base).resolve() / "cont-gate"
    shutil.rmtree(run_dir, ignore_errors=True)
    run_dir.mkdir(parents=True, exist_ok=True)

    r = await run_repro(run_dir, n_proposals)
    ok = r.get("ok")
    print(f"\n final={r.get('final')} gate_calls={r.get('gate_calls')} "
          f"done_calls={r.get('done_calls')} empty_resume={r.get('empty_resume')} "
          f"-> {'PASS (converged)' if ok else 'FAIL (BUG REPRODUCED)'}")
    for line in r["trace"]:
        print(f"   · {line}")
    print("=" * 72)
    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    base = os.environ.get("M6_CONT_RUN_DIR", "/tmp/m6-cont-repro")
    sys.exit(asyncio.run(main(n, base)))
