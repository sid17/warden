"""M6 — durable HITL over HTTP (the Runs-API pause/resume cycle).

Hermetic, provider-agnostic: a mock agent that consults the injected permission
handler for one ``Write`` tool stands in for any provider. On the first pass the
durable handler has no recorded decision, so it ejects (deny-to-end) — the run
parks in ``requires_action``, emits a ``permission_request`` on ``run_events``, and
releases its slot. ``tool_confirmation`` records a decision and re-drives; the
handler now injects it (allow → tool runs; deny → blocked, model re-plans).

These cover the provider-agnostic mechanics (T1). Resume allow/deny + idempotency
+ SLA live in the sibling tests once 3c/3d land.
"""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from warden.harness_api.config import HarnessApiConfig
from warden.harness_api.credentials.keys import KeyRegistry
from warden.harness_api.runner import Runner
from warden.harness_api.schemas import RunSpec, Sink
from warden.schemas.events import (
    CompletionEvent,
    MessageEvent,
    OrchestratorEvent,
    SessionCreatedEvent,
)

_KEYS = KeyRegistry.from_config(
    {
        "keys": {"k1": {"provider": "claude", "secret_env": "S1"}},
        "users": {"u1": {"key_id": "k1", "budget_usd": 100.0}},
    },
    secrets={"S1": "sk-1"},
)

# The single tool call every mock run makes — a stable id models Claude's exact-id
# resume (the re-driven consult reaches the SAME id, so get_decision hits).
_TOOL_ID = "toolu_write_1"
_TOOL_INPUT = {"path": "out.txt", "content": "hello"}


class _DurableMockChatAPI:
    """Consults the injected durable handler for one ``Write`` tool.

    - eject (source contains ``defer``) → end the turn with NO result (the provider
      stopping cleanly at the tool call — Claude defer / OH deny-to-end);
    - allow → "run" the tool (emit tool_use) then finish with a result;
    - deny (not a defer) → the tool is blocked; the model re-plans and reports.
    """

    def __init__(self, *, spec: Any, auth_env: dict | None, tracker: dict) -> None:
        self._spec = spec
        self._sid = f"sess-{spec.task_id}"
        self._handler = None
        self._tracker = tracker
        tracker.setdefault("consults", [])

    def set_permission_handler(self, handler) -> None:
        # OpenHarness/Codex path: the Runner injects the real DurableDeferHandler.
        self._handler = handler

    def set_durable_defer(self, dd) -> None:
        # Claude path: the real hook is the SDK-native PreToolUse defer (needs the
        # live SDK). Hermetically stand it in with a real DurableDeferHandler over
        # the SAME store — identical record-and-eject / resolved-inject semantics, so
        # the provider-agnostic Runner transport (post-send detect + resume) is what's
        # under test; Claude's exact-id native defer is proven live in the bed.
        from warden.seams.defer import DurableDeferHandler
        from warden.seams.defer_store import FileDeferStore

        self._handler = DurableDeferHandler(FileDeferStore(dd.store_root))

    async def init(self) -> None:
        return None

    async def send(
        self, content: str, *, session_id: str | None = None,
        workflow: str | None = None,
    ) -> AsyncIterator[OrchestratorEvent]:
        resumed = session_id is not None
        sid = session_id or self._sid
        yield SessionCreatedEvent(session_id=sid, resumed=resumed)
        decision = await self._handler.request_permission(
            "Write", dict(_TOOL_INPUT), "write a file", tool_use_id=_TOOL_ID,
        )
        self._tracker["consults"].append(decision)
        if "defer" in decision.source:
            return  # ejected: the turn ends, the run parks (requires_action)
        if decision.allowed:
            self._tracker["ran_tool"] = self._tracker.get("ran_tool", 0) + 1
            yield MessageEvent(
                kind="tool_use", content={"toolName": "Write"}, session_id=sid
            )
            result = "wrote out.txt"
        else:
            result = f"blocked: {decision.reason}"
        yield MessageEvent(
            kind="status",
            content={
                "subtype": "result",
                "result": result,
                "usage": {"input_tokens": 5, "output_tokens": 5},
            },
            session_id=sid,
        )
        yield CompletionEvent(session_id=sid)

    async def close(self) -> None:
        return None


def _durable_factory(tracker: dict):
    def factory(spec: Any, auth_env: dict | None) -> _DurableMockChatAPI:
        return _DurableMockChatAPI(spec=spec, auth_env=auth_env, tracker=tracker)

    return factory


def _durable_config(tmp: Path) -> HarnessApiConfig:
    cfg = HarnessApiConfig()
    cfg.engine.permissions.handler = "durable_http"
    # Point persistence at tmp so run_events.db + hitl_defer/ land under tmp.
    cfg.engine.persistence.session_db_path = str(tmp / "sessions.db")
    return cfg


_UNSET = object()


def _make_runner(tmp: Path, tracker: dict, *, sla_seconds=_UNSET) -> Runner:
    # ``_UNSET`` leaves the config default (60s). Pass an explicit float for a bounded
    # SLA, or ``None`` for an INDEFINITE gate (H1 — no auto-expiry, parks forever).
    cfg = _durable_config(tmp)
    if sla_seconds is not _UNSET:
        cfg.hitl.sla_seconds = sla_seconds
    return Runner(cfg, keys=_KEYS, chat_api_factory=_durable_factory(tracker))


async def _poll_until(runner: Runner, run_id: str, target: str, tries: int = 200) -> str:
    for _ in range(tries):
        if runner.get(run_id).status == target:
            return target
        await asyncio.sleep(0.01)
    return runner.get(run_id).status


def _spec(user: str, task: str, *, provider: str = "claude") -> RunSpec:
    return RunSpec(
        user_id=user, task_id=task, provider=provider, model="claude-opus-4-8",
        input={"prompt": "write the file"}, sink=Sink(type="sse"),
    )


