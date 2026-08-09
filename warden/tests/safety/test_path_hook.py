"""SAFE-6 (M4 3e-2) — PreToolUse path-enforcement hook.

The hook fires for ALL tools (including auto-allowed Read/Grep/Glob that
``can_use_tool`` never sees) and denies out-of-glob or sensitive-path access.
We build the hook, call its async callback with a fake ``hook_input`` dict, and
assert the returned decision dict — no live SDK needed.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from warden.config.models import AuditConfig, PathHookConfig, PathRule
from warden.safety.permissions.path_hook import build_path_hook


def _run(coro):
    return asyncio.run(coro)


def _callback(cfg: PathHookConfig):
    """Obtain the closurized PreToolUse callback."""
    return build_path_hook(cfg)["PreToolUse"][0].hooks[0]


def _fire(cfg: PathHookConfig, hook_input: dict) -> dict:
    return _run(_callback(cfg)(hook_input, "tuid", {"signal": None}))


def _is_deny(result: dict) -> bool:
    hso = result.get("hookSpecificOutput", {})
    return (
        hso.get("hookEventName") == "PreToolUse"
        and hso.get("permissionDecision") == "deny"
    )


# --- Path-rule glob enforcement ---------------------------------------------


def test_read_outside_allowed_globs_denied() -> None:
    cfg = PathHookConfig(
        enabled=True,
        deny_sensitive=False,
        rules=[PathRule(match_tools=["Read"], allow_path_globs=["/repo/**"])],
    )
    result = _fire(
        cfg,
        {"tool_name": "Read", "tool_input": {"file_path": "/etc/passwd"}},
    )
    assert _is_deny(result)
    assert "outside allowed globs" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_read_inside_allowed_globs_allowed() -> None:
    cfg = PathHookConfig(
        enabled=True,
        deny_sensitive=False,
        rules=[PathRule(match_tools=["Read"], allow_path_globs=["/repo/**"])],
    )
    result = _fire(
        cfg,
        {"tool_name": "Read", "tool_input": {"file_path": "/repo/src/x.py"}},
    )
    assert result == {}


def test_unmatched_tool_not_restricted() -> None:
    """A tool not in match_tools is not glob-checked (allow)."""
    cfg = PathHookConfig(
        enabled=True,
        deny_sensitive=False,
        rules=[PathRule(match_tools=["Read"], allow_path_globs=["/repo/**"])],
    )
    result = _fire(
        cfg,
        {"tool_name": "Write", "tool_input": {"file_path": "/etc/x"}},
    )
    assert result == {}


# --- Sensitive-path deny (no glob rule needed) ------------------------------


def test_sensitive_file_path_denied_without_rules() -> None:
    cfg = PathHookConfig(enabled=True, deny_sensitive=True, rules=[])
    result = _fire(
        cfg,
        {"tool_name": "Read", "tool_input": {"file_path": "/home/u/.ssh/id_rsa"}},
    )
    assert _is_deny(result)


def test_sensitive_bash_command_denied_without_rules() -> None:
    cfg = PathHookConfig(enabled=True, deny_sensitive=True, rules=[])
    result = _fire(
        cfg,
        {"tool_name": "Bash", "tool_input": {"command": "cat /home/u/.ssh/id_rsa"}},
    )
    assert _is_deny(result)


def test_deny_sensitive_off_allows_sensitive() -> None:
    cfg = PathHookConfig(enabled=True, deny_sensitive=False, rules=[])
    result = _fire(
        cfg,
        {"tool_name": "Read", "tool_input": {"file_path": "/home/u/.ssh/id_rsa"}},
    )
    assert result == {}


# --- Fail-open on internal error --------------------------------------------


def test_malformed_hook_input_allows_and_does_not_raise() -> None:
    cfg = PathHookConfig(enabled=True, deny_sensitive=True, rules=[])
    # Missing tool_name / tool_input keys => allow, no raise.
    assert _fire(cfg, {}) == {}


# --- Session-level install (disabled => not installed; coexist w/ audit) -----


class _Opts:
    """Minimal stand-in for the SDK options object (just a .hooks attribute)."""

    def __init__(self, hooks=None):
        self.hooks = hooks


def _safety_matcher_count(opts: _Opts) -> int:
    """Count PreToolUse matchers whose callback came from the path hook (its
    callback closure name is ``_closured_hook`` in path_hook.py; distinguish by
    identity via a fresh build is overkill — assert total count deltas instead)."""
    return len((opts.hooks or {}).get("PreToolUse", []))


def test_session_does_not_install_when_disabled() -> None:
    from warden.providers.claude.session import ClaudeSession

    sess = ClaudeSession(
        repo_path=Path("."),
        safety_hooks=PathHookConfig(enabled=False),
    )
    opts = _Opts(hooks=None)
    sess.install_hooks(opts)
    assert opts.hooks is None  # neither audit nor safety installed


def test_session_installs_safety_when_enabled() -> None:
    from warden.providers.claude.session import ClaudeSession

    sess = ClaudeSession(
        repo_path=Path("."),
        safety_hooks=PathHookConfig(enabled=True),
    )
    opts = _Opts(hooks=None)
    sess.install_hooks(opts)
    assert opts.hooks is not None
    assert _safety_matcher_count(opts) == 1


def test_audit_and_safety_hooks_coexist() -> None:
    from warden.providers.claude.session import ClaudeSession

    sess = ClaudeSession(
        repo_path=Path("."),
        audit=AuditConfig(enabled=True),
        safety_hooks=PathHookConfig(enabled=True),
    )
    opts = _Opts(hooks=None)
    sess.install_hooks(opts)
    # audit installs one PreToolUse matcher (among other event types); safety
    # appends a second — both coexist under PreToolUse.
    assert _safety_matcher_count(opts) == 2
