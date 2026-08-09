"""B1 — the Claude top-level ``Stop`` continuation hook.

The harness wraps one ``POST /runs`` == one provider turn. A multi-agent
orchestrator that dispatches research sub-agents (the ``Task`` tool) then ENDS
ITS TURN (``end_turn``) BEFORE the pipeline's final custom tool
(``course_complete``) fires terminates the run prematurely. The fix: a Claude SDK
top-level ``Stop`` hook that blocks an early stop and re-prompts IN-STREAM (same
session) until the named completion tool fires.

These tests exercise the pure hook logic (no live SDK):
- the loop-guard (``stop_hook_active`` → allow),
- allow-on-fired,
- block-when-not-fired,
- ``CompletionTracker.mark`` for bare + prefixed + unrelated names,
- ``install_hooks`` merges a ``Stop`` matcher ONLY when continuation is enabled.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from claude_agent_sdk import HookMatcher

from warden.config.models import ContinuationConfig
from warden.providers.claude.continuation_hook import (
    DEFAULT_DIRECTIVE,
    CompletionTracker,
    build_continuation_stop_hook,
    observe_completion,
)
from warden.providers.claude.session import ClaudeSession

_PREFIX = "mcp__harness_custom__"


class _Opts:
    """Minimal stand-in for ClaudeAgentOptions (hooks + max_turns only)."""

    def __init__(self, hooks=None, max_turns=None):
        self.hooks = hooks
        self.max_turns = max_turns


def _run(coro):
    return asyncio.run(coro)


# --- The Stop-hook callback -------------------------------------------------


def _callback(tracker: CompletionTracker, directive: str = "GO"):
    mapping = build_continuation_stop_hook(tracker, directive)
    assert set(mapping) == {"Stop"}
    matcher = mapping["Stop"][0]
    assert isinstance(matcher, HookMatcher)
    assert matcher.matcher is None  # fires on EVERY top-level Stop
    return matcher.hooks[0]


def test_loop_guard_allows_on_reentry():
    """stop_hook_active True → ``{}`` even when not fired (MANDATORY loop-guard)."""
    tracker = CompletionTracker("course_complete")  # not fired
    cb = _callback(tracker)
    out = _run(cb({"stop_hook_active": True}, None, None))
    assert out == {}


def test_allows_when_completion_fired():
    """tracker.fired True → allow the stop."""
    tracker = CompletionTracker("course_complete")
    tracker.fired = True
    cb = _callback(tracker)
    out = _run(cb({"stop_hook_active": False}, None, None))
    assert out == {}


def test_blocks_when_not_fired():
    """not fired + not re-entry → block with the directive as the reason."""
    tracker = CompletionTracker("course_complete")
    cb = _callback(tracker, directive="collect, write, index, then call course_complete")
    out = _run(cb({"stop_hook_active": False}, None, None))
    assert out["decision"] == "block"
    assert out["reason"] == "collect, write, index, then call course_complete"


# --- CompletionTracker.mark -------------------------------------------------


def test_mark_bare_name():
    tracker = CompletionTracker("course_complete")
    tracker.mark("course_complete")
    assert tracker.fired is True


def test_mark_prefixed_name():
    tracker = CompletionTracker("course_complete")
    tracker.mark(f"{_PREFIX}course_complete")
    assert tracker.fired is True


def test_mark_ignores_other_names():
    tracker = CompletionTracker("course_complete")
    tracker.mark("research")
    tracker.mark(f"{_PREFIX}write_course")
    tracker.mark("Task")
    assert tracker.fired is False


def test_mark_empty_tool_name_never_fires():
    tracker = CompletionTracker("")
    tracker.mark("course_complete")
    tracker.mark("")
    assert tracker.fired is False


# --- install_hooks gating ---------------------------------------------------


def _session(continuation) -> ClaudeSession:
    return ClaudeSession(repo_path=Path("/tmp/x"), continuation=continuation)


def _stop_matchers(opts: _Opts):
    return (opts.hooks or {}).get("Stop", [])


def test_install_hooks_merges_stop_when_enabled():
    cfg = ContinuationConfig(enabled=True, until_tool="course_complete", max_turns=25)
    session = _session(cfg)
    opts = _Opts()
    session.install_hooks(opts)
    matchers = _stop_matchers(opts)
    assert len(matchers) == 1
    assert isinstance(matchers[0], HookMatcher)
    # Outer safety cap threaded onto the options when previously unset.
    assert opts.max_turns == 25


def test_install_hooks_no_stop_when_disabled_default():
    cfg = ContinuationConfig()  # enabled defaults False
    session = _session(cfg)
    opts = _Opts()
    session.install_hooks(opts)
    assert _stop_matchers(opts) == []
    assert opts.max_turns is None


def test_install_hooks_no_stop_when_config_absent():
    session = _session(None)
    opts = _Opts()
    session.install_hooks(opts)
    assert _stop_matchers(opts) == []


def test_install_hooks_respects_preexisting_max_turns():
    cfg = ContinuationConfig(enabled=True, until_tool="course_complete", max_turns=25)
    session = _session(cfg)
    opts = _Opts(max_turns=5)  # caller already set a cap
    session.install_hooks(opts)
    assert opts.max_turns == 5  # not overwritten


def test_install_hooks_uses_default_directive_when_blank():
    """Blank directive on the config → the module DEFAULT_DIRECTIVE is used."""
    cfg = ContinuationConfig(enabled=True, until_tool="course_complete")  # directive ""
    session = _session(cfg)
    opts = _Opts()
    session.install_hooks(opts)
    cb = _stop_matchers(opts)[0].hooks[0]
    out = _run(cb({"stop_hook_active": False}, None, None))
    assert out["decision"] == "block"
    assert out["reason"] == DEFAULT_DIRECTIVE


# --- send()-side observation (tracker.mark via _observe_completion) ----------


class _ToolUseBlock:
    def __init__(self, name, inp=None):
        self.name = name
        self.input = inp or {}


class _ToolResultBlock:
    """A result block has an id but NO .name/.input — must not fire the tracker."""

    def __init__(self, tool_use_id):
        self.tool_use_id = tool_use_id


class _Msg:
    def __init__(self, content):
        self.content = content


def test_observe_completion_marks_on_tooluse_block():
    tracker = CompletionTracker("course_complete")
    observe_completion(tracker, _Msg([_ToolUseBlock(f"{_PREFIX}course_complete")]))
    assert tracker.fired is True


def test_observe_completion_ignores_result_and_other_tools():
    tracker = CompletionTracker("course_complete")
    observe_completion(tracker, _Msg([_ToolResultBlock("abc"), _ToolUseBlock("research")]))
    assert tracker.fired is False


def test_session_builds_tracker_when_enabled():
    cfg = ContinuationConfig(enabled=True, until_tool="course_complete")
    session = _session(cfg)
    assert session._continuation_tracker is not None
    assert session._continuation_tracker.tool_name == "course_complete"


def test_session_no_tracker_when_disabled():
    assert _session(ContinuationConfig())._continuation_tracker is None
    assert _session(None)._continuation_tracker is None
