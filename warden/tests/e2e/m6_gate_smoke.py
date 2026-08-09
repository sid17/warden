"""EXT-G1/G2 — the landscape-gate bed cell (--m6-gate), over the real Runs API.

Proves the *allow-all-except-one* rule end-to-end on a live Claude durable-HITL run:
a workflow manifest with ``mode: auto`` + ``confirm: [Write]`` gates exactly the
named tool. The run auto-allows everything EXCEPT ``Write``, which pauses
(``requires_action`` + a ``permission_request`` naming ``Write`` on ``run_events``);
``POST /runs/{id}/tool_confirmation`` resumes with **allow / deny / allow-with-edit**.

This rides the exact ``--m6-hitl`` machinery (real ``Runner`` + FastAPI routes via
in-process ASGI; a real Claude subprocess) — the only delta is a gate manifest placed
in the task dir + ``input.workflow`` pointing at it, so the real ``PermissionChecker``
+ the new ``confirm`` branch (EXT-G1) are in the live loop. The gate tool is the
built-in ``Write`` (the confirm mechanism is identical whether the tool is built-in or
a custom ``confirm_landscape``; custom-tool delivery is separately proven by
``t_custom_tool``), keeping the cell's live surface minimal.

Claude-only (07b: durable_http is Claude-only). OAuth / free lane — never the API-key
lane. Run in-image via
``python -m warden.tests.e2e.m6_gate_smoke [cases]``; 0=PASS, 1=FAIL.
"""

from __future__ import annotations

import asyncio
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

# mode: auto ⇒ auto-allow everything EXCEPT the one confirm-listed tool → the gate.
_GATE_MANIFEST = """\
name: gate
description: landscape gate — allow all except Write
permissions:
  mode: auto
  tool_access:
    confirm: [Write]
"""

# E6 revise case: gate the CUSTOM confirm_landscape tool (doc 06 Gotcha #7 — a built-in
# Write has a side effect a real model overwrites, confounding "the second proposal
# differs"; a recording custom tool records the concepts it was called with).
_REVISE_MANIFEST = """\
name: gate
description: landscape gate — confirm_landscape only
permissions:
  mode: auto
  tool_access:
    confirm: [confirm_landscape]
"""

REVISE_PROMPT = (
    "You are proposing a course LANDSCAPE (a short list of concept strings). Call the "
    "confirm_landscape tool now with a 'concepts' array of 2-3 short concept strings to "
    "submit your proposal for operator approval. You MUST call confirm_landscape — do "
    "not answer in text. If the operator asks you to revise, call confirm_landscape "
    "AGAIN with a DIFFERENT concepts array that addresses their feedback."
)

PROMPT = (
    "Create a file named out.txt in the current directory containing exactly the "
    "word hello. You MUST use the Write tool to create it — do not use Bash or any "
    "shell command. Do it now, no explanation."
)

# Edited content the allow-with-edit case rewrites the Write args to.
_EDIT_CONTENT = "edited-by-operator"


def _place_manifest(base_dir: Path, task_id: str) -> None:
    """Lay the gate manifest at the task's ``.workflows/gate.yaml`` (load_workflow
    reads ``<repo>/.workflows/<name>.yaml``)."""
    wf_dir = base_dir / USER / task_id / ".workflows"
    wf_dir.mkdir(parents=True, exist_ok=True)
    (wf_dir / "gate.yaml").write_text(_GATE_MANIFEST)


def _place_revise_manifest(base_dir: Path, task_id: str) -> None:
    wf_dir = base_dir / USER / task_id / ".workflows"
    wf_dir.mkdir(parents=True, exist_ok=True)
    (wf_dir / "gate.yaml").write_text(_REVISE_MANIFEST)


def make_confirm_landscape_tool(record: Path) -> CustomTool:
    """A recording gate tool: each call appends the concepts it received (one JSON line
    per call) so the driver can assert the SECOND proposal differs from the first."""

    def handler(**kwargs) -> str:
        import json
        concepts = kwargs.get("concepts") or []
        with record.open("a") as fh:
            fh.write(json.dumps(concepts) + "\n")
        return "landscape confirmed"

    return CustomTool(
        name="confirm_landscape",
        description="Submit a proposed course landscape (a 'concepts' array) for "
                    "operator approval.",
        input_schema={
            "type": "object",
            "properties": {
                "concepts": {"type": "array", "items": {"type": "string"},
                             "description": "the proposed concept strings"},
            },
            "required": ["concepts"],
        },
        handler=handler,
    )


