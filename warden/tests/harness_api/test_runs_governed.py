"""M2 3e-2 — governed-Runner tests: the GovernorService wired into the Runner.

Hermetic (mock skill, no LLM/subprocess), following the repo's
``asyncio.run(_run())`` pattern. Exercises the governed path end to end: a normal
governed run settles its reservation; a cost/headroom breach yields a ``stopped``
terminal; an unhonorable deadline is rejected at submit; and the ungoverned path
(no ``governor_service``) is unchanged (GOV-2).
"""

import asyncio
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from warden.harness_api.config import GovernanceConfig, HarnessApiConfig
from warden.harness_api.credentials.keys import KeyRegistry
from warden.harness_api.event_log import RunEventLog
from warden.harness_api.governance import (
    GovernorService,
    InMemoryReservationLedger,
    StaticBalanceSource,
)
from warden.harness_api.runner import Runner
from warden.harness_api.schemas import RunSpec, Sink
from warden.tests.harness_api.mock_skill import Tracker, build_factory

# --- keys: one user u1 → managed claude key ------------------------------
_KEYS = KeyRegistry.from_config(
    {
        "keys": {"k1": {"provider": "claude", "secret_env": "S1"}},
        "users": {"u1": {"key_id": "k1", "budget_usd": 100.0}},
    },
    secrets={"S1": "sk-1"},
)


def _tmp_event_log() -> RunEventLog:
    return RunEventLog(Path(tempfile.mkdtemp()) / "run_events.db")


def _spec(user, task, *, provider="claude", model="claude-opus-4-8",
          budget=None, deadline=None, max_turns=None, label="main"):
    return RunSpec(
        user_id=user,
        task_id=task,
        provider=provider,
        model=model,
        input={"prompt": "hi", "label": label},
        sink=Sink(type="sse"),
        budget_usd=budget,
        deadline=deadline,
        max_turns=max_turns,
    )


def _governor(balances: dict[str, float], ledger=None) -> GovernorService:
    return GovernorService(
        key_registry=_KEYS,
        ledger=ledger or InMemoryReservationLedger(),
        balance_source=StaticBalanceSource(balances),
    )


def _make_runner(tracker, *, governor_service=None, **mock_kwargs):
    # Config governance OFF so the GOV-2 test (no governor_service) truly exercises
    # the ungoverned path; the governed tests inject an explicit governor_service,
    # which wins regardless. (Governance defaults ON as of 3g.2b.)
    return Runner(
        HarnessApiConfig(governance=GovernanceConfig(enabled=False)),
        keys=_KEYS,
        chat_api_factory=build_factory(tracker, **mock_kwargs),
        event_log=_tmp_event_log(),
        governor_service=governor_service,
    )


async def _run_and_collect(runner, spec):
    run_id = runner.submit(spec)
    await runner.task_for(run_id)
    sse = runner.sse_for(run_id)
    events = [e async for e in sse.stream()]
    return run_id, events


# --- 1. governed run settles the reservation to the actual ----------------

def test_governed_run_settles_reservation():
    async def _run():
        tracker = Tracker()
        ledger = InMemoryReservationLedger()
        governor = _governor({"u1": 100.0}, ledger=ledger)
        # A small, cheap turn — a few output tokens => sub-cent actual cost.
        runner = _make_runner(
            tracker, governor_service=governor,
            usage={"input_tokens": 10, "output_tokens": 20},
        )
        run_id, events = await _run_and_collect(runner, _spec("u1", "course_A"))
        # Clean finish (a result terminal), governed auth threaded from resolve().
        assert events[-1].type == "result"
        assert tracker.calls[0][1] == {"ANTHROPIC_API_KEY": "sk-1"}
        # The hold was settled to the actual: committed for the tenant is the tiny
        # actual (~ a fraction of a cent), NOT the 16384-token worst-case hold.
        committed = ledger._committed["u1"]
        assert 0.0 < committed < 0.01
        view = runner.get(run_id)
        assert view.status == "succeeded"

    asyncio.run(_run())


# --- 2. cost cap crossed at the turn boundary => stopped terminal ---------

def test_cost_stop_yields_stopped_terminal():
    async def _run():
        tracker = Tracker()
        governor = _governor({"u1": 100.0})
        # budget_usd = cost cap = $0.01. One turn of 1000 output tokens on opus
        # ($25/Mtok) => $0.025 committed >= cap => Stop("budget") at turn_boundary.
        runner = _make_runner(
            tracker, governor_service=governor,
            usage={"input_tokens": 10, "output_tokens": 1000},
        )
        run_id, events = await _run_and_collect(
            runner, _spec("u1", "course_A", budget=0.01)
        )
        assert events[-1].type == "stopped"
        assert events[-1].data["reason"] == "budget"
        view = runner.get(run_id)
        assert view.status == "stopped"
        # The mock DID run (a turn happened) before the boundary stop.
        assert tracker.calls != []

    asyncio.run(_run())


# --- 3. no balance headroom => pre_flight stop, minimal provider work -----

def test_no_headroom_stops_at_preflight():
    async def _run():
        tracker = Tracker()
        governor = _governor({"u1": 0.0})  # zero balance => no headroom
        runner = _make_runner(tracker, governor_service=governor)
        run_id, events = await _run_and_collect(runner, _spec("u1", "course_A"))
        assert events[-1].type == "stopped"
        assert events[-1].data["reason"] == "budget"
        assert runner.get(run_id).status == "stopped"
        # pre_flight stops before any token/result — only session + stopped emit.
        assert [e.type for e in events] == ["session", "stopped"]

    asyncio.run(_run())


# --- 4. deadline on openharness rejected at submit ------------------------

def test_deadline_on_openharness_rejected_at_submit():
    async def _run():
        tracker = Tracker()
        governor = _governor({"u1": 100.0})
        runner = _make_runner(tracker, governor_service=governor)
        deadline = (
            datetime.now(timezone.utc) + timedelta(minutes=5)
        ).isoformat()
        run_id, events = await _run_and_collect(
            runner, _spec("u1", "course_A", provider="openharness", deadline=deadline)
        )
        assert len(events) == 1
        assert events[0].type == "error"
        assert events[0].data["reason"] == "deadline_unsupported_on_provider"
        # The run never started — the factory/mock was never invoked.
        assert tracker.calls == []
        assert runner.get(run_id).status == "error"

    asyncio.run(_run())


# --- 5. GOV-2: no governor_service => the ungoverned path is unchanged -----

def test_ungoverned_path_unchanged():
    async def _run():
        tracker = Tracker()
        runner = _make_runner(tracker)  # no governor_service
        run_id, events = await _run_and_collect(runner, _spec("u1", "course_A"))
        assert events[0].type == "session"
        assert events[-1].type == "result"
        assert runner.get(run_id).status == "succeeded"
        # Ungoverned auth still comes straight from the KeyRegistry.
        assert tracker.calls[0][1] == {"ANTHROPIC_API_KEY": "sk-1"}

    asyncio.run(_run())
