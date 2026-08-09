"""§9 Runs-API tests — the two-folder / create→revise(same session)→Q&A flow.

Deterministic: driven by the mock skill (no LLM, no subprocess). Follows the
repo's ``asyncio.run(_run())`` pattern (no pytest-asyncio dependency).
"""

import asyncio
import json
import tempfile
from pathlib import Path

import httpx

from warden.harness_api.credentials.keys import KeyRegistry
from warden.harness_api.app import create_app
from warden.harness_api.config import HarnessApiConfig
from warden.harness_api.event_log import RunEventLog
from warden.harness_api.runner import Runner
from warden.harness_api.schemas import RunSpec, Sink
from warden.tests.harness_api.mock_skill import Tracker, build_factory


def _tmp_event_log() -> RunEventLog:
    """A throwaway durable-log at a temp path, so tests never touch ``data/``."""
    return RunEventLog(Path(tempfile.mkdtemp()) / "run_events.db")


# --- helpers --------------------------------------------------------------

_KEYS = KeyRegistry.from_config(
    {
        "keys": {
            "k1": {"provider": "claude", "secret_env": "S1"},
            "k2": {"provider": "claude", "secret_env": "S2"},
        },
        "users": {
            "u1": {"key_id": "k1", "budget_usd": 100.0},
            "u2": {"key_id": "k2", "budget_usd": 100.0},
        },
    },
    secrets={"S1": "sk-1", "S2": "sk-2"},
)


def _spec(user, task, *, session_id=None, sink=None, label="main", budget=None):
    return RunSpec(
        user_id=user,
        task_id=task,
        session_id=session_id,
        input={"prompt": "hi", "label": label},
        sink=sink or Sink(type="sse"),
        budget_usd=budget,
    )


def _ungoverned_config() -> HarnessApiConfig:
    """Governance-off config: the ungoverned path these tests exercise (no ledger,
    no durable balance file). Governance defaults ON now (3g.2b), so pin it off."""
    from warden.harness_api.config import GovernanceConfig

    return HarnessApiConfig(governance=GovernanceConfig(enabled=False))


def _make_runner(tracker, *, webhook_client=None, **mock_kwargs):
    return Runner(
        _ungoverned_config(),
        keys=_KEYS,
        chat_api_factory=build_factory(tracker, **mock_kwargs),
        webhook_client=webhook_client,
        event_log=_tmp_event_log(),
    )


async def _run_and_collect(runner, spec):
    run_id = runner.submit(spec)
    await runner.task_for(run_id)
    sse = runner.sse_for(run_id)
    events = [e async for e in sse.stream()]
    return run_id, events


# --- §9.1 two-folder isolation + distinct keys ----------------------------

def test_two_folder_isolation_distinct_keys():
    async def _run():
        tracker = Tracker()
        runner = _make_runner(tracker)
        ids = await asyncio.gather(
            _run_and_collect(runner, _spec("u1", "course_A")),
            _run_and_collect(runner, _spec("u2", "course_B")),
        )
        # Each run got its user's managed key in the subprocess env — no bleed.
        by_user = {spec.user_id: (spec.task_id, auth_env) for spec, auth_env in tracker.calls}
        assert by_user["u1"] == ("course_A", {"ANTHROPIC_API_KEY": "sk-1"})
        assert by_user["u2"] == ("course_B", {"ANTHROPIC_API_KEY": "sk-2"})
        # Both runs terminated on a result event.
        for _run_id, events in ids:
            assert events[-1].type == "result"

    asyncio.run(_run())


# --- §9.2 session in/out + resume (same session) --------------------------

def test_session_out_then_resume_same_id():
    async def _run():
        tracker = Tracker()
        runner = _make_runner(tracker)
        # Create → get a session id (creation).
        _, create_events = await _run_and_collect(
            runner, _spec("u1", "course_42", label="creation")
        )
        assert create_events[0].type == "session"
        assert create_events[0].data["resumed"] is False
        sid = create_events[0].session_id
        assert sid == "sess-course_42-creation"

        # Revise → resume the SAME session id.
        _, revise_events = await _run_and_collect(
            runner, _spec("u1", "course_42", session_id=sid, label="creation")
        )
        assert revise_events[0].type == "session"
        assert revise_events[0].session_id == sid
        assert revise_events[0].data["resumed"] is True

    asyncio.run(_run())


# --- §9.3 many sessions / one folder --------------------------------------

def test_multiple_sessions_one_folder():
    async def _run():
        tracker = Tracker()
        runner = _make_runner(tracker)
        _, creation = await _run_and_collect(
            runner, _spec("u1", "course_42", label="creation")
        )
        _, qa = await _run_and_collect(
            runner, _spec("u1", "course_42", label="qa")
        )
        # Same workspace (task_id) — the runner routed both there.
        assert all(spec.task_id == "course_42" for spec, _ in tracker.calls)
        # Distinct session ids (separate threads over the one folder).
        assert creation[0].session_id != qa[0].session_id

    asyncio.run(_run())


