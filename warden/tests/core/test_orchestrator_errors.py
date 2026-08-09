"""Error / permission-path unit tests for the orchestrator core loop.

Targets send_message (stream error handling), _can_use_tool (deny +
handler-raises), resume_session (happy path), and resolve_turn_session
(3-way lookup + provider mismatch). Hermetic: no LLM / subprocess / network;
a fake SessionManager drives every path. Async style matches the repo —
``asyncio.run(...)`` in sync tests (see test_api.py), NOT pytest-asyncio.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

from warden.orchestrator.orchestrator import Orchestrator
from warden.orchestrator.stream_runtime import resolve_turn_session
from warden.schemas.events import (
    CompletionEvent,
    ErrorEvent,
    MessageEvent,
    SessionCreatedEvent,
)
from warden.seams.permissions import PermissionDecision
from warden.schemas.tool_scope import ToolScope
from warden.safety.permissions.checker import PermissionMode


# --- Fakes (SDK-style content blocks, mirroring test_api.py) ---------------

class _FakeBlock:
    def __init__(self, class_name: str, **attrs: Any) -> None:
        self.__class__ = type(class_name, (), {})
        for k, v in attrs.items():
            object.__setattr__(self, k, v)


class _FakeMessage:
    def __init__(self, *blocks: _FakeBlock) -> None:
        self.content = list(blocks)


class _FakeSession:
    """Minimal AgentProvider — deterministic, no subprocess."""

    def __init__(self, session_id: str = "fake-session-123") -> None:
        self.session_id = session_id
        self.jsonl_path: str | None = None
        self.stopped = False
        self.closed = False

    async def start(self) -> None:
        pass

    async def send(self, prompt: str) -> AsyncGenerator[Any, None]:
        yield _FakeMessage(_FakeBlock("TextBlock", text="Hello from fake"))

    async def stop(self) -> None:
        self.stopped = True

    async def close(self) -> None:
        self.closed = True


class _RaisingSession(_FakeSession):
    """Session whose stream raises after emitting one good message."""

    async def send(self, prompt: str) -> AsyncGenerator[Any, None]:
        yield _FakeMessage(_FakeBlock("TextBlock", text="partial output"))
        raise RuntimeError("provider exploded mid-stream")


class _FakeIndex:
    """In-memory SessionIndex stand-in; records jsonl updates."""

    def __init__(self, entries: dict[str, dict] | None = None) -> None:
        self._entries = entries or {}
        self.jsonl_updates: list[tuple[str, str]] = []

    async def get(self, session_id: str) -> dict | None:
        return self._entries.get(session_id)

    async def update_jsonl_path(self, session_id: str, jsonl_path: str) -> None:
        self.jsonl_updates.append((session_id, jsonl_path))


class _FakeSessionManager:
    """Fake SessionManager exposing exactly what the orchestrator touches."""

    def __init__(
        self,
        *,
        active: dict[str, Any] | None = None,
        index: _FakeIndex | None = None,
        create_session: Any = None,
        resume_result: Any = None,
    ) -> None:
        self._active = active or {}
        self._index = index or _FakeIndex()
        self._create_session = create_session
        self._resume_result = resume_result
        self.registered: list[Any] = []
        self.closed_ids: list[str] = []
        self.create_calls: list[dict] = []
        self.resume_calls: list[dict] = []

    def get(self, session_id: str) -> Any:
        return self._active.get(session_id)

    async def create(self, **kwargs: Any) -> Any:
        self.create_calls.append(kwargs)
        return self._create_session if self._create_session is not None else _FakeSession()

    async def resume(self, **kwargs: Any) -> tuple[str, Any]:
        self.resume_calls.append(kwargs)
        if self._resume_result is not None:
            return self._resume_result
        sid = kwargs["session_id"]
        return sid, _FakeSession(session_id=sid)

    async def register(self, session: Any, **kwargs: Any) -> None:
        self.registered.append(session)
        if session.session_id:
            self._active[session.session_id] = session

    async def close(self, session_id: str) -> None:
        self.closed_ids.append(session_id)
        self._active.pop(session_id, None)


# --- Permission handlers ---------------------------------------------------

class _Handler:
    """Configurable handler: fixed decision, or raise ``exc`` on confirm."""

    def __init__(
        self,
        decision: PermissionDecision | None = None,
        exc: type[BaseException] | None = None,
        answers: dict | None = None,
    ) -> None:
        self._decision = decision
        self._exc = exc
        self._answers = answers or {"result": {}}

    async def request_permission(self, *_a: Any, **_k: Any) -> PermissionDecision:
        if self._exc is not None:
            raise self._exc("handler failed")
        return self._decision or PermissionDecision(allowed=False)

    async def ask_user_question(self, questions: list[dict]) -> dict:
        return self._answers


# --- Helpers ---------------------------------------------------------------

def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _make_orchestrator(
    session_manager: _FakeSessionManager,
    *,
    permission_handler: Any = None,
    tool_scope: ToolScope | None = None,
) -> Orchestrator:
    return Orchestrator(
        session_manager=session_manager,
        repo_path=Path("."),
        permission_handler=permission_handler,
        tool_scope=tool_scope,
    )


async def _drain(orch: Orchestrator, content: str, **kwargs: Any) -> list[Any]:
    return [event async for event in orch.send_message(content, **kwargs)]


async def _resolve(sm: _FakeSessionManager, **overrides: Any) -> tuple:
    kwargs: dict[str, Any] = dict(
        session_manager=sm,
        session_id=None,
        current_session_id=None,
        provider="claude",
        model=None,
        can_use_tool=lambda *_a, **_k: None,
        disallowed_tools=[],
        system_prompt=None,
        custom_tools=None,
        repo_path=".",
        provider_kwargs=None,
    )
    kwargs.update(overrides)
    return await resolve_turn_session(**kwargs)


# === send_message — stream error handling ==================================

def test_send_message_session_raises_midstream_yields_error_then_closes() -> None:
    """A mid-stream raise surfaces one ErrorEvent and the generator still
    terminates cleanly (sentinel drains the queue)."""

    async def _test() -> None:
        sm = _FakeSessionManager(create_session=_RaisingSession(session_id="s-raise"))
        events = await _drain(_make_orchestrator(sm), "hi")

        # The good message emitted before the raise still gets through.
        assert [e for e in events if isinstance(e, MessageEvent) and e.kind == "text"], events

        errors = [e for e in events if isinstance(e, ErrorEvent)]
        assert len(errors) == 1, f"expected 1 ErrorEvent, got {errors}"
        assert errors[0].text == "Stream error"

        # Error path emits NO CompletionEvent, but _drain returned (no hang).
        assert not [e for e in events if isinstance(e, CompletionEvent)]

    _run(_test())


def test_send_message_happy_path_completes() -> None:
    """Baseline: a clean session yields SessionCreated + text + Completion,
    no ErrorEvent."""

    async def _test() -> None:
        sm = _FakeSessionManager(create_session=_FakeSession(session_id="s-ok"))
        events = await _drain(_make_orchestrator(sm), "hi")

        assert any(isinstance(e, SessionCreatedEvent) for e in events)
        assert any(isinstance(e, MessageEvent) and e.kind == "text" for e in events)
        assert len([e for e in events if isinstance(e, CompletionEvent)]) == 1
        assert not [e for e in events if isinstance(e, ErrorEvent)]

    _run(_test())


# === _can_use_tool — deny paths ============================================

def test_can_use_tool_scope_denies() -> None:
    """A tool outside the active scope is denied before handler/checker."""

    async def _test() -> None:
        orch = _make_orchestrator(
            _FakeSessionManager(), tool_scope=ToolScope(allowed=["Read"]),
        )
        result = await orch._can_use_tool("Bash", {"command": "ls"}, None)

        assert isinstance(result, PermissionResultDeny)
        assert result.behavior == "deny"
        assert "tool scope" in result.message

    _run(_test())


def test_can_use_tool_checker_hard_deny_skips_handler() -> None:
    """A non-confirmable checker deny (sensitive path) returns deny without
    reaching the handler — so a raising handler is never invoked."""

    async def _test() -> None:
        orch = _make_orchestrator(
            _FakeSessionManager(), permission_handler=_Handler(exc=RuntimeError),
        )
        result = await orch._can_use_tool(
            "Read", {"file_path": "/home/user/.ssh/id_rsa"}, None,
        )

        assert isinstance(result, PermissionResultDeny)
        assert result.message == "Sensitive path"

    _run(_test())


def test_can_use_tool_handler_denies_confirmation() -> None:
    """CONFIRM-mode mutating tool asks the handler; a handler deny maps to a
    PermissionResultDeny carrying the handler's reason."""

    async def _test() -> None:
        handler = _Handler(PermissionDecision(allowed=False, reason="user said no"))
        orch = _make_orchestrator(_FakeSessionManager(), permission_handler=handler)
        assert orch._permission_checker.mode == PermissionMode.CONFIRM

        result = await orch._can_use_tool("Bash", {"command": "npm test"}, None)

        assert isinstance(result, PermissionResultDeny)
        assert result.message == "user said no"

    _run(_test())


