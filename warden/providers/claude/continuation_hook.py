"""B1 — a top-level ``Stop`` hook that continues an orchestrator run in-stream
until a named completion tool fires.

The problem (B1): the harness wraps one ``POST /runs`` == one provider turn. A
multi-agent orchestrator that dispatches research sub-agents (the ``Task`` tool)
then ENDS ITS TURN (``stop_reason=end_turn``) BEFORE the pipeline's final custom
tool (``course_complete``) fires terminates the run prematurely — after
``research``, before write/index/course_complete. An external
"resume-until-complete" driver loop worked around it.

The fix (canonical): a Claude SDK top-level ``Stop`` hook on the orchestrator
session that gates on "has the completion tool fired?":

- ``stop_hook_active`` True  → ``{}``  (MANDATORY loop-guard: NEVER block on
  re-entry — the SDK sets this flag when a prior block already re-prompted, so
  blocking again would loop forever).
- completion tool fired      → ``{}``  (allow the stop — the pipeline finished).
- otherwise                  → ``{"decision": "block", "reason": <directive>}``.
  The SDK continues in-stream, same session, feeding ``reason`` as the next
  prompt — no new ``query()``.

Gated on the ORCHESTRATOR top-level ``Stop`` (NOT ``SubagentStop`` — its block
is documented-broken). Verified against the installed ``claude_agent_sdk``
(0.2.116): ``StopHookInput`` carries ``stop_hook_active: bool``; the blocking
output shape is ``{"decision": "block", "reason": ...}`` (``SyncHookJSONOutput``).
"""

from __future__ import annotations

import logging
from typing import Any

from claude_agent_sdk import HookMatcher

logger = logging.getLogger(__name__)

#: Prefix under which the harness registers in-proc custom tools (mirrors
#: ``session._CUSTOM_TOOL_PREFIX``). The completion tool surfaces to the model as
#: ``mcp__harness_custom__course_complete`` — the tracker matches bare-or-prefixed.
_CUSTOM_TOOL_PREFIX = "mcp__harness_custom__"

#: The default directive fed back to the model when it tries to stop early. Kept
#: product-neutral AND order-preserving: it must NOT enumerate a fixed set of
#: remaining steps (e.g. "write, index, complete") — doing so tells the model to
#: JUMP to those steps and SKIP anything it has not done yet, including a required
#: confirmation/approval gate. Instead it says "resume in order, don't skip". A
#: product with a named gate should override this via ``ContinuationConfig.directive``
#: to name the gated step explicitly (see your profile's factory).
DEFAULT_DIRECTIVE = (
    "Do not stop yet — you have not called the completion tool, so the task is not "
    "finished. Resume the workflow IN ORDER from exactly where you left off. Do NOT "
    "skip any step you have not yet completed, including any required confirmation or "
    "approval gate. Complete each remaining step in sequence and call the completion "
    "tool only as the final step. Continue now."
)


class CompletionTracker:
    """Per-run flag: has the run's completion tool fired?

    Shared between (a) the message-observation point that ``mark(name)``s each
    tool call and (b) the ``Stop``-hook closure that reads ``fired``. One instance
    per run — created once when continuation is enabled, referenced by both.
    """

    __slots__ = ("tool_name", "fired")

    def __init__(self, tool_name: str) -> None:
        #: The bare completion-tool name to watch for (e.g. ``course_complete``).
        self.tool_name = tool_name
        #: Set True once a matching tool call is observed. Read by the Stop hook.
        self.fired = False

    def mark(self, name: str) -> None:
        """Flip ``fired`` if ``name`` is the completion tool (bare or FQMN-prefixed).

        Accepts both the bare name (``course_complete``) and the SDK-MCP
        fully-qualified name (``mcp__harness_custom__course_complete``) — the
        model calls custom tools by their prefixed name, so both must match.
        Any other tool name is ignored (idempotent; a no-op once fired).
        """
        if not self.tool_name:
            return
        bare = name.rsplit("__", 1)[-1] if name.startswith(_CUSTOM_TOOL_PREFIX) else name
        if bare == self.tool_name:
            if not self.fired:
                logger.info("CompletionTracker: completion tool %r fired", self.tool_name)
            self.fired = True