# --- §9.4 serialize per task; concurrent across tasks ---------------------

def test_serialize_same_task():
    async def _run():
        tracker = Tracker()
        gate = asyncio.Event()
        runner = _make_runner(tracker, gate=gate)
        r1 = runner.submit(_spec("u1", "course_42", label="a"))
        r2 = runner.submit(_spec("u1", "course_42", label="b"))
        await asyncio.sleep(0.05)  # let r1 start and hit the gate
        # r2 is blocked on the per-task lock — never runs concurrently.
        assert tracker.max_active == 1
        gate.set()
        await asyncio.gather(runner.task_for(r1), runner.task_for(r2))
        assert tracker.max_active == 1  # serialized throughout

    asyncio.run(_run())


def test_concurrent_across_tasks():
    async def _run():
        tracker = Tracker()
        tracker.started = asyncio.Event()
        gate = asyncio.Event()
        runner = _make_runner(tracker, gate=gate)
        r1 = runner.submit(_spec("u1", "course_A"))
        r2 = runner.submit(_spec("u2", "course_B"))
        # Both should be in-flight at once (distinct folders).
        await asyncio.wait_for(tracker.started.wait(), timeout=1.0)
        assert tracker.max_active >= 2
        gate.set()
        await asyncio.gather(runner.task_for(r1), runner.task_for(r2))

    asyncio.run(_run())


# --- §9.5 egress: webhook ordering + SSE stream ---------------------------

def test_egress_webhook_ordered_by_seq():
    async def _run():
        received = []
        receiver = httpx.ASGITransport(app=_receiver_app(received))
        client = httpx.AsyncClient(transport=receiver, base_url="http://rx")
        tracker = Tracker()
        runner = _make_runner(
            tracker, webhook_client=client, checkpoint={"phase": "draft"}
        )
        run_id = runner.submit(
            _spec("u1", "course_42", sink=Sink(type="webhook", url="http://rx/hook"))
        )
        await runner.task_for(run_id)
        await client.aclose()
        seqs = [e["seq"] for e in received]
        assert seqs == list(range(1, len(received) + 1))  # contiguous, in order
        assert received[0]["type"] == "session"
        assert any(e["type"] == "checkpoint" for e in received)
        assert received[-1]["type"] == "result"

    asyncio.run(_run())


def test_egress_sse_via_app():
    async def _run():
        tracker = Tracker()
        runner = _make_runner(tracker, checkpoint={"phase": "draft"})
        app = create_app(runner)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            resp = await client.post(
                "/runs",
                json={
                    "user_id": "u1",
                    "task_id": "course_42",
                    "input": {"prompt": "hi"},
                    "sink": {"type": "sse"},
                },
            )
            assert resp.status_code == 202
            run_id = resp.json()["run_id"]

            events = []
            async with client.stream("GET", f"/runs/{run_id}/events") as stream:
                async for line in stream.aiter_lines():
                    if line.startswith("data: "):
                        events.append(json.loads(line[6:]))
                        if events[-1]["type"] == "result":
                            break

            assert events[0]["type"] == "session"
            assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
            assert events[-1]["type"] == "result"

            status = (await client.get(f"/runs/{run_id}")).json()
            assert status["status"] == "succeeded"
            assert status["session_id"] == "sess-course_42-main"

    asyncio.run(_run())


# --- §9.6 spend cap: retired (3g.2b) --------------------------------------
# The ungoverned pre-flight spend gate (``SpendTracker.over_budget``) is gone; the
# ungoverned path is uncapped (budgets = enable governance). The governed
# no-headroom reject is covered in ``test_runs_governed.py``.


# --- M3 3a: result usage is normalized to one shape -----------------------

def test_result_usage_is_normalized_one_shape() -> None:
    from warden.harness_api.runner import _RunState
    from warden.schemas.events import MessageEvent

    runner = Runner()
    state = _RunState(run_id="r", user_id="u", task_id="t", model="claude-opus-4-8")
    oe = MessageEvent(
        kind="status",
        content={
            "subtype": "result",
            "usage": {"input_tokens": 100, "output_tokens": 40,
                      "cache_read_input_tokens": 12},
            "result": "done",
        },
    )

    class _Egress:
        async def emit(self, event): ...
        async def aclose(self): ...

    asyncio.run(runner._handle_message("r", _Egress(), state, oe))
    # normalized shape, not the raw provider dict
    assert set(state.usage) == {"input", "output", "cached", "cost_usd"}
    assert "input_tokens" not in state.usage
    assert state.usage["input"] == 100
    assert state.usage["output"] == 40
    assert state.usage["cached"] == 12
    # cost table stays authoritative and matches the accumulated cost
    assert state.usage["cost_usd"] == state.cost_usd > 0


