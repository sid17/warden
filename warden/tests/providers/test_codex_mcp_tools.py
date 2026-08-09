"""Unit tests — codex ungated custom-tool delivery via in-proc MCP server.

Code-truth ($0, no model, no real codex turn):
  - flag OFF + custom_tools → raises at construction, message NAMES the flag
    (fail-closed default, TOOL-1 consume-or-error).
  - flag ON + custom_tools → adapter stores tools, custom_tool_delivery == "mcp".
  - flag ON + NO tools → custom_tool_delivery == "none".
  - factory: create_session("codex", custom_tools=[...]) raises (flag off) but
    builds with allow_ungated_custom_tools=True.
  - CustomToolMcpServer maps a CustomTool → a FastMCP tool whose handler runs
    (call the wrapper directly, no codex); start()/stop() lifecycle is clean.
  - _approval: MCP elicitation is auto-accepted ONLY when opted-in ({action:
    accept}); without opt-in it declines; exec/patch stay {decision:...}.
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

import pytest

from warden.providers import create_session
from warden.providers.codex.custom_tool_mcp_server import (
    CustomToolMcpServer,
    _make_handler_fn,
)
from warden.providers.codex.sdk_message_handler import COMMAND_APPROVAL_METHOD
from warden.providers.codex.sdk_session import (
    CodexSdkSession,
    _MCP_ELICITATION_METHOD,
)
from warden.seams.custom_tools import CustomTool


def _tool(handler=None) -> CustomTool:
    return CustomTool(
        name="ping",
        description="write a marker",
        input_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        handler=handler or (lambda text: f"ran:{text}"),
    )


# --- construction gate (fail-closed default) --------------------------------

def test_flag_off_with_custom_tools_raises_naming_flag() -> None:
    with pytest.raises(NotImplementedError) as ei:
        CodexSdkSession(repo_path=Path("."), custom_tools=[_tool()])
    assert "allow_ungated_custom_tools" in str(ei.value)


def test_flag_on_with_custom_tools_builds_and_delivers_via_mcp() -> None:
    s = CodexSdkSession(
        repo_path=Path("."),
        custom_tools=[_tool()],
        allow_ungated_custom_tools=True,
    )
    assert s._custom_tools and s._custom_tools[0].name == "ping"
    assert s.custom_tool_delivery == "mcp"


def test_flag_on_without_tools_is_none() -> None:
    s = CodexSdkSession(repo_path=Path("."), allow_ungated_custom_tools=True)
    assert s.custom_tool_delivery == "none"


# --- factory routing ---------------------------------------------------------

def test_factory_flag_off_raises_flag_on_builds() -> None:
    with pytest.raises(NotImplementedError):
        create_session("codex", repo_path=Path("."), custom_tools=[_tool()])
    s = create_session(
        "codex",
        repo_path=Path("."),
        custom_tools=[_tool()],
        allow_ungated_custom_tools=True,
    )
    assert isinstance(s, CodexSdkSession)
    assert s.custom_tool_delivery == "mcp"


# --- MCP server maps CustomTool → FastMCP tool, handler runs ----------------

def test_handler_fn_signature_and_invocation() -> None:
    calls: list[str] = []

    def handler(text: str) -> str:
        calls.append(text)
        return f"ok:{text}"

    fn = _make_handler_fn(_tool(handler))
    # Signature derived from input_schema (required 'text').
    assert list(inspect.signature(fn).parameters) == ["text"]
    result = asyncio.run(fn(text="hi"))
    assert result == "ok:hi"
    assert calls == ["hi"]


def test_handler_fn_wraps_async_handler_and_stringifies() -> None:
    async def ahandler(text: str) -> str:
        return f"async:{text}"

    fn = _make_handler_fn(_tool(ahandler))
    assert asyncio.run(fn(text="z")) == "async:z"


def test_mcp_server_start_returns_localhost_url_and_stops() -> None:
    async def run() -> None:
        server = CustomToolMcpServer([_tool()])
        url = await server.start()
        assert url.startswith("http://127.0.0.1:")
        assert url.endswith("/mcp")
        assert server.url == url
        await server.stop()
        assert server.url is None

    asyncio.run(run())


# --- _approval: MCP elicitation ungated only when opted-in ------------------

def test_approval_mcp_elicitation_accepts_when_opted_in() -> None:
    s = CodexSdkSession(
        repo_path=Path("."),
        custom_tools=[_tool()],
        allow_ungated_custom_tools=True,
    )
    # MCP elicitation → auto-accept WITHOUT consulting can_use_tool.
    result = s._approval(_MCP_ELICITATION_METHOD, {"serverName": "harness_custom"})
    assert result == {"action": "accept"}


def test_approval_mcp_elicitation_declines_without_opt_in() -> None:
    # No tools + no opt-in → an elicitation (shouldn't occur) still fails closed.
    s = CodexSdkSession(repo_path=Path("."))
    result = s._approval(_MCP_ELICITATION_METHOD, {"serverName": "x"})
    assert result == {"action": "decline"}


def test_approval_exec_path_unaffected_fail_closed() -> None:
    # exec/patch approval with no can_use_tool wired → decline ({decision}).
    s = CodexSdkSession(repo_path=Path("."), allow_ungated_custom_tools=True)
    result = s._approval(COMMAND_APPROVAL_METHOD, {"command": "rm -rf /"})
    assert result == {"decision": "decline"}
