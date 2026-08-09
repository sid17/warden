"""Checkpoint-and-inject defer mechanic (pre-07b) — the in-process primitive M6
wraps in its durable HTTP transport.

The field standard (OpenHands `WAITING_FOR_CONFIRMATION`, OpenAI Agents SDK
`RunState.approve/reject`, LangGraph `interrupt()`/`Command`, Claude native
`defer`) is **not** a nudge (re-send a message and hope the model re-issues the
call). It is **checkpoint-and-inject**:

1. **Capture** the pending call's identity (`tool_use_id`) at the permission seam.
2. **Pause** at that exact call — hold it, don't run it, don't crash.
3. **Resume** by injecting the allow/deny decision *into that held call*, keyed by
   the id — deterministically, no re-generation.

:class:`DeferRegistry` is a ``PermissionHandler`` that does exactly this for the
**warm** path (the reference, exact-id-deterministic behaviour): each consult
parks on an :class:`asyncio.Future` keyed by ``tool_use_id`` while the turn's
provider blocks on the seam; a controller resolves that future by id to inject
the decision. Because the call is *held* (never denied-and-redriven), the exact
pending call proceeds on allow — no nudge, and multiple concurrent consults each
get their own id and resolve independently (the case a nudge could never handle).

Scope + limits (honest):
- **Warm hold works** where the provider awaits the async seam without deadlock —
  Claude (`can_use_tool` / the pre-07 custom-tool `PreToolUse` gate) and
  OpenHarness (`PRE_TOOL_USE` hook). **Codex cannot warm-hold**: its approval
  bridge is a sync reader-thread call with a hard timeout, so holding pins the
  thread → it must decline-to-end + `thread_resume` (re-drive, content-matched).
- **Cross-process** resume loses the in-memory future, so it too falls to the
  provider's native continuation + a content-matched pre-seed (see
  :meth:`DeferRegistry.preseed`). The stored ``tool_use_id`` remains the durable
  record key even when the re-driven call mints a fresh id.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from warden.seams.permissions import PermissionDecision


@dataclass
class PendingCall:
    """A permission consult paused at the seam, awaiting an injected decision."""

    tool_use_id: str
    tool_name: str
    tool_input: dict
    reason: str
    future: "asyncio.Future[PermissionDecision]" = field(repr=False)


# Called (best-effort) whenever a call is parked, so a transport can persist it
# (e.g. write pending.json / a run_events row). Never blocks resolution.
OnPending = Callable[[PendingCall], None]


class DeferRegistry:
    """A ``PermissionHandler`` that checkpoints each consult and resolves it by
    injecting a decision keyed by ``tool_use_id`` (warm path — no nudge).

    Usage::

        reg = DeferRegistry()
        config.permissions.handler_instance = reg   # drives can_use_tool
        # ... drive a turn; it parks at the tool call ...
        reg.resolve(tool_use_id, allow=True, updated_input={...})   # inject

    A missing ``tool_use_id`` (provider carried none) falls back to a synthetic
    content key so the call is still holdable; :meth:`preseed` covers the
    re-drive path where the resumed call's id differs from the stored one.
    """

    def __init__(self, on_pending: OnPending | None = None) -> None:
        self._on_pending = on_pending
        self._pending: dict[str, PendingCall] = {}
        # Content-key → decision, pre-seeded before a re-drive so the re-reached
        # consult (which mints a NEW id) auto-resolves by (tool_name, input).
        self._preseeded: dict[str, PermissionDecision] = {}
        self.captured: list[PendingCall] = []  # audit trail for tests/probes

    # --- PermissionHandler protocol ---------------------------------------

    async def request_permission(
        self, tool_name: str, tool_input: dict, reason: str,
        tool_use_id: str | None = None,
    ) -> PermissionDecision:
        # A pre-seeded decision (re-drive path) short-circuits the hold: the
        # re-reached call is auto-resolved by content, no second await.
        ckey = _content_key(tool_name, tool_input)
        if ckey in self._preseeded:
            return self._preseeded.pop(ckey)

        key = tool_use_id or ckey
        loop = asyncio.get_running_loop()
        pending = PendingCall(
            tool_use_id=key,
            tool_name=tool_name,
            tool_input=tool_input,
            reason=reason,
            future=loop.create_future(),
        )
        self._pending[key] = pending
        self.captured.append(pending)
        if self._on_pending is not None:
            self._on_pending(pending)
        # HOLD — the turn's provider blocks here; the tool does NOT run until a
        # controller injects the decision via resolve(). This is the pause.
        return await pending.future

    async def ask_user_question(self, questions: list[dict]) -> dict:
        return {"result": {}}

    # --- controller side (inject) -----------------------------------------

    def pending_ids(self) -> list[str]:
        """Ids currently parked at the seam (order of arrival)."""
        return list(self._pending)

    def get_pending(self, tool_use_id: str) -> PendingCall | None:
        return self._pending.get(tool_use_id)

    def resolve(
        self, tool_use_id: str, *, allow: bool,
        updated_input: dict | None = None, reason: str = "",
    ) -> bool:
        """Inject a decision into the exact held call. Returns False if unknown.

        Thread-safe w.r.t. the event loop: the future is resolved via
        ``call_soon_threadsafe`` so a controller on another thread (e.g. an HTTP
        handler) can resolve a call parked on the orchestrator loop.
        """
        pending = self._pending.pop(tool_use_id, None)
        if pending is None:
            return False
        decision = PermissionDecision(
            allowed=allow, source="defer",
            reason=reason or ("" if allow else "denied via defer"),
            updated_input=updated_input,
        )
        loop = pending.future.get_loop()
        if pending.future.done():
            return False
        loop.call_soon_threadsafe(_safe_set_result, pending.future, decision)
        return True

    def preseed(
        self, tool_name: str, tool_input: dict, *, allow: bool,
        updated_input: dict | None = None, reason: str = "",
    ) -> None:
        """Pre-seed a decision for a call that will be RE-DRIVEN (cross-process /
        Codex): the resumed consult mints a fresh id, so it is matched by
        ``(tool_name, normalized input)`` instead of the old id. The stored
        ``tool_use_id`` stays the durable record key."""
        self._preseeded[_content_key(tool_name, tool_input)] = PermissionDecision(
            allowed=allow, source="defer-preseed",
            reason=reason or ("" if allow else "denied via defer"),
            updated_input=updated_input,
        )


class DurableDeferHandler:
    """Durable ``PermissionHandler`` — the long-delay path (never holds a future).

    Backed by a :class:`~warden.seams.defer_store.DurableDeferStore`. On
    each consult:

    - **resolved** (an approver already wrote a decision) → return it (this is the
      resume/inject step — the re-driven or re-fired call auto-resolves); or
    - **unresolved** → record the pending call + return a **deny-to-end** decision
      that ejects: the tool does not run, the turn ends, memory is released, and
      the process can exit. The approval can arrive minutes/days later.

    This is the re-drive path used by OpenHarness (resume via
    ``continue_pending``/``load_messages``) and Codex (``thread_resume``); the
    decision is matched by ``tool_use_id`` when present, else by content. Claude
    uses native ``defer`` for exact-id inject instead (see the provider). ``M6``
    swaps the deny-to-end for a first-class ``requires_action`` transport event.

    ``session_id`` is injected by the driver (the seam callback doesn't receive
    it); ``last_action`` (``"injected"``/``"ejected"``) is a probe signal.
    """

    def __init__(self, store: "Any") -> None:
        self._store = store
        self.session_id: str | None = None
        self.last_action: str | None = None

    async def request_permission(
        self, tool_name: str, tool_input: dict, reason: str,
        tool_use_id: str | None = None,
    ) -> PermissionDecision:
        decision = self._store.get_decision(tool_use_id, tool_name, tool_input)
        if decision is not None:
            self.last_action = "injected"
            if decision.allow:
                return PermissionDecision(
                    allowed=True, source="durable",
                    updated_input=decision.updated_input,
                )
            return PermissionDecision(
                allowed=False, source="durable",
                reason=decision.reason or "denied via durable defer",
            )
        # Unresolved → persist + EJECT (deny-to-end; the tool does not run, no
        # future is held, the turn ends cleanly so the process can exit).
        key = tool_use_id or _content_key(tool_name, tool_input)
        self._store.record_pending(key, tool_name, tool_input, self.session_id)
        self.last_action = "ejected"
        return PermissionDecision(
            allowed=False, source="durable-defer",
            reason=("DEFERRED: approval is pending out-of-band. Stop now — the "
                    "turn will end and resume once a decision is recorded."),
        )

    async def ask_user_question(self, questions: list[dict]) -> dict:
        return {"result": {}}


class UnwiredDurableHandler:
    """Fail-closed placeholder for the ``durable_http`` handler kind (M6).

    The real durable HTTP handler needs per-run context (run id, event egress, the
    durable store) that config alone can't supply, so ``build_permission_handler``
    returns THIS at construction and the Runner replaces it via
    ``ChatAPI.set_permission_handler`` before ``init()``. If a ``durable_http`` run
    is ever driven WITHOUT the Runner wiring it, every consult is **denied** (never
    auto-allowed) with a loud reason — a misconfiguration refuses tools, it does not
    silently permit them.
    """

    async def request_permission(
        self, tool_name: str, tool_input: dict, reason: str,
        tool_use_id: str | None = None,
    ) -> PermissionDecision:
        return PermissionDecision(
            allowed=False, source="durable-unwired",
            reason=("durable_http permission handler was not wired by the Runner "
                    "(deny-closed). This run must be driven via the Runs API, which "
                    "injects the per-run durable handler."),
        )

    async def ask_user_question(self, questions: list[dict]) -> dict:
        return {"result": {}}


def _content_key(tool_name: str, tool_input: dict) -> str:
    """Stable content key for the re-drive/no-id fallback: a re-issued call has
    a new id but the same (tool_name, input)."""
    import json

    try:
        payload = json.dumps(tool_input, sort_keys=True, default=str)
    except Exception:
        payload = repr(tool_input)
    return f"{tool_name}\x00{payload}"


def _safe_set_result(fut: "asyncio.Future[PermissionDecision]", value: PermissionDecision) -> None:
    if not fut.done():
        fut.set_result(value)