# --- M3 3b-1: GenAI semconv on the wire Event -----------------------------

def test_tool_use_event_carries_semconv() -> None:
    from warden.harness_api.runner import _RunState
    from warden.schemas.events import MessageEvent

    runner = _make_runner(Tracker())
    state = _RunState(run_id="r", user_id="u", task_id="t")
    oe = MessageEvent(
        kind="tool_use",
        content={"toolName": "Bash", "toolInput": {}, "toolCallId": "x"},
    )

    class _Egress:
        def __init__(self):
            self.events = []

        async def emit(self, event):
            self.events.append(event)

        async def aclose(self): ...

    egress = _Egress()

    async def _drive():
        await runner._ensure_event_log()
        await runner._handle_message("r", egress, state, oe)

    asyncio.run(_drive())
    (ev,) = egress.events
    assert ev.type == "tool_use"
    # gen_ai.* semconv merged onto the data...
    assert ev.data["gen_ai.tool.name"] == "Bash"
    assert ev.data["gen_ai.operation.name"] == "execute_tool"
    # ...without dropping the original wire content.
    assert ev.data["toolName"] == "Bash"
    assert ev.data["toolInput"] == {}
    assert ev.data["toolCallId"] == "x"


def test_result_event_carries_usage_semconv() -> None:
    from warden.harness_api.runner import _RunState

    runner = _make_runner(Tracker())
    state = _RunState(run_id="r", user_id="u", task_id="t", model="claude-opus-4-8")
    state.usage = {"input": 100, "output": 40, "cached": 12, "cost_usd": 0.5}
    state.result_text = "done"

    class _Egress:
        def __init__(self):
            self.events = []

        async def emit(self, event):
            self.events.append(event)

        async def aclose(self): ...

    egress = _Egress()

    async def _drive():
        await runner._ensure_event_log()
        await runner._emit_terminal("r", egress, state)

    asyncio.run(_drive())
    (ev,) = egress.events
    assert ev.type == "result"
    # gen_ai.* usage semconv on the terminal result event...
    assert ev.data["gen_ai.request.model"] == "claude-opus-4-8"
    assert ev.data["gen_ai.usage.input_tokens"] == 100
    assert ev.data["gen_ai.usage.output_tokens"] == 40
    assert ev.data["gen_ai.operation.name"] == "chat"
    # ...alongside the existing keys, unchanged.
    assert ev.data["result"] == "done"
    assert ev.data["usage"] == state.usage
    assert ev.data["cost_usd"] == state.cost_usd


def test_governance_stop_recorded_in_audit_trail(tmp_path) -> None:
    """AUD-3: a stopped(reason) terminal mirrors into the audit JSONL as a Stop.

    A non-pausable budget stop → status "stopped" (state.run_governor is None so
    pausable is False); the runner emits "stopped" and RECORDS the halt.
    """
    from warden.config.models import (
        AuditConfig,
        HarnessConfig,
        ObservabilityConfig,
    )
    from warden.harness_api.config import HarnessApiConfig
    from warden.harness_api.runner import Runner, _RunState

    cfg = HarnessApiConfig(
        engine=HarnessConfig(
            observability=ObservabilityConfig(
                audit=AuditConfig(
                    enabled=True, run_id="run-gov2", log_dir=str(tmp_path)
                )
            )
        )
    )
    runner = Runner(
        config=cfg,
        keys=_KEYS,
        chat_api_factory=build_factory(Tracker()),
        event_log=_tmp_event_log(),
    )
    state = _RunState(run_id="r", user_id="u", task_id="t", model="claude-opus-4-8")
    state.session_id = "sess-gov"
    state.stopped_reason = "budget"

    class _Egress:
        def __init__(self):
            self.events = []

        async def emit(self, event):
            self.events.append(event)

        async def aclose(self): ...

    egress = _Egress()

    async def _drive():
        await runner._ensure_event_log()
        await runner._emit_terminal("r", egress, state)

    asyncio.run(_drive())

    (ev,) = egress.events
    assert ev.type == "stopped"

    out = tmp_path / "run-gov2.jsonl"
    assert out.exists()
    lines = [json.loads(ln) for ln in out.read_text().splitlines() if ln.strip()]
    stops = [r for r in lines if r.get("event_type") == "Stop"]
    assert len(stops) == 1
    assert stops[0]["stop_reason"] == "budget"


# --- receiver app for the webhook test ------------------------------------

def _receiver_app(received: list):
    from fastapi import FastAPI, Request

    rx = FastAPI()

    @rx.post("/hook")
    async def hook(request: Request):
        received.append(await request.json())
        return {"ok": True}

    return rx