def _build_runner(run_dir: Path, *, revise_record: Path | None = None) -> Runner:
    cfg = HarnessApiConfig()
    cfg.engine.permissions.handler = "durable_http"
    cfg.engine.provider.provider = "claude"
    cfg.engine.persistence.session_db_path = str(run_dir / "sessions.db")
    cfg.engine.workspace.base_dir = str(run_dir / "ws")
    cfg.engine.workspace.user_id = USER
    if revise_record is not None:
        cfg.engine.custom_tools.tools = [make_confirm_landscape_tool(revise_record)]
    cfg.governance.enabled = False  # ungoverned ⇒ auth inherits the process cred
    cfg.hitl.sla_seconds = 3600.0
    return Runner(cfg)


async def _poll(client, run_id, targets, tries=600):
    status = None
    for _ in range(tries):
        status = (await client.get(f"/runs/{run_id}")).json()["status"]
        if status in targets:
            return status
        await asyncio.sleep(0.25)
    return status


def _out_txt(run_dir: Path) -> Path | None:
    ws = run_dir / "ws"
    if not ws.exists():
        return None
    return next((p for p in ws.rglob("out.txt")), None)


async def _error_reason(client, run_id) -> str:
    """The reason on the run's terminal error/stopped event — for diagnosing a FAIL
    (e.g. distinguishing the §3c 'identical proposal' hard-stop from an SDK error)."""
    hist = (await client.get(f"/runs/{run_id}/history")).json()
    errs = [e for e in hist if e["type"] in ("error", "stopped")]
    return errs[-1]["data"].get("reason", "(no reason)") if errs else "(no error event)"


async def _confirm_loop(client, run_id, *, case, trace, cap=12):
    """Confirm EVERY pause until the run is terminal (a multi-tool agent pauses once
    per confirm-listed call). Records the tool of each pause so the gate property
    ('only Write ever pauses') is asserted. Applies the edit on the first Write pause.
    Returns ``(paused_tools, final_status)``."""
    confirmed: set[str] = set()
    paused_tools: list[str] = []
    edited = False
    for _ in range(cap):
        status = await _poll(client, run_id, {"requires_action", "succeeded", "error"})
        if status != "requires_action":
            return paused_tools, status
        hist = (await client.get(f"/runs/{run_id}/history")).json()
        asks = [e for e in hist if e["type"] == "permission_request"]
        fresh = [a for a in asks if a["data"]["tool_use_id"] not in confirmed]
        if not fresh:
            await asyncio.sleep(0.25)
            continue
        ask = fresh[-1]
        tool = ask["data"]["tool_name"]
        tuid = ask["data"]["tool_use_id"]
        paused_tools.append(tool)
        # allow: approve every Write. deny: reject every Write. edit: apply the edit to
        # the FIRST Write, then REJECT subsequent Writes — otherwise a real model
        # re-writes its own content and overwrites the operator's edit (the edit
        # mechanism itself is proven hermetically; here we isolate its effect).
        # (E6 wire modes: approve / reject / revise; the revise case has its own driver.)
        body: dict = {"tool_use_id": tuid, "decision": "approve"}
        if case == "deny":
            body["decision"] = "reject"
        elif case == "edit":
            if not edited:
                new_input = dict(ask["data"].get("tool_input") or {})
                new_input["content"] = _EDIT_CONTENT
                body["updated_input"] = new_input
                edited = True
            else:
                body["decision"] = "reject"
        await client.post(f"/runs/{run_id}/tool_confirmation", json=body)
        confirmed.add(tuid)
        trace.append(f"pause[{tool}] decision={body['decision']}"
                     f"{'+edit' if 'updated_input' in body else ''}")
    return paused_tools, await _poll(client, run_id, {"succeeded", "error"})


async def run_case(run_dir: Path, case: str) -> dict:
    """Drive one gate cycle for a case in {allow, deny, edit}. Confirms every pause;
    records which tools paused (the gate property = ONLY Write ever pauses)."""
    task_id = f"gate_{case}"
    _place_manifest(run_dir / "ws", task_id)
    runner = _build_runner(run_dir)
    app = create_app(runner)
    transport = httpx.ASGITransport(app=app)
    trace: list[str] = []

    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://gate",
                                     timeout=180.0) as client:
            resp = await client.post("/runs", json={
                "user_id": USER, "task_id": task_id, "provider": "claude",
                "input": {"prompt": PROMPT, "workflow": "gate"},
                "sink": {"type": "sse"},
            })
            if resp.status_code != 202:
                return {"trace": [f"POST /runs {resp.status_code}"], "paused_tools": [],
                        "resumed": False, "wrote": False, "content": None}
            run_id = resp.json()["run_id"]
            paused_tools, status = await _confirm_loop(client, run_id, case=case,
                                                       trace=trace)
            trace.append(f"paused_tools={paused_tools} final={status}")

        out = _out_txt(run_dir)
        content = out.read_text().strip() if out else None
        return {"trace": trace, "paused_tools": paused_tools,
                "resumed": status == "succeeded", "wrote": out is not None,
                "content": content}
    finally:
        await runner.aclose()  # driver hygiene (see run_revise_case) — avoid exit-124 hang


