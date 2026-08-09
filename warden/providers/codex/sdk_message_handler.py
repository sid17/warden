"""Codex Python SDK (``openai-codex``) notification + approval mapping helpers.

This module holds the two pure mapping functions the SDK adapter
(``sdk_session.py``) delegates to, so the session file stays focused on
lifecycle/plumbing and under the 500-line limit (LAW 1):

  1. ``approval_to_tool_call(method, params)`` — map a Codex app-server approval
     server-request (``item/commandExecution/requestApproval`` /
     ``item/fileChange/requestApproval``) to the ``(tool_name, tool_input)`` pair
     the harness three-stage permission chain (``can_use_tool``) consumes. An
     UNKNOWN method returns ``None`` so the caller fails CLOSED (declines an
     unrecognized approval rather than accepting it).

  2. ``notification_to_event(notification, session_id)`` — normalize one SDK
     ``Notification`` (the sync ``TurnHandle.stream()`` yields these) into the
     provider-native ChatMessage dict shape the rest of the harness already
     consumes from the Claude/OpenHarness paths.

The exact approval ``params`` key sets were captured empirically from a real
credentialed turn (2026-07-18, openai-codex 0.144.4):

  item/commandExecution/requestApproval →
    keys = {availableDecisions, command, commandActions, cwd,
            environmentId, itemId, proposedExecpolicyAmendment,
            startedAtMs, threadId, turnId}
    command = "/bin/zsh -lc 'echo written > f.txt'"  (the escalated argv/string)

  item/fileChange/requestApproval →
    keys = {grantRoot, itemId, reason, startedAtMs, threadId, turnId}
    (NOTE: this request does NOT carry the patch body/path directly — it carries
    the escalation reason + grantRoot; the concrete paths live on the streamed
    file-change item. We forward what the request carries.)
"""

from __future__ import annotations

import time
import uuid
from typing import Any

# Codex app-server approval server-request method names (the risk surface).
COMMAND_APPROVAL_METHOD = "item/commandExecution/requestApproval"
FILE_CHANGE_APPROVAL_METHOD = "item/fileChange/requestApproval"


def _make_id() -> str:
    return str(uuid.uuid4())


def _now() -> float:
    return time.time()


def approval_to_tool_call(
    method: str, params: dict[str, Any] | None
) -> tuple[str, dict[str, Any]] | None:
    """Map a Codex approval server-request to ``(tool_name, tool_input)``.

    Returns ``None`` for an UNKNOWN approval method so the adapter fails CLOSED
    (declines) rather than accepting an unrecognized request.

    - ``item/commandExecution/requestApproval`` → ``("Bash", {command, cwd, ...})``
    - ``item/fileChange/requestApproval``       → ``("Edit", {reason, grant_root, ...})``
    """
    p = params or {}
    if method == COMMAND_APPROVAL_METHOD:
        return "Bash", {
            "command": p.get("command"),
            "cwd": p.get("cwd"),
            "item_id": p.get("itemId"),
            "thread_id": p.get("threadId"),
            "turn_id": p.get("turnId"),
            "command_actions": p.get("commandActions"),
        }
    if method == FILE_CHANGE_APPROVAL_METHOD:
        return "Edit", {
            "reason": p.get("reason"),
            "grant_root": p.get("grantRoot"),
            "item_id": p.get("itemId"),
            "thread_id": p.get("threadId"),
            "turn_id": p.get("turnId"),
        }
    # Unknown approval method → caller must fail closed.
    return None


def notification_to_event(notification: Any, session_id: str) -> dict[str, Any] | None:
    """Normalize one SDK ``Notification`` into a ChatMessage dict, or ``None``.

    ``notification`` is an ``openai_codex.models.Notification`` with ``.method``
    (str) and ``.payload`` (a typed pydantic model or ``UnknownNotification``).
    We read the payload defensively via ``getattr`` so a schema drift degrades to
    a dropped (``None``) event rather than a crash.
    """
    method = getattr(notification, "method", "") or ""
    payload = getattr(notification, "payload", None)

    # Agent text delta / full message.
    if method in ("item/agentMessage/delta", "item/agentMessage/updated"):
        text = _payload_text(payload)
        if not text:
            return None
        return _text_event(text, session_id)

    if method == "item/completed":
        return _item_completed_event(payload, session_id)

    if method == "item/started":
        return _item_started_event(payload, session_id)

    # Coarse per-turn usage (C5) arrives on its own notification, NOT on
    # turn/completed. token_usage is a ThreadTokenUsage with a `.total` breakdown.
    if method == "thread/tokenUsage/updated":
        usage = getattr(payload, "token_usage", None)
        return _status_event(usage, session_id)

    if method == "turn/completed":
        return None  # terminal marker only; usage came via tokenUsage/updated

    if method == "turn/failed":
        err = getattr(payload, "error", None)
        msg = getattr(err, "message", None) or "Turn failed"
        return _error_event(msg, session_id)

    # thread/*, turn/started, deltas we don't surface → drop.
    return None


