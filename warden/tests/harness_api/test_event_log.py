"""C2 — durable append-only run_events log + C1 schema-vocab tests.

Hermetic (aiosqlite on a tmp file, no server). Proves the contract: monotonic
seq, PK(run_id, seq), idempotent dup writes, replay from seq+1, and durability
across a close/reopen (history survives process teardown).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from warden.harness_api.event_log import RunEventLog
from warden.harness_api.schemas import Event, EventType, RunSpec, RunStatus


def _ev(run_id: str, seq: int, etype: str = "token", **data) -> Event:
    return Event(
        run_id=run_id, seq=seq, type=etype, session_id="s1",
        data=data or {"text": f"t{seq}"}, at="2026-07-18T00:00:00Z",
    )


async def _fresh_log(path: Path) -> RunEventLog:
    log = RunEventLog(path)
    await log.init()
    return log


def test_append_replay_roundtrip(tmp_path: Path) -> None:
    async def _run() -> None:
        log = await _fresh_log(tmp_path / "e.db")
        for i in range(1, 4):
            assert await log.append(_ev("run_1", i)) is True
        got = await log.replay("run_1")
        assert [e.seq for e in got] == [1, 2, 3]
        assert got[0].data == {"text": "t1"}
        await log.close()

    asyncio.run(_run())


def test_replay_after_seq_is_reconnect_cursor(tmp_path: Path) -> None:
    async def _run() -> None:
        log = await _fresh_log(tmp_path / "e.db")
        for i in range(1, 6):
            await log.append(_ev("run_1", i))
        # A consumer that saw up to seq=3 resumes at 4.
        got = await log.replay("run_1", after_seq=3)
        assert [e.seq for e in got] == [4, 5]
        assert await log.last_seq("run_1") == 5
        await log.close()

    asyncio.run(_run())


def test_duplicate_write_is_idempotent_noop(tmp_path: Path) -> None:
    async def _run() -> None:
        log = await _fresh_log(tmp_path / "e.db")
        assert await log.append(_ev("run_1", 1, text="first")) is True
        # Same (run_id, seq) again → no-op, original row preserved.
        assert await log.append(_ev("run_1", 1, text="SECOND")) is False
        got = await log.replay("run_1")
        assert len(got) == 1
        assert got[0].data == {"text": "first"}
        await log.close()

    asyncio.run(_run())


def test_runs_are_isolated(tmp_path: Path) -> None:
    async def _run() -> None:
        log = await _fresh_log(tmp_path / "e.db")
        await log.append(_ev("run_A", 1))
        await log.append(_ev("run_B", 1))
        await log.append(_ev("run_B", 2))
        assert [e.seq for e in await log.replay("run_A")] == [1]
        assert [e.seq for e in await log.replay("run_B")] == [1, 2]
        assert await log.last_seq("run_missing") == 0
        await log.close()

    asyncio.run(_run())


def test_history_survives_close_and_reopen(tmp_path: Path) -> None:
    """Durability: a brand-new log on the same path replays prior history."""
    async def _run() -> None:
        path = tmp_path / "e.db"
        log = await _fresh_log(path)
        for i in range(1, 4):
            await log.append(_ev("run_1", i, etype="tool_use"))
        await log.close()  # simulate process teardown

        reopened = await _fresh_log(path)  # a fresh process
        got = await reopened.replay("run_1")
        assert [e.seq for e in got] == [1, 2, 3]
        assert all(e.type == "tool_use" for e in got)
        await reopened.close()

    asyncio.run(_run())


def test_init_is_idempotent(tmp_path: Path) -> None:
    async def _run() -> None:
        log = RunEventLog(tmp_path / "e.db")
        await log.init()
        await log.init()  # second init is a no-op, not an error
        await log.append(_ev("run_1", 1))
        assert await log.last_seq("run_1") == 1
        await log.close()

    asyncio.run(_run())


# --- C1: durable-vocab schema additions -----------------------------------

def test_c1_new_event_types_accepted() -> None:
    for t in ("stopped", "compaction", "tool_result"):
        e = Event(run_id="r", seq=1, type=t, at="2026-07-18T00:00:00Z")
        assert e.type == t


def test_c1_new_run_statuses_valid() -> None:
    import typing

    valid = set(typing.get_args(RunStatus))
    assert {"paused", "requires_action"} <= valid
    valid_events = set(typing.get_args(EventType))
    assert {"stopped", "compaction", "tool_result"} <= valid_events


def test_c1_runspec_bounds_default_none_and_accept() -> None:
    from warden.harness_api.schemas import Sink

    base = RunSpec(user_id="u", task_id="t", sink=Sink(type="sse"))
    assert base.deadline is None and base.max_turns is None
    bounded = RunSpec(
        user_id="u", task_id="t", sink=Sink(type="sse"),
        deadline="2026-07-18T01:00:00Z", max_turns=5,
    )
    assert bounded.max_turns == 5 and bounded.deadline.endswith("Z")


def test_c1_invalid_event_type_rejected() -> None:
    with pytest.raises(Exception):
        Event(run_id="r", seq=1, type="bogus_kind", at="2026-07-18T00:00:00Z")
