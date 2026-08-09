"""EXT-G2 — the landscape gate end-to-end over the durable-HITL Runs-API cycle.

The 3-tool recipe (emit_checkpoint / confirm_landscape / course_complete) at the
Runner transport level: the two sibling tools stream freely while only
``confirm_landscape`` pauses (requires_action); the confirm supports **allow / deny /
allow-with-edit**, with the edited ``updated_input`` round-tripped through the real
FileDeferStore + DurableDeferHandler back to the re-fired tool.

Gate isolation at the *checker* level (only confirm_landscape defers) is proven in
``tests/safety/test_permission_checker.py``; the live checker+SDK path is the
``--m6-gate`` bed cell. Hermetic here (mock agent, no LLM), following the durable
HITL test's mock-handler pattern.
"""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from warden.harness_api.config import HarnessApiConfig
from warden.harness_api.credentials.keys import KeyRegistry
from warden.harness_api.runner import Runner
from warden.harness_api.schemas import RunSpec, Sink, ToolConfirmation
from warden.schemas.events import (
    CompletionEvent,
    MessageEvent,
    OrchestratorEvent,
    SessionCreatedEvent,
)

_KEYS = KeyRegistry.from_config(
    {"keys": {"k1": {"provider": "claude", "secret_env": "S1"}},
     "users": {"u1": {"key_id": "k1"}}},
    secrets={"S1": "sk-1"},
)

_GATE_ID = "toolu_confirm_landscape_1"
_GATE_INPUT = {"concepts": ["a", "b", "c"]}


class _GateMockChatAPI:
    """Streams emit_checkpoint, then gates on confirm_landscape (consulting the
    durable handler), then — once resumed with an allow — streams course_complete.

    Models the 3-tool recipe: the two siblings never pause; only the gate tool does.
    On an allow, records the handler-injected ``updated_input`` so the edit round-trip
    (EXT-G2) is observable.
    """

    def __init__(self, *, spec: Any, auth_env: dict | None, tracker: dict) -> None:
        self._sid = f"sess-{spec.task_id}"
        self._handler = None
        self._tracker = tracker
        tracker.setdefault("streamed", [])

    def set_durable_defer(self, dd) -> None:
        from warden.seams.defer import DurableDeferHandler
        from warden.seams.defer_store import FileDeferStore

        self._handler = DurableDeferHandler(FileDeferStore(dd.store_root))

    async def init(self) -> None:
        return None

    async def send(
        self, content: str, *, session_id: str | None = None,
        workflow: str | None = None,
    ) -> AsyncIterator[OrchestratorEvent]:
        resumed = session_id is not None
        sid = session_id or self._sid
        yield SessionCreatedEvent(session_id=sid, resumed=resumed)
        # Sibling 1: emit_checkpoint — streams, never pauses.
        if not resumed:
            self._tracker["streamed"].append("emit_checkpoint")
            yield MessageEvent(kind="checkpoint", content={"phase": "landscape"},
                               session_id=sid)
        # The gate: consult the durable handler for confirm_landscape.
        decision = await self._handler.request_permission(
            "confirm_landscape", dict(_GATE_INPUT), "confirm the landscape",
            tool_use_id=_GATE_ID,
        )
        if "defer" in decision.source:
            return  # ejected → the run parks on confirm_landscape (requires_action)
        if not decision.allowed:
            self._tracker["gate"] = {"allowed": False, "reason": decision.reason}
            yield MessageEvent(
                kind="status",
                content={"subtype": "result", "result": f"blocked: {decision.reason}",
                         "usage": {"input_tokens": 5, "output_tokens": 5}},
                session_id=sid,
            )
            yield CompletionEvent(session_id=sid)
            return
        # Allowed: record what input the re-fired gate tool saw (edit round-trip).
        self._tracker["gate"] = {"allowed": True,
                                 "input": decision.updated_input or dict(_GATE_INPUT)}
        # Sibling 2: course_complete — streams after the gate clears.
        self._tracker["streamed"].append("course_complete")
        yield MessageEvent(kind="tool_use", content={"toolName": "course_complete"},
                           session_id=sid)
        yield MessageEvent(
            kind="status",
            content={"subtype": "result", "result": "course built",
                     "usage": {"input_tokens": 5, "output_tokens": 5}},
            session_id=sid,
        )
        yield CompletionEvent(session_id=sid)

    async def close(self) -> None:
        return None


