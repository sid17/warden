"""Stateless data + helpers for the Runs execution engine.

Pure utilities extracted from ``runner.py``: the per-run state record
(:class:`_RunState`), the durable-resume continuation vocabulary, and the
timestamp / event-log-path helpers. Nothing here holds Runner state or imports
the mixins — it is imported BY them (and by ``runner.py``), so it must never
import back the other way (avoids a circular import).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from warden.config.models import HarnessConfig
from warden.harness_api.egress import EgressAdapter
from warden.harness_api.schemas import RunSpec

# M6 07b: durable_http HITL is Claude-only. OpenHarness/Codex have no native defer
# — their only HTTP option is re-drive, which must RESTATE the task to make the
# model re-issue the call, and restating breaks multi-tool convergence (each resume
# restarts the plan → defer storm). So they are hard fail-closed on this path.
_DURABLE_HTTP_UNSUPPORTED = ("openharness", "codex")

# M6 07b: on a durable resume, Claude's SDK re-fires the deferred tool_use_id
# (exact-id inject) and the model CONTINUES the paused conversation. Re-sending the
# ORIGINAL task prompt there makes a multi-tool agent restart its plan — the bug
# 07b fixes. Resume instead with a neutral continuation: advance the held call, do
# not restart. (Mirrors pre-07b ``durable-defer-probe.py``'s RESUME_PROMPT.)
#
# The continuation is DECISION-AWARE. An APPROVE tells the model to proceed. A DENY
# must tell it the action is refused and NOT to retry — otherwise a model on a
# task it "must" finish (e.g. "write this file, do it now") re-issues the denied
# call every resume (a fresh id, no stored decision → re-eject → re-pause), a deny
# storm that never terminates (live bed finding, builtin/deny, 2026-07-24).
_DURABLE_RESUME_CONTINUATION = (
    "Continue where you left off. The pending action's approval decision is now in "
    "effect — proceed from that point. Do not restart or repeat the task."
)
_DURABLE_RESUME_DENIED = (
    "The operator DENIED the pending action; it will not be permitted. Do NOT retry "
    "it or attempt an equivalent action. Acknowledge that you cannot complete that "
    "step, do anything else that does not require it, and then stop. Do not restart "
    "the task or re-issue the denied action."
)
# E6 (three-mode gate): a REVISE is a deny-on-the-wire that must NOT halt (reject) and
# must NOT re-fire the identical proposal (storm). It carries the operator feedback and
# an explicit "must differ / do not resubmit the same" instruction — the storm lever
# (research: always attach feedback + a re-propose instruction; never resume empty). The
# model regenerates a NEW proposal and re-submits the SAME tool → a fresh tool_use_id
# with no stored decision → the durable-defer hook re-ejects → the run pauses again.
_DURABLE_RESUME_REVISE = (
    "The operator did NOT approve the pending proposal and asked you to REVISE it. "
    "Operator feedback: {feedback}\n"
    "Produce a REVISED proposal that addresses this feedback — it MUST differ from "
    "the one you just submitted — and call the same tool again to submit it for "
    "approval. Do not proceed past this step, and do not resubmit the same proposal."
)

# E6 §3c storm-stop reason (a revise that re-fired the byte-identical proposal).
_DUPLICATE_REVISE_REASON = (
    "revised proposal identical to prior — model ignored feedback"
)

# Empty-resume recovery: a durable resume occasionally returns with NO assistant work —
# the provider SDK yields only a zero-usage terminal ``status`` message (0 input/output
# tokens, empty result), no thinking/text/tool_use. This is a KNOWN, still-open upstream
# bug triggered by interrupt() + a deferred tool + a delegated Task sub-agent settling
# right before the resume's Result (claude-code #77313 / #76807; claude-agent-sdk #1190,
# where the continuation control response is dropped). The continuation Stop hook cannot
# catch it (it only fires on an assistant end_turn), so the run would otherwise emit a
# spurious empty terminal and end BEFORE its completion tool. When that happens the Runner
# re-drives the SAME session (up to this many times) with a firm continuation that
# re-engages the model. Detection keys on ``produced == 0`` (no work-kind message) — NOT a
# raw message count, since the empty result IS itself a (status) message.
_DURABLE_RESUME_REDRIVE = (
    "Your previous step produced no output. Resume the task now: continue from exactly "
    "where you left off, complete every remaining step in order, and call the "
    "completion tool as the final step. Do not restart the task. Continue now."
)
_MAX_EMPTY_REDRIVES = 2


def _durable_http_unsupported_reason(provider: str) -> str:
    """The fail-closed error a non-Claude ``durable_http`` run terminates with."""
    return (
        f"durable_http HITL is unsupported for {provider}: re-drive cannot hold a "
        "tool call across a durable pause (restating the task breaks multi-tool "
        "convergence). Use the in-process warm hold (DeferRegistry), or run this "
        "provider without durable_http."
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class _RunState:
    run_id: str
    user_id: str
    task_id: str
    model: str | None = None
    status: str = "queued"
    session_id: str | None = None
    last_seq: int = 0
    usage: dict[str, int | float] = field(default_factory=dict)
    cost_usd: float = 0.0
    error: str | None = None
    result_text: str = ""
    # Set when a StoppedEvent is folded in (a Governor halt); drives the single
    # ``stopped`` terminal instead of ``result``/``error``.
    stopped_reason: str | None = None
    # Per-run governor (governed path only); read at terminal-emit for pause (GOV-6).
    run_governor: object | None = None
    task: asyncio.Task | None = None
    # M6 durable HITL: the run's spec + egress are stashed so a later
    # tool_confirmation can re-drive (resume) the run; the pending ids let the
    # confirm path match + stay idempotent; ``sla_task`` is the per-ask deadline.
    spec: RunSpec | None = None
    egress: EgressAdapter | None = None
    pending_tool_use_id: str | None = None
    pending_store_key: str | None = None
    sla_task: asyncio.Task | None = None
    # M6 07b: set once the run has been resumed at least once via tool_confirmation,
    # so _execute sends a neutral continuation (not the restated prompt) on re-drive.
    durable_resumed: bool = False
    # M6 07b / E6: the most recent durable decision, so the re-drive continuation is
    # decision-aware. E6 widens this from a bool to a 3-valued mode — a ``reject`` tells
    # the model to STOP; a ``revise`` tells it to re-plan with ``last_feedback`` and
    # re-submit (reject and revise are BOTH deny on the wire, so a bool could not tell
    # them apart — only this 3-valued field selects the right continuation).
    last_decision: Literal["approve", "reject", "revise"] | None = None
    last_feedback: str | None = None
    # E6 §3c: the content_key of the immediately-prior gate ask for a given tool, so a
    # revise that re-fires the byte-identical proposal (model ignored feedback) is
    # detected at the next pause and hard-stopped instead of pausing forever (the gate
    # runs UNGOVERNED, so the Governor max_turns/deadline backstop is NOT in this loop).
    last_ask_content_key: dict[str, str] = field(default_factory=dict)
    # Resolved asks (tool_use_id -> {"allow", "reason"}), so a duplicate
    # tool_confirmation is a no-op returning the recorded decision (DRIVE-3).
    confirmations: dict[str, dict] = field(default_factory=dict)
    # EXT-P1/A2 (E4): the run's {tool_name → event_type} re-tag map, resolved once
    # per run (config base ∪ the workflow manifest's map) for _handle_message.
    event_tool_map: dict[str, str] = field(default_factory=dict)
    # N2: True once a ``stream_delta`` token has been emitted for this run. With
    # ``include_partial_messages`` on, every assembled ``text`` TextBlock was already
    # streamed as deltas, so its token would DUPLICATE the answer — suppress it once
    # streaming is observed. A non-streaming run never sets this, so its text still emits.
    saw_stream_delta: bool = False


def _event_log_path(engine: HarnessConfig) -> Path:
    """Where the durable ``run_events`` log lives — a sibling of the session DB.

    Reuses the persistence session-db location (``run_events.db`` next to
    ``sessions.db``) so one storage dir holds the durable control-plane state; falls
    back to ``data/run_events.db`` when no session db is configured.
    """
    session_db = getattr(engine.persistence, "session_db_path", None)
    base = Path(session_db).parent if session_db else Path("data")
    return base / "run_events.db"
