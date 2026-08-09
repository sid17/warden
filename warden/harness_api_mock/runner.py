"""``MockRunner`` — the script-player behind the mock Runs API (D2).

Public method surface is IDENTICAL to the real ``harness_api.runner.Runner`` (§5)
so ``app.py`` is a near-copy: ``submit`` / ``get`` / ``sse_for`` / ``replay`` /
``cancel`` / ``confirm`` / ``init`` / ``aclose`` / ``task_for`` — plus the new
``read_file``. ``run_id`` is a **UUID** (not the real sequential counter) so product
code never depends on the shape.

Each run spawns a background task that plays ``SCRIPTS[input.workflow]``, assigning a
per-run monotonic ``seq``, mirroring every event to the durable ``RunEventLog``
BEFORE egress (so history survives even if egress fails), and driving the durable-HITL
gate (§6). Reuses the real ``egress`` + ``event_log`` by import (one workspace, zero
drift).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# Reused verbatim (import, do not copy — one uv workspace).
from warden.harness_api.egress import (
    EgressAdapter,
    SseEgress,
    WebhookEgress,
)
from warden.harness_api.event_log import RunEventLog

from warden.harness_api_mock.event_log import MockRunEventLog
from warden.harness_api_mock.config import MockConfig
from warden.harness_api_mock.contract import (
    PERMISSION_REQUEST,
    PERMISSION_RESOLVED,
    Event,
    RunSpec,
    RunView,
    Sink,
)
from warden.harness_api_mock.files import (
    FileMissingError,
    FixtureStore,
    PathGuardError,
)
from warden.harness_api_mock.profile_loader import load_profile
from warden.harness_api_mock.steps import (
    EmitStep,
    GateStep,
    InvokeToolStep,
    Script,
    SleepStep,
)
from warden.harness_api_mock.tool_seam import (
    ToolInvoker,
    build_tool_invoker,
)

logger = logging.getLogger(__name__)

_TERMINAL_TYPES = {"result", "error", "stopped"}


class _GateDenied(Exception):
    """Internal control-flow marker: a denied gate produced the terminal already,
    so the step-loop should stop without emitting a second terminal."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class _Pending:
    """A paused gate awaiting a decision (§6)."""

    tool_use_id: str
    tool_name: str
    resume: asyncio.Event = field(default_factory=asyncio.Event)
    decision: str | None = None  # "allow" | "deny" once resolved
    reason: str = ""
    sla_task: asyncio.Task | None = None


@dataclass
class _RunState:
    run_id: str
    user_id: str
    task_id: str
    workflow: str
    status: str = "queued"
    session_id: str | None = None
    last_seq: int = 0
    usage: dict = field(default_factory=dict)
    cost_usd: float = 0.0
    error: str | None = None
    task: asyncio.Task | None = None
    pending: _Pending | None = None
    # idempotency: (tool_use_id) -> recorded decision
    resolved: dict[str, str] = field(default_factory=dict)