def _factory(tracker: dict):
    def factory(spec: Any, auth_env: dict | None) -> _GateMockChatAPI:
        return _GateMockChatAPI(spec=spec, auth_env=auth_env, tracker=tracker)
    return factory


def _make_runner(tmp: Path, tracker: dict) -> Runner:
    cfg = HarnessApiConfig()
    cfg.engine.permissions.handler = "durable_http"
    cfg.engine.persistence.session_db_path = str(tmp / "sessions.db")
    return Runner(cfg, keys=_KEYS, chat_api_factory=_factory(tracker))


def _spec(task: str) -> RunSpec:
    return RunSpec(user_id="u1", task_id=task, provider="claude",
                   model="claude-opus-4-8", input={"prompt": "build"},
                   sink=Sink(type="sse"))


async def _pause_on_gate(runner: Runner, task: str) -> str:
    run_id = runner.submit(_spec(task))
    await runner.task_for(run_id)
    assert runner.get(run_id).status == "requires_action"
    events = await runner.replay(run_id)
    asks = [e for e in events if e.type == "permission_request"]
    assert len(asks) == 1 and asks[0].data["tool_name"] == "confirm_landscape"
    # The sibling emit_checkpoint streamed before the pause; course_complete did not.
    kinds = {e.type for e in events}
    assert "checkpoint" in kinds
    return run_id


def test_gate_isolation_and_allow():
    async def _run():
        tmp = Path(tempfile.mkdtemp())
        tracker: dict = {}
        runner = _make_runner(tmp, tracker)
        await runner.init()
        run_id = await _pause_on_gate(runner, "c1")

        # Approve → the gate clears, course_complete streams, run converges.
        await runner.confirm(run_id, _GATE_ID, decision="approve")
        await runner.task_for(run_id)
        assert runner.get(run_id).status == "succeeded"
        assert tracker["gate"] == {"allowed": True, "input": _GATE_INPUT}
        assert "course_complete" in tracker["streamed"]
        await runner.aclose()

    asyncio.run(_run())


def test_gate_deny():
    async def _run():
        tmp = Path(tempfile.mkdtemp())
        tracker: dict = {}
        runner = _make_runner(tmp, tracker)
        await runner.init()
        run_id = await _pause_on_gate(runner, "c2")

        # Reject → the gate tool never "runs" course_complete; model reports blocked.
        await runner.confirm(run_id, _GATE_ID, decision="reject",
                             reason="not these concepts")
        await runner.task_for(run_id)
        assert runner.get(run_id).status == "succeeded"
        assert tracker["gate"]["allowed"] is False
        assert "course_complete" not in tracker["streamed"]
        await runner.aclose()

    asyncio.run(_run())


def test_gate_allow_with_edit_roundtrips_updated_input():
    async def _run():
        tmp = Path(tempfile.mkdtemp())
        tracker: dict = {}
        runner = _make_runner(tmp, tracker)
        await runner.init()
        run_id = await _pause_on_gate(runner, "c3")

        # Approve-with-edit (dormant updated_input) → the re-fired gate tool sees the
        # mutated concepts. Not a distinct mode; a plain approve that still round-trips.
        edited = {"concepts": ["x", "y"]}
        await runner.confirm(run_id, _GATE_ID, decision="approve", updated_input=edited)
        await runner.task_for(run_id)
        assert runner.get(run_id).status == "succeeded"
        assert tracker["gate"] == {"allowed": True, "input": edited}
        await runner.aclose()

    asyncio.run(_run())


def test_tool_confirmation_schema_accepts_updated_input_alias():
    # snake_case and camelCase both parse (catalog naming).
    a = ToolConfirmation(tool_use_id="t", decision="approve",
                         updated_input={"concepts": ["a"]})
    b = ToolConfirmation.model_validate(
        {"tool_use_id": "t", "decision": "approve", "updatedInput": {"concepts": ["a"]}}
    )
    assert a.updated_input == b.updated_input == {"concepts": ["a"]}
