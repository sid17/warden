"""SAFE-6 PreToolUse path-enforcement hook (Claude provider).

A ``PreToolUse`` hook fires for EVERY tool — including the auto-allowed reads
(``Read``/``Grep``/``Glob``) that the permission ``checker`` short-circuits at
step 6 and that the SDK's ``can_use_tool`` never sees. That makes it the only
enforcement point for per-path restriction of auto-allowed reads.

The hook denies when:
  * ``deny_sensitive`` is on and the invocation targets a sensitive path
    (``check_sensitive`` — reused from 3e-1); OR
  * a matching :class:`PathRule` has a non-empty ``allow_path_globs`` and the
    target path matches NONE of them.

DENY SHAPE (verified against the installed ``claude_agent_sdk`` types.py —
``PreToolUseHookSpecificOutput`` TypedDict, fields ``hookEventName`` /
``permissionDecision: Literal["allow","deny","ask","defer"]`` /
``permissionDecisionReason``)::

    {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                            "permissionDecision": "deny",
                            "permissionDecisionReason": <reason>}}

An empty ``{}`` return is allow/no-op. The callback NEVER raises — an internal
error fails OPEN (allow) but is logged (LAW 4: no silent swallow).
"""

from __future__ import annotations

import fnmatch
import logging
from typing import Any

from claude_agent_sdk import HookContext, HookMatcher

from warden.config.models import PathHookConfig
from warden.safety.permissions.sensitive_paths import (
    _extract_path,
    check_sensitive,
)

logger = logging.getLogger(__name__)


def _deny(reason: str) -> dict:
    """Build the SDK's PreToolUse deny decision (see module docstring)."""
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _evaluate(cfg: PathHookConfig, tool_name: str, tool_input: dict) -> dict:
    """Return a DENY decision dict, or ``{}`` (allow) — pure, no I/O."""
    # Sensitive-path deny: covers the auto-allowed reads can_use_tool bypasses.
    if cfg.deny_sensitive and check_sensitive(tool_name, tool_input):
        return _deny(f"sensitive path access denied for {tool_name}")

    # Per-rule glob enforcement.
    for rule in cfg.rules:
        if tool_name not in rule.match_tools or not rule.allow_path_globs:
            continue
        path = _extract_path(tool_name, tool_input)
        if path is None:
            continue
        if not any(fnmatch.fnmatch(path, g) for g in rule.allow_path_globs):
            return _deny(
                f"{tool_name} path {path!r} is outside allowed globs "
                f"{rule.allow_path_globs}"
            )

    return {}


def build_path_hook(cfg: PathHookConfig) -> dict[str, list[HookMatcher]]:
    """Build the ``{"PreToolUse": [HookMatcher(...)]}`` dict to merge into
    ``options.hooks``. ``cfg`` is closurized into the async callback."""

    async def _closured_hook(
        hook_input: Any,
        tool_use_id: str | None,
        context: HookContext,
    ) -> dict:
        try:
            tool_name = hook_input.get("tool_name")
            tool_input = hook_input.get("tool_input", {}) or {}
            if not tool_name:
                return {}
            return _evaluate(cfg, tool_name, tool_input)
        except Exception:
            # Fail OPEN (allow) on internal error, but never silently — a hook
            # crash must not take down the turn (LAW 4).
            logger.exception(
                "Path hook error for tool %s", getattr(hook_input, "get", lambda *_: None)("tool_name")
            )
            return {}

    return {"PreToolUse": [HookMatcher(matcher=None, hooks=[_closured_hook], timeout=5.0)]}