def test_can_use_tool_handler_allows_confirmation() -> None:
    """The confirm branch: a handler ALLOW maps to an allow."""

    async def _test() -> None:
        handler = _Handler(PermissionDecision(allowed=True))
        orch = _make_orchestrator(_FakeSessionManager(), permission_handler=handler)

        result = await orch._can_use_tool("Bash", {"command": "npm test"}, None)

        assert isinstance(result, PermissionResultAllow)
        assert result.behavior == "allow"

    _run(_test())


def test_can_use_tool_handler_remember_persists() -> None:
    """A handler allow with always=True remembers the tool for the session."""

    async def _test() -> None:
        handler = _Handler(PermissionDecision(allowed=True, always=True))
        orch = _make_orchestrator(_FakeSessionManager(), permission_handler=handler)

        first = await orch._can_use_tool("Bash", {"command": "npm test"}, None)
        assert isinstance(first, PermissionResultAllow)

        decision = orch._permission_checker.evaluate("Bash", {"command": "npm test"})
        assert decision.allowed
        assert decision.source == "remembered"

    _run(_test())


def test_can_use_tool_ask_user_question_forwarded() -> None:
    """AskUserQuestion routes to the handler and returns answers via
    updated_input (allow)."""

    async def _test() -> None:
        handler = _Handler(answers={"result": {"q1": "answer"}})
        orch = _make_orchestrator(_FakeSessionManager(), permission_handler=handler)

        result = await orch._can_use_tool(
            "AskUserQuestion", {"questions": [{"question": "q1"}]}, None,
        )

        assert isinstance(result, PermissionResultAllow)
        assert result.updated_input["answers"] == {"q1": "answer"}

    _run(_test())


