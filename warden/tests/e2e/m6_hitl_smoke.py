"""M6 durable-HITL bed gate — pause → confirm → resume over the real Runs API.

Drives the REAL :class:`Runner` (real provider subprocess) through the actual
FastAPI routes via an in-process ASGI transport, so every call exercises the full
route + runner + provider stack (only the socket is elided). A run that hits a
confirm-required tool must reach ``requires_action``, emit a durable
``permission_request`` on ``run_events``, release its slot, and resume when
``POST /runs/{id}/tool_confirmation`` arrives.

Per the M6 provider split (``docs/improve_scope/new_tasks/07b-durable-hitl-provider-split.md``):

  * **Claude = HARD GATE**: built-in AND custom, exact-id resume (SDK-native
    ``defer``), **multi-tool convergent** — the driver confirms EVERY ask until
    terminal (``_confirm_loop``), not just the first. Each cell asserts
    allow→tool-ran / deny→not-ran / idempotent→ran-once / SLA→not-ran, all
    completing in a bounded number of confirms.
  * **OpenHarness / Codex = FAIL-CLOSED**: durable_http HITL is Claude-only (they
    have no native defer; re-drive restates the task on resume and breaks
    multi-tool convergence). Each cell asserts the run is **rejected** — it ends
    ``error`` naming the split, never pausing. Their HITL path is the in-process
    warm hold instead. Codex custom = N/A (ungated).

This is the durable analogue of ``permission-gating-probe.py``; the confirm-required
tool is ``Write`` (built-in row) or the ``ping`` custom tool (custom row). Run
in-image via ``python -m warden.tests.e2e.m6_hitl_smoke <provider> [tools] [cases]``.
Exits 0 on all-pass, 1 on any fail. Auth inherits the process credential (OAuth /
free Ollama — never the API-key lane).
"""

from __future__ import annotations

import asyncio
import os
import shutil
import stat
import sys
from pathlib import Path

import httpx

from warden import CustomTool
from warden.harness_api.app import create_app
from warden.harness_api.config import HarnessApiConfig
from warden.harness_api.runner import Runner

BUILTIN_PROMPT = (
    "Create a file named out.txt in the current directory containing exactly the "
    "word hello. Use your file-writing tool. Do it now, no explanation."
)
CUSTOM_PROMPT = (
    "Call the ping tool now. You must invoke the ping tool — do not answer in text."
)


def make_ping_tool(marker: Path) -> CustomTool:
    """A custom tool whose handler writes an ABSOLUTE marker — the definitive
    'the tool actually ran' signal (independent of the workspace layout)."""

    def ping_handler(**kwargs) -> str:
        marker.write_text("custom tool handler executed")
        return "pong"

    return CustomTool(
        name="ping",
        description="A ping tool. When asked to ping, call this tool.",
        input_schema={
            "type": "object",
            "properties": {"reason": {"type": "string", "description": "why"}},
            "required": [],
        },
        handler=ping_handler,
    )


def _make_writable(task_dir: Path) -> None:
    """Codex pins CODEX_HOME/.codex with 0444 pack files; persistence restore
    re-extracts over them and fails. Make the tree writable (as durable-defer-probe)."""
    if not task_dir.exists():
        return
    for root, dirs, files in os.walk(task_dir):
        for name in dirs + files:
            p = Path(root) / name
            try:
                os.chmod(p, os.stat(p).st_mode | stat.S_IWUSR)
            except OSError:
                pass


def _build_runner(provider, model, run_dir, *, custom, sla_seconds, marker):
    cfg = HarnessApiConfig()
    cfg.engine.permissions.handler = "durable_http"
    cfg.engine.provider.provider = provider
    cfg.engine.provider.model = model
    cfg.engine.provider.codex_allow_ungated_custom_tools = True
    cfg.engine.persistence.session_db_path = str(run_dir / "sessions.db")
    cfg.engine.workspace.base_dir = str(run_dir / "ws")
    cfg.engine.workspace.user_id = "probe"
    if custom:
        cfg.engine.custom_tools.tools = [make_ping_tool(marker)]
    cfg.governance.enabled = False  # ungoverned ⇒ auth inherits the process cred
    cfg.hitl.sla_seconds = sla_seconds
    return Runner(cfg)


async def _poll(client, run_id, targets, tries=600):
    for _ in range(tries):
        status = (await client.get(f"/runs/{run_id}")).json()["status"]
        if status in targets:
            return status
        await asyncio.sleep(0.25)
    return status


