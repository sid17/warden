"""B1 — the sub-agent-sync PreToolUse hook forces ``Task`` dispatch synchronous.

A ``Task`` call carrying a truthy ``run_in_background`` is rewritten to ``false`` via
``updatedInput`` (so a backgrounded sub-agent can't strand the orchestrator); a
synchronous Task call, and any non-Task tool, pass through untouched.
"""

from __future__ import annotations

import asyncio

from warden.providers.claude.subagent_sync_hook import build_subagent_sync_hook


def _cb():
    return build_subagent_sync_hook()["PreToolUse"][0].hooks[0]


def _run(coro):
    return asyncio.run(coro)


def _fire(tool_name: str, tool_input: dict) -> dict:
    cb = _cb()
    return _run(cb({"tool_name": tool_name, "tool_input": tool_input}, None, None))


def test_rewrites_run_in_background_true_to_false():
    out = _fire("Task", {"subagent_type": "researcher", "run_in_background": True})
    hso = out["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert hso["permissionDecision"] == "allow"
    assert hso["updatedInput"]["run_in_background"] is False
    # other input is preserved
    assert hso["updatedInput"]["subagent_type"] == "researcher"


def test_synchronous_task_is_noop():
    assert _fire("Task", {"subagent_type": "researcher", "run_in_background": False}) == {}
    assert _fire("Task", {"subagent_type": "researcher"}) == {}


def test_non_task_tool_is_noop():
    # A Bash run_in_background is legitimate (background shell) — only Task is forced.
    assert _fire("Bash", {"command": "sleep 1", "run_in_background": True}) == {}


def test_name_agnostic_background_flag():
    """Any truthy *background* key is cleared, not just run_in_background."""
    out = _fire("Task", {"background": True, "prompt": "x"})
    assert out["hookSpecificOutput"]["updatedInput"]["background"] is False
    assert out["hookSpecificOutput"]["updatedInput"]["prompt"] == "x"


def test_matcher_is_scoped_to_task():
    hook_map = build_subagent_sync_hook()
    matcher = hook_map["PreToolUse"][0]
    assert matcher.matcher == "Task"


def test_falsy_background_value_not_rewritten():
    """A background key already falsy needs no rewrite → no-op (avoids churn)."""
    assert _fire("Task", {"run_in_background": 0, "prompt": "x"}) == {}
