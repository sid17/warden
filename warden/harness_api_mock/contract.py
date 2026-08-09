"""The mock's single contract import point.

Re-exports the stable models straight from ``harness_api.schemas`` and declares the
two additive durable-HITL types (the ``tool_confirmation`` body + the
``permission_*`` event types) LOCALLY, so the rest of the mock imports ONE place and
stays wire-compatible with the real Runs API contract.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

# --- stable models: imported from THIS repo's harness_api (already matching) ---
from warden.harness_api.schemas import (
    RunAccepted,
    RunSpec,
    RunStatus,
    RunView,
    Sink,
)
from warden.harness_api.schemas import EventType as _StableEventType

# --- additive durable-HITL contract types (declared locally) -------------------

# The stable ``EventType`` Literal adds two durable-HITL members
# (``permission_request`` / ``permission_resolved``). It cannot be widened in place,
# and — critically — the stable ``Event`` model *re-narrows* ``type`` to that Literal,
# so emitting a permission event through the imported ``Event`` fails pydantic
# validation. The mock therefore declares the widened ``EventType`` and a local
# ``Event`` (identical shape, widened ``type``) here.
EventType = Literal[
    "session", "checkpoint", "token", "tool_use", "tool_result",
    "result", "error", "stopped", "compaction",
    "permission_request", "permission_resolved",
]

PERMISSION_REQUEST = "permission_request"
PERMISSION_RESOLVED = "permission_resolved"


class Event(BaseModel):
    """One typed event in a run's stream (mirrors the stable ``Event``).

    Identical field shape to the stable ``Event`` — ``{run_id, seq, type, session_id,
    data, at}`` — but ``type`` is the widened ``EventType`` so the mock can emit the two
    durable-HITL events. Wire-compatible
    (``model_dump()`` produces the same JSON), so egress + the durable ``RunEventLog``
    (which take the stable ``Event``) accept it structurally.
    """

    run_id: str
    seq: int
    type: EventType
    session_id: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    at: str  # ISO-8601 UTC


class ToolConfirmation(BaseModel):
    """``POST /runs/{id}/tool_confirmation`` body (M6) — the durable HITL resume.

    Mirrors the stable ``harness_api/schemas.py`` shape.
    Carries a *decision* for a paused run's ask, keyed by ``tool_use_id`` — never a
    credential. ``allow`` runs the exact deferred tool; ``deny`` blocks it and the
    reason is fed back. Idempotent on ``(run_id, tool_use_id)``: a duplicate is a
    no-op. ``updated_input`` (D3): an optional forward-compat passthrough — concept
    edits stay product-side today, so the mock tolerates but ignores it.
    """

    tool_use_id: str
    decision: Literal["allow", "deny"]
    reason: str | None = None
    updated_input: dict | None = None


__all__ = [
    "Event",
    "EventType",
    "RunAccepted",
    "RunSpec",
    "RunStatus",
    "RunView",
    "Sink",
    "ToolConfirmation",
    "PERMISSION_REQUEST",
    "PERMISSION_RESOLVED",
    "_StableEventType",
]