def _tool_ran(run_dir: Path, custom: bool, marker: Path) -> bool:
    if custom:
        return marker.exists()
    ws = run_dir / "ws"
    return any(p.name == "out.txt" for p in ws.rglob("out.txt")) if ws.exists() else False


async def _confirm_loop(client, run_id, *, decision, dup_first, cap=14):
    """Confirm EVERY ask until the run is terminal (07b multi-tool convergence).

    A Claude native-defer resume advances ONE held tool per re-drive, so a multi-tool
    agent (``pwd → write → verify``) pauses once per tool; the driver must confirm each
    fresh ask, not just the first (the pre-07b single-confirm driver stalled here).
    Returns ``(final_status, confirmed_ids, trace)``. Capped so a non-converging run
    returns instead of hanging — the gate then sees a non-``succeeded`` status."""
    confirmed: set[str] = set()
    trace: list[str] = []
    for _ in range(cap):
        status = await _poll(client, run_id, {"requires_action", "succeeded", "error"})
        if status != "requires_action":
            return status, confirmed, trace
        hist = (await client.get(f"/runs/{run_id}/history")).json()
        asks = [e for e in hist if e["type"] == "permission_request"]
        fresh = [a for a in asks if a["data"]["tool_use_id"] not in confirmed]
        if not fresh:
            await asyncio.sleep(0.25)  # paused but the new ask hasn't landed yet
            continue
        tuid = fresh[-1]["data"]["tool_use_id"]
        c = await client.post(f"/runs/{run_id}/tool_confirmation",
                              json={"tool_use_id": tuid, "decision": decision})
        confirmed.add(tuid)
        trace.append(f"confirm[{tuid[:16]}]={c.json().get('status')}")
        if dup_first and len(confirmed) == 1:  # idempotency: duplicate the first
            d = await client.post(f"/runs/{run_id}/tool_confirmation",
                                  json={"tool_use_id": tuid, "decision": decision})
            trace.append(f"dup[{tuid[:16]}]={d.json().get('status')}")
    status = await _poll(client, run_id, {"succeeded", "error"})
    return status, confirmed, trace


async def run_case(provider, model, run_dir, tool, case):
    """Drive one durable cycle. Returns the observed mechanics + outcome:
    ``paused``, ``resumed`` (completed), ``tool_ran``, ``expected_ran``,
    ``final_status``, ``error`` (the fail-closed reason for OH/Codex)."""
    custom = tool == "custom"
    marker = run_dir / "custom_ran.marker"
    prompt = CUSTOM_PROMPT if custom else BUILTIN_PROMPT
    sla = 3.0 if case == "sla" else 3600.0
    _make_writable(run_dir / "ws")
    runner = _build_runner(provider, model, run_dir, custom=custom,
                           sla_seconds=sla, marker=marker)
    # Driver hygiene: close the runner on EVERY exit path so its durable RunEventLog
    # (aiosqlite) worker thread doesn't outlive the process and block interpreter
    # shutdown (the exit-124 hang). Mirrors m6_gate_smoke / safety_block_smoke. This
    # is a short-lived per-case runner; the long-lived server keeps ONE shared
    # connection for its life and closes it via Runner.aclose() on shutdown.
    try:
        app = create_app(runner)
        transport = httpx.ASGITransport(app=app)
        trace: list[str] = []

        def _result(**kw):
            base = {"paused": False, "resumed": False, "tool_ran": False,
                    "expected_ran": _expected_ran(case), "final_status": None,
                    "error": None, "trace": trace}
            base.update(kw)
            return base

        async with httpx.AsyncClient(transport=transport, base_url="http://m6",
                                     timeout=180.0) as client:
            resp = await client.post("/runs", json={
                "user_id": "probe", "task_id": f"{provider}_{tool}_{case}",
                "provider": provider, "model": model,
                "input": {"prompt": prompt}, "sink": {"type": "sse"},
            })
            if resp.status_code != 202:
                return _result(trace=[f"POST /runs {resp.status_code}"])
            run_id = resp.json()["run_id"]

            status = await _poll(client, run_id, {"requires_action", "succeeded", "error"})
            trace.append(f"first-stop={status}")

            if status == "error":
                # 07b: OH/Codex durable_http is fail-closed — the run ends error, naming the
                # split. (Also catches a genuine error.) The gate asserts this for OH/Codex.
                hist = (await client.get(f"/runs/{run_id}/history")).json()
                err = next((e["data"].get("reason", "") for e in hist
                            if e["type"] == "error"), "")
                trace.append(f"error={err[:120]}")
                return _result(final_status="error", error=err)

            if status != "requires_action":  # completed without ever pausing
                return _result(resumed=status == "succeeded", final_status=status,
                               tool_ran=_tool_ran(run_dir, custom, marker))

            hist = (await client.get(f"/runs/{run_id}/history")).json()
            asks = [e for e in hist if e["type"] == "permission_request"]
            if asks:
                trace.append(f"ask={asks[0]['data']['tool_use_id']} "
                             f"tool={asks[0]['data']['tool_name']}")

            if case == "sla":
                status = await _poll(client, run_id, {"succeeded", "error"})
                trace.append(f"after-sla={status}")
            else:
                decision = "reject" if case == "deny" else "approve"  # E6 wire modes
                status, confirmed, ctrace = await _confirm_loop(
                    client, run_id, decision=decision, dup_first=case == "idempotent")
                trace += ctrace
                trace.append(f"confirms={len(confirmed)} after-loop={status}")

        ran = _tool_ran(run_dir, custom, marker)
        trace.append(f"tool_ran={ran} final={status}")
        return _result(paused=True, resumed=status == "succeeded", tool_ran=ran,
                       final_status=status)
    finally:
        await runner.aclose()


