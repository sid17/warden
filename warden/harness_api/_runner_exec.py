"""Core execution methods for :class:`Runner` — the run loop, event handling,
terminal emit, and the event-log/egress plumbing.

Composed into ``Runner`` via the MRO; assumes ``Runner.__init__`` state
(``self._runs``, ``self._cfg``, ``self._sem``, ``self._task_lock``,
``self._governor_service``, ``self._auth_resolver``, ``self._factory``,
``self._table``, ``self._run_registry``, ``self._event_log``, ``self._sse``,
``self._webhook_client`` etc.) and helpers on other mixins
(``self._is_durable_http``, ``self._wire_durable_eject``, ``self._maybe_pause_durable``,
``self._assert_provisioned``).
"""

from __future__ import annotations

import asyncio
import logging

from warden.harness_api._run_state import (
    _DURABLE_HTTP_UNSUPPORTED,
    _DURABLE_RESUME_CONTINUATION,
    _DURABLE_RESUME_DENIED,
    _DURABLE_RESUME_REVISE,
    _RunState,
    _durable_http_unsupported_reason,
    _now,
)
from warden.harness_api.governance.pricing import cost_usd
from warden.harness_api.governance import (
    DeadlineUnsupportedError,
    assert_deadline_supported,
)
from warden.harness_api.governance.run_wiring import resolve_run_governor
from warden.providers import provider_hard_kill_tier
from warden.schemas.events import (
    CompletionEvent,
    ErrorEvent,
    MessageEvent,
    SessionCreatedEvent,
    StoppedEvent,
)
from warden.schemas.semconv import tool_attrs, usage_attrs
from warden.schemas.usage import normalize_usage
from warden.harness_api.egress import EgressAdapter, SseEgress, WebhookEgress
from warden.harness_api.run_registry import RunIdentity
from warden.harness_api.schemas import Event, RunSpec, Sink

logger = logging.getLogger(__name__)


