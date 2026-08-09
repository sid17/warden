"""M2 3a — the Governor seam (B17). Hermetic orchestrator-loop tests.

Asserts the fourth seam is an OPTIONAL callback threaded into the turn loop:
  * GOV-2 — no Governor wired ⇒ ``continue`` always; the harness behaves as
    ungoverned (the provider runs to completion, no ``StoppedEvent``).
  * A ``stop(reason)`` verdict HALTS the loop and yields a typed ``StoppedEvent``
    at the two loop-controlled checkpoints wired in 3a: ``pre_flight`` (the
    provider is never called) and ``turn_boundary`` (stop after the turn's usage
    is seen; the session is stopped).
  * GOV-1 — only ``Usage`` (tokens) + ``elapsed_s`` (seconds) cross the seam,
    never dollars/users.

Hermetic: no LLM / subprocess / network. Async style matches the repo
(``asyncio.run(...)``; see test_orchestrator_errors.py), NOT pytest-asyncio.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

from claude_agent_sdk import StreamEvent

from warden.orchestrator.orchestrator import Orchestrator
from warden.schemas.events import (
    CompletionEvent,
    MessageEvent,
    StoppedEvent,
)
from warden.schemas.usage import Usage
from warden.seams.governor import CONTINUE, Continue, Stop, Verdict


# --- Fakes (SDK-style, mirroring test_orchestrator_errors.py) --------------

class _FakeBlock:
    def __init__(self, class_name: str, **attrs: Any) -> None:
        self.__class__ = type(class_name, (), {})
        for k, v in attrs.items():
            object.__setattr__(self, k, v)


class _FakeMessage:
    def __init__(self, *blocks: _FakeBlock) -> None:
        self.content = list(blocks)


class _FakeResult:
    """A ResultMessage-shaped terminal carrying per-turn ``usage``."""

    def __init__(self, usage: dict[str, int]) -> None:
        self.__class__ = type("ResultMessage", (), {})
        object.__setattr__(self, "usage", usage)
        for k, v in {
            "duration_ms": 1, "is_error": False,
            "num_turns": 1, "total_cost_usd": 0.0,
        }.items():
            object.__setattr__(self, k, v)


class _StatusSession:
    """Yields one text block, then a ResultMessage with usage. Records send()."""

    def __init__(self, session_id: str = "gov-sess-1") -> None:
        self.session_id = session_id
        self.jsonl_path: str | None = None
        self.sent = False
        self.stopped = False
        self.closed = False

    async def start(self) -> None:
        pass

    async def send(self, prompt: str) -> AsyncGenerator[Any, None]:
        self.sent = True
        yield _FakeMessage(_FakeBlock("TextBlock", text="hello"))
        yield _FakeResult(usage={"input_tokens": 10, "output_tokens": 20})

    async def stop(self) -> None:
        self.stopped = True

    async def close(self) -> None:
        self.closed = True


class _FakeIndex:
    def __init__(self) -> None:
        self._entries: dict[str, dict] = {}

    async def get(self, session_id: str) -> dict | None:
        return self._entries.get(session_id)

    async def update_jsonl_path(self, session_id: str, jsonl_path: str) -> None:
        pass


class _FakeSessionManager:
    def __init__(self, session: Any) -> None:
        self._session = session
        self._active: dict[str, Any] = {}
        self._index = _FakeIndex()

    def get(self, session_id: str) -> Any:
        return self._active.get(session_id)

    async def create(self, **kwargs: Any) -> Any:
        return self._session

    async def register(self, session: Any, **kwargs: Any) -> None:
        if session.session_id:
            self._active[session.session_id] = session

    async def close(self, session_id: str) -> None:
        self._active.pop(session_id, None)


class _FakeGovernor:
    """Returns a fixed verdict per checkpoint; records every call (GOV-1 proof)."""

    def __init__(self, verdicts: dict[str, Verdict] | None = None) -> None:
        self._verdicts = verdicts or {}
        self.calls: list[tuple[str, Usage, float]] = []

    async def check(
        self, checkpoint: str, usage: Usage, elapsed_s: float,
    ) -> Verdict:
        self.calls.append((checkpoint, usage, elapsed_s))
        return self._verdicts.get(checkpoint, CONTINUE)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _make_orch(session: Any, governor: Any = None) -> Orchestrator:
    return Orchestrator(
        session_manager=_FakeSessionManager(session),
        repo_path=Path("."),
        governor=governor,
    )


async def _drain(orch: Orchestrator, content: str = "hi") -> list[Any]:
    return [event async for event in orch.send_message(content)]


# === GOV-2 — no Governor ⇒ ungoverned passthrough ==========================

def test_no_governor_runs_to_completion() -> None:
    async def _test() -> None:
        session = _StatusSession()
        events = await _drain(_make_orch(session, governor=None))
        assert session.sent is True
        assert [e for e in events if isinstance(e, MessageEvent)]
        assert [e for e in events if isinstance(e, CompletionEvent)]
        assert not [e for e in events if isinstance(e, StoppedEvent)]

    _run(_test())


# === stop(reason) at pre_flight ⇒ provider never called ====================

def test_preflight_stop_halts_before_provider() -> None:
    async def _test() -> None:
        session = _StatusSession()
        gov = _FakeGovernor({"pre_flight": Stop(reason="budget")})
        events = await _drain(_make_orch(session, governor=gov))

        # The provider was NEVER called (bounded before the first token).
        assert session.sent is False
        stops = [e for e in events if isinstance(e, StoppedEvent)]
        assert len(stops) == 1 and stops[0].reason == "budget"
        # A stopped run does NOT also complete.
        assert not [e for e in events if isinstance(e, CompletionEvent)]

    _run(_test())


# === stop(reason) at turn_boundary ⇒ halt after the turn's usage ===========

def test_turn_boundary_stop_halts_and_stops_session() -> None:
    async def _test() -> None:
        session = _StatusSession()
        gov = _FakeGovernor({"turn_boundary": Stop(reason="budget")})
        events = await _drain(_make_orch(session, governor=gov))

        assert session.sent is True
        # The turn's text still reached the consumer before the stop.
        assert [e for e in events if isinstance(e, MessageEvent)]
        stops = [e for e in events if isinstance(e, StoppedEvent)]
        assert len(stops) == 1 and stops[0].reason == "budget"
        assert not [e for e in events if isinstance(e, CompletionEvent)]
        assert session.stopped is True

        # GOV-1: the turn_boundary check saw normalized Usage (tokens) + a float
        # elapsed — never a dollar figure.
        tb = [c for c in gov.calls if c[0] == "turn_boundary"]
        assert tb, gov.calls
        _cp, usage, elapsed = tb[0]
        assert isinstance(usage, Usage)
        assert usage.input == 10 and usage.output == 20
        assert isinstance(elapsed, float)

    _run(_test())


# === clock-tick watchdog — the wall-clock time bound (3b / B18) ============

class _SlowSession:
    """Yields one message, then blocks cooperatively until ``stop()`` is called
    — models a long generation that only a deadline can end."""

    def __init__(self, session_id: str = "slow-sess-1") -> None:
        self.session_id = session_id
        self.jsonl_path: str | None = None
        self.sent = False
        self.stopped = False
        self.closed = False

    async def start(self) -> None:
        pass

    async def send(self, prompt: str) -> AsyncGenerator[Any, None]:
        self.sent = True
        yield _FakeMessage(_FakeBlock("TextBlock", text="working..."))
        for _ in range(2000):  # ~cooperative long-run; ends when stop() flips
            if self.stopped:
                break
            await asyncio.sleep(0.005)

    async def stop(self) -> None:
        self.stopped = True

    async def close(self) -> None:
        self.closed = True


def test_clock_tick_stop_halts_midstream_cooperatively() -> None:
    """A clock_tick stop verdict interrupts a still-streaming turn and yields a
    typed StoppedEvent — the wall-clock deadline, enforced through the seam. The
    engine never learns the deadline; it ticks and obeys (GOV-1)."""

    async def _test() -> None:
        session = _SlowSession()
        gov = _FakeGovernor({"clock_tick": Stop(reason="deadline")})
        orch = Orchestrator(
            session_manager=_FakeSessionManager(session),
            repo_path=Path("."),
            governor=gov,
            clock_tick_interval_s=0.01,
        )
        events = [e async for e in orch.send_message("hi")]

        assert session.sent is True
        stops = [e for e in events if isinstance(e, StoppedEvent)]
        assert len(stops) == 1 and stops[0].reason == "deadline"
        assert not [e for e in events if isinstance(e, CompletionEvent)]
        assert session.stopped is True
        # The clock_tick check saw a float elapsed (time only, never dollars).
        ticks = [c for c in gov.calls if c[0] == "clock_tick"]
        assert ticks and isinstance(ticks[0][2], float)

    _run(_test())


# === mid-stream tripwire — stop before the turn ends (3c/3e / B20a) ========

class _MidStreamSession:
    """Yields text, then a Claude ``message_delta`` carrying cumulative
    output tokens, then a terminal result. A ``mid_stream`` stop must halt the
    run at the delta — before the turn boundary is ever reached."""

    def __init__(self, session_id: str = "mid-sess-1") -> None:
        self.session_id = session_id
        self.jsonl_path: str | None = None
        self.sent = False
        self.stopped = False
        self.closed = False

    async def start(self) -> None:
        pass

    async def send(self, prompt: str) -> AsyncGenerator[Any, None]:
        self.sent = True
        yield _FakeMessage(_FakeBlock("TextBlock", text="partial"))
        yield StreamEvent(
            uuid="u1", session_id=self.session_id,
            event={
                "type": "message_delta", "delta": {},
                "usage": {"output_tokens": 9999},
            },
        )
        # Would normally end the turn — but a mid_stream stop fires first.
        yield _FakeResult(usage={"input_tokens": 10, "output_tokens": 9999})

    async def stop(self) -> None:
        self.stopped = True

    async def close(self) -> None:
        self.closed = True


def test_mid_stream_stop_halts_before_turn_boundary() -> None:
    async def _test() -> None:
        session = _MidStreamSession()
        gov = _FakeGovernor({"mid_stream": Stop(reason="budget")})
        events = await _drain(_make_orch(session, governor=gov))

        stops = [e for e in events if isinstance(e, StoppedEvent)]
        assert len(stops) == 1 and stops[0].reason == "budget"
        assert session.stopped is True
        assert not [e for e in events if isinstance(e, CompletionEvent)]

        # The tripwire fired on the delta's cumulative output tokens, and the
        # turn_boundary (terminal result) was never reached.
        mids = [c for c in gov.calls if c[0] == "mid_stream"]
        assert mids and mids[0][1].output == 9999
        assert not [c for c in gov.calls if c[0] == "turn_boundary"]

    _run(_test())


# === the seam defaults are pure data (no dollars in the engine) ============

def test_verdict_types_are_engine_opaque() -> None:
    assert isinstance(CONTINUE, Continue)
    assert Stop(reason="deadline").reason == "deadline"
