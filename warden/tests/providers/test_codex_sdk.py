"""Unit tests for the Codex Python SDK adapter (Phase 3).

Code-truth ($0, no model, no real codex turn):
  - custom_tools → NotImplementedError (TOOL-1); kwargs-reject; flags declared.
  - factory: `codex` → CodexSdkSession; retirees (`codex-exec`, `claude-cli`) gated.
  - approval params → tool_input mapping (the EXACT keys captured from a real
    credentialed approval, 2026-07-18): commandExecution→Bash, fileChange→Edit,
    unknown→None (caller fails closed).
  - the run_coroutine_threadsafe fail-closed bridge in isolation: allow→accept,
    deny→decline, raising callback→decline, no-callback→decline, unknown
    method→decline — WITHOUT a real codex turn (call _approval directly against a
    session whose loop is captured).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from warden.providers import create_session
from warden.providers.codex.sdk_session import CodexSdkSession
from warden.providers.codex.sdk_message_handler import (
    approval_to_tool_call,
    COMMAND_APPROVAL_METHOD,
    FILE_CHANGE_APPROVAL_METHOD,
)
from warden.seams.custom_tools import CustomTool


# --- construction / flags / guards ------------------------------------------

def test_construct_defers_auth_and_declares_flags() -> None:
    s = CodexSdkSession(repo_path=Path("."), can_use_tool=lambda *a: None)
    assert s.perm_tier == "arg_level"
    assert s.hard_kill_tier == "os"
    assert s.crash_isolated is True
    assert s.cost_visibility == "coarse"
    assert s.custom_tool_delivery == "none"
    assert s.supports_hard_deadline is True
    assert s.session_id is None


def test_custom_tools_raises_not_implemented() -> None:
    tool = CustomTool(
        name="t", description="d",
        input_schema={"type": "object", "properties": {}},
        handler=lambda: "x",
    )
    with pytest.raises(NotImplementedError):
        CodexSdkSession(repo_path=Path("."), custom_tools=[tool])


def test_rejects_unknown_kwargs() -> None:
    with pytest.raises(TypeError):
        CodexSdkSession(repo_path=Path("."), bogus=1)


def test_accepts_approval_mode_hint_without_drop() -> None:
    s = CodexSdkSession(repo_path=Path("."), approval_mode="auto_review")
    assert s._approval_mode == "auto_review"


# --- factory routing ---------------------------------------------------------

def test_factory_codex_routes_to_sdk_adapter() -> None:
    s = create_session("codex", repo_path=Path("."), can_use_tool=lambda *a: None)
    assert isinstance(s, CodexSdkSession)


def test_factory_gates_retirees() -> None:
    with pytest.raises(NotImplementedError):
        create_session("codex-exec", repo_path=Path("."))
    with pytest.raises(NotImplementedError):
        create_session("claude-cli", repo_path=Path("."))


# --- approval params → tool_input mapping (exact captured keys) --------------

def test_command_approval_maps_to_bash_with_command_and_cwd() -> None:
    params = {
        "threadId": "th-1", "turnId": "tn-1", "itemId": "exec-1",
        "startedAtMs": "1", "environmentId": "local",
        "command": "/bin/zsh -lc 'echo written > f.txt'",
        "cwd": "/work",
        "commandActions": [{"type": "unknown", "command": "echo written"}],
        "proposedExecpolicyAmendment": ["/bin/zsh", "-lc", "echo"],
        "availableDecisions": ["accept", "cancel"],
    }
    mapping = approval_to_tool_call(COMMAND_APPROVAL_METHOD, params)
    assert mapping is not None
    tool_name, tool_input = mapping
    assert tool_name == "Bash"
    assert tool_input["command"] == "/bin/zsh -lc 'echo written > f.txt'"
    assert tool_input["cwd"] == "/work"
    assert tool_input["item_id"] == "exec-1"


def test_file_change_approval_maps_to_edit() -> None:
    params = {
        "threadId": "th-1", "turnId": "tn-1", "itemId": "exec-2",
        "startedAtMs": "1", "reason": "escalate", "grantRoot": None,
    }
    mapping = approval_to_tool_call(FILE_CHANGE_APPROVAL_METHOD, params)
    assert mapping is not None
    tool_name, tool_input = mapping
    assert tool_name == "Edit"
    assert tool_input["reason"] == "escalate"
    assert tool_input["item_id"] == "exec-2"


def test_unknown_approval_method_returns_none() -> None:
    assert approval_to_tool_call("mcpServer/elicitation/request", {}) is None
    assert approval_to_tool_call("something/unknown", None) is None


# --- the run_coroutine_threadsafe fail-closed bridge (no real codex turn) ----

class _Allow:
    behavior = "allow"


class _Deny:
    behavior = "deny"


async def _bridge_case(can_use_tool, method, params) -> dict:
    """Capture a real running loop on a session, then call the SYNC _approval
    from a WORKER THREAD (mirrors the SDK reader thread) so
    run_coroutine_threadsafe bridges back to this loop."""
    s = CodexSdkSession(repo_path=Path("."), can_use_tool=can_use_tool)
    s._loop = asyncio.get_running_loop()
    return await asyncio.to_thread(s._approval, method, params)


_CMD_PARAMS = {"command": "echo hi", "cwd": "/w", "itemId": "i", "threadId": "t", "turnId": "u"}


def test_bridge_allow_returns_accept() -> None:
    async def allow(name, inp, ctx):
        assert name == "Bash"
        assert inp["command"] == "echo hi"
        return _Allow()

    res = asyncio.run(_bridge_case(allow, COMMAND_APPROVAL_METHOD, _CMD_PARAMS))
    assert res == {"decision": "accept"}


def test_bridge_deny_returns_decline() -> None:
    async def deny(name, inp, ctx):
        return _Deny()

    res = asyncio.run(_bridge_case(deny, COMMAND_APPROVAL_METHOD, _CMD_PARAMS))
    assert res == {"decision": "decline"}


def test_bridge_raising_callback_fails_closed() -> None:
    async def boom(name, inp, ctx):
        raise RuntimeError("boom")

    res = asyncio.run(_bridge_case(boom, COMMAND_APPROVAL_METHOD, _CMD_PARAMS))
    assert res == {"decision": "decline"}, "raising callback must fail CLOSED"


def test_bridge_unknown_method_fails_closed() -> None:
    async def allow(name, inp, ctx):
        return _Allow()

    # Unknown method → decline BEFORE the callback is even consulted.
    # (The MCP elicitation path is now a recognized-but-ungated method; its
    # fail-closed-without-opt-in behavior is covered in test_codex_mcp_tools.py.)
    res = asyncio.run(_bridge_case(allow, "some/unknown/method", {}))
    assert res == {"decision": "decline"}


def test_bridge_no_callback_fails_closed() -> None:
    async def _run() -> dict:
        s = CodexSdkSession(repo_path=Path("."), can_use_tool=None)
        s._loop = asyncio.get_running_loop()
        return await asyncio.to_thread(s._approval, COMMAND_APPROVAL_METHOD, _CMD_PARAMS)

    assert asyncio.run(_run()) == {"decision": "decline"}


def test_bridge_timeout_fails_closed(monkeypatch) -> None:
    """A callback slower than the approval timeout fails CLOSED (decline)."""
    import warden.providers.codex.sdk_session as mod

    monkeypatch.setattr(mod, "_APPROVAL_TIMEOUT_S", 0.05)

    async def slow(name, inp, ctx):
        await asyncio.sleep(5)
        return _Allow()

    res = asyncio.run(_bridge_case(slow, COMMAND_APPROVAL_METHOD, _CMD_PARAMS))
    assert res == {"decision": "decline"}, "slow callback must fail CLOSED on timeout"