# --- T1: pause emits the durable ask, releases the slot, is replayable --------

def test_pause_emits_permission_request_and_parks():
    async def _run():
        tmp = Path(tempfile.mkdtemp())
        tracker: dict = {}
        runner = _make_runner(tmp, tracker)
        await runner.init()
        run_id = runner.submit(_spec("u1", "c1"))
        await runner.task_for(run_id)  # initial pass runs then parks

        # Parked, not terminal.
        assert runner.get(run_id).status == "requires_action"
        # The durable ask is on run_events (replayable in a fresh reader), not an
        # out-of-band channel.
        events = await runner.replay(run_id)
        asks = [e for e in events if e.type == "permission_request"]
        assert len(asks) == 1
        assert asks[0].data["tool_use_id"] == _TOOL_ID
        assert asks[0].data["tool_name"] == "Write"
        assert asks[0].data["tool_input"] == _TOOL_INPUT
        # No terminal was emitted (the run is paused, not finished).
        assert not [e for e in events if e.type in ("result", "error", "stopped")]
        # The eject consulted the handler exactly once and got a defer decision.
        assert len(tracker["consults"]) == 1
        assert "defer" in tracker["consults"][0].source
        await runner.aclose()

    asyncio.run(_run())


# --- T1b: a paused run releases its slot (a second run proceeds) --------------

def test_paused_run_releases_its_slot():
    async def _run():
        tmp = Path(tempfile.mkdtemp())
        tracker: dict = {}
        runner = _make_runner(tmp, tracker)
        # Cap concurrency at 1: if the paused run pinned the slot, run 2 would hang.
        runner._sem = asyncio.Semaphore(1)
        await runner.init()

        r1 = runner.submit(_spec("u1", "c1"))
        await runner.task_for(r1)
        assert runner.get(r1).status == "requires_action"

        # A second run on a different task must acquire the freed slot and finish.
        r2 = runner.submit(_spec("u1", "c2"))
        await asyncio.wait_for(runner.task_for(r2), timeout=5)
        assert runner.get(r2).status == "requires_action"  # it too parks (own ask)
        # Both parked independently ⇒ the first never held the slot.
        e2 = await runner.replay(r2)
        assert any(e.type == "permission_request" for e in e2)
        await runner.aclose()

    asyncio.run(_run())


# --- T2: resume with allow → the deferred tool runs, run completes ------------

def test_confirm_allow_runs_the_tool_and_completes():
    async def _run():
        tmp = Path(tempfile.mkdtemp())
        tracker: dict = {}
        runner = _make_runner(tmp, tracker)
        await runner.init()
        run_id = runner.submit(_spec("u1", "c1"))
        await runner.task_for(run_id)
        assert runner.get(run_id).status == "requires_action"

        result = await runner.confirm(run_id, _TOOL_ID, decision="approve")
        assert result["status"] == "resumed"
        await runner.task_for(run_id)  # await the re-drive

        assert runner.get(run_id).status == "succeeded"
        assert tracker.get("ran_tool") == 1  # the tool ran exactly once
        events = await runner.replay(run_id)
        assert any(e.type == "permission_resolved" and e.data["decision"] == "approve"
                   for e in events)
        assert any(e.type == "result" for e in events)
        await runner.aclose()

    asyncio.run(_run())


# --- T3: resume with deny → the tool never runs, run continues ----------------

def test_confirm_deny_blocks_the_tool():
    async def _run():
        tmp = Path(tempfile.mkdtemp())
        tracker: dict = {}
        runner = _make_runner(tmp, tracker)
        await runner.init()
        run_id = runner.submit(_spec("u1", "c1"))
        await runner.task_for(run_id)

        result = await runner.confirm(run_id, _TOOL_ID, decision="reject", reason="nope")
        assert result["status"] == "resumed"
        await runner.task_for(run_id)

        assert runner.get(run_id).status == "succeeded"  # the run continued
        assert tracker.get("ran_tool") is None  # the tool never ran
        events = await runner.replay(run_id)
        assert any(e.type == "permission_resolved" and e.data["decision"] == "reject"
                   for e in events)
        await runner.aclose()

    asyncio.run(_run())


# --- T4: duplicate confirm is a no-op (idempotent, tool runs once) ------------

def test_duplicate_confirm_is_idempotent():
    async def _run():
        tmp = Path(tempfile.mkdtemp())
        tracker: dict = {}
        runner = _make_runner(tmp, tracker)
        await runner.init()
        run_id = runner.submit(_spec("u1", "c1"))
        await runner.task_for(run_id)

        first = await runner.confirm(run_id, _TOOL_ID, decision="approve")
        assert first["status"] == "resumed"
        await runner.task_for(run_id)
        assert tracker.get("ran_tool") == 1

        # Second confirm on the same (run_id, tool_use_id): no-op, no re-run.
        second = await runner.confirm(run_id, _TOOL_ID, decision="approve")
        assert second["status"] == "already_resolved"
        assert second["decision"] == "approve"
        assert tracker.get("ran_tool") == 1  # still exactly once
        await runner.aclose()

    asyncio.run(_run())


# --- confirm on an unknown run → None (404 at the route) ----------------------

def test_confirm_unknown_run_returns_none():
    async def _run():
        tmp = Path(tempfile.mkdtemp())
        runner = _make_runner(tmp, {})
        await runner.init()
        assert await runner.confirm("run_999999", "x", decision="approve") is None
        await runner.aclose()

    asyncio.run(_run())


# --- the HTTP route: POST /runs → poll requires_action → tool_confirmation ----

