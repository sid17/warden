"""pre-07 · M9 — Custom-tool permission parity on the Claude provider.

Claude custom tools are registered as an in-proc SDK-MCP server and their
fully-qualified names (``mcp__harness_custom__<name>``) are added to
``options.allowed_tools`` so the model may call them. The SDK **shadows**
``can_use_tool`` for any whole-tool ``allowed_tools`` entry — so a deny at the
permission seam never reaches a custom tool. The fix (this module) installs a
``PreToolUse`` gating hook, scoped to the custom-server prefix, that routes each
custom-tool call through the SAME ``self._can_use_tool`` seam regular tools use.

These tests build the hook via ``install_hooks`` and fire its callback with a
fake ``hook_input`` dict — no live SDK needed — exactly like ``test_path_hook``.
The fail-before state: no ``^mcp__harness_custom__`` matcher is installed, so a
denied custom tool is NOT gated.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

from warden.providers.claude.session import ClaudeSession
from warden.seams.custom_tools import CustomTool

_GATE_MATCHER = "^mcp__harness_custom__"


def _ping_tool() -> CustomTool:
    return CustomTool(
        name="ping",
        description="A ping tool.",
        input_schema={"type": "object", "properties": {}, "required": []},
        handler=lambda **_: "pong",
    )


class _Opts:
    """Minimal stand-in for the SDK options object (just a .hooks attribute)."""

    def __init__(self, hooks=None):
        self.hooks = hooks


def _deny_all(reason: str = "denied by test"):
    async def _cut(tool_name, tool_input, context):
        _cut.calls.append(tool_name)
        return PermissionResultDeny(behavior="deny", message=reason)

    _cut.calls = []
    return _cut


def _allow_all():
    async def _cut(tool_name, tool_input, context):
        _cut.calls.append(tool_name)
        return PermissionResultAllow(behavior="allow")

    _cut.calls = []
    return _cut


def _gate_callback(opts: _Opts):
    """Return the PreToolUse callback of the custom-tool gate matcher, or None."""
    for m in (opts.hooks or {}).get("PreToolUse", []):
        if m.matcher == _GATE_MATCHER:
            return m.hooks[0]
    return None


def _fire(callback, hook_input: dict) -> dict:
    return asyncio.run(callback(hook_input, "tuid", {"signal": None}))


def _is_deny(result: dict) -> bool:
    hso = result.get("hookSpecificOutput", {})
    return (
        hso.get("hookEventName") == "PreToolUse"
        and hso.get("permissionDecision") == "deny"
    )


# --- Install: the gate is present only with custom tools + a can_use_tool -----


def test_gate_installed_with_custom_tools() -> None:
    sess = ClaudeSession(
        repo_path=Path("."),
        can_use_tool=_deny_all(),
        custom_tools=[_ping_tool()],
    )
    opts = _Opts(hooks=None)
    sess.install_hooks(opts)
    assert _gate_callback(opts) is not None


def test_gate_not_installed_without_custom_tools() -> None:
    sess = ClaudeSession(repo_path=Path("."), can_use_tool=_deny_all())
    opts = _Opts(hooks=None)
    sess.install_hooks(opts)
    assert _gate_callback(opts) is None


def test_gate_not_installed_without_can_use_tool() -> None:
    sess = ClaudeSession(repo_path=Path("."), custom_tools=[_ping_tool()])
    opts = _Opts(hooks=None)
    sess.install_hooks(opts)
    assert _gate_callback(opts) is None


# --- Deny blocks the custom tool (the core parity fix) -----------------------


def test_denied_custom_tool_is_gated() -> None:
    cut = _deny_all("nope")
    sess = ClaudeSession(
        repo_path=Path("."), can_use_tool=cut, custom_tools=[_ping_tool()],
    )
    opts = _Opts(hooks=None)
    sess.install_hooks(opts)
    result = _fire(
        _gate_callback(opts),
        {"tool_name": "mcp__harness_custom__ping", "tool_input": {}},
    )
    # The seam was consulted with the BARE tool name...
    assert cut.calls == ["ping"]
    # ...and the deny is translated into a PreToolUse deny decision.
    assert _is_deny(result)
    assert result["hookSpecificOutput"]["permissionDecisionReason"] == "nope"


def test_allowed_custom_tool_falls_through() -> None:
    cut = _allow_all()
    sess = ClaudeSession(
        repo_path=Path("."), can_use_tool=cut, custom_tools=[_ping_tool()],
    )
    opts = _Opts(hooks=None)
    sess.install_hooks(opts)
    result = _fire(
        _gate_callback(opts),
        {"tool_name": "mcp__harness_custom__ping", "tool_input": {}},
    )
    assert cut.calls == ["ping"]
    assert result == {}  # allow => no-op fall-through


def test_hyphenated_custom_tool_name_maps_to_bare() -> None:
    """rsplit('__', 1) — a custom-tool name may itself contain hyphens/words."""
    cut = _deny_all()
    tool = CustomTool(
        name="web-search",
        description="x",
        input_schema={"type": "object", "properties": {}, "required": []},
        handler=lambda **_: "",
    )
    sess = ClaudeSession(
        repo_path=Path("."), can_use_tool=cut, custom_tools=[tool],
    )
    opts = _Opts(hooks=None)
    sess.install_hooks(opts)
    _fire(
        _gate_callback(opts),
        {"tool_name": "mcp__harness_custom__web-search", "tool_input": {}},
    )
    assert cut.calls == ["web-search"]


# --- No double-gate: a non-custom tool name is ignored by the gate -----------


def test_regular_tool_name_ignored_by_gate() -> None:
    """The gate's callback guards on the prefix so a regular tool that reaches
    it (belt-and-braces beyond the SDK matcher) is a pass-through — regular
    tools stay on can_use_tool, never double-gated."""
    cut = _deny_all()
    sess = ClaudeSession(
        repo_path=Path("."), can_use_tool=cut, custom_tools=[_ping_tool()],
    )
    opts = _Opts(hooks=None)
    sess.install_hooks(opts)
    result = _fire(
        _gate_callback(opts),
        {"tool_name": "Bash", "tool_input": {"command": "ls"}},
    )
    assert result == {}
    assert cut.calls == []  # the seam was NOT consulted for a non-custom tool


# --- Coexists with the audit + safety PreToolUse hooks ------------------------


def test_gate_fails_closed_on_seam_error() -> None:
    """A permission gate that errors must DENY, never let the tool run — else a
    handler bug (e.g. a stale signature) would silently un-gate custom tools."""

    async def _boom(tool_name, tool_input, context):
        raise TypeError("stale handler signature")

    sess = ClaudeSession(
        repo_path=Path("."), can_use_tool=_boom, custom_tools=[_ping_tool()],
    )
    opts = _Opts(hooks=None)
    sess.install_hooks(opts)
    result = _fire(
        _gate_callback(opts),
        {"tool_name": "mcp__harness_custom__ping", "tool_input": {}},
    )
    assert _is_deny(result)
    assert "fail-closed" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_gate_forwards_tool_use_id_to_seam() -> None:
    """3a: the custom-tool gate identifies the call — it passes the hook's
    tool_use_id to the seam via the context (case #2 identification)."""
    seen = {}

    async def _cut(tool_name, tool_input, context):
        seen["ctx"] = context
        return PermissionResultAllow(behavior="allow")

    sess = ClaudeSession(
        repo_path=Path("."), can_use_tool=_cut, custom_tools=[_ping_tool()],
    )
    opts = _Opts(hooks=None)
    sess.install_hooks(opts)
    asyncio.run(
        _gate_callback(opts)(
            {"tool_name": "mcp__harness_custom__ping", "tool_input": {}},
            "toolu_custom_1",
            {"signal": None},
        )
    )
    assert seen["ctx"] == {"tool_use_id": "toolu_custom_1"}


def test_shadow_warning_suppressed_only_for_custom_prefix() -> None:
    """3b: the intentional shadow for our custom-server prefix is silenced, but
    a genuine shadow of a regular tool still warns."""
    import warnings

    from claude_agent_sdk import CanUseToolShadowedWarning

    class _WireOpts:
        mcp_servers = None
        allowed_tools = None

    sess = ClaudeSession(
        repo_path=Path("."), can_use_tool=_deny_all(), custom_tools=[_ping_tool()],
    )
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        sess._wire_custom_tools(_WireOpts())  # registers the message-scoped ignore
        warnings.warn(
            "can_use_tool will not be invoked for: mcp__harness_custom__ping.",
            CanUseToolShadowedWarning,
        )
        warnings.warn(
            "can_use_tool will not be invoked for: Read.",
            CanUseToolShadowedWarning,
        )
    msgs = [str(w.message) for w in caught]
    assert not any("mcp__harness_custom__" in m for m in msgs)  # suppressed
    assert any("Read" in m for m in msgs)  # still warns


def test_gate_coexists_with_audit_and_safety_hooks() -> None:
    from warden.config.models import AuditConfig, PathHookConfig

    sess = ClaudeSession(
        repo_path=Path("."),
        can_use_tool=_deny_all(),
        custom_tools=[_ping_tool()],
        audit=AuditConfig(enabled=True),
        safety_hooks=PathHookConfig(enabled=True),
    )
    opts = _Opts(hooks=None)
    sess.install_hooks(opts)
    matchers = (opts.hooks or {}).get("PreToolUse", [])
    # audit (1) + safety (1) + custom-tool gate (1) all under PreToolUse.
    assert len(matchers) == 3
    assert _gate_callback(opts) is not None
