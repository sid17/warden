"""Phase 2 unit coverage for the OpenHarness provider (contract parity).

Hermetic — no Ollama, no network. Covers:
  - B15 hook adapter: blocked on deny / exception, not-blocked on allow; forwards
    the REAL tool_input.
  - PermissionHookExecutor: merges the arg-level gate with an audit delegate and
    delegates non-PRE_TOOL_USE events.
  - Per-run auth: auth_env -> api_key wiring (with the "ollama" dummy fallback).
  - Custom tools: create_session("openharness", custom_tools=[...]) no longer
    raises; the adapter advertises the schema and executes sync/async handlers.
  - kwargs-reject guardrail still fires.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from openharness.hooks.events import HookEvent
from openharness.tools.base import ToolExecutionContext
from openharness.tools.base import ToolRegistry

from warden.providers import create_session
from warden.providers.openharness.custom_tool_adapter import (
    CustomToolAdapter,
    register_custom_tools,
)
from warden.providers.openharness.permission_bridge import (
    PermissionHookExecutor,
    build_auto_confirm_prompt,
    build_permission_hook,
)
from warden.providers.openharness.session import OpenHarnessSession
from warden.seams.custom_tools import CustomTool


# --------------------------------------------------------------------------- #
# B15 hook adapter
# --------------------------------------------------------------------------- #

def _allow(_name, _input, _ctx):
    async def _c():
        return SimpleNamespace(behavior="allow")
    return _c()


def test_hook_allow_not_blocked() -> None:
    hook = build_permission_hook(_allow)
    res = asyncio.run(hook({"tool_name": "read_file", "tool_input": {"path": "a"}}))
    assert res.blocked is False
    assert res.success is True


def test_hook_deny_blocked_with_reason() -> None:
    async def deny(_n, _i, _c):
        return SimpleNamespace(behavior="deny", message="policy says no")

    hook = build_permission_hook(deny)
    res = asyncio.run(hook({"tool_name": "write_file", "tool_input": {"path": "x"}}))
    assert res.blocked is True
    assert "policy says no" in res.reason


def test_hook_exception_fails_closed() -> None:
    async def boom(_n, _i, _c):
        raise RuntimeError("kaboom")

    hook = build_permission_hook(boom)
    res = asyncio.run(hook({"tool_name": "Bash", "tool_input": {"command": "ls"}}))
    assert res.blocked is True, "an exception must fail closed (block)."


def test_hook_forwards_real_tool_input() -> None:
    seen = {}

    async def spy(name, tool_input, _c):
        seen["name"] = name
        seen["input"] = tool_input
        return SimpleNamespace(behavior="allow")

    hook = build_permission_hook(spy)
    payload = {"tool_name": "write_file", "tool_input": {"path": "secret", "content": "z"}}
    asyncio.run(hook(payload))
    assert seen["input"] == {"path": "secret", "content": "z"}


def test_auto_confirm_prompt_always_approves() -> None:
    """The DEFAULT-mode ceremony prompt approves unconditionally — the real gate
    is the hook, which fires first and blocks denied tools before the checker."""
    prompt = build_auto_confirm_prompt()
    assert asyncio.run(prompt("write_file", "needs confirmation")) is True
    assert asyncio.run(prompt("Bash", "any reason")) is True


# --------------------------------------------------------------------------- #
# PermissionHookExecutor merge / delegate
# --------------------------------------------------------------------------- #

class _FakeAuditExec:
    def __init__(self):
        self.events = []

    async def execute(self, event, payload):
        self.events.append(event)
        from openharness.hooks.types import AggregatedHookResult
        return AggregatedHookResult(results=[])


def test_executor_pre_tool_use_blocks_and_delegates() -> None:
    async def deny(_n, _i, _c):
        return SimpleNamespace(behavior="deny", message="no")

    audit = _FakeAuditExec()
    ex = PermissionHookExecutor(build_permission_hook(deny), audit_executor=audit)
    agg = asyncio.run(
        ex.execute(HookEvent.PRE_TOOL_USE, {"tool_name": "write_file", "tool_input": {}})
    )
    assert agg.blocked is True
    assert HookEvent.PRE_TOOL_USE in audit.events, "audit delegate must still fire."


def test_executor_non_pre_tool_use_only_delegates() -> None:
    async def deny(_n, _i, _c):
        return SimpleNamespace(behavior="deny")

    audit = _FakeAuditExec()
    ex = PermissionHookExecutor(build_permission_hook(deny), audit_executor=audit)
    agg = asyncio.run(
        ex.execute(HookEvent.USER_PROMPT_SUBMIT, {"prompt": "hi"})
    )
    assert agg.blocked is False, "no permission gate outside PRE_TOOL_USE."
    assert audit.events == [HookEvent.USER_PROMPT_SUBMIT]


def test_executor_no_audit_delegate() -> None:
    async def allow(_n, _i, _c):
        return SimpleNamespace(behavior="allow")

    ex = PermissionHookExecutor(build_permission_hook(allow), audit_executor=None)
    agg = asyncio.run(
        ex.execute(HookEvent.PRE_TOOL_USE, {"tool_name": "read_file", "tool_input": {}})
    )
    assert agg.blocked is False


# --------------------------------------------------------------------------- #
# Per-run auth
# --------------------------------------------------------------------------- #

def test_auth_env_openai_key_becomes_api_key() -> None:
    sess = OpenHarnessSession(
        repo_path=Path("."), auth_env={"OPENAI_API_KEY": "sk-managed-123"}
    )
    assert sess._api_key == "sk-managed-123"


def test_auth_env_openharness_key_becomes_api_key() -> None:
    sess = OpenHarnessSession(
        repo_path=Path("."), auth_env={"OPENHARNESS_API_KEY": "oh-key-9"}
    )
    assert sess._api_key == "oh-key-9"


def test_explicit_api_key_wins_over_auth_env() -> None:
    sess = OpenHarnessSession(
        repo_path=Path("."), api_key="explicit", auth_env={"OPENAI_API_KEY": "env"}
    )
    assert sess._api_key == "explicit"


def test_dummy_ollama_key_when_no_auth() -> None:
    sess = OpenHarnessSession(repo_path=Path("."))
    assert sess._api_key == "ollama"


# --------------------------------------------------------------------------- #
# Session-home pin
# --------------------------------------------------------------------------- #

def test_session_home_pins_transcript_dir(tmp_path) -> None:
    sess = OpenHarnessSession(repo_path=Path("."), session_home=tmp_path)
    assert sess._session_root() == tmp_path.resolve()


def test_no_session_home_uses_global(monkeypatch) -> None:
    sess = OpenHarnessSession(repo_path=Path("."))
    assert sess._session_root() == Path.home() / ".openharness"


# --------------------------------------------------------------------------- #
# Custom tools
# --------------------------------------------------------------------------- #

_CT = CustomTool(
    name="echo_it",
    description="echo the text back",
    input_schema={
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    },
    handler=lambda text: f"echo:{text}",
)


def test_create_session_custom_tools_no_longer_raises() -> None:
    sess = create_session("openharness", repo_path=Path("."), custom_tools=[_CT])
    assert _CT in sess._custom_tools


def test_adapter_advertises_schema() -> None:
    a = CustomToolAdapter(_CT)
    schema = a.to_api_schema()
    assert schema["name"] == "echo_it"
    assert schema["input_schema"]["required"] == ["text"]


def test_adapter_executes_sync_handler() -> None:
    a = CustomToolAdapter(_CT)
    args = a.input_model(text="hi")
    ctx = ToolExecutionContext(cwd=Path("."))
    res = asyncio.run(a.execute(args, ctx))
    assert res.output == "echo:hi"
    assert res.is_error is False


def test_adapter_executes_async_handler() -> None:
    async def ahandler(text):
        return f"async:{text}"

    ct = CustomTool(
        name="a_echo", description="d", input_schema={"type": "object"}, handler=ahandler
    )
    a = CustomToolAdapter(ct)
    args = a.input_model(text="yo")
    res = asyncio.run(a.execute(args, ToolExecutionContext(cwd=Path("."))))
    assert res.output == "async:yo"


def test_adapter_handler_error_surfaces_as_tool_error() -> None:
    def boom(text):
        raise ValueError("bad")

    ct = CustomTool(
        name="boom", description="d", input_schema={"type": "object"}, handler=boom
    )
    a = CustomToolAdapter(ct)
    res = asyncio.run(a.execute(a.input_model(text="x"), ToolExecutionContext(cwd=Path("."))))
    assert res.is_error is True
    assert "bad" in res.output


def test_register_custom_tools_into_registry() -> None:
    reg = ToolRegistry()
    register_custom_tools(reg, [_CT])
    assert reg.get("echo_it") is not None


# --------------------------------------------------------------------------- #
# kwargs-reject guardrail
# --------------------------------------------------------------------------- #

def test_kwargs_reject_still_fires() -> None:
    with pytest.raises(TypeError):
        OpenHarnessSession(repo_path=Path("."), bogus_kwarg="x")
