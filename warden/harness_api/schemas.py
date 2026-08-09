"""Runs API wire schemas (pydantic v2).

The HTTP contract the harness offers to any product: a ``RunSpec`` in, a stream
of ``Event``s out to a product-chosen ``Sink``. See the L1 plan §3.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, Field, model_validator


class Sink(BaseModel):
    """Where a run's events go. ``webhook`` POSTs each event to ``url``; ``sse``
    buffers them for a ``GET /runs/{id}/events`` hold-open."""

    type: Literal["webhook", "sse"]
    url: str | None = None  # required for webhook
    headers: dict[str, str] = Field(default_factory=dict)


class RunSpec(BaseModel):
    """``POST /runs`` body — start (or resume) one run.

    ``session_id`` omitted => a new session; supplied => resume it (creation and
    revision of a course share one session_id; Q&A/notes use their own). Runs on
    the same ``task_id`` serialize (per-task lock); concurrency is across tasks.
    """

    user_id: str
    task_id: str
    session_id: str | None = None
    provider: str = "claude"  # Claude SDK (canonical Anthropic adapter; claude-cli retired, D7)
    model: str | None = None
    input: dict[str, Any] = Field(default_factory=dict)  # skill-specific prompt/params
    sink: Sink
    budget_usd: float | None = None  # optional per-run cost ceiling
    # N1 durable-foundations: optional run bounds a Governor/HITL layer enforces.
    # ``deadline`` is an ISO-8601 UTC wall-clock cutoff; ``max_turns`` caps the
    # number of agent turns. Both None => unbounded (today's behavior).
    deadline: str | None = None
    max_turns: int | None = None


# N1: added ``paused`` (a HITL/Governor hold) and ``requires_action`` (the run is
# blocked awaiting an external decision, e.g. a tool approval) to the run lifecycle.
# M2 3e-2: added ``stopped`` — a deliberate Governor halt (budget/deadline/max_turns),
# distinct from ``error`` (a failure) and ``cancelled`` (a caller abort).
RunStatus = Literal[
    "queued", "running", "succeeded", "error", "cancelled",
    "paused", "requires_action", "stopped",
]

# N1: added ``stopped`` (a deliberate mid-run halt, distinct from ``result``/
# ``error``), ``compaction`` (the provider/harness compacted context), and
# ``tool_result`` (a tool's output event, the pair to ``tool_use``).
# M6: added ``permission_request`` (a durable HITL ask — the run paused at a
# confirm-required tool; data carries {tool_use_id, tool_name, tool_input, reason}),
# and ``permission_resolved`` (the ask's resolution — allow/deny, keyed by
# tool_use_id). Both ride ``run_events`` like any event, so the ask + its outcome
# survive teardown and replay (not an out-of-band side channel).
# H2: ``permission_expired`` — a BOUNDED gate whose SLA elapsed with no answer. A
# distinct signal (NOT a ``permission_resolved:deny``): it means "no human decided in
# time", so the product leaves ``awaiting_confirmation`` for a recoverable ``expired``
# state (retry re-runs from the gate) instead of a silent failure. An INDEFINITE gate
# (``hitl.sla_seconds`` None) never emits this — it just stays parked until resumed.
# EXT-A2/P1 (E4): ``completion`` (course_complete carries the files manifest) +
# a generic ``milestone`` — the FIXED, closed set the workflow's ``event_tool_map``
# may re-tag a custom-tool call into (with the existing ``checkpoint``). The
# product's specific phase/kind rides in the opaque ``data``, never a new top-level
# type — so the harness stays general-purpose without a per-app enum edit.
EventType = Literal[
    "session", "checkpoint", "token", "tool_use", "tool_result",
    "result", "error", "stopped", "compaction",
    "permission_request", "permission_resolved", "permission_expired",
    "completion", "milestone",
]

# The subset a workflow's ``event_tool_map`` may target (validated at load, E4).
MILESTONE_EVENT_TYPES = frozenset({"checkpoint", "completion", "milestone"})


class Event(BaseModel):
    """One typed event in a run's stream. ``seq`` is monotonic per run; the first
    event is always ``type:"session"`` carrying the resolved ``session_id``; the
    terminal event is always ``result`` or ``error``."""

    run_id: str
    seq: int
    type: EventType
    session_id: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    at: str  # ISO-8601 UTC


class RunAccepted(BaseModel):
    """``202`` response to ``POST /runs``."""

    run_id: str


class SeedAccepted(BaseModel):
    """``POST /seeds`` response (EXT-W1) — the opaque, immutable seed reference the
    product holds and passes to ``POST /provision`` (upload once, reference many)."""

    seed_ref: str


class ProvisionSpec(BaseModel):
    """``POST /provision`` body (EXT-W1) — lay a seed into a ``(user, task)`` workspace.

    ``seed_ref`` (from ``POST /seeds``) names *what to install*; distinct from a run's
    ``input.workflow`` (which surface a session binds to, just a name)."""

    user_id: str
    task_id: str
    seed_ref: str


class ProvisionAck(BaseModel):
    """``POST /provision`` response (EXT-W1) — what landed, for the product to validate.

    ``copied`` are the generic copy-list dest paths (manifest-driven seeds — e.g. skill/
    agent/doc trees laid at arbitrary ``to`` paths); ``mkdirs`` are the empty write-dirs
    created. Both empty for a legacy (dir-scan) seed."""

    workflows: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    copied: list[str] = Field(default_factory=list)
    mkdirs: list[str] = Field(default_factory=list)


class ToolConfirmation(BaseModel):
    """``POST /runs/{id}/tool_confirmation`` body (M6 · E6) — the durable HITL resume.

    Carries a *decision* for a paused run's ask, keyed by ``tool_use_id`` — never a
    credential (DRIVE-4: the key is resolved server-side). E6 widens the binary
    ``allow``/``deny`` to **three modes**:

    - ``approve`` — the exact deferred tool runs, the run proceeds;
    - ``reject``  — the tool is refused and the run halts (model is told NOT to retry);
    - ``revise``  — the tool is refused, but the model re-plans with the operator's
      ``feedback`` and re-submits the same gate for approval (the revise loop).

    ``reject`` and ``revise`` are BOTH ``deny`` on the wire (the proposed call is not
    approved); they differ only in the resume continuation the model receives. The
    dedicated ``feedback`` field carries a ``revise``'s guidance (never overloaded onto
    ``reason``, which stays for a ``reject``'s optional "why"); ``revise`` with empty
    ``feedback`` is rejected (422) so the model is never resumed empty (the #1 storm
    cause). Idempotent on ``(run_id, tool_use_id)``: a duplicate is a no-op.
    """

    tool_use_id: str
    decision: Literal["approve", "reject", "revise"]
    reason: str | None = None
    # E6: the operator's revision guidance — required (non-empty) for ``revise``.
    feedback: str | None = None
    # EXT-G2 edit-on-confirm (DORMANT): dropped by the 3 modes, kept on the schema
    # (built + hermetically tested in E2, harmless). Not wired to any mode; a future
    # agent may remove it. When present it still round-trips through the resume path
    # (FileDeferStore.resolve → DurableDeferHandler → PermissionResultAllow).
    updated_input: dict[str, Any] | None = Field(
        default=None, validation_alias=AliasChoices("updated_input", "updatedInput"),
    )

    @model_validator(mode="after")
    def _require_feedback_for_revise(self) -> "ToolConfirmation":
        """A ``revise`` MUST carry non-empty ``feedback`` — the model is never resumed
        with an empty revise instruction (the research's #1 storm cause). Empty/absent
        feedback on a ``revise`` → a validation error (422 at the route)."""
        if self.decision == "revise" and not (self.feedback or "").strip():
            raise ValueError("decision 'revise' requires non-empty 'feedback'")
        return self


class RunView(BaseModel):
    """``GET /runs/{id}`` status snapshot (from the in-memory run registry)."""

    run_id: str
    status: RunStatus
    session_id: str | None = None
    last_seq: int
    usage: dict[str, int | float] = Field(default_factory=dict)
    cost_usd: float = 0.0
    error: str | None = None