def test_tool_confirmation_route_end_to_end():
    import httpx

    from warden.harness_api.app import create_app

    async def _poll_status(client, run_id, target, tries=50):
        for _ in range(tries):
            status = (await client.get(f"/runs/{run_id}")).json()["status"]
            if status == target:
                return status
            await asyncio.sleep(0.05)
        return status

    async def _run():
        tmp = Path(tempfile.mkdtemp())
        tracker: dict = {}
        runner = _make_runner(tmp, tracker)
        app = create_app(runner)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            resp = await client.post("/runs", json={
                "user_id": "u1", "task_id": "c1", "provider": "claude",
                "input": {"prompt": "write the file"}, "sink": {"type": "sse"},
            })
            assert resp.status_code == 202
            run_id = resp.json()["run_id"]

            assert await _poll_status(client, run_id, "requires_action") == "requires_action"
            # The durable ask is visible on the history (run_events) channel.
            hist = (await client.get(f"/runs/{run_id}/history")).json()
            ask = [e for e in hist if e["type"] == "permission_request"]
            assert ask and ask[0]["data"]["tool_use_id"] == _TOOL_ID

            conf = await client.post(f"/runs/{run_id}/tool_confirmation", json={
                "tool_use_id": _TOOL_ID, "decision": "approve",
            })
            assert conf.status_code == 200
            assert conf.json()["status"] == "resumed"

            assert await _poll_status(client, run_id, "succeeded") == "succeeded"
            assert tracker.get("ran_tool") == 1

            # 404 on an unknown run.
            missing = await client.post("/runs/run_xxx/tool_confirmation", json={
                "tool_use_id": "z", "decision": "approve",
            })
            assert missing.status_code == 404

    asyncio.run(_run())


def test_revise_with_empty_feedback_is_422_at_the_route():
    """E6: a ``revise`` with empty ``feedback`` is rejected by the schema validator as
    a 422 at the HTTP boundary (not only at model construction) — the model is never
    resumed with an empty revise instruction. Driven against a REAL paused run so the
    422 is unambiguously the body validator (not a 404/403)."""
    import httpx

    from warden.harness_api.app import create_app

    async def _poll_status(client, run_id, target, tries=50):
        for _ in range(tries):
            status = (await client.get(f"/runs/{run_id}")).json()["status"]
            if status == target:
                return status
            await asyncio.sleep(0.05)
        return status

    async def _run():
        tmp = Path(tempfile.mkdtemp())
        runner = _make_runner(tmp, {})
        app = create_app(runner)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            resp = await client.post("/runs", json={
                "user_id": "u1", "task_id": "c1", "provider": "claude",
                "input": {"prompt": "write the file"}, "sink": {"type": "sse"},
            })
            run_id = resp.json()["run_id"]
            assert await _poll_status(client, run_id, "requires_action") == "requires_action"

            # Empty feedback on a revise → 422 at the boundary, before the route logic.
            bad = await client.post(f"/runs/{run_id}/tool_confirmation", json={
                "tool_use_id": _TOOL_ID, "decision": "revise", "feedback": "  ",
            })
            assert bad.status_code == 422
            # The run stayed paused — the invalid confirm never resolved the ask.
            assert (await client.get(f"/runs/{run_id}")).json()["status"] == "requires_action"

    asyncio.run(_run())


# --- T5 (H2): an unanswered BOUNDED ask EXPIRES cleanly past the SLA -----------

def test_sla_timeout_expires_cleanly():
    """H2: a bounded gate whose SLA elapses EXPIRES to a clean, product-synced
    terminal — a distinct ``permission_expired`` event + an ``hitl_expired`` terminal
    — NOT a silent auto-deny re-drive. The tool never runs, and the run leaves
    ``requires_action`` (never stranded)."""
    async def _run():
        tmp = Path(tempfile.mkdtemp())
        tracker: dict = {}
        runner = _make_runner(tmp, tracker, sla_seconds=0.05)
        await runner.init()
        run_id = runner.submit(_spec("u1", "c1"))
        await runner.task_for(run_id)
        assert runner.get(run_id).status == "requires_action"

        # No confirmation arrives → the SLA fires and the gate EXPIRES (a terminal).
        assert await _poll_until(runner, run_id, "error") == "error"
        assert tracker.get("ran_tool") is None  # never ran (expired, not allowed)
        events = await runner.replay(run_id)
        # The distinct expiry signal fired, keyed to the pending ask.
        expired = [e for e in events if e.type == "permission_expired"]
        assert expired and expired[0].data["tool_use_id"] == _TOOL_ID
        assert "hitl_expired" in expired[0].data["reason"]
        # An expiry is NOT modeled as a model reject (no permission_resolved:reject).
        assert not [e for e in events if e.type == "permission_resolved"]
        # The terminal carries the machine-readable expiry reason (product → expired).
        assert events[-1].type == "error"
        assert events[-1].data["reason"].startswith("hitl_expired")
        await runner.aclose()

    asyncio.run(_run())


# --- T5c (H1): an INDEFINITE gate never expires + resumes whenever the human returns

def test_indefinite_gate_never_expires_and_resumes_late():
    """H1: with ``sla_seconds=None`` (indefinite), no timer is armed — the ask stays
    durably parked past any bound, emits no expiry/terminal, and a LATE confirm
    (arriving well after the old 60s SLA would have fired) still resumes + completes.
    This is the 'a person comes back later and recovers' guarantee."""
    async def _run():
        tmp = Path(tempfile.mkdtemp())
        tracker: dict = {}
        runner = _make_runner(tmp, tracker, sla_seconds=None)  # indefinite
        await runner.init()
        run_id = runner.submit(_spec("u1", "c1"))
        await runner.task_for(run_id)
        assert runner.get(run_id).status == "requires_action"
        # No SLA task was armed (indefinite).
        assert runner._runs[run_id].sla_task is None

        # Wait far longer than a short bound would take to fire — still parked, no
        # expiry, no terminal (the run is genuinely waiting, not stranded).
        await asyncio.sleep(0.2)
        assert runner.get(run_id).status == "requires_action"
        events = await runner.replay(run_id)
        assert not [e for e in events
                    if e.type in ("permission_expired", "error", "stopped", "result")]

        # The human returns "late" and approves → the same park resumes + completes.
        result = await runner.confirm(run_id, _TOOL_ID, decision="approve")
        assert result["status"] == "resumed"
        await runner.task_for(run_id)
        assert runner.get(run_id).status == "succeeded"
        assert tracker.get("ran_tool") == 1  # the deferred tool ran on resume
        await runner.aclose()

    asyncio.run(_run())


