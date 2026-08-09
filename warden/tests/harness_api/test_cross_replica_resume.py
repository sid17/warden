"""EXT-C3c — cross-replica cold-resume of a paused durable-HITL run (hermetic).

The acceptance capability: a run paused for HITL on replica A is confirmed on replica B
— a DIFFERENT process that never held the run in memory. B must rebuild the run's
context from durable SHARED state (the run registry's persisted spec + the event log's
pending ask) and re-drive it to completion.

Proven here over shared LOCAL durable stores (a JSONL run registry + a sqlite event log
+ a file defer store, all under one shared dir — the A4 shared-volume model), so it runs
in the default suite with no DB. The identical stores on Postgres are proven in the C3a
live suite; the two-container Postgres+S3 proof is the Docker bed gate. What is under
test HERE is the reconstruct-and-redrive LOGIC (``_reconstruct_paused_run`` +
``confirm`` fallback), which is backend-agnostic.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from warden.harness_api.config import HarnessApiConfig
from warden.harness_api.runner import Runner
from warden.tests.harness_api.test_runs_durable_hitl import (
    _KEYS,
    _TOOL_ID,
    _durable_factory,
    _spec,
)


def _shared_cfg(tmp: Path) -> HarnessApiConfig:
    """A durable-HITL config whose registry + event log + defer store all live under one
    shared dir, so two Runners over the same ``tmp`` share durable state cross-process."""
    cfg = HarnessApiConfig()
    cfg.engine.permissions.handler = "durable_http"
    cfg.engine.persistence.session_db_path = str(tmp / "sessions.db")
    # Durable, shared run registry (default is process-local memory — no cross-replica).
    cfg.run_registry.store_backend = "jsonl"
    cfg.run_registry.state_dir = str(tmp)
    return cfg


def _replica(tmp: Path, tracker: dict) -> Runner:
    """A fresh Runner ("replica") over the shared ``tmp`` — its own in-memory _runs, but
    the same durable registry / event log / defer store on disk."""
    return Runner(_shared_cfg(tmp), keys=_KEYS, chat_api_factory=_durable_factory(tracker))


def test_paused_on_A_resumes_on_B():
    """Replica A pauses a run; replica B (never held it) confirms + completes it."""
    async def _run():
        tmp = Path(tempfile.mkdtemp())

        # --- replica A: run until it parks on the durable ask ---
        tracker_a: dict = {}
        a = _replica(tmp, tracker_a)
        await a.init()
        run_id = a.submit(_spec("u1", "c1"))
        await a.task_for(run_id)
        assert a.get(run_id).status == "requires_action"
        await a.aclose()  # A is gone — as if its container died / LB moved on

        # --- replica B: a fresh process, empty memory, same durable dir ---
        tracker_b: dict = {}
        b = _replica(tmp, tracker_b)
        await b.init()
        assert b.get(run_id) is None  # B never held this run in memory

        # Confirm on B: it must reconstruct the paused run from durable state + re-drive.
        result = await b.confirm(run_id, _TOOL_ID, decision="approve")
        assert result is not None and result["status"] == "resumed"
        await b.task_for(run_id)  # await B's re-drive

        assert b.get(run_id).status == "succeeded"
        assert tracker_b.get("ran_tool") == 1   # the tool ran ON B (cross-replica)
        assert "ran_tool" not in tracker_a       # A never ran it (it only paused)
        await b.aclose()

    asyncio.run(_run())


def test_reject_on_B_halts_the_run():
    """A paused run rejected on the other replica halts (deny), not runs the tool."""
    async def _run():
        tmp = Path(tempfile.mkdtemp())
        a = _replica(tmp, {})
        await a.init()
        run_id = a.submit(_spec("u1", "c1"))
        await a.task_for(run_id)
        assert a.get(run_id).status == "requires_action"
        await a.aclose()

        tracker_b: dict = {}
        b = _replica(tmp, tracker_b)
        await b.init()
        result = await b.confirm(run_id, _TOOL_ID, decision="reject", reason="no")
        assert result["status"] == "resumed"
        await b.task_for(run_id)
        assert b.get(run_id).status == "succeeded"  # model re-planned + reported
        assert tracker_b.get("ran_tool") is None      # the tool was NOT run
        await b.aclose()

    asyncio.run(_run())


def test_history_replay_from_a_different_replica_after_seq():
    """EXT-C3d — the reconnection path: a consumer that saw up to seq K reconnects on a
    DIFFERENT replica and replays ``after=K`` from the SHARED event log — the gap, in
    order, no loss, no dup (the log is INSERT-OR-IGNORE on (run_id, seq))."""
    async def _run():
        tmp = Path(tempfile.mkdtemp())
        # A runs a run to a pause, producing a durable event stream.
        a = _replica(tmp, {})
        await a.init()
        run_id = a.submit(_spec("u1", "c1"))
        await a.task_for(run_id)
        full = await a.replay(run_id, 0)
        assert len(full) >= 2  # at least session + permission_request
        await a.aclose()

        # B — a different replica — reconnects mid-stream: it already saw up to seq=1,
        # so it replays after=1 and must get exactly the tail (seq > 1), contiguous.
        b = _replica(tmp, {})
        await b.init()
        tail = await b.replay(run_id, 1)
        assert [e.seq for e in tail] == [e.seq for e in full if e.seq > 1]
        assert all(e.seq > 1 for e in tail)                 # no re-delivery of seen events
        assert len({e.seq for e in tail}) == len(tail)       # no duplicates
        # replaying past the end is empty (no phantom events)
        assert await b.replay(run_id, full[-1].seq) == []
        await b.aclose()

    asyncio.run(_run())


def test_confirm_unknown_run_on_B_is_404():
    """A confirm for a run absent from BOTH memory and durable state → None (404)."""
    async def _run():
        tmp = Path(tempfile.mkdtemp())
        b = _replica(tmp, {})
        await b.init()
        assert await b.confirm("does-not-exist", _TOOL_ID, decision="approve") is None
        await b.aclose()

    asyncio.run(_run())


def test_confirm_on_B_for_a_non_paused_run_does_not_reconstruct():
    """A completed run has no pending ask → reconstruct declines (not_pending/404),
    never a spurious resume."""
    async def _run():
        tmp = Path(tempfile.mkdtemp())
        # A runs to completion (approve on A itself), so there is no live pending ask.
        tracker_a: dict = {}
        a = _replica(tmp, tracker_a)
        await a.init()
        run_id = a.submit(_spec("u1", "c1"))
        await a.task_for(run_id)
        await a.confirm(run_id, _TOOL_ID, decision="approve")
        await a.task_for(run_id)
        assert a.get(run_id).status == "succeeded"
        await a.aclose()

        # B tries to confirm the already-finished run: reconstruct sees a non-paused
        # view → returns None → confirm 404s (no re-run of a done course).
        b = _replica(tmp, {})
        await b.init()
        assert await b.confirm(run_id, _TOOL_ID, decision="approve") is None
        await b.aclose()

    asyncio.run(_run())