def _expected_ran(case: str) -> bool:
    # allow / idempotent → the tool should run; deny / sla → it should NOT.
    return case in ("allow", "idempotent")


async def run_m6_gate(provider, *, tools, cases, base) -> int:
    """Claude (durable HTTP supported) is the HARD gate: pause→confirm→resume must
    complete AND the allow/deny outcome must be correct, multi-tool convergent.
    OpenHarness/Codex are FAIL-CLOSED on durable_http (07b): each cell must be
    REJECTED (run ends ``error`` naming the split) — a pause there would be a bug."""
    fail_closed = provider in ("openharness", "codex")
    mode = "FAIL-CLOSED (assert rejected)" if fail_closed else "STRICT (hard gate)"
    print("=" * 70)
    print(f" M6 DURABLE-HITL GATE — provider={provider} mode={mode}")
    print("=" * 70)
    fails: list[str] = []
    for tool in tools:
        if provider == "codex" and tool == "custom":
            print(f"\n [{tool}] SKIP — Codex custom tools are ungated by design (N/A).")
            continue
        for case in cases:
            run_dir = Path(base).resolve() / f"{provider}-{tool}-{case}"
            shutil.rmtree(run_dir, ignore_errors=True)
            run_dir.mkdir(parents=True, exist_ok=True)
            r = await run_case(provider, None if provider == "claude" else _model(provider),
                               run_dir, tool, case)
            cell = f"{tool}/{case}"
            if fail_closed:
                rejected = (r["final_status"] == "error"
                            and "durable_http" in (r["error"] or ""))
                ok = rejected
                print(f"\n [{cell}] fail-closed(rejected)={rejected} "
                      f"final={r['final_status']} -> {'PASS' if ok else 'FAIL'}")
            else:
                mechanics = r["paused"] and r["resumed"]
                outcome = r["tool_ran"] == r["expected_ran"]
                ok = mechanics and outcome
                match = "match" if outcome else "MISMATCH"
                print(f"\n [{cell}] mechanics(paused+resumed)={mechanics} "
                      f"tool_ran={r['tool_ran']} expected={r['expected_ran']} "
                      f"[{match}] -> {'PASS' if ok else 'FAIL'}")
            for line in r["trace"]:
                print(f"     · {line}")
            if not ok:
                fails.append(cell)

    print("\n" + "=" * 70)
    proof = ("all durable_http runs rejected (fail-closed)" if fail_closed
             else "pause→confirm→resume proven (multi-tool convergent)")
    if not fails:
        print(f" M6 DURABLE-HITL GATE: PASS ({provider}) — {proof}.")
        print("=" * 70)
        return 0
    print(f" M6 DURABLE-HITL GATE: FAIL ({provider}) — {', '.join(fails)}")
    print("=" * 70)
    return 1


def _model(provider: str) -> str | None:
    if provider == "openharness":
        return os.environ.get("OPENHARNESS_MODEL", "qwen3:8b")
    return None  # codex uses its configured default


if __name__ == "__main__":
    prov = sys.argv[1] if len(sys.argv) > 1 else "claude"
    tools = (sys.argv[2].split(",") if len(sys.argv) > 2 else ["builtin", "custom"])
    cases = (sys.argv[3].split(",") if len(sys.argv) > 3 else ["allow", "deny"])
    base = os.environ.get("M6_GATE_RUN_DIR", "/work/run")
    sys.exit(asyncio.run(run_m6_gate(prov, tools=tools, cases=cases, base=base)))