# === _can_use_tool — handler raises / times out ============================

def test_can_use_tool_handler_raises_propagates() -> None:
    """When the confirmation handler raises, the exception propagates out of
    _can_use_tool (no silent swallow)."""

    async def _test() -> None:
        orch = _make_orchestrator(
            _FakeSessionManager(), permission_handler=_Handler(exc=RuntimeError),
        )
        raised = False
        try:
            await orch._can_use_tool("Bash", {"command": "npm test"}, None)
        except RuntimeError:
            raised = True
        assert raised, "expected RuntimeError to propagate from the handler"

    _run(_test())


def test_can_use_tool_handler_times_out_propagates() -> None:
    """A handler timeout surfaces as TimeoutError out of _can_use_tool."""

    async def _test() -> None:
        orch = _make_orchestrator(
            _FakeSessionManager(), permission_handler=_Handler(exc=TimeoutError),
        )
        raised = False
        try:
            await orch._can_use_tool("Bash", {"command": "npm test"}, None)
        except TimeoutError:
            raised = True
        assert raised, "expected TimeoutError to propagate from the handler"

    _run(_test())


# === resume_session — happy path ===========================================

def test_resume_session_happy_path() -> None:
    """resume_session delegates to the manager, returns the (reused) session id,
    and records it as the current session."""

    async def _test() -> None:
        resumed = _FakeSession(session_id="s-resumed")
        sm = _FakeSessionManager(resume_result=("s-resumed", resumed))
        orch = _make_orchestrator(sm)

        sid = await orch.resume_session("s-resumed")

        assert sid == "s-resumed"
        assert orch._current_session_id == "s-resumed"
        assert len(sm.resume_calls) == 1
        assert sm.resume_calls[0]["session_id"] == "s-resumed"
        assert sm.resume_calls[0]["provider"] == "claude"

    _run(_test())