def make_tracker(continuation: Any) -> CompletionTracker | None:
    """Build ONE per-session tracker when continuation is enabled, else ``None``.

    Called once from the session ctor; the returned instance is shared by the
    Stop-hook closure (reads ``.fired``) and the send() observation (``.mark``)."""
    if continuation is None or not getattr(continuation, "enabled", False):
        return None
    return CompletionTracker(getattr(continuation, "until_tool", "") or "")


def observe_completion(tracker: CompletionTracker, msg: Any) -> None:
    """Mark ``tracker`` if ``msg`` carries the completion tool call.

    Scans content blocks for a ``tool_use``-kind block (by attribute, so it works
    whether or not ``anthropic.types`` is importable) and passes its ``.name`` to
    ``tracker.mark`` (which matches bare-or-prefixed). A ``ToolUseBlock`` has both
    ``.name`` and ``.input``; the ``id``-only ``ToolResultBlock`` does not."""
    content = getattr(msg, "content", None)
    if not isinstance(content, list):
        return
    for block in content:
        name = getattr(block, "name", None)
        if name and hasattr(block, "input"):
            tracker.mark(name)


def install_continuation_hook(
    merge: Any, options: Any, tracker: CompletionTracker, continuation: Any
) -> None:
    """Merge the ``Stop`` matcher onto ``options`` + set the outer ``max_turns`` cap.

    ``merge`` is the session's ``_merge_hooks(options, hooks)`` bound method (so the
    hook joins the same per-event merge the other hooks use). The directive falls
    back to :data:`DEFAULT_DIRECTIVE` when the config leaves it blank. ``max_turns``
    is an OUTER safety cap so a mis-behaving loop is still bounded — set only when
    the caller has not already pinned one (respects a pre-existing cap)."""
    directive = getattr(continuation, "directive", "") or DEFAULT_DIRECTIVE
    merge(options, build_continuation_stop_hook(tracker, directive))
    cap = getattr(continuation, "max_turns", 0) or 0
    if cap and getattr(options, "max_turns", None) is None:
        options.max_turns = cap


def build_continuation_stop_hook(
    tracker: CompletionTracker, directive: str
) -> dict[str, list[HookMatcher]]:
    """Build the ``{"Stop": [HookMatcher(...)]}`` mapping for :meth:`_merge_hooks`.

    The callback closes over ``tracker`` (read at fire time) and ``directive``
    (captured at build time). Registered with ``matcher=None`` so it fires on
    EVERY top-level ``Stop`` (there is no per-tool scoping for Stop).
    """

    async def _stop_gate(
        input_data: dict, tool_use_id: str | None, context: Any
    ) -> dict:
        try:
            # MANDATORY loop-guard: on re-entry the SDK sets stop_hook_active — a
            # prior block already re-prompted, so blocking again would loop forever.
            if input_data.get("stop_hook_active"):
                return {}
            # The completion tool fired → the pipeline finished; allow the stop.
            if tracker.fired:
                return {}
            # Not fired, first entry → block the stop and re-prompt in-stream. The
            # SDK feeds ``reason`` as the next user turn on the SAME session.
            logger.info(
                "Continuation Stop hook: completion tool %r not yet fired → blocking stop",
                tracker.tool_name,
            )
            return {"decision": "block", "reason": directive}
        except Exception:
            # Fail OPEN (allow the stop) — this hook only PROLONGS a run; an internal
            # error here must never trap a run in an un-stoppable loop. Logged, not
            # swallowed (LAW 4). Contrast the custom-tool permission gate, which
            # fails closed because it IS a security boundary.
            logger.exception("Continuation Stop hook error → fail-open (allow stop)")
            return {}

    return {"Stop": [HookMatcher(matcher=None, hooks=[_stop_gate])]}
