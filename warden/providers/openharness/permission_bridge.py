"""Bridge the orchestrator's ``can_use_tool`` callback to OpenHarness.

OpenHarness's ``QueryEngine`` enforces tool permissions through a
``PermissionChecker`` (mode-driven) plus lifecycle hooks. The arg-level seam is
the ``PRE_TOOL_USE`` hook: ``_execute_tool_call`` fires it with the FULL
``{tool_name, tool_input}`` (``openharness.engine.query._execute_tool_call``,
``query.py:881-891``) and blocks the tool BEFORE ``tool.execute`` when the
aggregated result reports ``blocked``.

The orchestrator expresses permission policy through an async
``can_use_tool(tool_name, tool_input, context)`` callback returning a
``PermissionResultAllow | PermissionResultDeny`` (Claude Agent SDK shape). This
module adapts that callback into a ``PRE_TOOL_USE`` hook so workflow-YAML rules
and orchestrator decisions are enforced with the REAL ``tool_input`` (closing
B15 — the old ``permission_prompt`` seam only carried ``(tool_name, reason)`` and
so could never see the path/command).

SECURITY: this is the enforcement seam for the OpenHarness provider. It fails
CLOSED — anything that is not an explicit ``behavior == "allow"`` blocks the tool.

The stock ``HookExecutor`` only dispatches DECLARATIVE hook definitions
(command / http / prompt / agent) — it has no arbitrary-callable path. Rather
than fork upstream, ``PermissionHookExecutor`` wraps the QueryEngine's opaque
``hook_executor`` contract (``async execute(event, payload) -> AggregatedHookResult``
with ``.blocked`` / ``.reason``): on ``PRE_TOOL_USE`` it runs the orchestrator
permission check and MERGES its verdict with any audit-hook results; every other
event delegates straight to the wrapped audit executor (so audit hooks keep
firing).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from openharness.hooks.events import HookEvent
from openharness.hooks.types import AggregatedHookResult, HookResult

# OpenHarness legacy confirmation contract: Callable[[tool_name, reason], bool].
PermissionPrompt = Callable[[str, str], Awaitable[bool]]

# Orchestrator contract: async (tool_name, tool_input, context) -> result with a
# ``.behavior`` attribute of "allow" or "deny".
CanUseTool = Callable[[str, dict, Any], Awaitable[Any]]

# The PRE_TOOL_USE hook contract we synthesize: async (payload) -> HookResult.
PermissionHook = Callable[[dict], Awaitable[HookResult]]

_HOOK_TYPE = "orchestrator_permission"


def _openharness_call_ctx(payload: dict) -> dict:
    """Build the seam context for an OpenHarness PRE_TOOL_USE payload (3a).

    Threads a per-call id if the payload exposes one under any of the plausible
    keys; ``{"tool_use_id": None}`` otherwise (the §8 fallback — resume then
    content-matches ``(tool_name, tool_input)`` instead of an exact id).
    """
    event = payload.get("event")
    event_id = getattr(event, "id", None) if event is not None else None
    tuid = (
        payload.get("tool_use_id")
        or payload.get("call_id")
        or payload.get("id")
        or event_id
    )
    return {"tool_use_id": tuid}


def build_permission_prompt(can_use_tool: CanUseTool) -> PermissionPrompt:
    """DEPRECATED — legacy 2-arg confirmation seam (name-level only).

    Retained for the standalone ``permission_prompt`` path and existing unit
    coverage, but the session now routes the orchestrator decision through
    :func:`build_permission_hook` (arg-level). OpenHarness only hands this
    callback ``(tool_name, reason)`` — the tool input is NOT available here, so
    an empty dict is forwarded and arg/path rules cannot fire. Fails closed:
    only an explicit ``behavior == "allow"`` returns ``True``.
    """

    async def permission_prompt(tool_name: str, reason: str) -> bool:
        try:
            result = await can_use_tool(tool_name, {}, None)
        except Exception:
            # A failing permission callback must never open the gate.
            return False
        return getattr(result, "behavior", None) == "allow"

    return permission_prompt


def build_auto_confirm_prompt() -> PermissionPrompt:
    """A ``permission_prompt`` that auto-approves upstream's DEFAULT-mode
    confirmation for mutating tools.

    Load-bearing: OpenHarness's ``_execute_tool_call`` runs the PRE_TOOL_USE hook
    FIRST (``query.py:881`` — the real arg-level orchestrator gate; a deny there
    returns a block and never reaches the checker), THEN
    ``PermissionChecker.evaluate()``. In DEFAULT mode every mutating tool returns
    ``allowed=False, requires_confirmation=True`` and is blocked UNLESS a
    ``permission_prompt`` confirms it (``query.py:927-947``). Since the hook has
    already made the arg-aware decision, this prompt simply approves — otherwise
    DEFAULT mode would block ALL mutations (and custom tools), starving the model
    into a ``/permissions full_auto`` loop. The checker's OWN hard denies
    (sensitive paths, explicit deny rules, command deny patterns) return
    ``allowed=False`` WITHOUT ``requires_confirmation`` and so still block here —
    they never reach this prompt. No double-deny: the hook and the prompt are
    complementary (hook = orchestrator policy, prompt = upstream ceremony).
    """

    async def auto_confirm(tool_name: str, reason: str) -> bool:
        del tool_name, reason
        return True

    return auto_confirm


def build_permission_hook(can_use_tool: CanUseTool) -> PermissionHook:
    """Adapt ``can_use_tool`` into a ``PRE_TOOL_USE`` hook (arg-level, B15).

    The returned coroutine receives the OpenHarness hook payload
    ``{tool_name, tool_input, event}`` and consults ``can_use_tool`` with the
    REAL ``tool_input`` (not ``{}``). It returns a ``HookResult`` whose
    ``blocked`` is ``True`` unless the decision explicitly allows the tool.

    Fails CLOSED: any exception in the callback blocks the tool.
    """

    async def permission_hook(payload: dict) -> HookResult:
        tool_name = payload.get("tool_name", "")
        tool_input = payload.get("tool_input") or {}
        try:
            # pre-07b / 3a — surface a per-call id if the payload carries one
            # (§8 spike: OpenHarness may not — then it's None and the resume path
            # content-matches). Never fabricate an id.
            result = await can_use_tool(tool_name, tool_input, _openharness_call_ctx(payload))
        except Exception as exc:  # noqa: BLE001 — deliberate fail-closed
            return HookResult(
                hook_type=_HOOK_TYPE,
                success=False,
                blocked=True,
                reason=f"permission callback error (fail-closed): {exc}",
            )
        allowed = getattr(result, "behavior", None) == "allow"
        reason = "" if allowed else (
            getattr(result, "message", None) or f"denied by orchestrator: {tool_name}"
        )
        return HookResult(
            hook_type=_HOOK_TYPE,
            success=allowed,
            blocked=not allowed,
            reason=reason,
        )

    return permission_hook


class PermissionHookExecutor:
    """Wrap the QueryEngine ``hook_executor`` contract to add the arg-level gate.

    QueryEngine only depends on ``async execute(event, payload) -> AggregatedHookResult``
    (``query_engine.py:157-164`` for USER_PROMPT_SUBMIT, ``query.py:881-891`` for
    PRE_TOOL_USE, which reads ``.blocked`` / ``.reason``). This wrapper injects the
    orchestrator permission check on ``PRE_TOOL_USE`` — as the SINGLE arg-level
    gate (the session leaves ``permission_prompt=None`` to avoid a double-deny) —
    and MERGES it with any ``audit`` executor results so audit hooks keep firing.
    Non-PRE_TOOL_USE events delegate straight to the wrapped audit executor.
    """

    def __init__(self, permission_hook: PermissionHook, audit_executor: Any = None) -> None:
        self._permission_hook = permission_hook
        self._audit_executor = audit_executor

    async def execute(self, event: HookEvent, payload: dict) -> AggregatedHookResult:
        results: list[HookResult] = []
        if self._audit_executor is not None:
            delegated = await self._audit_executor.execute(event, payload)
            results.extend(delegated.results)
        if event == HookEvent.PRE_TOOL_USE:
            results.append(await self._permission_hook(payload))
        return AggregatedHookResult(results=results)