# === resolve_turn_session — 3-way lookup + provider mismatch ================

def test_resolve_turn_session_client_id_matching_provider() -> None:
    """Step 1: an active client session whose type matches the provider is
    reused as-is (no resume, no create)."""

    async def _test() -> None:
        class ClaudeSession(_FakeSession):
            pass

        active = {"s-client": ClaudeSession(session_id="s-client")}
        sm = _FakeSessionManager(active=active)

        session, is_resumed, cur, resumed_event = await _resolve(
            sm, session_id="s-client",
        )

        assert session is active["s-client"]
        assert is_resumed is False
        assert cur == "s-client"
        assert resumed_event is None
        assert not sm.create_calls and not sm.resume_calls

    _run(_test())


def test_resolve_turn_session_db_resume_emits_event() -> None:
    """Step 3: no active session but a matching DB entry → resume + a
    SessionCreatedEvent(resumed=True)."""

    async def _test() -> None:
        index = _FakeIndex(entries={"s-db": {"provider": "claude"}})
        sm = _FakeSessionManager(
            index=index, resume_result=("s-db", _FakeSession(session_id="s-db")),
        )

        session, is_resumed, cur, resumed_event = await _resolve(
            sm, session_id="s-db",
        )

        assert is_resumed is True
        assert cur == "s-db"
        assert isinstance(resumed_event, SessionCreatedEvent)
        assert resumed_event.resumed is True
        assert len(sm.resume_calls) == 1

    _run(_test())


def test_resolve_turn_session_provider_mismatch_creates_fresh() -> None:
    """Provider-mismatch branch: a DB entry recorded under a different provider
    is discarded — no resume — and a fresh session is created for the current
    provider."""

    async def _test() -> None:
        index = _FakeIndex(entries={"s-mismatch": {"provider": "codex"}})
        fresh = _FakeSession(session_id="s-fresh")
        sm = _FakeSessionManager(index=index, create_session=fresh)

        session, is_resumed, cur, resumed_event = await _resolve(
            sm, session_id="s-mismatch", provider="claude",
        )

        assert not sm.resume_calls, "provider mismatch must skip resume"
        assert session is fresh
        assert is_resumed is False
        assert resumed_event is None
        assert len(sm.create_calls) == 1
        assert sm.create_calls[0]["provider"] == "claude"

    _run(_test())


def test_resolve_turn_session_resume_failure_falls_back_to_create() -> None:
    """If the manager's resume raises, resolve_turn_session logs and falls back
    to creating a fresh session rather than propagating."""

    async def _test() -> None:
        index = _FakeIndex(entries={"s-db": {"provider": "claude"}})

        class _ResumeFailsManager(_FakeSessionManager):
            async def resume(self, **kwargs: Any) -> tuple[str, Any]:
                self.resume_calls.append(kwargs)
                raise RuntimeError("resume blew up")

        fresh = _FakeSession(session_id="s-fresh")
        sm = _ResumeFailsManager(index=index, create_session=fresh)

        session, is_resumed, cur, resumed_event = await _resolve(
            sm, session_id="s-db",
        )

        assert len(sm.resume_calls) == 1  # attempted
        assert session is fresh  # fell back to create
        assert is_resumed is False
        assert resumed_event is None
        assert len(sm.create_calls) == 1

    _run(_test())