# --- T5b: a confirmation that beats the SLA wins (SLA is cancelled) -----------

def test_confirmation_cancels_the_sla():
    async def _run():
        tmp = Path(tempfile.mkdtemp())
        tracker: dict = {}
        # Generous SLA so the confirm clearly lands first.
        runner = _make_runner(tmp, tracker, sla_seconds=30.0)
        await runner.init()
        run_id = runner.submit(_spec("u1", "c1"))
        await runner.task_for(run_id)

        result = await runner.confirm(run_id, _TOOL_ID, decision="approve")
        assert result["status"] == "resumed"
        assert await _poll_until(runner, run_id, "succeeded") == "succeeded"
        assert tracker.get("ran_tool") == 1  # approved, ran once — SLA never denied it
        events = await runner.replay(run_id)
        # Exactly one resolution, and it's the approve (not an SLA reject).
        resolved = [e for e in events if e.type == "permission_resolved"]
        assert len(resolved) == 1 and resolved[0].data["decision"] == "approve"
        await runner.aclose()

    asyncio.run(_run())


# --- eject wiring: Claude native defer; OH/Codex fail closed (07b) ------------

def test_wire_durable_eject_claude_native_defer_oh_codex_fail_closed():
    import pytest

    class _FakeApi:
        def __init__(self):
            self.durable = None
            self.handler = None

        def set_durable_defer(self, dd):
            self.durable = dd

        def set_permission_handler(self, h):
            self.handler = h

    tmp = Path(tempfile.mkdtemp())
    runner = _make_runner(tmp, {})

    claude = _FakeApi()
    runner._wire_durable_eject(claude, "run_1", _spec("u1", "c", provider="claude"))
    assert claude.durable is not None and claude.durable.enabled  # native defer
    assert claude.handler is None

    # OH/Codex have no native defer — durable_http wiring is a hard error (07b),
    # never a silent DurableDeferHandler downgrade.
    for prov in ("openharness", "codex"):
        oh = _FakeApi()
        with pytest.raises(RuntimeError, match="durable_http"):
            runner._wire_durable_eject(oh, "run_2", _spec("u1", "c", provider=prov))
        assert oh.handler is None and oh.durable is None


# --- T6: the paused state survives a control-plane restart --------------------

def test_paused_state_survives_restart():
    """After a run parks, kill the Runner and reconstruct from the durable
    artifacts ALONE (a fresh RunEventLog + FileDeferStore over the same paths, as a
    fresh process would): the run is still awaiting the same (run_id, tool_use_id),
    with no terminal — state reconstructed from disk, not the in-memory registry."""
    from warden.harness_api.event_log import RunEventLog
    from warden.seams.defer_store import FileDeferStore

    async def _run():
        tmp = Path(tempfile.mkdtemp())
        tracker: dict = {}
        runner = _make_runner(tmp, tracker, sla_seconds=30.0)
        await runner.init()
        run_id = runner.submit(_spec("u1", "c1"))
        await runner.task_for(run_id)
        assert runner.get(run_id).status == "requires_action"
        await runner.aclose()  # "kill" the control plane (registry lost)

        # Fresh process: the durable event log still shows the ask, no terminal.
        log = RunEventLog(tmp / "run_events.db")
        await log.init()
        events = await log.replay(run_id)
        await log.close()
        asks = [e for e in events if e.type == "permission_request"]
        assert asks and asks[0].data["tool_use_id"] == _TOOL_ID
        assert not [e for e in events if e.type in ("result", "error", "stopped")]

        # Fresh durable store still holds the pending record (the resume key).
        store = FileDeferStore(tmp / "hitl_defer" / run_id)
        pending = store.read_pending()
        assert len(pending) == 1
        assert pending[0].tool_use_id == _TOOL_ID
        assert pending[0].status == "pending"  # unresolved — awaiting a decision

    asyncio.run(_run())


# --- T7: the tool_confirmation body carries no credential (DRIVE-4) ------------

def test_tool_confirmation_carries_no_credential():
    from warden.harness_api.schemas import ToolConfirmation

    fields = set(ToolConfirmation.model_fields)
    # E6 added ``feedback`` (a revise's operator guidance — not a credential); EXT-G2's
    # ``updated_input`` stays dormant on the schema.
    assert fields == {"tool_use_id", "decision", "reason", "feedback", "updated_input"}
    # No credential-ish field could smuggle a secret over the resume verb.
    assert not (fields & {"secret", "api_key", "token", "auth", "auth_env", "key"})


# --- T8: a non-durable run is unaffected (durable wiring is inert) -------------

def test_non_durable_run_is_unaffected():
    """DRIVE-1 no-regress: with the default (non-durable) handler, the Runner never
    wires the durable path — the run completes normally, no permission_request."""
    from warden.tests.harness_api.mock_skill import Tracker, build_factory

    async def _run():
        tmp = Path(tempfile.mkdtemp())
        cfg = HarnessApiConfig()  # default handler (NOT durable_http)
        cfg.engine.persistence.session_db_path = str(tmp / "sessions.db")
        runner = Runner(cfg, keys=_KEYS, chat_api_factory=build_factory(Tracker()))
        assert runner._is_durable_http() is False
        await runner.init()
        run_id = runner.submit(_spec("u1", "c1"))
        await runner.task_for(run_id)
        assert runner.get(run_id).status == "succeeded"
        events = await runner.replay(run_id)
        assert not [e for e in events if e.type == "permission_request"]
        assert events[-1].type == "result"
        await runner.aclose()

    asyncio.run(_run())


