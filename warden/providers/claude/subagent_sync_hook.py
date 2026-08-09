"""B1 — force sub-agent (``Task``) dispatch SYNCHRONOUS.

The Claude ``Task`` tool accepts ``run_in_background: true``, which fires a sub-agent
asynchronously and lets the orchestrator END ITS TURN to "wait for" it. Two documented,
**closed-as-not-planned** Anthropic bugs make that strand the run:

- background sub-agents stop early and **falsely report ``completed``** to the parent
  (anthropics/claude-code#47936, ~14–30% of runs), and
- the ``Task`` tool has **no timeout**, so a hung sub-agent hangs the orchestrator
  indefinitely (anthropics/claude-code#49150).

Our multi-agent course pipeline dispatches research sub-agents in parallel (one per
concept). When the model backgrounds them, the orchestrator goes silent — no more
checkpoints → no heartbeats → the product's ~15-minute watchdog fails the job (the
observed B1: ~21-minute stall after a single research checkpoint). The course-authoring
skill directive already *asks* the model to run sub-agents synchronously; this is the
deterministic enforcement of that ask.

The hook is a ``PreToolUse`` matcher scoped to ``Task``: if the call carries a truthy
``run_in_background`` (or any other ``*background*`` flag), it rewrites that flag to
``false`` via ``updatedInput`` and allows the call. A synchronous sub-agent blocks the
orchestrator's turn until it returns, so the turn ends normally, checkpoints keep
flowing, and the continuation Stop hook can do its job. A call that is already
synchronous is a no-op (allowed unchanged).
"""

from __future__ import annotations

import logging
from typing import Any

from claude_agent_sdk import HookMatcher

logger = logging.getLogger(__name__)

#: The sub-agent dispatch tool. Scoped via the HookMatcher regex AND re-checked in the
#: callback (belt-and-suspenders) so the rewrite only ever touches Task calls.
_TASK_TOOL = "Task"


def _background_keys(tool_input: dict) -> list[str]:
    """Truthy input keys whose name mentions 'background' (e.g. ``run_in_background``).

    Name-agnostic on purpose: the CLI's param is ``run_in_background`` today, but any
    ``*background*`` flag the model sets truthy would strand the run just the same."""
    return [
        k for k, v in tool_input.items()
        if isinstance(k, str) and "background" in k.lower() and v
    ]


def build_subagent_sync_hook() -> dict[str, list[HookMatcher]]:
    """Build the ``{"PreToolUse": [HookMatcher(...)]}`` that forces ``Task`` synchronous.

    Registered with ``matcher="Task"`` so it fires only on the sub-agent dispatch tool;
    the callback double-checks ``tool_name`` and only rewrites when a background flag is
    actually set truthy — otherwise it returns ``{}`` (allow unchanged)."""

    async def _force_sync(
        input_data: dict, tool_use_id: str | None, context: Any
    ) -> dict:
        try:
            if input_data.get("tool_name") != _TASK_TOOL:
                return {}
            tool_input = input_data.get("tool_input") or {}
            bg = _background_keys(tool_input)
            if not bg:
                return {}  # already synchronous → no-op
            updated = {**tool_input}
            for k in bg:
                updated[k] = False
            logger.info(
                "subagent-sync: forced Task synchronous (cleared %s)", bg
            )
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                    "updatedInput": updated,
                }
            }
        except Exception:
            # Fail OPEN (allow unchanged): this hook only PREVENTS a background
            # dispatch; an internal error must never block a legitimate Task call.
            # Logged, not swallowed (LAW 4).
            logger.exception("subagent-sync hook error → allow unchanged")
            return {}

    return {"PreToolUse": [HookMatcher(matcher=_TASK_TOOL, hooks=[_force_sync])]}