async def run_revise_case(run_dir: Path) -> dict:
    """E6 revise loop on the CUSTOM confirm_landscape gate: pause on proposal 1 →
    ``{decision:"revise", feedback:...}`` → assert a SECOND pause on confirm_landscape
    with a DIFFERENT tool_input → ``{decision:"approve"}`` → assert convergence and the
    recording handler saw the revised concepts."""
    task_id = "gate_revise"
    record = run_dir / "landscape_proposals.jsonl"
    _place_revise_manifest(run_dir / "ws", task_id)
    runner = _build_runner(run_dir, revise_record=record)
    app = create_app(runner)
    transport = httpx.ASGITransport(app=app)
    trace: list[str] = []
    proposals: list = []

    async def _first_pending(client, run_id, seen: set[str]):
        for _ in range(600):
            status = await _poll(client, run_id, {"requires_action", "succeeded",
                                                  "error"})
            if status != "requires_action":
                return None, status
            hist = (await client.get(f"/runs/{run_id}/history")).json()
            asks = [e for e in hist if e["type"] == "permission_request"]
            fresh = [a for a in asks if a["data"]["tool_use_id"] not in seen]
            if fresh:
                return fresh[-1], status
            await asyncio.sleep(0.25)
        return None, "timeout"

    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://gate",
                                     timeout=180.0) as client:
            resp = await client.post("/runs", json={
                "user_id": USER, "task_id": task_id, "provider": "claude",
                "input": {"prompt": REVISE_PROMPT, "workflow": "gate"},
                "sink": {"type": "sse"},
            })
            if resp.status_code != 202:
                return {"trace": [f"POST /runs {resp.status_code}"], "paused_tools": [],
                        "resumed": False, "revised_differs": False}
            run_id = resp.json()["run_id"]
            seen: set[str] = set()
            paused_tools: list[str] = []

            # Pause 1 — the first proposal.
            ask1, status = await _first_pending(client, run_id, seen)
            if ask1 is None:
                trace.append(f"no first pause (status={status})")
                if status == "error":
                    trace.append(f"error_reason={await _error_reason(client, run_id)}")
                return {"trace": trace, "paused_tools": paused_tools, "resumed": False,
                        "revised_differs": False}
            seen.add(ask1["data"]["tool_use_id"])
            paused_tools.append(ask1["data"]["tool_name"])
            input1 = ask1["data"].get("tool_input")
            round1 = ask1["data"].get("revise_round")
            trace.append(f"pause1[{ask1['data']['tool_name']}] round={round1} input={input1}")

            # Revise with feedback → the model re-plans and re-submits a DIFFERENT
            # proposal. The feedback is forceful about changing EVERY concept so a
            # compliant model diverges (a byte-identical re-fire is a genuine
            # "ignored feedback" case that §3c hard-stops — proven separately, hermetically).
            await client.post(f"/runs/{run_id}/tool_confirmation", json={
                "tool_use_id": ask1["data"]["tool_use_id"], "decision": "revise",
                "feedback": "Replace ALL of the concepts with COMPLETELY DIFFERENT "
                            "topics — every concept string MUST change and none may "
                            "reuse the previous wording.",
            })

            # Pause 2 — a DIFFERENT proposal on the SAME tool. Real Claude is
            # non-deterministic here: it either regenerates a different proposal (→ a
            # second pause, the happy path) OR re-fires the byte-identical one (which
            # §3c correctly hard-stops — a valid outcome, captured as ``storm_stopped``).
            ask2, status = await _first_pending(client, run_id, seen)
            if ask2 is None:
                reason = await _error_reason(client, run_id) if status == "error" else ""
                trace.append(f"no second pause (status={status}) reason={reason!r}")
                return {"trace": trace, "paused_tools": paused_tools, "resumed": False,
                        "revised_differs": False,
                        "storm_stopped": "identical" in reason}
            seen.add(ask2["data"]["tool_use_id"])
            paused_tools.append(ask2["data"]["tool_name"])
            input2 = ask2["data"].get("tool_input")
            round2 = ask2["data"].get("revise_round")
            differs = input2 != input1
            trace.append(f"pause2[{ask2['data']['tool_name']}] round={round2} "
                         f"input={input2} differs={differs}")

            # Approve the revised proposal, then DRAIN any further confirm_landscape
            # pauses. Real Claude re-calls a gated tool several times before finishing
            # (the allow case pauses on Write 5×); once the revise is approved the model
            # may re-submit confirm_landscape again, so approve EVERY subsequent pause
            # until a terminal — the same "confirm every pause" contract allow/deny use.
            to_approve = ask2["data"]["tool_use_id"]
            status = "requires_action"
            for _ in range(12):
                await client.post(f"/runs/{run_id}/tool_confirmation", json={
                    "tool_use_id": to_approve, "decision": "approve",
                })
                seen.add(to_approve)
                nxt, status = await _first_pending(client, run_id, seen)
                if nxt is None:
                    break  # reached a terminal (succeeded/error)
                paused_tools.append(nxt["data"]["tool_name"])
                to_approve = nxt["data"]["tool_use_id"]
            if status == "error":
                trace.append(f"error_reason={await _error_reason(client, run_id)}")
            trace.append(f"final={status} round2={round2} pauses={len(paused_tools)}")

        if record.exists():
            import json
            proposals = [json.loads(ln) for ln in record.read_text().splitlines() if ln]
        return {"trace": trace, "paused_tools": paused_tools,
                "resumed": status == "succeeded", "revised_differs": differs,
                "revise_round2": round2, "proposals": proposals}
    finally:
        # Driver hygiene: close the Runner so its aiosqlite event-log thread + any
        # parked SLA timer don't outlive the case and hang process exit (the hermetic
        # tests do this; the bed driver previously leaked it → exit 124 on teardown).
        await runner.aclose()


