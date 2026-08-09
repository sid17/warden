"""A deterministic mock agent for the §9 Runs-API tests — no LLM, no subprocess.

``MockChatAPI`` mimics the ``ChatAPI`` surface the runner drives (``init`` /
``send`` async-generator / ``close``) and yields a scripted sequence of
``OrchestratorEvent``s: a session event (new or resumed), token(s), an optional
checkpoint, then a result-status carrying usage, then completion. ``build_factory``
returns a ``ChatApiFactory`` that records the ``(spec, auth_env)`` of every run so
tests can assert per-user key isolation and concurrency behavior.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from warden.schemas.events import (
    CompletionEvent,
    MessageEvent,
    OrchestratorEvent,
    SessionCreatedEvent,
    StoppedEvent,
)
from warden.schemas.usage import Usage, normalize_usage
from warden.seams.governor import Stop


@dataclass
class Tracker:
    """Shared observation state across all mock runs in one test."""

    calls: list[tuple[Any, dict | None]] = field(default_factory=list)
    active: int = 0
    max_active: int = 0
    started: asyncio.Event | None = None  # set when active reaches a target


class MockChatAPI:
    def __init__(
        self,
        *,
        spec: Any,
        auth_env: dict | None,
        tracker: Tracker,
        tokens: tuple[str, ...] = ("Hello ", "world"),
        checkpoint: dict | None = None,
        usage: dict | None = None,
        result: str = "done",
        gate: asyncio.Event | None = None,
    ) -> None:
        self._spec = spec
        # New-session id models "many sessions over one folder": distinct logical
        # labels (creation/qa/notes) on the same task_id get distinct session ids.
        label = spec.input.get("label", "main")
        self._session_new = f"sess-{spec.task_id}-{label}"
        self._tracker = tracker
        self._tokens = tokens
        self._checkpoint = checkpoint
        self._usage = usage or {"input_tokens": 10, "output_tokens": 20}
        self._result = result
        self._gate = gate
        # Optional per-run Governor (threaded by the Runner via set_governor).
        # None ⇒ ungoverned: send() behaves EXACTLY as before (existing tests).
        self._governor = None
        tracker.calls.append((spec, auth_env))

    def set_governor(self, governor) -> None:
        self._governor = governor

    async def init(self) -> None:
        return None

    async def send(
        self,
        content: str,
        *,
        mode: str = "free",
        session_id: str | None = None,
        workflow: str | None = None,
    ) -> AsyncIterator[OrchestratorEvent]:
        t = self._tracker
        t.active += 1
        t.max_active = max(t.max_active, t.active)
        if t.started is not None and t.active >= 2:
            t.started.set()
        try:
            resumed = session_id is not None
            sid = session_id or self._session_new
            # Governed: a pre_flight breach (no headroom) stops the run before any
            # provider work — the session is created but nothing else is yielded.
            if self._governor is not None:
                verdict = await self._governor.check("pre_flight", Usage(), 0.0)
                if isinstance(verdict, Stop):
                    yield SessionCreatedEvent(session_id=sid, resumed=resumed)
                    yield StoppedEvent(reason=verdict.reason, session_id=sid)
                    return
            yield SessionCreatedEvent(session_id=sid, resumed=resumed)
            for tok in self._tokens:
                yield MessageEvent(kind="text", content={"text": tok}, session_id=sid)
            if self._checkpoint is not None:
                yield MessageEvent(
                    kind="checkpoint", content=self._checkpoint, session_id=sid
                )
            if self._gate is not None:
                await self._gate.wait()
            yield MessageEvent(
                kind="status",
                content={
                    "subtype": "result",
                    "result": self._result,
                    "usage": self._usage,
                },
                session_id=sid,
            )
            # Governed: commit this turn's cost at the boundary; a cost/turn breach
            # stops the run — emit a StoppedEvent instead of a clean completion.
            if self._governor is not None:
                verdict = await self._governor.check(
                    "turn_boundary", normalize_usage(self._usage), 0.0
                )
                if isinstance(verdict, Stop):
                    yield StoppedEvent(reason=verdict.reason, session_id=sid)
                    return
            yield CompletionEvent(session_id=sid)
        finally:
            t.active -= 1

    async def close(self) -> None:
        return None


def build_factory(tracker: Tracker, **kwargs: Any):
    """Return a ``ChatApiFactory`` producing ``MockChatAPI``s wired to ``tracker``."""

    def factory(spec: Any, auth_env: dict | None) -> MockChatAPI:
        return MockChatAPI(spec=spec, auth_env=auth_env, tracker=tracker, **kwargs)

    return factory