# --- T9: a MULTI-TOOL agent converges (Claude native continuation, no restate) --
#
# The single-tool mock above proves the transport STATE MACHINE but cannot surface
# the resume-prompt bug (07b §2): on a durable resume the Runner used to re-send the
# ORIGINAL prompt, so a multi-tool agent re-read the task as fresh every resume and
# RESTARTED its plan → a defer storm that never converges. This mock issues THREE
# tools in sequence and RESTARTS its plan iff it is re-sent the original prompt —
# exactly the real-agent behaviour. It converges only when the Runner resumes with a
# NEUTRAL CONTINUATION (Change 1), so this test fails before the fix and passes after.

_PLAN = ["pwd", "write", "verify"]  # a 3-tool agent (each a distinct tool_use_id)


class _MultiToolMockChatAPI:
    """A multi-tool agent: runs ``_PLAN`` in order, one deferrable tool per turn.

    Progress is tracked per session in the shared tracker (a fresh ChatAPI is built
    for every re-drive, so per-instance state would be lost). The BUG MODEL: when the
    turn is *resumed* and the content is the ORIGINAL task prompt (a restate), the
    agent restarts its plan from step 0 — this is what a real model does when re-told
    to "do the task". A neutral continuation preserves progress, so the plan advances
    one confirmed tool at a time and converges.
    """

    def __init__(self, *, spec: Any, auth_env: dict | None, tracker: dict) -> None:
        self._spec = spec
        self._original = str(spec.input.get("prompt") or "")
        self._sid = f"sess-{spec.task_id}"
        self._handler = None
        self._tracker = tracker
        tracker.setdefault("ran", [])
        tracker.setdefault("consults", [])

    def set_permission_handler(self, handler) -> None:
        self._handler = handler

    def set_durable_defer(self, dd) -> None:
        # Stand in Claude's SDK-native exact-id defer with a real DurableDeferHandler
        # over the same store (identical record-and-eject / resolved-inject semantics).
        from warden.seams.defer import DurableDeferHandler
        from warden.seams.defer_store import FileDeferStore

        self._handler = DurableDeferHandler(FileDeferStore(dd.store_root))

    async def init(self) -> None:
        return None

    async def send(
        self, content: str, *, session_id: str | None = None,
        workflow: str | None = None,
    ) -> AsyncIterator[OrchestratorEvent]:
        resumed = session_id is not None
        sid = session_id or self._sid
        yield SessionCreatedEvent(session_id=sid, resumed=resumed)

        progress = self._tracker.setdefault("progress", {})
        idx = progress.get(sid, 0)
        # THE BUG: re-sent the original task on resume ⇒ the agent restarts the plan.
        if resumed and content.strip() == self._original.strip():
            idx = 0
        progress[sid] = idx

        while idx < len(_PLAN):
            tool = _PLAN[idx]
            tuid = f"toolu_{tool}"
            decision = await self._handler.request_permission(
                tool, {"step": tool}, f"run {tool}", tool_use_id=tuid,
            )
            self._tracker["consults"].append((tool, decision.source))
            if "defer" in decision.source:
                progress[sid] = idx
                return  # eject on this tool — the run parks (requires_action)
            if decision.allowed:
                self._tracker["ran"].append(tool)
                idx += 1
                progress[sid] = idx
                yield MessageEvent(
                    kind="tool_use", content={"toolName": tool}, session_id=sid
                )
                continue
            break  # denied → stop the plan and report

        yield MessageEvent(
            kind="status",
            content={"subtype": "result", "result": "done",
                     "usage": {"input_tokens": 5, "output_tokens": 5}},
            session_id=sid,
        )
        yield CompletionEvent(session_id=sid)

    async def close(self) -> None:
        return None


def _multi_factory(tracker: dict):
    def factory(spec: Any, auth_env: dict | None) -> _MultiToolMockChatAPI:
        return _MultiToolMockChatAPI(spec=spec, auth_env=auth_env, tracker=tracker)

    return factory


async def _confirm_until_terminal(
    runner: Runner, run_id: str, *, decision: str = "approve", cap: int = 10,
) -> int:
    """Drive the full durable loop: confirm EVERY ask until the run is terminal.

    Returns the number of confirmations issued. Capped so a non-converging run (the
    defer storm) returns instead of hanging — the test then sees status != succeeded."""
    confirms = 0
    for _ in range(cap):
        status = await _poll_until(runner, run_id, "requires_action")
        if status != "requires_action":
            break  # reached a terminal (succeeded/error) — done
        tuid = runner._runs[run_id].pending_tool_use_id
        await runner.confirm(run_id, tuid, decision=decision)
        confirms += 1
        await runner.task_for(run_id)
    return confirms


def test_multi_tool_agent_converges_with_native_continuation():
    async def _run():
        tmp = Path(tempfile.mkdtemp())
        tracker: dict = {}
        runner = Runner(_durable_config(tmp), keys=_KEYS,
                        chat_api_factory=_multi_factory(tracker))
        await runner.init()
        run_id = runner.submit(_spec("u1", "c1"))
        await runner.task_for(run_id)  # initial pass parks on the first tool (pwd)

        confirms = await _confirm_until_terminal(runner, run_id)

        # Converges: reaches succeeded, each tool ran EXACTLY once, in order, and the
        # confirm count is bounded to the number of tools (no storm / no re-runs).
        assert runner.get(run_id).status == "succeeded"
        assert tracker["ran"] == _PLAN, f"expected each tool once in order, got {tracker['ran']}"
        assert confirms == len(_PLAN), f"expected {len(_PLAN)} confirms, got {confirms}"
        await runner.aclose()

    asyncio.run(_run())


