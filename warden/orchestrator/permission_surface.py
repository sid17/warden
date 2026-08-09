"""Workflow-bound permission surface — build & restore (SESS-1 / SESS-2 / N9).

A session's permission surface is *init-bound session identity* (D1): the
``PermissionChecker`` is derived once from the session's workflow and never
re-pointed mid-session. These free helpers own that derivation so the
orchestrator core stays focused on lifecycle. Both the create path (build from
the init-bound workflow) and the resume path (rebuild from the workflow
persisted in the session DB) route through :func:`build_permission_checker`.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

from warden.safety.permissions.checker import PermissionChecker
from warden.schemas.events import ToolAccessNotificationEvent
from warden.schemas.tool_scope import ToolScope
from warden.workspace.workflow.loader import load_workflow

logger = logging.getLogger(__name__)


class WorkflowMismatchError(Exception):
    """A ``send`` named a workflow different from the session's init-bound one.

    The workflow is init-bound *session identity* (D1 / SESS-1), not a mutable
    per-send input: changing it is a deliberate new-session act (mints a new
    ``session_id``), never a live re-point of a running session.
    """


def build_permission_checker(
    repo_path: Path, workflow_name: str | None,
) -> PermissionChecker:
    """Build the permission checker from a workflow (once, at bind time).

    Fail-CLOSED: a present-but-broken workflow raises ``WorkflowLoadError`` (the
    same hard-stop ``compute_deny_baseline`` enforces at init). A MISSING
    workflow → permissive default checker.
    """
    wf = load_workflow(repo_path, workflow_name) if workflow_name else None
    permissions = wf.permissions if wf else None
    # Pass the workspace root so file_access globs (workspace-relative) match the
    # ABSOLUTE paths the SDK sends for file tools (else nothing matches → deny-all).
    return PermissionChecker.from_workflow_permissions(
        permissions, workspace_root=repo_path
    )


async def evaluate_tool_permission(
    tool_name: str,
    tool_input: dict,
    *,
    tool_scope: ToolScope | None,
    permission_handler: Any,
    permission_checker: PermissionChecker,
    tool_event_queue: asyncio.Queue | None,
    tool_use_id: str | None = None,
) -> PermissionResultAllow | PermissionResultDeny:
    """The orchestrator's permission callback (extracted from ``_can_use_tool``).

    Priority chain: per-turn ``tool_scope`` (no I/O) → ``AskUserQuestion``
    forward → ``PermissionChecker`` (workflow YAML rules) → ``permission_handler``
    (may block on the user). Reads its inputs by value so the orchestrator can
    pass live per-turn state (the active scope, the resumed checker, the turn's
    event queue).
    """
    # Per-turn scope: cheapest deny, no I/O.
    if tool_scope and not tool_scope.is_allowed(tool_name):
        return PermissionResultDeny(
            behavior="deny", message=f"Tool '{tool_name}' blocked by tool scope",
        )

    # AskUserQuestion — forward to handler, return answers via updated_input.
    if tool_name == "AskUserQuestion":
        questions = tool_input.get("questions", [])
        answers = await permission_handler.ask_user_question(questions)
        result = answers.get("result", {})
        return PermissionResultAllow(
            updated_input={
                "questions": questions,
                "answers": result if isinstance(result, dict) else {},
            },
        )

    decision = permission_checker.evaluate(tool_name, tool_input)

    # Emit an informational notification for workflow-driven decisions.
    if decision.source == "tool_access" and tool_event_queue is not None:
        action = "allowed" if decision.allowed else "denied"
        try:
            tool_event_queue.put_nowait(ToolAccessNotificationEvent(
                tool_name=tool_name, action=action, reason=decision.reason,
            ))
        except asyncio.QueueFull:
            logger.warning("Tool event queue full, dropping notification")

    if decision.allowed:
        return PermissionResultAllow(behavior="allow")

    if not decision.requires_confirmation:
        return PermissionResultDeny(
            behavior="deny",
            message=decision.reason or "Denied by permission checker",
        )

    # Requires confirmation — ask the handler. Forward the tool_use_id (3a) so
    # a durable transport can key/pause/resume this exact call.
    handler_decision = await permission_handler.request_permission(
        tool_name, tool_input, decision.reason, tool_use_id=tool_use_id,
    )
    if handler_decision.allowed:
        if handler_decision.always:
            permission_checker.remember(tool_name)
        # Round-trip an optional handler-supplied mutated input (3a) — the
        # inject/resume path uses this to also rewrite the call's args.
        if handler_decision.updated_input is not None:
            return PermissionResultAllow(updated_input=handler_decision.updated_input)
        return PermissionResultAllow(behavior="allow")

    return PermissionResultDeny(
        behavior="deny", message=handler_decision.reason or "Denied by user",
    )


async def read_stored_workflow(session_manager: Any, session_id: str) -> tuple[bool, str | None]:
    """Read a persisted session's ``workflow`` column (SESS-2 / N9).

    Returns ``(found, workflow)``: ``found`` is False when no DB row exists (the
    caller leaves the init-bound surface intact); when True, ``workflow`` is the
    stored value (possibly ``None`` = no bound workflow).
    """
    entry = await session_manager._index.get(session_id)
    if entry is None:
        return False, None
    return True, entry.get("workflow")
