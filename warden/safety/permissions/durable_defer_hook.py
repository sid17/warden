"""Durable HITL defer hook (Claude PreToolUse) — exact-id inject across a
process restart (pre-07b durable mode).

Unlike the warm ``DeferRegistry`` (holds an ``asyncio.Future`` in memory) and
the ``DurableDeferHandler`` (deny-to-end + re-drive via ``can_use_tool``), this
uses the **Claude SDK's native ``defer``** for TRUE exact-inject:

- **Unresolved** call → record it in the :class:`DurableDeferStore` and return
  ``permissionDecision:"defer"``. The run stops; the ``ResultMessage`` carries
  ``deferred_tool_use{id,name,input}`` and the pending call is persisted on disk
  (transcript + our store) — the process can exit, nothing held in memory.
- **Resolved** call (an approver wrote a decision, possibly in another process) →
  return ``permissionDecision:"allow"`` (optionally ``updatedInput`` — camelCase
  per the SDK) or ``"deny"``. On ``options.resume`` the SDK **re-fires this hook
  for the SAME ``tool_use_id``** with no model regeneration, so the exact deferred
  call resolves.

Constraints (SDK, verified): headless-only; **one deferrable tool per model turn**
(multiple tool calls in one turn ignore ``defer`` with a warning); the
``permission_mode`` must match on resume. Fails CLOSED (deny) on internal error —
this is a permission gate.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from claude_agent_sdk import HookContext, HookMatcher

from warden.seams.defer_store import DurableDeferStore

logger = logging.getLogger(__name__)


def _out(decision: str, reason: str = "", updated_input: dict | None = None) -> dict:
    hso: dict[str, Any] = {
        "hookEventName": "PreToolUse",
        "permissionDecision": decision,
    }
    if reason:
        hso["permissionDecisionReason"] = reason
    if updated_input is not None:
        hso["updatedInput"] = updated_input  # camelCase — SDK TypedDict key
    return {"hookSpecificOutput": hso}


def build_durable_defer_hook(
    store: DurableDeferStore,
    *,
    permission_check: Any = None,
    on_defer: Any = None,
) -> dict[str, list[HookMatcher]]:
    """Build the ``{"PreToolUse": [HookMatcher(...)]}`` durable defer hook.

    Matcher ``None`` → fires for EVERY tool (built-in and custom), so in durable
    mode this single hook is the gate (the warm custom-tool gate is skipped).

    **Checker-aware (EXT-G1).** ``permission_check`` is an optional
    ``(tool_name, tool_input) -> PermissionDecision`` from the workflow's
    :class:`PermissionChecker` (the *checker*, NOT the handler — no re-entrancy).
    When provided, a call with no stored decision is classified BEFORE deferring:

      * checker **allows** (e.g. ``mode: auto`` auto-allow, a read-only tool) →
        return ``{}`` so the call proceeds via the normal ``can_use_tool`` path —
        **do not defer**. This is what makes "allow-all-except-[confirm]" work: only
        the confirm-listed tool pauses, siblings stream.
      * checker **requires confirmation** (the ``confirm``-listed tool) → defer
        (record pending, eject) exactly as before.
      * checker **denies** (a hard ``deny`` / sensitive path) → deny immediately (a
        hard deny aborts, it must not pause — E2 gotcha #3).

    When ``permission_check`` is ``None`` (no checker wired), the legacy behavior is
    preserved: defer every tool (the M6 default).

    ``on_defer`` is an optional ``(tool_name, tool_use_id) -> None`` callback invoked
    the moment a NEW pending defer is recorded. The Claude SDK's ``defer`` does NOT
    halt the agent loop — the model keeps going after the deferred call — so the
    session uses this signal to interrupt the turn immediately, else a non-yielding
    orchestrator races past the gate to completion before the pause ever surfaces.
    """

    async def _hook(hook_input: Any, tool_use_id: str | None, context: HookContext) -> dict:
        try:
            tool_name = hook_input.get("tool_name")
            if not tool_name:
                return {}
            tool_input = hook_input.get("tool_input", {}) or {}
            tuid = tool_use_id or hook_input.get("tool_use_id")

            # PERF + EXT-G1: consult the checker FIRST (in-memory, microseconds), BEFORE
            # any durable-store disk read. The multi-agent research phase fires a FLOOD of
            # auto-allowed tools, and an auto-allowed tool can NEVER have a recorded
            # decision (it never deferred) — so the old store.get_decision() read for every
            # tool was pure waste. Worse, the store lives on a bind-mounted volume, so that
            # wasted per-call disk read is what intermittently exceeds the SDK's hook
            # timeout under load → the hook is cancelled → "Error in hook callback" → the
            # can_use_tool permission stream closes → the run dies mid-research. Ordering
            # the checker first means ONLY a requires_confirmation tool (the gate) ever
            # touches the store. (No checker wired ⇒ legacy defer-every-tool below.)
            if permission_check is not None:
                verdict = permission_check(tool_name, tool_input)
                if verdict is not None:
                    if getattr(verdict, "allowed", False):
                        return {}  # auto-allow — proceed via can_use_tool, NO disk, NO defer
                    if not getattr(verdict, "requires_confirmation", False):
                        return _out(
                            "deny",
                            reason=getattr(verdict, "reason", "")
                            or "denied by workflow permissions",
                        )
                    # requires_confirmation → fall through to the durable store

            # Reached only for a requires_confirmation tool (the gate) or a wired-checker-
            # less legacy run. Now the disk read is warranted: resume re-fires this hook
            # for the deferred tool_use_id, and this is where the approver's decision lands.
            decision = store.get_decision(tuid, tool_name, tool_input)
            if decision is not None:
                # Resume/inject: the approver already decided.
                if decision.allow:
                    return _out("allow", updated_input=decision.updated_input)
                return _out("deny", reason=decision.reason or "denied via durable defer")

            # Unresolved: persist + defer (the run ends, the call is ejected to
            # disk; a later resume re-fires this hook for the same id).
            store.record_pending(
                tuid or tool_name, tool_name, tool_input,
                session_id=hook_input.get("session_id"),
            )
            # Signal the session to interrupt NOW — ``defer`` alone doesn't stop the
            # agent loop, so without this a non-yielding orchestrator streams past the
            # gate. Best-effort: a callback error must never block the (already
            # recorded) defer.
            if on_defer is not None:
                try:
                    on_defer(tool_name, tuid)
                except Exception:
                    logger.exception("durable defer on_defer callback error (ignored)")
            return _out(
                "defer",
                reason="approval pending out-of-band; the run will resume when a "
                       "decision is recorded",
            )
        except asyncio.CancelledError:
            # A hook-timeout cancellation is a BaseException, so it escapes the
            # ``except Exception`` below and surfaces as "Error in hook callback" — which
            # tears down the SDK permission stream. Catch it and fail CLOSED (deny) so a
            # slow hook degrades to a clean denial, never a stream crash. (#1 above should
            # keep us well under the timeout; this is defense-in-depth.)
            logger.warning(
                "Durable defer hook cancelled (timeout) → fail-closed (deny) for %s",
                hook_input.get("tool_name"),
            )
            return _out("deny", reason="durable defer hook cancelled (fail-closed)")
        except Exception:
            logger.exception("Durable defer hook error → fail-closed (deny)")
            return _out("deny", reason="durable defer hook internal error (fail-closed)")

    # #2: a generous hook timeout (was 5s) — the gate's store read + record are on a
    # bind-mounted volume; 5s was too tight under research-phase load. With #1 the store
    # is off the hot path, so this is headroom, not a latency budget we expect to use.
    return {"PreToolUse": [HookMatcher(matcher=None, hooks=[_hook], timeout=30.0)]}
