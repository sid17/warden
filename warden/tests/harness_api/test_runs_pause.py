"""M2 3f — pause-not-fail (GOV-6) in the governed Runner.

A pausable tenant (a BillingBackend whose ``supports_topup`` is True) whose run trips
the budget cap PAUSES: the terminal ``stopped`` event carries ``paused=True`` and the
run's status is ``"paused"`` (not ``"stopped"``). A non-pausable tenant hard-stops as
before. Hermetic (mock skill), ``asyncio.run(...)`` style.
"""

import asyncio
import tempfile
from pathlib import Path

from warden.harness_api.config import HarnessApiConfig
from warden.harness_api.credentials.keys import KeyRegistry
from warden.harness_api.event_log import RunEventLog
from warden.harness_api.governance import (
    GovernorService,
    InMemoryBillingBackend,
    InMemoryReservationLedger,
)
from warden.harness_api.runner import Runner
from warden.harness_api.schemas import RunSpec, Sink
from warden.tests.harness_api.mock_skill import Tracker, build_factory

_KEYS = KeyRegistry.from_config(
    {
        "keys": {"k1": {"provider": "claude", "secret_env": "S1"}},
        "users": {"u1": {"key_id": "k1", "budget_usd": 100.0}},
    },
    secrets={"S1": "sk-1"},
)


def _tmp_event_log() -> RunEventLog:
    return RunEventLog(Path(tempfile.mkdtemp()) / "run_events.db")


def _spec(user, task, *, budget=None):
    return RunSpec(
        user_id=user,
        task_id=task,
        provider="claude",
        model="claude-opus-4-8",
        input={"prompt": "hi", "label": "main"},
        sink=Sink(type="sse"),
        budget_usd=budget,
    )


def _governor(*, supports_topup: bool, balance: float = 100.0) -> GovernorService:
    billing = InMemoryBillingBackend({"u1": balance}, supports_topup=supports_topup)
    return GovernorService(
        key_registry=_KEYS,
        ledger=InMemoryReservationLedger(),
        billing=billing,
    )


def _make_runner(tracker, governor_service, **mock_kwargs):
    return Runner(
        HarnessApiConfig(),
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


# --- pausable tenant trips the budget cap => PAUSED -----------------------

def test_pausable_tenant_budget_stop_pauses():
    async def _run():
        tracker = Tracker()
        governor = _governor(supports_topup=True)
        # $0.01 cap, one turn of 1000 opus output tokens => $0.025 => Stop("budget").
        runner = _make_runner(
            tracker, governor,
            usage={"input_tokens": 10, "output_tokens": 1000},
        )
        run_id, events = await _run_and_collect(runner, _spec("u1", "c", budget=0.01))
        assert events[-1].type == "stopped"
        assert events[-1].data["reason"] == "budget"
        assert events[-1].data["paused"] is True
        assert events[-1].data["requires_action"] == "topup"
        view = runner.get(run_id)
        assert view.status == "paused"

    asyncio.run(_run())


# --- non-pausable tenant trips the budget cap => hard STOPPED --------------

def test_non_pausable_tenant_budget_stop_hard_stops():
    async def _run():
        tracker = Tracker()
        governor = _governor(supports_topup=False)
        runner = _make_runner(
            tracker, governor,
            usage={"input_tokens": 10, "output_tokens": 1000},
        )
        run_id, events = await _run_and_collect(runner, _spec("u1", "c", budget=0.01))
        assert events[-1].type == "stopped"
        assert events[-1].data["reason"] == "budget"
        assert not events[-1].data.get("paused")
        view = runner.get(run_id)
        assert view.status == "stopped"

    asyncio.run(_run())


# --- resume: a topped-up re-submit re-admits the run (idempotent) ----------

def test_topup_then_resubmit_readmits_run():
    async def _run():
        tracker = Tracker()
        # Tiny balance => the first run has no headroom and stops at pre_flight.
        billing = InMemoryBillingBackend({"u1": 0.0}, supports_topup=True)
        governor = GovernorService(
            key_registry=_KEYS,
            ledger=InMemoryReservationLedger(),
            billing=billing,
        )
        runner = _make_runner(tracker, governor)
        _, events1 = await _run_and_collect(runner, _spec("u1", "c"))
        assert events1[-1].type == "stopped"
        assert events1[-1].data.get("paused") is True  # pausable ⇒ paused

        # Top up, re-submit the same course: resolve() now reads the restored balance
        # and the run is admitted (RESUME == topped-up submit).
        billing.grant("u1", 50.0)
        _, events2 = await _run_and_collect(runner, _spec("u1", "c"))
        assert events2[-1].type == "result"

    asyncio.run(_run())


if __name__ == "__main__":  # pragma: no cover
    test_pausable_tenant_budget_stop_pauses()
    test_non_pausable_tenant_budget_stop_hard_stops()
    test_topup_then_resubmit_readmits_run()
