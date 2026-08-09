"""EXT-C3 — the Postgres event log against a LIVE Postgres (opt-in).

Skipped unless ``WARDEN_TEST_POSTGRES_DSN`` is set — the default hermetic suite stays
DB-free (like the run registry). Run it locally against a throwaway DB to prove the
real SQL:

    WARDEN_TEST_POSTGRES_DSN=postgresql://warden:warden@localhost:5432/warden \\
      uv run --no-sync python -m pytest \\
      warden/tests/harness_api/test_postgres_event_log_live.py -q

Uses a unique ``run_id`` per test (uuid4) and deletes its own rows, so it is safe to
point at a shared DB. The Docker bed gate is the fuller proof (a real replica
serving/resuming another replica's run).
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

from warden.harness_api.postgres_event_log import PostgresRunEventLog
from warden.harness_api.schemas import Event

_DSN = os.environ.get("WARDEN_TEST_POSTGRES_DSN")

pytestmark = pytest.mark.skipif(
    not _DSN, reason="set WARDEN_TEST_POSTGRES_DSN to run the live Postgres tests"
)


def _ev(run_id: str, seq: int, type_: str, **data) -> Event:
    return Event(
        run_id=run_id,
        seq=seq,
        type=type_,
        session_id=data.pop("session_id", None),
        data=data,
        at="2026-07-29T00:00:00Z",
    )


async def _cleanup(log: PostgresRunEventLog, run_id: str) -> None:
    async with log._pool.acquire() as conn:  # noqa: SLF001 (test teardown)
        await conn.execute("DELETE FROM run_events WHERE run_id = $1", run_id)


def test_live_append_replay_round_trip():
    async def _run():
        log = await PostgresRunEventLog.connect(_DSN)
        run_id = f"c3-{uuid.uuid4()}"
        try:
            assert await log.replay(run_id) == []  # absent before append
            assert await log.append(
                _ev(run_id, 1, "session", session_id="sess-1")
            ) is True
            assert await log.append(
                _ev(run_id, 2, "token", text="hello")
            ) is True
            events = await log.replay(run_id)
            assert [e.seq for e in events] == [1, 2]
            assert events[0].session_id == "sess-1"
            assert events[1].data == {"text": "hello"}  # dict round-trips
            assert await log.last_seq(run_id) == 2
            # after_seq resumes past the cursor
            assert [e.seq for e in await log.replay(run_id, after_seq=1)] == [2]
        finally:
            await _cleanup(log, run_id)
            await log.close()

    asyncio.run(_run())


def test_live_append_idempotent_on_run_seq():
    """A duplicate (run_id, seq) returns False and adds no row (ON CONFLICT DO
    NOTHING) — the first writer wins."""
    async def _run():
        log = await PostgresRunEventLog.connect(_DSN)
        run_id = f"c3-{uuid.uuid4()}"
        try:
            assert await log.append(_ev(run_id, 1, "token", text="first")) is True
            # same (run_id, seq) again → no-op, returns False
            assert await log.append(_ev(run_id, 1, "token", text="second")) is False
            events = await log.replay(run_id)
            assert len(events) == 1
            assert events[0].data == {"text": "first"}  # first writer wins, no dup
        finally:
            await _cleanup(log, run_id)
            await log.close()

    asyncio.run(_run())


def test_live_reconstruct_view_succeeded():
    async def _run():
        log = await PostgresRunEventLog.connect(_DSN)
        run_id = f"c3-{uuid.uuid4()}"
        try:
            await log.append(_ev(run_id, 1, "session", session_id="s"))
            await log.append(
                _ev(run_id, 2, "result", usage={"input": 10, "output": 5},
                    cost_usd=0.02)
            )
            view = await log.reconstruct_view(run_id)
            assert view is not None
            assert view.status == "succeeded"
            assert view.session_id == "s"
            assert view.last_seq == 2
            assert view.usage == {"input": 10, "output": 5}
            assert view.cost_usd == 0.02
        finally:
            await _cleanup(log, run_id)
            await log.close()

    asyncio.run(_run())


def test_live_reconstruct_view_requires_action():
    """A trailing permission_request with no later permission_resolved → the durable
    HITL pause (requires_action)."""
    async def _run():
        log = await PostgresRunEventLog.connect(_DSN)
        run_id = f"c3-{uuid.uuid4()}"
        try:
            await log.append(_ev(run_id, 1, "session", session_id="s"))
            await log.append(
                _ev(run_id, 2, "permission_request", tool_name="Bash")
            )
            view = await log.reconstruct_view(run_id)
            assert view is not None and view.status == "requires_action"
            # a later resolve clears the pending flag → running
            await log.append(_ev(run_id, 3, "permission_resolved", allow=True))
            view2 = await log.reconstruct_view(run_id)
            assert view2 is not None and view2.status == "running"
        finally:
            await _cleanup(log, run_id)
            await log.close()

    asyncio.run(_run())


def test_live_revise_round_counting():
    """revise_round counts permission_request events matching data.tool_name."""
    async def _run():
        log = await PostgresRunEventLog.connect(_DSN)
        run_id = f"c3-{uuid.uuid4()}"
        try:
            await log.append(_ev(run_id, 1, "permission_request", tool_name="Bash"))
            await log.append(_ev(run_id, 2, "permission_request", tool_name="Write"))
            await log.append(_ev(run_id, 3, "permission_request", tool_name="Bash"))
            assert await log.revise_round(run_id, "Bash") == 2
            assert await log.revise_round(run_id, "Write") == 1
            assert await log.revise_round(run_id, "Read") == 0
        finally:
            await _cleanup(log, run_id)
            await log.close()

    asyncio.run(_run())


def test_live_cross_instance_visibility():
    """The multi-replica invariant: events written by instance A are replayed by a
    SEPARATE instance B (fresh pool) — the read is live shared state, not a cache."""
    async def _run():
        writer = await PostgresRunEventLog.connect(_DSN)
        reader = await PostgresRunEventLog.connect(_DSN)  # a distinct "replica"
        run_id = f"c3-{uuid.uuid4()}"
        try:
            await writer.append(_ev(run_id, 1, "session", session_id="sX"))
            await writer.append(_ev(run_id, 2, "result", cost_usd=0.01))
            events = await reader.replay(run_id)  # reader never saw the append locally
            assert [e.seq for e in events] == [1, 2]
            view = await reader.reconstruct_view(run_id)
            assert view is not None and view.status == "succeeded"
        finally:
            await _cleanup(writer, run_id)
            await writer.close()
            await reader.close()

    asyncio.run(_run())
