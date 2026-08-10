"""Empty-resume recovery — a durable resume that yields ZERO assistant messages is
re-driven, not terminated.

Live bug (course run ``44622262…``): under the B1 continuation hook a durable APPROVE
resume occasionally came back EMPTY — the provider SDK yielded no assistant message at
all (``result:""``, ``input_tokens:0``). The continuation Stop hook can't catch that (it
only fires on an assistant end_turn), so the Runner emitted a spurious empty ``result``
terminal and the run ended BEFORE its completion tool → no ``draft_manifest`` → the
product surfaced "Creation failed — result event but no draft_manifest to bridge".

The fix: on a zero-message durable resume (when a continuation contract is in force), the
Runner RE-DRIVES the same session with a firm continuation (bounded by
``_MAX_EMPTY_REDRIVES``) instead of terminating. These tests inject the empty resume
deterministically via a mock and prove (1) recovery converges and (2) a persistent empty
resume is bounded (no infinite loop).
"""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from warden.harness_api._run_state import _DURABLE_RESUME_REDRIVE, _MAX_EMPTY_REDRIVES
from warden.harness_api.config import HarnessApiConfig
from warden.harness_api.credentials.keys import KeyRegistry
from warden.harness_api.runner import Runner
from warden.harness_api.schemas import RunSpec, Sink
from warden.schemas.events import (
    CompletionEvent,
    ErrorEvent,
    MessageEvent,
    OrchestratorEvent,
    SessionCreatedEvent,
    StoppedEvent,
)

_KEYS = KeyRegistry.from_config(
    {
        "keys": {"k1": {"provider": "claude", "secret_env": "S1"}},
        "users": {"u1": {"key_id": "k1", "budget_usd": 100.0}},
    },
    secrets={"S1": "sk-1"},
)

_GATE_TOOL = "confirm_landscape"
_DONE_TOOL = "course_complete"
_GATE_ID = "toolu_landscape_1"


def _empty_status(sid: str) -> MessageEvent:
    """The REAL empty-resume shape: a single zero-usage terminal ``status`` message —
    no thinking/text/tool_use. This is what the SDK actually yields on the glitch
    (verified live: events={SessionCreatedEvent, MessageEvent:status, CompletionEvent},
    usage input:0/output:0). A naive ``messages == 0`` detector MISSES it (this IS a
    message); the fix keys on ``produced == 0`` (no work-kind message)."""
    return MessageEvent(
        kind="status",
        content={"subtype": "result", "result": "",
                 "usage": {"input_tokens": 0, "output_tokens": 0}},
        session_id=sid,
    )