# --- T10: a DENY resume must tell the model to STOP retrying (decision-aware) -----
#
# Live bed finding (builtin/deny, 2026-07-24): with an allow-framed continuation, a
# model on a "must finish" task re-issues the denied call every resume (fresh id, no
# stored decision → re-eject → re-pause) — a deny storm that never terminates. The
# fix makes the resume continuation DECISION-AWARE: a deny sends _DURABLE_RESUME_DENIED
# ("do not retry"). This mock retries the denied tool UNLESS told denied — so it
# converges only when the Runner sends the deny-aware continuation.

_DENY_TOOL = "toolu_write"


class _DenyRetryMockChatAPI:
    """Models a persistent model: it re-issues the deferred tool every resume
    (a fresh content each turn is not needed — the same id, re-ejected) UNLESS the
    resume prompt tells it the action was DENIED, in which case it gives up cleanly."""

    def __init__(self, *, spec: Any, auth_env: dict | None, tracker: dict) -> None:
        self._spec = spec
        self._sid = f"sess-{spec.task_id}"
        self._handler = None
        self._tracker = tracker
        tracker.setdefault("contents", [])

    def set_permission_handler(self, handler) -> None:
        self._handler = handler

    def set_durable_defer(self, dd) -> None:
        from warden.seams.defer import DurableDeferHandler
        from warden.seams.defer_store import FileDeferStore

        self._handler = DurableDeferHandler(FileDeferStore(dd.store_root))

    async def init(self) -> None:
        return None

    async def send(
        self, content: str, *, session_id: str | None = None,
        workflow: str | None = None,
    ) -> AsyncIterator[OrchestratorEvent]:
        from warden.harness_api.runner import _DURABLE_RESUME_DENIED

        self._tracker["contents"].append(content)
        sid = session_id or self._sid
        yield SessionCreatedEvent(session_id=sid, resumed=session_id is not None)

        # Told denied → give up (the well-behaved response the fix elicits).
        if content == _DURABLE_RESUME_DENIED:
            yield MessageEvent(
                kind="status",
                content={"subtype": "result", "result": "blocked — cannot proceed",
                         "usage": {"input_tokens": 3, "output_tokens": 3}},
                session_id=sid,
            )
            yield CompletionEvent(session_id=sid)
            return

        # Otherwise (re)attempt the tool — a persistent model retrying the denied call.
        decision = await self._handler.request_permission(
            "Write", {"path": "out.txt"}, "write", tool_use_id=_DENY_TOOL,
        )
        if "defer" in decision.source:
            return  # ejected → park
        if not decision.allowed:
            # Injected deny on the exact id — but a persistent model retries: re-issue
            # with a NEW id so there is no stored decision (models the storm).
            nxt = await self._handler.request_permission(
                "Write", {"path": "out.txt"}, "write",
                tool_use_id=f"{_DENY_TOOL}_{len(self._tracker['contents'])}",
            )
            if "defer" in nxt.source:
                return
        yield MessageEvent(
            kind="status",
            content={"subtype": "result", "result": "done",
                     "usage": {"input_tokens": 3, "output_tokens": 3}},
            session_id=sid,
        )
        yield CompletionEvent(session_id=sid)

    async def close(self) -> None:
        return None


def test_deny_resume_is_decision_aware_and_terminates():
    """A deny confirm resumes with _DURABLE_RESUME_DENIED (not the allow-framed
    continuation), so a persistent model stops retrying and the run terminates —
    the fix for the live builtin/deny storm."""
    from warden.harness_api.runner import _DURABLE_RESUME_DENIED

    async def _run():
        tmp = Path(tempfile.mkdtemp())
        tracker: dict = {}

        def factory(spec, auth_env):
            return _DenyRetryMockChatAPI(spec=spec, auth_env=auth_env, tracker=tracker)

        runner = Runner(_durable_config(tmp), keys=_KEYS, chat_api_factory=factory)
        await runner.init()
        run_id = runner.submit(_spec("u1", "c1"))
        await runner.task_for(run_id)
        assert runner.get(run_id).status == "requires_action"

        confirms = await _confirm_until_terminal(runner, run_id, decision="reject")

        # Terminates (no storm) and the deny-aware continuation was the resume prompt.
        assert runner.get(run_id).status == "succeeded"
        assert confirms == 1, f"deny should converge in one confirm, got {confirms}"
        assert _DURABLE_RESUME_DENIED in tracker["contents"]
        await runner.aclose()

    asyncio.run(_run())


# --- Change 2: durable_http is Claude-only — OH/Codex fail closed (no silent DL) --

def test_durable_http_fail_closed_for_openharness_and_codex():
    """OH/Codex have no native defer; their only HTTP option (re-drive) restates the
    task and breaks multi-tool convergence (07b). So durable_http is HARD fail-closed
    for them: the run ends ``error`` BEFORE taking a slot / invoking the provider — no
    pause, no silent downgrade to auto-allow."""
    async def _run():
        for prov in ("openharness", "codex"):
            tmp = Path(tempfile.mkdtemp())
            tracker: dict = {}
            runner = _make_runner(tmp, tracker)
            await runner.init()
            run_id = runner.submit(_spec("u1", "c1", provider=prov))
            await runner.task_for(run_id)

            view = runner.get(run_id)
            assert view.status == "error", f"{prov}: expected error, got {view.status}"
            assert "durable_http" in (view.error or "") and prov in (view.error or "")
            events = await runner.replay(run_id)
            assert not [e for e in events if e.type == "permission_request"]
            assert events[-1].type == "error"
            # The provider/mock was never driven (rejected pre-flight, no slot taken).
            assert not tracker.get("consults")
            await runner.aclose()

    asyncio.run(_run())