def _judge_revise(r: dict) -> bool:
    paused = r.get("paused_tools") or []
    # At least one pause, ALL on confirm_landscape (the gate leaked if a sibling paused).
    # A custom tool surfaces through the SDK as the FQMN
    # ``mcp__harness_custom__confirm_landscape``, so match by suffix, not bare name.
    if not paused or any(not t.endswith("confirm_landscape") for t in paused):
        return False
    # Real Claude is non-deterministic on a revise — BOTH endings prove the feature:
    #  (1) HAPPY path: regenerates a DIFFERENT proposal (revise_round 2) → approve →
    #      converges (proven live in a diverging run + hermetically).
    #  (2) STORM path: re-fires the IDENTICAL proposal → §3c hard-stops with the
    #      "identical" reason (proven live in a non-diverging run).
    happy = (bool(r.get("revised_differs")) and r.get("revise_round2") == 2
             and r.get("resumed") is True)
    return bool(happy or r.get("storm_stopped"))


def _judge(case: str, r: dict) -> bool:
    if case == "revise":
        return _judge_revise(r)
    # The gate property: at least one pause, and EVERY pause is on Write (the
    # confirm-listed tool) — a sibling ever pausing means the gate leaked.
    paused = r.get("paused_tools") or []
    if not paused or any(t != "Write" for t in paused):
        return False
    if not r.get("resumed"):
        return False
    if case == "allow":
        return r.get("wrote") is True
    if case == "deny":
        return r.get("wrote") is False  # denied → the file was never written
    if case == "edit":
        # edit applied → the re-fired Write wrote the operator's content.
        return r.get("wrote") is True and r.get("content") == _EDIT_CONTENT
    return False


async def run_gate(cases: list[str], base: str) -> int:
    print("=" * 70)
    print(" M6 LANDSCAPE-GATE — provider=claude (mode:auto + confirm:[Write])")
    print("=" * 70)
    fails: list[str] = []
    for case in cases:
        run_dir = Path(base).resolve() / f"gate-{case}"
        shutil.rmtree(run_dir, ignore_errors=True)
        run_dir.mkdir(parents=True, exist_ok=True)
        # E6: the revise case gates a CUSTOM confirm_landscape tool (Gotcha #7).
        r = await (run_revise_case(run_dir) if case == "revise"
                   else run_case(run_dir, case))
        ok = _judge(case, r)
        print(f"\n [{case}] paused_tools={r.get('paused_tools')} "
              f"resumed={r.get('resumed')} wrote={r.get('wrote')} "
              f"content={r.get('content')!r} differs={r.get('revised_differs')} "
              f"-> {'PASS' if ok else 'FAIL'}")
        for line in r["trace"]:
            print(f"     · {line}")
        if not ok:
            fails.append(case)

    print("\n" + "=" * 70)
    if not fails:
        print(" M6 LANDSCAPE-GATE: PASS (claude) — allow/deny/edit + revise loop ok.")
        print("=" * 70)
        return 0
    print(f" M6 LANDSCAPE-GATE: FAIL (claude) — {', '.join(fails)}")
    print("=" * 70)
    return 1


if __name__ == "__main__":
    cases = (sys.argv[1].split(",") if len(sys.argv) > 1
             else ["allow", "deny", "edit", "revise"])
    base = os.environ.get("M6_GATE_RUN_DIR", "/work/run")
    sys.exit(asyncio.run(run_gate(cases, base)))
