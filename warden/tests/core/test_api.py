"""Tests for drive.api — ChatAPI Python interface."""

import asyncio
from collections.abc import AsyncGenerator
from typing import Any

import pytest

from warden import (
    AutoAllowHandler,
    ChatAPI,
    CompletionEvent,
    CustomTool,
    HarnessConfig,
    MessageEvent,
    PermissionDecision,
    SessionCreatedEvent,
    ToolScope,
)


# ---------------------------------------------------------------------------
# Fake provider for mocked tests
# ---------------------------------------------------------------------------

class _FakeBlock:
    """Lightweight stand-in for SDK content blocks."""
    def __init__(self, class_name: str, **attrs: Any) -> None:
        self.__class__ = type(class_name, (), {})
        for k, v in attrs.items():
            object.__setattr__(self, k, v)


class _FakeMessage:
    """Fake SDK message containing content blocks."""
    def __init__(self, *blocks: _FakeBlock) -> None:
        self.content = list(blocks)


class _FakeSession:
    """Minimal AgentProvider implementation for testing."""

    def __init__(self) -> None:
        self.session_id: str = "fake-session-123"
        self.jsonl_path: str | None = None
        self._started = False

    async def start(self) -> None:
        self._started = True

    async def send(self, prompt: str) -> AsyncGenerator[Any, None]:
        block = _FakeBlock("TextBlock", text="Hello from fake")
        yield _FakeMessage(block)

    async def stop(self) -> None:
        pass

    async def close(self) -> None:
        pass


def _run(coro: Any) -> Any:
    """Run an async coroutine in a new event loop."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# T008: Construction and init tests
# ---------------------------------------------------------------------------

def test_chatapi_construction() -> None:
    api = ChatAPI(HarnessConfig(), repo_path=".")
    assert api._orchestrator is None
    assert api._provider == "claude"
    assert api._model is None


def test_chatapi_init() -> None:
    async def _test() -> None:
        api = ChatAPI(HarnessConfig(), repo_path=".")
        await api.init()
        assert api._orchestrator is not None
        await api._session_manager.close_all()

    _run(_test())


def test_chatapi_send_before_init_raises() -> None:
    async def _test() -> None:
        api = ChatAPI(HarnessConfig(), repo_path=".")
        with pytest.raises(RuntimeError, match="Call init"):
            async for _ in api.send("test"):
                pass

    _run(_test())


def test_chatapi_close_before_init_raises() -> None:
    async def _test() -> None:
        api = ChatAPI(HarnessConfig(), repo_path=".")
        with pytest.raises(RuntimeError, match="Call init"):
            await api.close()

    _run(_test())


def test_chatapi_resume_before_init_raises() -> None:
    async def _test() -> None:
        api = ChatAPI(HarnessConfig(), repo_path=".")
        with pytest.raises(RuntimeError, match="Call init"):
            await api.resume("some-id")

    _run(_test())


# ---------------------------------------------------------------------------
# T009: ChatAPI.send() with mocked provider
# ---------------------------------------------------------------------------

def test_chatapi_send_basic() -> None:
    async def _test() -> None:
        api = ChatAPI(HarnessConfig(), repo_path=".")
        await api.init()

        fake_session = _FakeSession()

        async def _fake_create(**kwargs: Any) -> _FakeSession:
            await fake_session.start()
            return fake_session

        api._orchestrator._session_manager.create = _fake_create

        events: list[Any] = []
        async for event in api.send("test message"):
            events.append(event)

        session_events = [e for e in events if isinstance(e, SessionCreatedEvent)]
        assert len(session_events) >= 1, f"No SessionCreatedEvent found in {events}"

        text_events = [
            e for e in events
            if isinstance(e, MessageEvent) and e.kind == "text"
        ]
        assert len(text_events) >= 1, f"No text MessageEvent found in {events}"

        completion_events = [e for e in events if isinstance(e, CompletionEvent)]
        assert len(completion_events) == 1, (
            f"Expected 1 CompletionEvent, got {len(completion_events)}"
        )

        await api._session_manager.close_all()

    _run(_test())


# ---------------------------------------------------------------------------
# T010: AutoAllowHandler and PermissionDecision
# ---------------------------------------------------------------------------

def test_auto_allow_handler() -> None:
    async def _test() -> None:
        handler = AutoAllowHandler()

        decision = await handler.request_permission("Bash", {"command": "ls"}, "test")
        assert decision.allowed is True
        assert decision.source == "auto"

        result = await handler.ask_user_question([{"question": "How?"}])
        assert result == {"result": {}}

    _run(_test())


def test_permission_decision_dataclass() -> None:
    d1 = PermissionDecision(allowed=True, source="test", reason="because")
    assert d1.allowed is True
    assert d1.source == "test"
    assert d1.reason == "because"

    d2 = PermissionDecision(allowed=False)
    assert d2.allowed is False
    assert d2.source == ""
    assert d2.reason == ""


# ---------------------------------------------------------------------------
# T012: ChatAPI allowed_tools kwarg wiring
# ---------------------------------------------------------------------------

def test_chatapi_allowed_tools_wiring() -> None:
    async def _test() -> None:
        config = HarnessConfig()
        config.permissions.allowed_tools = ["Read", "Grep"]
        api = ChatAPI(config, repo_path=".")
        await api.init()

        orch = api._orchestrator
        assert orch._tool_scope == ToolScope(allowed=["Read", "Grep"])
        assert orch._active_tool_scope == ToolScope(allowed=["Read", "Grep"])

        await api._session_manager.close_all()

    _run(_test())


def test_chatapi_no_allowed_tools_means_no_scope() -> None:
    async def _test() -> None:
        api = ChatAPI(HarnessConfig(), repo_path=".")
        await api.init()

        assert api._orchestrator._tool_scope is None

        await api._session_manager.close_all()

    _run(_test())


# ---------------------------------------------------------------------------
# Phase 5: system_prompt, custom_tools, middleware kwargs
# ---------------------------------------------------------------------------

def test_chatapi_system_prompt_wiring() -> None:
    async def _test() -> None:
        config = HarnessConfig()
        config.safety.system_prompt = "You are a math tutor."
        api = ChatAPI(config, repo_path=".")
        await api.init()

        assert api._orchestrator._system_prompt == "You are a math tutor."

        await api._session_manager.close_all()

    _run(_test())


def test_chatapi_custom_tools_wiring() -> None:
    async def _test() -> None:
        tool = CustomTool(
            name="read-chapter",
            description="Read chapter",
            input_schema={},
            handler=lambda: "content",
        )
        config = HarnessConfig()
        config.custom_tools.tools = [tool]
        api = ChatAPI(config, repo_path=".")
        await api.init()

        assert len(api._orchestrator._custom_tools) == 1
        assert api._orchestrator._custom_tools[0].name == "read-chapter"

        await api._session_manager.close_all()

    _run(_test())


def test_chatapi_middleware_wiring() -> None:
    class _DummyMiddleware:
        async def before_send(self, content, context):
            return content

    async def _test() -> None:
        mw = _DummyMiddleware()
        config = HarnessConfig()
        config.middleware.input_instances = [mw]
        config.middleware.enable_input_middleware = True  # §4a: assert MW is wired
        api = ChatAPI(config, repo_path=".")
        await api.init()

        assert len(api._orchestrator._middleware) == 1

        await api._session_manager.close_all()

    _run(_test())