def transform_codex_sdk_message(msg: Any, session_id: str) -> list[dict[str, Any]]:
    """Orchestrator per-turn message handler for the Codex **SDK** session.

    ``CodexSdkSession.send`` already yields fully-normalized ChatMessage dicts
    keyed ``"kind"`` (via :func:`notification_to_event`), so the orchestrator's
    handler is a pure passthrough: wrap the single dict in a list. A non-dict
    (schema drift) degrades to a dropped event rather than a crash.

    This function exists to close **bug 4b**: the legacy ``codex exec --json``
    handler (:func:`providers.codex.message_handler.transform_codex_message`)
    branches on ``event["type"]`` — a key the SDK session never emits — so
    routing the SDK session through it dropped *every* event (the orchestrator/
    WS codex path produced no text or tool output). ``session_id`` is accepted
    for signature parity with the other provider handlers; the dict already
    carries its own ``sessionId``.
    """
    if not isinstance(msg, dict):
        return []
    return [msg]


# --- payload readers (defensive) --------------------------------------------

def _payload_text(payload: Any) -> str:
    for attr in ("delta", "text", "message"):
        val = getattr(payload, attr, None)
        if isinstance(val, str) and val:
            return val
    return ""


def _item_of(payload: Any) -> Any:
    item = getattr(payload, "item", None)
    # The SDK wraps the concrete item in a pydantic RootModel discriminated union
    # (``ThreadItem.root`` -> ``CommandExecutionThreadItem`` / ``AgentMessageThreadItem``
    # / ``UserMessageThreadItem``). Unwrap so type + field reads hit the concrete
    # item; fall back to ``item`` for older SDKs that exposed the fields directly.
    return getattr(item, "root", item)


def _item_kind(item: Any) -> str:
    """Normalize the item's kind from an explicit ``type`` field OR (current SDK)
    its concrete class name — the wrapped union items carry no ``type`` attribute,
    so ``CommandExecutionThreadItem`` etc. must be recognized by class."""
    explicit = getattr(item, "type", None) or getattr(item, "item_type", None)
    if explicit:
        return str(explicit)
    cls = type(item).__name__
    if "CommandExecution" in cls:
        return "command_execution"
    if "AgentMessage" in cls:
        return "agent_message"
    if "UserMessage" in cls:
        return "user_message"
    return ""


def _item_started_event(payload: Any, session_id: str) -> dict[str, Any] | None:
    item = _item_of(payload)
    item_type = _item_kind(item)
    if item_type in ("command_execution", "commandExecution"):
        return {
            "kind": "tool_use",
            "id": _make_id(),
            "sessionId": session_id,
            "timestamp": _now(),
            "toolName": "Bash",
            "toolCallId": getattr(item, "id", _make_id()),
            "toolInput": {"command": getattr(item, "command", "")},
        }
    return None


def _item_completed_event(payload: Any, session_id: str) -> dict[str, Any] | None:
    item = _item_of(payload)
    item_type = _item_kind(item)
    if item_type in ("agent_message", "agentMessage"):
        text = getattr(item, "text", "") or ""
        return _text_event(text, session_id) if text else None
    if item_type in ("command_execution", "commandExecution"):
        return {
            "kind": "tool_result",
            "id": _make_id(),
            "sessionId": session_id,
            "timestamp": _now(),
            "toolCallId": getattr(item, "id", ""),
            "toolResult": getattr(item, "aggregated_output", "") or "",
            "isError": (getattr(item, "exit_code", 0) or 0) != 0,
        }
    return None


def _text_event(text: str, session_id: str) -> dict[str, Any]:
    return {
        "kind": "text",
        "id": _make_id(),
        "sessionId": session_id,
        "timestamp": _now(),
        "text": text,
    }


def _error_event(text: str, session_id: str) -> dict[str, Any]:
    return {
        "kind": "error",
        "id": _make_id(),
        "sessionId": session_id,
        "timestamp": _now(),
        "text": text,
        "isError": True,
    }


def _status_event(usage: Any, session_id: str) -> dict[str, Any]:
    """Emit a terminal ``status`` event carrying coarse per-turn usage (C5)."""
    breakdown = getattr(usage, "total", None) or usage
    return {
        "kind": "status",
        "id": _make_id(),
        "sessionId": session_id,
        "timestamp": _now(),
        "text": "result",
        "subtype": "result",
        "usage": {
            "input_tokens": getattr(breakdown, "input_tokens", 0) or 0,
            "output_tokens": getattr(breakdown, "output_tokens", 0) or 0,
            "cache_read_input_tokens": getattr(breakdown, "cached_input_tokens", 0) or 0,
        },
    }
