"""C2 — the Runner mirrors every event to the durable log; /history replays it.

Deterministic (mock skill, no LLM). Proves the runner-level guarantee: after a
run finishes, its full event stream is durably recorded and replayable — even from
a brand-new RunEventLog on the same path (history survives process teardown).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from warden.harness_api.app import create_app
from warden.harness_api.event_log import RunEventLog
from warden.harness_api.runner import Runner
from warden.tests.harness_api.mock_skill import Tracker, build_factory
from warden.tests.harness_api.test_runs_api import (
    _KEYS,
    _spec,
    _ungoverned_config,
)


def _runner_with_log(tracker, path: Path) -> Runner:
    return Runner(
        _ungoverned_config(),
        keys=_KEYS,
        chat_api_factory=build_factory(tracker),
        event_log=RunEventLog(path),
    )


def test_runner_mirrors_full_stream_to_durable_log(tmp_path: Path) -> None:
    async def _run() -> None:
        path = tmp_path / "run_events.db"
        runner = _runner_with_log(Tracker(), path)
        run_id = runner.submit(_spec("u1", "course_A"))
        await runner.task_for(run_id)

        # In-process replay returns the same stream the SSE consumer saw.
        durable = await runner.replay(run_id)
        assert durable, "durable log recorded no events"
        seqs = [e.seq for e in durable]
        assert seqs == sorted(seqs) == list(range(1, len(seqs) + 1)), "seq not monotonic 1..n"
        # First event is the session; a terminal result/error closes it.
        assert durable[0].type == "session"
        assert durable[-1].type in ("result", "error")
        # after_seq cursor works at the runner level too.
        assert [e.seq for e in await runner.replay(run_id, after_seq=seqs[-1])] == []
        await runner.aclose()

    asyncio.run(_run())


def test_history_survives_runner_teardown(tmp_path: Path) -> None:
    """A fresh RunEventLog on the same path replays a completed run's history."""
    async def _run() -> None:
        path = tmp_path / "run_events.db"
        runner = _runner_with_log(Tracker(), path)
        run_id = runner.submit(_spec("u1", "course_A"))
        await runner.task_for(run_id)
        recorded = [(e.seq, e.type) for e in await runner.replay(run_id)]
        await runner.aclose()  # process gone; in-memory registry lost

        # New process: only the on-disk log remains.
        fresh = RunEventLog(path)
        await fresh.init()
        replayed = [(e.seq, e.type) for e in await fresh.replay(run_id)]
        assert replayed == recorded and replayed, "history did not survive teardown"
        await fresh.close()

    asyncio.run(_run())


def test_history_endpoint_replays_durably(tmp_path: Path) -> None:
    async def _run() -> None:
        path = tmp_path / "run_events.db"
        runner = _runner_with_log(Tracker(), path)
        app = create_app(runner)
        transport = httpx.ASGITransport(app=app)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=transport, base_url="http://t"
            ) as client:
                r = await client.post("/runs", json=_spec("u1", "course_A").model_dump())
                run_id = r.json()["run_id"]
                await runner.task_for(run_id)

                full = await client.get(f"/runs/{run_id}/history")
                events = full.json()
                assert events and events[0]["type"] == "session"

                # after= cursor: only the tail past seq 1.
                tail = await client.get(f"/runs/{run_id}/history", params={"after": 1})
                tail_events = tail.json()
                assert all(e["seq"] > 1 for e in tail_events)
                assert len(tail_events) == len(events) - 1

    asyncio.run(_run())