class _EmptyResumeGateMock:
    """A landscape gate that reproduces the intermittent empty resume with the REAL
    event shape.

    - initial pass: consults the durable handler for ``confirm_landscape`` → defers →
      the run parks;
    - the APPROVE resume: yields ONLY the zero-usage terminal ``status`` message (no
      work) — the empty-resume glitch (``produced == 0``, but ``messages == 1``);
    - a RE-DRIVE (``content == _DURABLE_RESUME_REDRIVE``): streams ``course_complete``
      and finishes — UNLESS ``tracker['always_empty']`` (persistent glitch → even the
      re-drive comes back empty → the bounded-retry path);
    - ``tracker['resume_mode']`` == ``stopped``/``errored`` models a Governor halt / a
      provider error on the resume (a REAL terminal that ALSO yields no work — must NOT
      be re-driven).
    """

    def __init__(self, *, spec: Any, auth_env: dict | None, tracker: dict) -> None:
        self._sid = f"sess-{spec.task_id}"
        self._handler = None
        self._tracker = tracker
        tracker.setdefault("sends", [])
        tracker.setdefault("streamed", [])

    def set_durable_defer(self, dd) -> None:
        from warden.seams.defer import DurableDeferHandler
        from warden.seams.defer_store import FileDeferStore

        self._handler = DurableDeferHandler(FileDeferStore(dd.store_root))

    async def init(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def send(
        self, content: str, *, session_id: str | None = None,
        workflow: str | None = None,
    ) -> AsyncIterator[OrchestratorEvent]:
        self._tracker["sends"].append(content)
        sid = session_id or self._sid
        yield SessionCreatedEvent(session_id=sid, resumed=session_id is not None)

        if session_id is not None:
            # A durable resume. Complete on the re-drive (unless a persistent glitch).
            if content == _DURABLE_RESUME_REDRIVE and not self._tracker.get(
                "always_empty"
            ):
                self._tracker["streamed"].append(_DONE_TOOL)
                yield MessageEvent(
                    kind="tool_use", content={"toolName": _DONE_TOOL}, session_id=sid
                )
                yield MessageEvent(
                    kind="status",
                    content={"subtype": "result", "result": "course built",
                             "usage": {"input_tokens": 5, "output_tokens": 5}},
                    session_id=sid,
                )
                yield CompletionEvent(session_id=sid)
                return
            mode = self._tracker.get("resume_mode", "empty")
            if mode == "stopped":
                # A Governor halt on resume — no work, but a REAL terminal (must not
                # be re-driven; that would spend past the budget/deadline stop).
                yield StoppedEvent(reason="budget", session_id=sid)
                return
            if mode == "errored":
                yield ErrorEvent(text="provider boom", session_id=sid)
                return
            # The empty resume (the real shape): a zero-usage terminal status message
            # and nothing else → produced == 0 → the Runner re-drives.
            yield _empty_status(sid)
            yield CompletionEvent(session_id=sid)
            return

        # Initial pass: propose the landscape → the gate defers → the run parks.
        decision = await self._handler.request_permission(
            _GATE_TOOL, {"concepts": ["a", "b"]}, "confirm the landscape",
            tool_use_id=_GATE_ID,
        )
        if "defer" in decision.source:
            return  # ejected → park


def _factory(tracker: dict):
    def factory(spec: Any, auth_env: dict | None) -> _EmptyResumeGateMock:
        return _EmptyResumeGateMock(spec=spec, auth_env=auth_env, tracker=tracker)

    return factory


def _config(tmp: Path) -> HarnessApiConfig:
    cfg = HarnessApiConfig()
    cfg.engine.permissions.handler = "durable_http"
    cfg.engine.persistence.session_db_path = str(tmp / "sessions.db")
    # The continuation contract is what gates the empty-resume recovery.
    cfg.engine.continuation.enabled = True
    cfg.engine.continuation.until_tool = _DONE_TOOL
    return cfg


def _spec() -> RunSpec:
    return RunSpec(
        user_id="u1", task_id="c1", provider="claude", model="claude-opus-4-8",
        input={"prompt": "author a course"}, sink=Sink(type="sse"),
    )


async def _poll(runner: Runner, run_id: str, targets: set[str], tries: int = 300) -> str:
    for _ in range(tries):
        if runner.get(run_id).status in targets:
            break
        await asyncio.sleep(0.01)
    return runner.get(run_id).status


def test_empty_resume_is_redriven_and_converges():
    """An APPROVE resume that yields zero messages is re-driven (not terminated); the
    re-drive reaches ``course_complete`` and the run converges."""
    async def _run():
        tmp = Path(tempfile.mkdtemp())
        tracker: dict = {}
        runner = Runner(_config(tmp), keys=_KEYS, chat_api_factory=_factory(tracker))
        await runner.init()
        run_id = runner.submit(_spec())
        await runner.task_for(run_id)  # initial pass parks on confirm_landscape
        assert runner.get(run_id).status == "requires_action"

        result = await runner.confirm(run_id, _GATE_ID, decision="approve")
        assert result["status"] == "resumed"
        await runner.task_for(run_id)  # empty resume → re-drive → converge

        assert runner.get(run_id).status == "succeeded"
        # The completion tool fired on the re-drive (not lost to a premature terminal).
        assert tracker["streamed"] == [_DONE_TOOL]
        # Exactly one re-drive was issued (one empty resume recovered).
        assert tracker["sends"].count(_DURABLE_RESUME_REDRIVE) == 1
        # A real result terminal exists (converged), no error.
        events = await runner.replay(run_id)
        assert any(e.type == "result" for e in events)
        assert not [e for e in events if e.type == "error"]
        await runner.aclose()

    asyncio.run(_run())


def test_persistent_empty_resume_is_bounded():
    """If the empty resume PERSISTS (every resume comes back empty), the Runner re-drives
    at most ``_MAX_EMPTY_REDRIVES`` times then terminates — it never loops forever."""
    async def _run():
        tmp = Path(tempfile.mkdtemp())
        tracker: dict = {"always_empty": True}
        runner = Runner(_config(tmp), keys=_KEYS, chat_api_factory=_factory(tracker))
        await runner.init()
        run_id = runner.submit(_spec())
        await runner.task_for(run_id)
        assert runner.get(run_id).status == "requires_action"

        await runner.confirm(run_id, _GATE_ID, decision="approve")
        # Bounded: must reach a terminal (not hang) within a generous poll.
        await asyncio.wait_for(runner.task_for(run_id), timeout=10)
        assert runner.get(run_id).status in ("succeeded", "error")
        # Re-driven exactly _MAX_EMPTY_REDRIVES times, then it gave up.
        assert tracker["sends"].count(_DURABLE_RESUME_REDRIVE) == _MAX_EMPTY_REDRIVES
        # The completion tool never fired (the glitch never cleared).
        assert tracker["streamed"] == []
        await runner.aclose()

    asyncio.run(_run())


def test_stopped_resume_is_not_redriven():
    """A Governor halt on a durable resume yields NO work (produced == 0) but is a REAL
    terminal — it must NOT be re-driven (re-driving would spend past the budget/deadline
    stop). The stopped guard keeps the empty-resume recovery from swallowing it."""
    async def _run():
        tmp = Path(tempfile.mkdtemp())
        tracker: dict = {"resume_mode": "stopped"}
        runner = Runner(_config(tmp), keys=_KEYS, chat_api_factory=_factory(tracker))
        await runner.init()
        run_id = runner.submit(_spec())
        await runner.task_for(run_id)
        assert runner.get(run_id).status == "requires_action"

        await runner.confirm(run_id, _GATE_ID, decision="approve")
        await asyncio.wait_for(runner.task_for(run_id), timeout=10)

        # The Governor stop stands as the terminal (stopped, or paused on a pausable
        # budget tenant per GOV-6) — the key invariant is it was NOT re-driven.
        assert runner.get(run_id).status in ("stopped", "paused")
        assert tracker["sends"].count(_DURABLE_RESUME_REDRIVE) == 0
        events = await runner.replay(run_id)
        assert any(e.type == "stopped" for e in events)
        await runner.aclose()

    asyncio.run(_run())


def test_errored_resume_is_not_redriven():
    """A provider error on a durable resume yields NO work (produced == 0) but is a REAL
    terminal — it must NOT be re-driven (that would hide the error and waste turns)."""
    async def _run():
        tmp = Path(tempfile.mkdtemp())
        tracker: dict = {"resume_mode": "errored"}
        runner = Runner(_config(tmp), keys=_KEYS, chat_api_factory=_factory(tracker))
        await runner.init()
        run_id = runner.submit(_spec())
        await runner.task_for(run_id)

        await runner.confirm(run_id, _GATE_ID, decision="approve")
        await asyncio.wait_for(runner.task_for(run_id), timeout=10)

        assert runner.get(run_id).status == "error"
        assert tracker["sends"].count(_DURABLE_RESUME_REDRIVE) == 0
        events = await runner.replay(run_id)
        assert any(e.type == "error" for e in events)
        await runner.aclose()

    asyncio.run(_run())


if __name__ == "__main__":  # pragma: no cover
    test_empty_resume_is_redriven_and_converges()
    test_persistent_empty_resume_is_bounded()
    test_stopped_resume_is_not_redriven()
    test_errored_resume_is_not_redriven()
