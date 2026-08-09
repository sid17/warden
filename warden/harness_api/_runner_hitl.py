"""M6 / E6 durable HITL methods for :class:`Runner`.

Composed into ``Runner`` via the MRO; assumes ``Runner.__init__`` state
(``self._runs``, ``self._run_registry``, ``self._event_log``, ``self._cfg``,
``self._build_egress``, ``self._emit``, ``self._run``, ``self._ensure_event_log``
etc.).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Literal

from warden.config.models import DurableDeferConfig
from warden.harness_api._run_state import (
    _DUPLICATE_REVISE_REASON,
    _DURABLE_HTTP_UNSUPPORTED,
    _RunState,
    _durable_http_unsupported_reason,
    _event_log_path,
)
from warden.harness_api.egress import EgressAdapter
from warden.harness_api.schemas import RunSpec
from warden.seams.defer_store import build_defer_store, content_key


class _HitlMixin:
    def _is_durable_http(self) -> bool:
        """True when the configured permission handler is the durable HTTP kind
        AND no bespoke instance overrides it (the instance is the escape hatch —
        an app that wired its own handler owns pause/resume, not the Runner)."""
        perms = self._cfg.engine.permissions
        return perms.handler == "durable_http" and perms.handler_instance is None

    def _hitl_store_root(self, run_id: str) -> Path:
        """Per-run durable-defer store dir (a sibling of ``run_events.db``). Run-
        scoped so concurrent runs never collide on a content key; file-backed so a
        fresh process (a resume / restart) opens the same records."""
        return _event_log_path(self._cfg.engine).parent / "hitl_defer" / run_id

    def _wire_durable_eject(
        self, api: object, run_id: str, spec: RunSpec
    ) -> None:
        """Install the per-run durable eject on the ChatAPI.

        **Claude only** (07b): the SDK-native ``defer`` PreToolUse hook
        (``config.safety.durable_defer``) records the parked call to the run-scoped
        store; resume re-fires the hook for the SAME ``tool_use_id`` — exact-id
        inject, no regeneration, so a multi-tool plan advances one held call at a
        time and converges.

        OpenHarness/Codex are **fail-closed** here (defense-in-depth: ``_run`` already
        rejects them pre-flight). They have no native defer — only re-drive, which
        restates the task on resume and breaks multi-tool convergence — so wiring one
        is a hard error, never a silent ``DurableDeferHandler`` downgrade.
        """
        if spec.provider in _DURABLE_HTTP_UNSUPPORTED:
            raise RuntimeError(_durable_http_unsupported_reason(spec.provider))
        store_root = self._hitl_store_root(run_id)
        if spec.provider == "claude" and hasattr(api, "set_durable_defer"):
            api.set_durable_defer(
                DurableDeferConfig(enabled=True, store_root=str(store_root))
            )

    async def _reconstruct_paused_run(self, run_id: str) -> "_RunState | None":
        """EXT-C3c — rebuild a paused run's :class:`_RunState` from durable state so a
        replica that never held it in memory can cold-resume it (a durable-HITL confirm
        landing on a different container than the one that paused).

        Returns None unless the run is known (durable registry), carries a persisted
        spec, and is currently paused (``requires_action`` derived from the shared event
        log). Everything else — session id, the pending ask, the last content key — comes
        from the durable event log; the spec (the one non-derivable piece) from the
        registry. The rebuilt state is registered in ``_runs`` so the normal
        confirm→resolve→re-drive path then runs here, restoring the workspace (shared
        persistence), session (shared backend), and defer record (shared store).
        """
        identity = await self._run_registry.get(run_id)
        if identity is None or not identity.spec_json:
            return None
        await self._ensure_event_log()
        events = await self._event_log.replay(run_id, 0)
        view = await self._event_log.reconstruct_view(run_id)
        if view is None or view.status != "requires_action":
            return None  # only a currently-paused run is cold-resumable here
        # The last permission_request is the live pending ask (durable, replayable).
        ask: dict | None = None
        for e in events:
            if e.type == "permission_request":
                ask = e.data
        if ask is None:
            return None
        spec = RunSpec.model_validate_json(identity.spec_json)
        state = _RunState(
            run_id=run_id,
            user_id=identity.user_id,
            task_id=identity.task_id,
            model=spec.model,
            status="requires_action",
            session_id=view.session_id,
            last_seq=view.last_seq,
            spec=spec,
            # Rebuild egress from the spec's sink. A live SSE stream was pinned to the
            # replica that paused (gone here) — events still persist to the shared log,
            # so the client reconnects via GET /history?after=<seq> (the C3d path);
            # a webhook sink is fully reconstructable and fires normally.
            egress=self._build_egress(run_id, spec.sink),
            pending_tool_use_id=ask.get("tool_use_id"),
            pending_store_key=ask.get("tool_use_id"),
        )
        tool_name = ask.get("tool_name")
        if tool_name:
            state.last_ask_content_key[tool_name] = content_key(
                tool_name, ask.get("tool_input") or {}
            )
        self._runs[run_id] = state
        return state

    async def confirm(
        self, run_id: str, tool_use_id: str, *,
        decision: Literal["approve", "reject", "revise"], reason: str = "",
        feedback: str | None = None, updated_input: dict | None = None,
    ) -> dict | None:
        """M6/E6 durable HITL resume — record a decision for a paused ask and re-drive.

        E6 three modes: ``approve`` runs the exact deferred tool; ``reject`` refuses it
        and halts; ``revise`` refuses it and re-drives the model with ``feedback`` so it
        re-plans and re-submits the same gate (the revise loop). ``reject`` and
        ``revise`` are BOTH ``deny`` on the wire — the store records ``allow=False`` for
        both; only the resume continuation (keyed off ``state.last_decision``) differs.

        Returns ``None`` for an unknown run (→ 404). Otherwise a status dict:
        - ``resumed`` — a valid decision for the pending ask: recorded to the store,
          a ``permission_resolved`` event emitted, and the run re-driven (the durable
          handler injects the decision on the re-reached tool call).
        - ``already_resolved`` — a duplicate confirm on an id we already resolved: a
          **no-op** returning the recorded decision (DRIVE-3 idempotency; the tool
          runs at most once).
        - ``not_pending`` — the run isn't paused on this ``tool_use_id`` (wrong id,
          or already resumed/finished): no action.
        """
        state = self._runs.get(run_id)
        if state is None:
            # EXT-C3c: the run isn't in THIS replica's memory — try to cold-resume it
            # from durable shared state (a confirm landing on a different container than
            # the one that paused). None ⇒ genuinely unknown / not paused ⇒ 404.
            state = await self._reconstruct_paused_run(run_id)
        if state is None:
            return None
        prior = state.confirmations.get(tool_use_id)
        if prior is not None:
            return {"run_id": run_id, "tool_use_id": tool_use_id,
                    "status": "already_resolved",
                    "decision": prior["decision"]}
        if state.status != "requires_action" or state.pending_tool_use_id != tool_use_id:
            return {"run_id": run_id, "tool_use_id": tool_use_id,
                    "status": "not_pending"}
        await self._resolve_and_resume(
            state, tool_use_id, decision=decision, reason=reason,
            feedback=feedback, updated_input=updated_input,
        )
        return {"run_id": run_id, "tool_use_id": tool_use_id,
                "status": "resumed", "decision": decision}

    async def _resolve_and_resume(
        self, state: _RunState, tool_use_id: str, *,
        decision: Literal["approve", "reject", "revise"], reason: str,
        feedback: str | None = None, updated_input: dict | None = None,
    ) -> None:
        """Record the decision in the run's durable store, emit the resolution, and
        spawn the re-drive. Shared by the confirm verb and the SLA auto-deny (3d).

        E6: ``approve`` → allow; ``reject``/``revise`` → deny on the wire
        (``store.resolve(allow=False)``). The 3-valued ``decision`` + ``feedback`` are
        stashed on ``state`` so ``_execute`` picks the right decision-aware continuation.

        EXT-G2 (dormant): an optional ``updated_input`` still rides through to
        ``store.resolve`` (unused by the 3 modes; kept for the schema round-trip)."""
        run_id = state.run_id
        allow = decision == "approve"
        key = state.pending_store_key or tool_use_id
        store = build_defer_store(
            self._cfg, run_id, self._hitl_store_root(run_id)
        )
        store.resolve(key, allow=allow, updated_input=updated_input, reason=reason)
        state.confirmations[tool_use_id] = {"decision": decision, "reason": reason}
        await self._emit(
            run_id, state.egress, "permission_resolved", state,
            data={"tool_use_id": tool_use_id, "decision": decision, "reason": reason},
        )
        if state.sla_task is not None:
            state.sla_task.cancel()
            state.sla_task = None
        state.pending_tool_use_id = None
        state.pending_store_key = None
        state.status = "running"
        # 07b: mark the run resumed so _execute sends a neutral continuation (not the
        # restated original prompt) — Claude's SDK re-fires the deferred id and the
        # model CONTINUES; restating would restart a multi-tool plan (defer storm).
        # Record the mode + feedback so the continuation is decision-aware (E6):
        # reject ⇒ don't retry; revise ⇒ re-plan with this feedback and re-submit.
        state.durable_resumed = True
        state.last_decision = decision
        state.last_feedback = feedback
        # Re-drive the SAME run (reuse run_id + stashed spec/egress). _execute reads
        # state.session_id, so the SAME session resumes and the handler injects.
        state.task = asyncio.create_task(
            self._run(run_id, state.spec, state.egress)
        )

    async def _maybe_pause_durable(
        self, run_id: str, egress: EgressAdapter, state: _RunState
    ) -> bool:
        """After the turn drains, park the run if the durable eject left a NEW
        (unresolved) pending record. Emits ``permission_request`` on ``run_events``
        (replayable, teardown-safe), transitions to ``requires_action``, and arms the
        SLA. Returns True if it parked (the caller then skips the terminal).

        A resolved/consumed record (an already-confirmed ask) is NOT ``pending``, so a
        resume that injected its decision doesn't re-ask; a chained new ask does."""
        store = build_defer_store(
            self._cfg, run_id, self._hitl_store_root(run_id)
        )
        pending = [p for p in store.read_pending() if p.status == "pending"]
        if not pending:
            return False
        # One deferrable tool per turn (the Claude SDK constraint) — take the first.
        ask = pending[0]
        # E6 §3c — exact-duplicate revise storm-stop. The gate runs UNGOVERNED (the
        # Governor's max_turns/deadline backstop is NOT in this loop), so a model that,
        # told to REVISE, re-fires the byte-identical proposal would pause forever. If
        # this pause is the result of a revise AND the new proposal's content_key equals
        # the immediately-prior ask's key for the SAME tool, hard-stop instead of pausing
        # (emit a clear terminal via the existing error path). Isolated: it never touches
        # the resume plumbing — it only decides pause-vs-stop at emit time.
        ck = content_key(ask.tool_name, ask.tool_input)
        if (
            state.last_decision == "revise"
            and state.last_ask_content_key.get(ask.tool_name) == ck
        ):
            state.error = _DUPLICATE_REVISE_REASON
            return False  # caller falls through to _emit_terminal → error
        # E6 §3b — the pause count for this (run, tool), derived from the durable log,
        # stamped onto the event as a convenience mirror (revise_round(...) stays the
        # source of truth). This pause is the Nth, so add 1 to the count-so-far.
        prior_pauses = await self._event_log.revise_round(run_id, ask.tool_name)
        revise_round = prior_pauses + 1
        # tool_input carries no secret (DRIVE-4: credentials never flow through tool
        # args); redaction of tool args per egress rules is a later refinement.
        await self._emit(
            run_id, egress, "permission_request", state,
            data={"tool_use_id": ask.tool_use_id, "tool_name": ask.tool_name,
                  "tool_input": ask.tool_input, "reason": "tool requires confirmation",
                  "revise_round": revise_round},
        )
        state.last_ask_content_key[ask.tool_name] = ck
        state.status = "requires_action"
        state.pending_tool_use_id = ask.tool_use_id
        state.pending_store_key = ask.tool_use_id
        # 3d / H1: arm the SLA only when a positive bound is configured. An unanswered
        # ask then EXPIRES to a clean terminal (never a pinned run). ``sla_seconds is
        # None`` ⇒ INDEFINITE: no timer, the ask stays durably parked until the human
        # returns (the interactive-gate model — resumes across a process restart via
        # ``_reconstruct_paused_run``). Cancelled by a real tool_confirmation.
        if self._cfg.hitl.sla_seconds is not None:
            state.sla_task = asyncio.create_task(
                self._sla_deadline(state, ask.tool_use_id)
            )
        return True

    async def _sla_deadline(self, state: _RunState, tool_use_id: str) -> None:
        """3d / H2: after the configured SLA, EXPIRE an unanswered ask.

        Only armed when ``hitl.sla_seconds`` is a positive bound (an INDEFINITE gate
        never arms this — it stays parked until the human returns). Cancelled by a real
        ``tool_confirmation`` (``_resolve_and_resume`` cancels ``state.sla_task``). If it
        fires, the run EXPIRES to a clean, product-synced terminal (never sits in
        ``requires_action`` forever, never auto-*allows*, and — unlike the pre-H2
        behavior — never a silent auto-*deny* re-drive)."""
        sla = self._cfg.hitl.sla_seconds
        if sla is None:  # indefinite — defensive; the arm site already guards this
            return
        try:
            await asyncio.sleep(sla)
        except asyncio.CancelledError:
            return
        if (
            state.status == "requires_action"
            and state.pending_tool_use_id == tool_use_id
        ):
            # Clear our own handle first (nothing else races it now).
            state.sla_task = None
            await self._expire_gate(state, tool_use_id)

    async def _expire_gate(self, state: _RunState, tool_use_id: str) -> None:
        """H2: a bounded gate whose SLA elapsed EXPIRES cleanly — a distinct
        ``permission_expired`` event + a terminal — NOT a silent auto-deny.

        Contrast with a real ``reject`` (an operator decision that re-drives the model
        with "do not retry"): an expiry means "no human answered in time", so it must
        NOT masquerade as a model decision or re-drive the model. It ends the run at a
        clean terminal the product renders as ``expired`` (recoverable via retry),
        never leaving the gate silently open at ``awaiting_confirmation``. A late
        confirm that lands after this finds the ask already resolved
        (``already_resolved``/``not_pending`` — DRIVE-3 idempotency), so the product
        stays on expired + Retry rather than falsely resuming."""
        run_id = state.run_id
        egress = state.egress
        key = state.pending_store_key or tool_use_id
        reason = "hitl_expired: no tool confirmation within the SLA"
        # Resolve the durable record (deny-on-the-wire) so no stray resume re-consults
        # it, and record the confirmation so a late confirm is idempotent.
        store = build_defer_store(self._cfg, run_id, self._hitl_store_root(run_id))
        store.resolve(key, allow=False, reason=reason)
        state.confirmations[tool_use_id] = {"decision": "expired", "reason": reason}
        state.pending_tool_use_id = None
        state.pending_store_key = None
        # The distinct signal the product keys on to LEAVE awaiting_confirmation for a
        # recoverable ``expired`` state (never a silent deny).
        await self._emit(
            run_id, egress, "permission_expired", state,
            data={"tool_use_id": tool_use_id, "reason": reason},
        )
        # Terminate. Reuse the error-terminal machinery (a real terminal, so the run
        # leaves ``requires_action`` and replay/reconstruct see it as done) but carry
        # the machine-readable ``hitl_expired`` reason so the product maps it to
        # ``expired`` (recoverable), not a raw failure.
        state.error = reason
        state.status = "error"
        await self._emit(run_id, egress, "error", state, data={"reason": reason})
        if egress is not None:
            await egress.aclose()