# --- E6: the three-mode gate (approve / reject / revise) on a confirm_landscape gate --
#
# A recording ``confirm_landscape`` gate (NOT a built-in Write proxy — a Write has a
# side effect a real model overwrites, confounding "did the second proposal differ";
# doc 06 §4 + Gotcha #7). The mock fires proposal N (concepts=["base", <feedback
# tokens...>]) so a revise → a NEW proposal with a DIFFERENT content_key → the durable
# hook re-ejects → the run pauses AGAIN (the revise loop). On approve it proceeds to
# course_complete and converges; on reject it reports blocked and halts.

_LANDSCAPE_TOOL = "confirm_landscape"


class _ReviseGateMockChatAPI:
    """A landscape gate that revises its proposal in response to operator feedback.

    Proposal state rides the shared tracker (a fresh ChatAPI is built per re-drive).
    Each turn it consults the durable handler for ``confirm_landscape`` with the CURRENT
    concept set. On a REVISE continuation it appends the feedback token to the concepts
    (a genuinely different proposal → a new content_key → a fresh pause). On an APPROVE
    it streams ``course_complete`` and finishes. Set ``tracker['echo']=True`` to model a
    misbehaving model that IGNORES feedback and re-fires the IDENTICAL proposal (§3c)."""

    def __init__(self, *, spec: Any, auth_env: dict | None, tracker: dict) -> None:
        self._spec = spec
        self._sid = f"sess-{spec.task_id}"
        self._handler = None
        self._tracker = tracker
        tracker.setdefault("proposals", [])
        tracker.setdefault("streamed", [])

    def set_durable_defer(self, dd) -> None:
        from warden.seams.defer import DurableDeferHandler
        from warden.seams.defer_store import FileDeferStore

        self._handler = DurableDeferHandler(FileDeferStore(dd.store_root))

    async def init(self) -> None:
        return None

    def _concepts(self) -> list[str]:
        """The current proposal = a base concept plus one token per revise round."""
        return ["base", *self._tracker.setdefault("tokens", [])]

    async def send(
        self, content: str, *, session_id: str | None = None,
        workflow: str | None = None,
    ) -> AsyncIterator[OrchestratorEvent]:
        from warden.harness_api.runner import _DURABLE_RESUME_REVISE

        sid = session_id or self._sid
        yield SessionCreatedEvent(session_id=sid, resumed=session_id is not None)

        # A revise continuation carries the operator feedback → regenerate the proposal
        # by appending the feedback token (unless ``echo`` — the ignore-feedback model).
        revise_prefix = _DURABLE_RESUME_REVISE.split("{feedback}")[0]
        if content.startswith(revise_prefix) and not self._tracker.get("echo"):
            token = content.split("Operator feedback: ", 1)[1].split("\n", 1)[0].strip()
            self._tracker.setdefault("tokens", []).append(token)

        concepts = self._concepts()
        # A stable-per-PROPOSAL id (keyed by the concept set) so an approve's exact-id
        # inject lands on re-drive; a DIFFERENT proposal → a different id → re-ejects
        # (no stored decision). Distinctness of the proposal is carried by the concepts.
        tuid = f"toolu_{_LANDSCAPE_TOOL}_{'.'.join(concepts)}"
        decision = await self._handler.request_permission(
            _LANDSCAPE_TOOL, {"concepts": list(concepts)}, "confirm the landscape",
            tool_use_id=tuid,
        )
        if "defer" in decision.source:
            self._tracker["proposals"].append(list(concepts))
            return  # ejected → the run parks on confirm_landscape (requires_action)
        if not decision.allowed:
            # §3c echo: a model that IGNORES the revise feedback re-issues the SAME
            # proposal under a FRESH id (the just-resolved deny was content-matched +
            # consumed above, so this fresh id re-ejects) → a NEW pending record with
            # the IDENTICAL content_key → the Runner's §3c detects the duplicate at the
            # next pause and hard-stops. (Mirrors _DenyRetryMockChatAPI's re-issue.)
            if self._tracker.get("echo"):
                self._tracker["turn"] = self._tracker.get("turn", 0) + 1
                nxt = await self._handler.request_permission(
                    _LANDSCAPE_TOOL, {"concepts": list(concepts)},
                    "confirm the landscape", tool_use_id=f"{tuid}_r{self._tracker['turn']}",
                )
                if "defer" in nxt.source:
                    self._tracker["proposals"].append(list(concepts))
                    return
            # A reject (not a revise — revise re-drives with a different continuation).
            yield MessageEvent(
                kind="status",
                content={"subtype": "result", "result": "blocked — cannot proceed",
                         "usage": {"input_tokens": 3, "output_tokens": 3}},
                session_id=sid,
            )
            yield CompletionEvent(session_id=sid)
            return
        # Approved: stream course_complete and converge.
        self._tracker["streamed"].append("course_complete")
        yield MessageEvent(kind="tool_use", content={"toolName": "course_complete"},
                           session_id=sid)
        yield MessageEvent(
            kind="status",
            content={"subtype": "result", "result": "course built",
                     "usage": {"input_tokens": 3, "output_tokens": 3}},
            session_id=sid,
        )
        yield CompletionEvent(session_id=sid)

    async def close(self) -> None:
        return None


def _revise_factory(tracker: dict):
    def factory(spec: Any, auth_env: dict | None) -> _ReviseGateMockChatAPI:
        return _ReviseGateMockChatAPI(spec=spec, auth_env=auth_env, tracker=tracker)

    return factory


def test_gate_approve_runs_once_and_converges():
    """E6 approve: the first proposal is approved → confirm_landscape's approval clears,
    course_complete streams, run converges. No revise loop."""
    async def _run():
        tmp = Path(tempfile.mkdtemp())
        tracker: dict = {}
        runner = Runner(_durable_config(tmp), keys=_KEYS,
                        chat_api_factory=_revise_factory(tracker))
        await runner.init()
        run_id = runner.submit(_spec("u1", "c1"))
        await runner.task_for(run_id)
        assert runner.get(run_id).status == "requires_action"

        tuid = runner._runs[run_id].pending_tool_use_id
        await runner.confirm(run_id, tuid, decision="approve")
        await runner.task_for(run_id)

        assert runner.get(run_id).status == "succeeded"
        assert tracker["streamed"] == ["course_complete"]
        assert tracker["proposals"] == [["base"]]  # exactly one proposal, approved
        await runner.aclose()

    asyncio.run(_run())


