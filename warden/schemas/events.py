"""Typed events yielded by the Orchestrator."""

from dataclasses import dataclass


@dataclass
class OrchestratorEvent:
    """Base event from the orchestrator."""


@dataclass
class SessionCreatedEvent(OrchestratorEvent):
    session_id: str
    resumed: bool = False


@dataclass
class MessageEvent(OrchestratorEvent):
    """A normalized chat message (text, tool use, result, etc.)."""

    kind: str  # "text", "thinking", "tool_use", "tool_result", "status", etc.
    content: dict  # Provider-normalized message content (from message handlers)
    session_id: str = ""


@dataclass
class CompletionEvent(OrchestratorEvent):
    session_id: str


@dataclass
class ErrorEvent(OrchestratorEvent):
    text: str
    session_id: str = ""


@dataclass
class StoppedEvent(OrchestratorEvent):
    """A deliberate mid-run halt from the Governor seam (M2 / B17).

    ``reason`` is a typed, engine-opaque token (``"budget"`` / ``"deadline"`` /
    ``"max_turns"``) — the ``stop(reason)`` verdict made observable. Distinct from
    ``ErrorEvent`` (a failure) and ``CompletionEvent`` (a clean finish): the run
    was bounded on purpose. M5 records it as the AUD-3 terminal event; the Runner
    maps it to the wire ``stopped`` event type."""

    reason: str
    session_id: str = ""


@dataclass
class ToolAccessNotificationEvent(OrchestratorEvent):
    """Informational: tool was allowed/denied by workflow config."""

    tool_name: str
    action: str  # "allowed" or "denied"
    reason: str