class _ExecMixin:
    async def _ensure_event_log(self) -> None:
        """Open the durable log once, under a lock (safe across concurrent runs)."""
        if self._event_log_ready:
            return
        async with self._event_log_lock:
            if not self._event_log_ready:
                await self._event_log.init()
                self._event_log_ready = True

    def _build_egress(self, run_id: str, sink: Sink) -> EgressAdapter:
        if sink.type == "sse":
            adapter = SseEgress()
            self._sse[run_id] = adapter
            return adapter
        return WebhookEgress(
            sink.url or "", headers=sink.headers, client=self._webhook_client
        )

    async def _run(self, run_id: str, spec: RunSpec, egress: EgressAdapter) -> None:
        state = self._runs[run_id]
        try:
            # 0. Ensure the durable event log is open before the first emit.
            await self._ensure_event_log()
            # 0a. EXT-C1: persist the run identity durably before the first event, so
            # GET /runs/{id} + the ownership check resolve (user, task) after a restart.
            # Idempotent — a resume re-enters _run with the same run_id; put() no-ops.
            await self._run_registry.put(RunIdentity(
                run_id=run_id, user_id=spec.user_id, task_id=spec.task_id,
                created_at=_now(),
                # EXT-C3c: persist the immutable spec so a replica that never held this
                # run can rebuild its context and cold-resume a durable-HITL pause.
                spec_json=spec.model_dump_json(),
            ))
            # 0b. Governed-only: reject an unhonorable deadline UP FRONT (B18) — a
            # provider that keeps generating after a disconnect (hard_kill_tier
            # "none") cannot promise a deadline. Reject before the concurrency slot
            # so the run never starts and the factory is never invoked.
            if self._governor_service is not None:
                try:
                    assert_deadline_supported(
                        hard_kill_tier=provider_hard_kill_tier(spec.provider),
                        has_deadline=spec.deadline is not None,
                    )
                except DeadlineUnsupportedError as exc:
                    await self._emit(
                        run_id, egress, "error", state,
                        data={"reason": exc.code},
                    )
                    state.status = "error"
                    state.error = exc.code
                    return
            # 0c. M6 07b: durable_http HITL is Claude-only. Reject an OpenHarness/Codex
            # durable run UP FRONT (fail-closed) — before the slot, before the factory —
            # so no worker is pinned and the provider is never driven. Not a silent
            # downgrade: the run terminates ``error`` naming the split + the warm path.
            if self._is_durable_http() and spec.provider in _DURABLE_HTTP_UNSUPPORTED:
                reason = _durable_http_unsupported_reason(spec.provider)
                await self._emit(run_id, egress, "error", state, data={"reason": reason})
                state.status = "error"
                state.error = reason
                return
            # 1. Serialize per course, then take a concurrency slot.
            # (3g.2b: the ungoverned path has NO budget gate — uncapped; budgets are
            # enforced by enabling governance, whose reservation ledger is the gate.)
            async with self._task_lock.hold(spec.user_id, spec.task_id):
                async with self._sem:
                    await self._execute(run_id, spec, egress, state)
        except asyncio.CancelledError:
            state.status = "cancelled"
            state.error = "cancelled"
            await self._emit(
                run_id, egress, "error", state, data={"reason": "cancelled"}
            )
        except Exception as exc:  # noqa: BLE001 - surfaced as a terminal error
            logger.exception("run %s failed", run_id)
            state.status = "error"
            state.error = str(exc)
            await self._emit(run_id, egress, "error", state, data={"reason": str(exc)})
        finally:
            # M6: a durable HITL pause is NOT terminal — keep the egress open so the
            # resumed run (re-driven by tool_confirmation) streams to the same sink.
            # The concurrency slot is already released (the ``async with self._sem``
            # exited when ``_execute`` returned), so a paused run pins no worker.
            if state.status != "requires_action":
                await egress.aclose()

    async def _execute(
        self, run_id: str, spec: RunSpec, egress: EgressAdapter, state: _RunState
    ) -> None:
        state.status = "running"
        state.model = spec.model
        # Governed path: resolve() owns auth + the reservation; ungoverned path uses
        # the same AuthResolver directly (pre-03 3d). run_governor is None when ungoverned.
        run_governor = None
        if self._governor_service is not None:
            run_governor = await resolve_run_governor(self._governor_service, spec)
            # Stash so _emit_terminal can read ``pausable`` (GOV-6) without a new param.
            state.run_governor = run_governor
            auth_env = run_governor.auth_env
        else:
            auth_env = self._auth_resolver.auth_env_for(spec.user_id, spec.provider)

        api = self._factory(spec, auth_env)
        if run_governor is not None and hasattr(api, "set_governor"):
            api.set_governor(run_governor)
        # M6: wire the per-run durable eject (build.py resolved durable_http to a
        # fail-closed placeholder; the Runner installs the real mechanism here).
        if self._is_durable_http():
            self._wire_durable_eject(api, run_id, spec)
        # 07b: a durable resume continues the paused conversation (the SDK re-fires the
        # deferred tool_use_id). Send a neutral continuation there, NOT the restated
        # prompt — restating restarts a multi-tool plan on every resume (defer storm).
        # Decision-aware: a deny tells the model not to retry (else it re-issues the
        # denied call every resume — a deny storm that never terminates).
        if self._is_durable_http() and state.durable_resumed:
            # E6 three-way, keyed off the recorded mode (reject and revise are both
            # deny-on-the-wire, so only ``last_decision`` — not a bool — distinguishes
            # them): revise re-plans with feedback; reject stops; approve proceeds.
            if state.last_decision == "revise":
                content = _DURABLE_RESUME_REVISE.format(
                    feedback=state.last_feedback or ""
                )
            elif state.last_decision == "reject":
                content = _DURABLE_RESUME_DENIED
            else:
                content = _DURABLE_RESUME_CONTINUATION
        else:
            content = str(spec.input.get("prompt") or spec.input.get("content") or "")
        workflow = spec.input.get("workflow")
        # M6 resume: on the initial pass ``state.session_id`` is None (a fresh/spec
        # session); on a tool_confirmation re-drive it holds the resolved id, so the
        # SAME session resumes and the durable handler injects the recorded decision.
        session_id = state.session_id or spec.session_id
        await api.init()  # type: ignore[attr-defined]
        # E3: a run that names input.workflow on a never-provisioned workspace errors
        # loudly here rather than running ungoverned (checked after init() so a
        # restored snapshot's manifest counts).
        self._assert_provisioned(spec)
        # E4: resolve the run's re-tag map now that the task dir is
        # restored/provisioned (the manifest is on disk) — a config base merged with
        # the workflow manifest's event_tool_map (validated at load; a broken manifest
        # raises WorkflowLoadError → the run errors fail-closed).
        state.event_tool_map = self._resolve_event_tool_map(spec)
        try:
            async for oe in api.send(  # type: ignore[attr-defined]
                content,
                session_id=session_id,
                workflow=workflow,
            ):
                await self._handle_event(run_id, egress, state, oe)
        finally:
            await api.close()  # type: ignore[attr-defined]
            # Settle the reservation to the committed run cost. Idempotent + runs in
            # this ``finally`` on error/stop/CANCEL too, so a hold is never leaked and a
            # cancel reconciles to the COMMITTED cost (turns that DID complete WERE
            # spent), not zero — LiteLLM reconciles a cancel to incurred cost. The
            # terminal path can't double-settle. No-op when ungoverned.
            if run_governor is not None:
                await run_governor.settle()

        # M6: a durable eject ends the turn WITHOUT a terminal, leaving a pending
        # record in the run's store (Claude native defer / OH-Codex deny-to-end).
        # Detect the pause here → emit the ask + park (slot already freed on return);
        # else fall through to the normal terminal. Resume re-enters via confirm().
        if self._is_durable_http() and await self._maybe_pause_durable(
            run_id, egress, state
        ):
            return
        await self._emit_terminal(run_id, egress, state)

    async def _emit_terminal(
        self, run_id: str, egress: EgressAdapter, state: _RunState
    ) -> None:
        """Emit exactly one terminal event after the stream is fully consumed.

        A Governor halt (``stopped_reason`` set) wins over error/result: the run was
        bounded on purpose, not a failure; else error, else result.
        Pause-not-fail (GOV-6): a **budget** stop on a **pausable** tenant
        (``run_governor.pausable`` — a top-up path exists) PAUSES (status ``paused``,
        ``paused=True`` on the terminal) instead of hard-stopping. The concurrency slot
        is already released here (``self._sem`` exited in ``_run``), so a paused run
        pins no worker. RESUME is idempotent — re-submitting after a top-up re-runs
        ``resolve()``, which reads the restored balance and re-admits (the ledger +
        idempotent ``settle`` make re-entry safe), so the topped-up submit IS the
        resume. A non-pausable budget / deadline / max_turns stop stays a hard stop.
        """
        pausable = getattr(state.run_governor, "pausable", False)
        if state.stopped_reason is not None:
            paused = state.stopped_reason == "budget" and pausable
            state.status = "paused" if paused else "stopped"
            extra = {"paused": True, "requires_action": "topup"} if paused else {}
            data = {"reason": state.stopped_reason, "usage": state.usage,
                    "cost_usd": state.cost_usd, **extra}
            await self._emit(run_id, egress, "stopped", state, data=data)
            # AUD-3 (M5): mirror the governance halt into the audit trail as a
            # terminal Stop (recording only — M2 produced the verdict).
            from warden.observability.audit.record import write_governance_stop
            write_governance_stop(
                self._cfg.engine.observability.audit,
                state.session_id,
                state.stopped_reason,
            )
        elif state.error is not None:
            state.status = "error"
            await self._emit(
                run_id, egress, "error", state, data={"reason": state.error}
            )
        else:
            state.status = "succeeded"
            # There is no discrete per-LLM-call wire event in the runner — LLM calls
            # fold into token deltas + this terminal, so the terminal result event is
            # where the run's LLM-call usage semconv (gen_ai.request.model +
            # gen_ai.usage.input/output_tokens) lands, alongside the existing keys.
            await self._emit(
                run_id, egress, "result", state,
                data={
                    "result": state.result_text,
                    "usage": state.usage,
                    "cost_usd": state.cost_usd,
                    **usage_attrs(state.usage, state.model),
                },
            )

    def _resolve_event_tool_map(self, spec: RunSpec) -> dict[str, str]:
        """E4: the run's effective ``{tool_name → event_type}`` map — the process
        config base merged with the per-task workflow manifest's map (the manifest
        wins). The manifest is loaded from the restored/provisioned task dir; a
        broken one raises ``WorkflowLoadError`` (fail-closed, propagates to error)."""
        from warden.persistence.keys import task_dir
        from warden.workspace.workflow.loader import load_workflow

        base = dict(self._cfg.engine.custom_tools.event_tool_map or {})
        wf_name = spec.input.get("workflow")
        if wf_name:
            td = task_dir(
                self._cfg.engine.workspace.base_dir, spec.user_id, spec.task_id
            )
            wf = load_workflow(td, wf_name)
            if wf and wf.event_tool_map:
                base.update(wf.event_tool_map)
        return base

    async def _handle_event(
        self, run_id: str, egress: EgressAdapter, state: _RunState, oe: object
    ) -> None:
        """Map one OrchestratorEvent to a typed egress Event (or fold it in)."""
        if isinstance(oe, SessionCreatedEvent):
            state.session_id = oe.session_id
            await self._emit(
                run_id, egress, "session", state,
                data={"resumed": oe.resumed}, session_id=oe.session_id,
            )
        elif isinstance(oe, MessageEvent):
            await self._handle_message(run_id, egress, state, oe)
        elif isinstance(oe, StoppedEvent):
            # A Governor halt. Fold like the terminal (don't emit mid-stream); the
            # single ``stopped`` terminal is emitted once after the stream drains.
            state.stopped_reason = oe.reason
        elif isinstance(oe, ErrorEvent):
            # Fold into the single terminal error; don't emit mid-stream.
            state.error = oe.text
        elif isinstance(oe, CompletionEvent):
            pass  # terminal is emitted once, after the whole stream

    async def _handle_message(
        self, run_id: str, egress: EgressAdapter, state: _RunState, oe: MessageEvent
    ) -> None:
        content = oe.content or {}
        kind = oe.kind
        if kind == "stream_delta":
            text = content.get("text", "")
            if text:
                state.saw_stream_delta = True
                await self._emit(run_id, egress, "token", state, data={"text": text})
        elif kind == "text":
            # N2 — with partial streaming on, this assembled TextBlock was ALREADY sent
            # as stream_delta tokens; emitting it again duplicates the whole answer.
            # Emit it only when no streaming was observed for this run (the non-streaming
            # path, where the assembled block is the sole source of the text).
            text = content.get("text", "")
            if text and not state.saw_stream_delta:
                await self._emit(run_id, egress, "token", state, data={"text": text})
        elif kind == "checkpoint":
            await self._emit(run_id, egress, "checkpoint", state, data=content)
        elif kind == "tool_use":
            # E4 re-tag: if the (bare) tool name is in the workflow's event_tool_map,
            # emit the mapped typed event with the tool INPUT passed through OPAQUE —
            # never through the tool_attrs gen_ai merge (or the product's checkpoint
            # data gets harness keys mixed in). Provider-agnostic: every provider
            # normalizes a call to kind=="tool_use", so re-tagging here covers all
            # three. The raw call is still recorded in run_events (audit intact).
            raw_name = content.get("toolName", "") or ""
            bare = raw_name.rsplit("__", 1)[-1]  # strip mcp__harness_custom__ prefix
            mapped = state.event_tool_map.get(bare)
            if mapped:
                await self._emit(
                    run_id, egress, mapped, state,
                    data=content.get("toolInput") or {},
                )
            else:
                # Merge gen_ai.* tool semconv onto a NEW dict (content may be shared
                # by other branches — never mutate it in place); keep the wire content.
                data = {**content, **tool_attrs(content.get("toolName"))}
                await self._emit(run_id, egress, "tool_use", state, data=data)
        elif kind == "status" and content.get("subtype") == "result":
            # Fold the provider's result summary into run state; the terminal
            # event carries it, and usage feeds the per-user spend cap.
            raw_usage = content.get("usage") or {}
            # Price the turn from the RAW provider shape (cost_usd reads raw keys
            # incl. cache-write, which normalization drops); keep the runner's
            # cost table authoritative via the cost_usd= override so state.usage
            # is one {input, output, cached, cost_usd} shape across providers.
            turn_cost = cost_usd(state.model, raw_usage, self._table)
            state.usage = normalize_usage(raw_usage, cost_usd=turn_cost).as_dict()
            state.result_text = content.get("result", "") or state.result_text
            state.cost_usd += turn_cost

    async def _emit(
        self,
        run_id: str,
        egress: EgressAdapter,
        etype: str,
        state: _RunState,
        *,
        data: dict | None = None,
        session_id: str | None = None,
    ) -> None:
        state.last_seq += 1
        event = Event(
            run_id=run_id,
            seq=state.last_seq,
            type=etype,  # type: ignore[arg-type]
            session_id=session_id or state.session_id,
            data=data or {},
            at=_now(),
        )
        # C2: mirror to the durable log BEFORE shipping out — the control plane
        # records every event so history survives teardown even if egress fails.
        await self._event_log.append(event)
        await egress.emit(event)