def test_gate_reject_halts_without_running_the_gate_tool():
    """E6 reject: the proposal is rejected → the gate tool never proceeds to
    course_complete and the run halts (decision-aware, no revise re-drive)."""
    async def _run():
        tmp = Path(tempfile.mkdtemp())
        tracker: dict = {}
        runner = Runner(_durable_config(tmp), keys=_KEYS,
                        chat_api_factory=_revise_factory(tracker))
        await runner.init()
        run_id = runner.submit(_spec("u1", "c1"))
        await runner.task_for(run_id)

        tuid = runner._runs[run_id].pending_tool_use_id
        await runner.confirm(run_id, tuid, decision="reject", reason="wrong concepts")
        await runner.task_for(run_id)

        assert runner.get(run_id).status == "succeeded"  # halted cleanly
        assert "course_complete" not in tracker["streamed"]  # the gate tool never ran
        events = await runner.replay(run_id)
        assert any(e.type == "permission_resolved" and e.data["decision"] == "reject"
                   for e in events)
        await runner.aclose()

    asyncio.run(_run())


def test_gate_revise_then_approve_converges_with_a_different_proposal():
    """E6 the revise loop: revise + feedback → a SECOND permission_request for
    confirm_landscape fires whose tool_input DIFFERS from the first → approve → the run
    converges. ``revise_round`` == 2 at the second pause (§3b)."""
    async def _run():
        tmp = Path(tempfile.mkdtemp())
        tracker: dict = {}
        runner = Runner(_durable_config(tmp), keys=_KEYS,
                        chat_api_factory=_revise_factory(tracker))
        await runner.init()
        run_id = runner.submit(_spec("u1", "c1"))
        await runner.task_for(run_id)  # parks on proposal 1
        assert runner.get(run_id).status == "requires_action"

        tuid1 = runner._runs[run_id].pending_tool_use_id
        # Revise with feedback → the model re-plans and re-submits a DIFFERENT proposal.
        await runner.confirm(run_id, tuid1, decision="revise",
                             feedback="add-chapter-X")
        await _poll_until(runner, run_id, "requires_action")
        assert runner.get(run_id).status == "requires_action"  # paused AGAIN
        tuid2 = runner._runs[run_id].pending_tool_use_id
        assert tuid2 != tuid1  # a fresh ask for a fresh proposal

        # The two permission_requests carry DIFFERENT tool_input (the revised concepts).
        events = await runner.replay(run_id)
        asks = [e for e in events if e.type == "permission_request"]
        assert len(asks) == 2
        assert asks[0].data["tool_input"] != asks[1].data["tool_input"]
        assert "add-chapter-X" in asks[1].data["tool_input"]["concepts"]
        # §3b: the second pause is revise_round 2 — the stamp AND the derived count.
        assert asks[1].data["revise_round"] == 2
        assert await runner._event_log.revise_round(run_id, _LANDSCAPE_TOOL) == 2

        # Approve the revised proposal → converge.
        await runner.confirm(run_id, tuid2, decision="approve")
        await runner.task_for(run_id)
        assert runner.get(run_id).status == "succeeded"
        assert tracker["streamed"] == ["course_complete"]
        assert tracker["proposals"] == [["base"], ["base", "add-chapter-X"]]
        await runner.aclose()

    asyncio.run(_run())


def test_gate_revise_exact_duplicate_hard_stops():
    """E6 §3c storm-stop: a model that IGNORES the revise feedback and re-fires the
    BYTE-IDENTICAL proposal hard-stops (a terminal error naming the reason) instead of
    pausing forever. The gate runs UNGOVERNED, so no Governor backstop covers this."""
    async def _run():
        tmp = Path(tempfile.mkdtemp())
        # echo=True → the mock re-fires the identical concepts on a revise.
        tracker: dict = {"echo": True}
        runner = Runner(_durable_config(tmp), keys=_KEYS,
                        chat_api_factory=_revise_factory(tracker))
        await runner.init()
        run_id = runner.submit(_spec("u1", "c1"))
        await runner.task_for(run_id)
        assert runner.get(run_id).status == "requires_action"

        tuid1 = runner._runs[run_id].pending_tool_use_id
        await runner.confirm(run_id, tuid1, decision="revise", feedback="please change")
        # The re-drive re-fires the SAME proposal → §3c detects it → hard-stop.
        assert await _poll_until(runner, run_id, "error") == "error"
        view = runner.get(run_id)
        assert "identical" in (view.error or "")
        events = await runner.replay(run_id)
        # Exactly one pause happened (the second identical fire hard-stopped, never
        # emitting a second permission_request).
        asks = [e for e in events if e.type == "permission_request"]
        assert len(asks) == 1
        assert events[-1].type == "error"
        await runner.aclose()

    asyncio.run(_run())


if __name__ == "__main__":  # pragma: no cover
    test_pause_emits_permission_request_and_parks()
    test_paused_run_releases_its_slot()
    test_confirm_allow_runs_the_tool_and_completes()
    test_confirm_deny_blocks_the_tool()
    test_duplicate_confirm_is_idempotent()
    test_confirm_unknown_run_returns_none()
    test_tool_confirmation_route_end_to_end()
    test_sla_timeout_expires_cleanly()
    test_indefinite_gate_never_expires_and_resumes_late()
    test_confirmation_cancels_the_sla()
    test_paused_state_survives_restart()
    test_tool_confirmation_carries_no_credential()
    test_non_durable_run_is_unaffected()