class MockRunner:
    """Run registry + per-run script-player background task."""

    def __init__(
        self,
        config: MockConfig | None = None,
        *,
        tool_invoker: ToolInvoker | None = None,
        event_log: RunEventLog | None = None,
        webhook_client=None,
    ) -> None:
        self._cfg = config or MockConfig()
        # Load the active product profile (task-14): its scripts, fixtures, and
        # writeback-invoker factory. Product-free at import; fail-loud on unknown name.
        self._profile = load_profile(self._cfg.profile)
        self._tools = tool_invoker or self._default_invoker()
        self._runs: dict[str, _RunState] = {}
        self._sse: dict[str, SseEgress] = {}
        self._webhook_client = webhook_client
        # Fixtures come from the profile unless the config overrides the dir explicitly.
        fixture_dir = self._cfg.fixture_dir or str(self._profile.fixture_dir)
        self._files = FixtureStore(self._cfg.workspace_root, fixture_dir)
        log_path = Path(self._cfg.event_log_dir) / "mock_run_events.db"
        self._event_log = event_log or MockRunEventLog(log_path)
        self._event_log_ready = False
        self._event_log_lock = asyncio.Lock()

    def _default_invoker(self) -> ToolInvoker:
        """Build the invoker for the configured mode. ``noop`` → the engine's canned
        invoker; ``profile`` → the active profile's real writeback bridge, which needs
        the run registry (to resolve ``run_id -> task_id``, D7) and the product API
        config. The profile owns the product import (D6: the engine's module graph
        never statically pulls in product code)."""
        if self._cfg.tool_invoker_mode == "profile":
            return self._profile.build_invoker(
                self._cfg, lambda rid: self._runs[rid].task_id
            )
        return build_tool_invoker(self._cfg.tool_invoker_mode)

    def _script_for(self, workflow: str) -> Script:
        """Resolve the active profile's script for ``workflow`` (falling back to its
        declared ``default``). The engine carries no scripts of its own (task-14)."""
        scripts = self._profile.scripts
        return scripts.get(workflow, scripts["default"])

    # --- public API (matches real Runner) --------------------------------

    def submit(self, spec: RunSpec) -> str:
        """Register a run, seed its workspace, spawn the script task, return run_id."""
        run_id = str(uuid.uuid4())
        workflow = str(spec.input.get("workflow") or "default")
        state = _RunState(
            run_id=run_id,
            user_id=spec.user_id,
            task_id=spec.task_id,
            workflow=workflow,
        )
        self._runs[run_id] = state
        # Seed fixtures so GET /file serves real bytes once the run reaches result.
        self._files.seed(run_id, workflow)
        egress = self._build_egress(run_id, spec.sink)
        state.task = asyncio.create_task(self._play(run_id, spec, egress))
        return run_id

    def get(self, run_id: str) -> RunView | None:
        state = self._runs.get(run_id)
        if state is None:
            return None
        return RunView(
            run_id=state.run_id,
            status=state.status,  # type: ignore[arg-type]
            session_id=state.session_id,
            last_seq=state.last_seq,
            usage=state.usage,
            cost_usd=state.cost_usd,
            error=state.error,
        )

    async def init(self) -> None:
        await self._ensure_event_log()

    def sse_for(self, run_id: str) -> SseEgress | None:
        return self._sse.get(run_id)

    async def replay(self, run_id: str, after_seq: int = 0) -> list[Event]:
        await self._ensure_event_log()
        return await self._event_log.replay(run_id, after_seq)

    def task_for(self, run_id: str) -> asyncio.Task | None:
        state = self._runs.get(run_id)
        return state.task if state else None

    async def cancel(self, run_id: str) -> bool:
        state = self._runs.get(run_id)
        if state is None or state.task is None or state.task.done():
            return False
        state.task.cancel()
        return True

    async def confirm(
        self, run_id: str, tool_use_id: str, *, allow: bool, reason: str = ""
    ) -> dict | None:
        """Resolve a paused gate (§6). Idempotent on ``(run_id, tool_use_id)``.

        Returns a status dict (``resumed`` / ``already_resolved`` / ``not_pending``)
        or ``None`` for an unknown run (→ 404).
        """
        state = self._runs.get(run_id)
        if state is None:
            return None
        decision = "allow" if allow else "deny"
        # Idempotent: a duplicate returns the recorded decision, no re-run.
        if tool_use_id in state.resolved:
            return {
                "run_id": run_id,
                "tool_use_id": tool_use_id,
                "status": "already_resolved",
                "decision": state.resolved[tool_use_id],
            }
        pending = state.pending
        if pending is None or pending.tool_use_id != tool_use_id:
            return {
                "run_id": run_id,
                "tool_use_id": tool_use_id,
                "status": "not_pending",
                "decision": decision,
            }
        pending.decision = decision
        pending.reason = reason
        state.resolved[tool_use_id] = decision
        pending.resume.set()  # wake the paused script; it emits permission_resolved
        return {
            "run_id": run_id,
            "tool_use_id": tool_use_id,
            "status": "resumed",
            "decision": decision,
        }

    def read_file(self, run_id: str, rel: str) -> bytes:
        """Serve guarded fixture bytes for a run (§8). Raises ``KeyError`` for an
        unknown run, ``PathGuardError`` (→400) / ``FileMissingError`` (→404)."""
        if run_id not in self._runs:
            raise KeyError(run_id)
        return self._files.read(run_id, rel)

    async def aclose(self) -> None:
        tasks = []
        for state in self._runs.values():
            if state.task and not state.task.done():
                state.task.cancel()
                tasks.append(state.task)
            if state.pending and state.pending.sla_task:
                state.pending.sla_task.cancel()
        # Await the cancelled runs so their terminal-error emit finishes BEFORE the
        # durable log closes — otherwise a lagging emit hits a closed DB (LAW 4:
        # a shutdown must not drop or error on an in-flight event).
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._event_log.close()

    # --- internals -------------------------------------------------------

    async def _ensure_event_log(self) -> None:
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

    async def _emit(
        self,
        state: _RunState,
        egress: EgressAdapter,
        etype: str,
        data: dict | None = None,
        *,
        session_id: str | None = None,
    ) -> None:
        """Assign the next per-run seq, mirror to the durable log, then egress."""
        state.last_seq += 1
        event = Event(
            run_id=state.run_id,
            seq=state.last_seq,
            type=etype,  # type: ignore[arg-type]
            session_id=session_id or state.session_id,
            data=data or {},
            at=_now(),
        )
        await self._event_log.append(event)  # durable-first (LAW 4: never dropped)
        await egress.emit(event)

    def _session_id(self, spec: RunSpec, run_id: str) -> tuple[str, bool]:
        """Resolve session_id + resumed flag (§7).

        A resume passes the prior id back verbatim. A NEW session (session_id=None)
        mints a fresh id keyed on the unique ``run_id`` — so two independent runs on
        the SAME task_id (e.g. a Q&A and a Notes-refine on one course) get DISTINCT
        sessions, matching the real harness. (Was keyed on task_id, which collapsed
        them into one session — task-13.)"""
        if spec.session_id:
            return spec.session_id, True
        label = str(spec.input.get("label") or "main")
        return f"sess-{run_id}-{label}", False

    async def _play(
        self, run_id: str, spec: RunSpec, egress: EgressAdapter
    ) -> None:
        state = self._runs[run_id]
        try:
            await self._ensure_event_log()
            state.status = "running"
            session_id, resumed = self._session_id(spec, run_id)
            state.session_id = session_id
            ctx = {
                "spec": spec,
                "run_id": run_id,
                "session_id": session_id,
                "resumed": resumed,
                # The seeded-fixture manifest ({path,title,order}) the completion
                # tool posts as its real {files} payload (D8); paths resolve back
                # through GET /file. Empty for scripts with no artifacts (qa).
                "manifest": self._files.manifest(run_id),
            }
            script = self._script_for(state.workflow)
            try:
                emitted_terminal = await self._run_steps(state, egress, script, ctx)
            except _GateDenied:
                emitted_terminal = True  # a denied gate emitted the terminal already
            if not emitted_terminal:
                # A script with no explicit terminal still needs exactly one.
                state.status = "succeeded"
                await self._emit(state, egress, "result", {"result": "", "usage": {}, "cost_usd": 0.0})
        except asyncio.CancelledError:
            state.status = "cancelled"
            state.error = "cancelled"
            await self._emit(state, egress, "error", {"reason": "cancelled"})
        except Exception as exc:  # noqa: BLE001 - surfaced as a terminal error (LAW 4)
            logger.exception("mock run %s failed", run_id)
            state.status = "error"
            state.error = str(exc)
            await self._emit(state, egress, "error", {"reason": str(exc)})
        finally:
            self._cancel_sla(state)
            await egress.aclose()

    async def _run_steps(
        self, state: _RunState, egress: EgressAdapter, script, ctx
    ) -> bool:
        """Play the steps. Returns True once a terminal event has been emitted."""
        for step_index, step in enumerate(script, start=1):
            # Fault injection: emit an error at step N instead of the step.
            if self._cfg.inject_error_at == step_index:
                state.status = "error"
                state.error = "injected"
                await self._emit(state, egress, "error", {"reason": "injected"})
                return True

            if isinstance(step, SleepStep):
                delay = step.seconds * self._cfg.step_delay_s
                if delay > 0:
                    await asyncio.sleep(delay)
                continue

            if isinstance(step, EmitStep):
                data = step.data_fn(ctx)
                if step.type == "session":
                    await self._emit(
                        state, egress, "session", data,
                        session_id=state.session_id,
                    )
                elif step.type == "result":
                    # budget-stop fault: replace the result with a stopped terminal.
                    if self._cfg.budget_stop:
                        state.status = "stopped"
                        await self._emit(
                            state, egress, "stopped",
                            {"reason": "budget", "requires_action": "topup"},
                        )
                        return True
                    state.usage = data.get("usage", {})
                    state.cost_usd = float(data.get("cost_usd", 0.0))
                    state.status = "succeeded"
                    await self._emit(state, egress, "result", data)
                    return True
                else:
                    await self._emit(state, egress, step.type, data)
                continue

            if isinstance(step, InvokeToolStep):
                args = step.args_fn(ctx)
                await self._tools.invoke(state.run_id, step.tool, args)
                continue

            if isinstance(step, GateStep):
                await self._run_gate(state, egress, step, ctx)
                continue

            raise TypeError(f"unknown step type: {type(step)!r}")
        return False

    async def _run_gate(
        self, state: _RunState, egress: EgressAdapter, step: GateStep, ctx
    ) -> None:
        """The durable-HITL gate (§6): pause, wait for confirm or SLA auto-deny."""
        concepts = step.concepts_fn(ctx)
        # N1: invoke the seam so the product's /confirm surfaces the gate row.
        await self._tools.confirm_landscape(state.run_id, concepts)

        tool_use_id = f"tu-{uuid.uuid4().hex[:12]}"
        pending = _Pending(tool_use_id=tool_use_id, tool_name=step.tool_name)
        state.pending = pending

        # Emit permission_request on sink + durable log; status → requires_action.
        # (permission_request is NOT terminal → SSE stays open across the pause.)
        state.status = "requires_action"
        await self._emit(
            state, egress, PERMISSION_REQUEST,
            {
                "tool_use_id": tool_use_id,
                "tool_name": step.tool_name,
                "tool_input": {"concepts": concepts},
                "reason": step.reason,
            },
        )

        # Arm the SLA timer: fires first → auto-resolve DENY, resume.
        pending.sla_task = asyncio.create_task(self._sla_timer(state, pending))
        try:
            await pending.resume.wait()
        finally:
            self._cancel_sla(state)

        decision = pending.decision or "deny"
        # Emit permission_resolved; back to running.
        state.status = "running"
        await self._emit(
            state, egress, PERMISSION_RESOLVED,
            {
                "tool_use_id": tool_use_id,
                "decision": decision,
                "reason": pending.reason,
            },
        )
        state.pending = None

        if decision == "deny":
            # The mock can't re-plan like the real model → terminal result{denied}.
            state.status = "succeeded"
            await self._emit(
                state, egress, "result",
                {"result": "", "denied": True, "reason": pending.reason,
                 "usage": {}, "cost_usd": 0.0},
            )
            # Signal the step-loop to stop by raising a controlled terminal marker.
            raise _GateDenied()

    async def _sla_timer(self, state: _RunState, pending: _Pending) -> None:
        """Auto-resolve DENY if no decision arrives within the SLA window."""
        try:
            await asyncio.sleep(self._cfg.sla_seconds)
        except asyncio.CancelledError:
            return
        if pending.decision is None and state.pending is pending:
            pending.decision = "deny"
            pending.reason = "SLA timeout: no decision within the confirmation window"
            state.resolved[pending.tool_use_id] = "deny"
            pending.resume.set()

    def _cancel_sla(self, state: _RunState) -> None:
        if state.pending and state.pending.sla_task:
            state.pending.sla_task.cancel()
            state.pending.sla_task = None


__all__ = [
    "MockRunner",
    "FileMissingError",
    "PathGuardError",
]
