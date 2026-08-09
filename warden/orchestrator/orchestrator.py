"""Interface-agnostic LLM orchestration.

Manages sessions, providers, permissions, and prompt assembly.
Transport layers delegate to this as their core engine.
"""

import asyncio
import logging
import shutil
import time
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

from warden.orchestrator.stream_runtime import (
    assemble_turn_provider_kwargs,
    compute_turn_deny_list,
    finalize_jsonl_path,
    get_message_handler,
    prepare_image_prompt,
    prepare_persisted_turn,
    resolve_turn_session,
    snapshot_turn,
)
from warden.schemas.events import (
    CompletionEvent,
    ErrorEvent,
    OrchestratorEvent,
    SessionCreatedEvent,
)
from warden.schemas.usage import Usage
from warden.seams.custom_tools import CustomTool
from warden.seams.governor import Governor
from warden.orchestrator.governor_surface import (
    ClockWatchdog,
    checkpoint_stop,
    process_provider_message,
)
from warden.orchestrator.output_pass import drain_with_output_pass
from warden.seams.middleware import (
    RejectResult,
    SendContext,
)
from warden.seams.permissions import (
    AutoAllowHandler,
    PermissionHandler,
)
from warden.schemas.tool_scope import ToolScope
from warden.orchestrator.permission_surface import (
    WorkflowMismatchError,
    build_permission_checker,
    evaluate_tool_permission,
    read_stored_workflow,
)
from warden.orchestrator.session.manager import SessionManager
from warden.workspace.workflow.loader import compute_deny_baseline
from warden.persistence import PersistenceConfig

logger = logging.getLogger(__name__)


def _extract_tool_use_id(context: Any) -> str | None:
    """Pull a pending call's stable id out of a provider's permission context
    (pre-07b / 3a). Providers deliver it differently:

    - Claude native: ``ToolPermissionContext.tool_use_id`` (attr).
    - dict-shaped bridges: ``tool_use_id`` or Codex ``item_id`` key.

    Returns ``None`` when the context carries no id (the honest un-identified
    case). Never raises — an unknown context shape just yields ``None``.
    """
    if context is None:
        return None
    tuid = getattr(context, "tool_use_id", None)
    if tuid:
        return tuid
    if isinstance(context, dict):
        return context.get("tool_use_id") or context.get("item_id")
    return None


class Orchestrator:
    """Interface-agnostic LLM orchestration.

    Manages sessions, providers, permissions, and prompt assembly.
    Transport layers (e.g. WS adapter, CLI, Python API) delegate here.

    The ``permission_handler`` parameter controls how permission prompts
    reach the user: ``AutoAllowHandler`` for CLI/scripts,
    ``WebSocketPermissionHandler`` for browser, etc.
    """

    def __init__(
        self,
        session_manager: SessionManager,
        repo_path: Path,
        permission_handler: PermissionHandler | None = None,
        workflow: str | None = None,
        tool_scope: ToolScope | None = None,
        governor: Governor | None = None,
        clock_tick_interval_s: float = 1.0,
        custom_tools: list[CustomTool] | None = None,
        middleware: list | None = None,
        output_middleware: list | None = None,
        system_prompt: str | None = None,
        persist_cfg: PersistenceConfig | None = None,
        persist_backend: Any = None,
        user_id: str = "default",
        task_id: str | None = None,
        auth_env: dict[str, str] | None = None,
        codex_allow_ungated_custom_tools: bool = False,
        provider_config: Any = None,
        telemetry: Any = None,
        audit: Any = None,
        safety_hooks: Any = None,
        durable_defer: Any = None,
        continuation: Any = None,
    ) -> None:
        self._session_manager = session_manager
        self._repo_path = repo_path
        self._permission_handler = permission_handler or AutoAllowHandler()
        # Workflow is INIT-BOUND session identity (D1 / SESS-1) — surface derived
        # once, never re-pointed mid-session.
        self._workflow_name = workflow
        self._tool_scope = tool_scope
        # Governor seam (B17) — optional; None ⇒ ungoverned (GOV-2). Threaded into
        # the turn loop at each checkpoint; the engine obeys continue|stop(reason)
        # and never sees a dollar (GOV-1). Init-bound like the permission surface.
        self._governor = governor
        self._clock_tick_interval_s = clock_tick_interval_s
        self._custom_tools = custom_tools or []
        # Opt-in threaded to the codex adapter as a provider kwarg (ungated MCP
        # custom-tool delivery). Absorbed by other providers via **kwargs — but we
        # only inject it for codex to avoid unknown-kwarg rejection elsewhere.
        self._codex_allow_ungated_custom_tools = codex_allow_ungated_custom_tools
        # C7 — openharness ProviderConfig slice, threaded to its session ctor.
        self._provider_config = provider_config
        # M3 4a — TelemetryConfig slice, threaded to the claude/openharness tracers.
        self._telemetry = telemetry
        # M5 3a-1 — AuditConfig slice, threaded to claude/openharness/codex sessions
        # (claude gates its hooks on it; OpenHarness is a later rung).
        self._audit = audit
        # SAFE-6 (M4 3e-2) — PathHookConfig slice, claude-only path hook.
        self._safety_hooks = safety_hooks
        # pre-07b durable — DurableDeferConfig slice, claude-only native-defer hook.
        self._durable_defer = durable_defer
        # B1 — ContinuationConfig slice, claude-only top-level Stop continuation hook.
        self._continuation = continuation
        self._middleware = middleware or []
        self._output_middleware = output_middleware or []  # M4 3b-2 (SAFE-1)
        self._system_prompt = system_prompt

        # Persistence is ACTIVE only when both cfg and task_id are set.
        self._persist_cfg = persist_cfg
        self._persist_backend = persist_backend
        self._user_id = user_id
        self._task_id = task_id
        # Per-run managed key (as subprocess auth env vars). Threaded into the
        # provider kwargs each turn so a subprocess-isolated provider (claude-cli)
        # uses this run's key instead of blind-inheriting os.environ. None =>
        # inherit the parent credential (unchanged single-key behavior).
        self._auth_env = auth_env

        self._persist_active = bool(persist_cfg and task_id)

        self._current_session_id: str | None = None
        self._current_provider: str = "claude"
        self._current_model: str | None = None
        self._active_tool_scope: ToolScope | None = tool_scope
        self._stream_task: asyncio.Task | None = None
        # Permission surface built ONCE at session creation (D1 / SESS-1):
        # deny-baseline + workflow checker fixed here, no mid-session re-derive.
        self._deny_baseline = compute_deny_baseline(repo_path)
        self._permission_checker = build_permission_checker(repo_path, workflow)
        self._tool_event_queue: asyncio.Queue[OrchestratorEvent] | None = None

    # ------------------------------------------------------------------
    # Permission callback (uses PermissionHandler protocol)
    # ------------------------------------------------------------------

    async def _can_use_tool(
        self, tool_name: str, tool_input: dict, context: Any,
    ) -> PermissionResultAllow | PermissionResultDeny:
        # Thin binding to the live per-turn state; logic lives in one place
        # (permission_surface.evaluate_tool_permission).
        #
        # pre-07b / 3a — forward the pending call's id (was DROPPED here). The
        # provider passes it via ``context``: Claude native
        # ``ToolPermissionContext.tool_use_id``; the Claude custom-tool gate and
        # other providers pass a context carrying the same attr (or ``None``).
        # A dict context (some bridges) is also supported via ``item_id``.
        tool_use_id = _extract_tool_use_id(context)
        return await evaluate_tool_permission(
            tool_name, tool_input,
            tool_scope=self._active_tool_scope,
            permission_handler=self._permission_handler,
            permission_checker=self._permission_checker,
            tool_event_queue=self._tool_event_queue,
            tool_use_id=tool_use_id,
        )

    # ------------------------------------------------------------------
    # send_message — the core orchestration async generator
    # ------------------------------------------------------------------

    async def send_message(
        self,
        content: str,
        *,
        provider: str = "claude",
        model: str | None = None,
        session_id: str | None = None,
        workflow: str | None = None,
        tool_scope: ToolScope | None = None,
        images: list[dict] | None = None,
    ) -> AsyncGenerator[OrchestratorEvent, None]:
        """Send a message and yield response events.

        Handles the full lifecycle: provider/model changes, 3-way session
        lookup, image injection, and streaming. The workflow is INIT-BOUND
        (SESS-1) — a per-send ``workflow`` may only RE-STATE the bound one;
        naming a different one is a new-session act and is rejected here.
        """
        # --- Workflow is init-bound (SESS-1 / D1): reject a re-point ---
        # None re-states the bound surface (no-op); a DIFFERENT name is a
        # new-session act, never a live swap.
        if workflow is not None and workflow != self._workflow_name:
            raise WorkflowMismatchError(
                f"Session is bound to workflow {self._workflow_name!r}; refusing "
                f"to switch to {workflow!r} mid-session — start a new session."
            )

        # --- Incoming DB-resume: adopt the session's persisted workflow (N9) ---
        # A client id not live in memory resolves via the DB-resume branch
        # below; rebuild the surface from its stored workflow FIRST so this turn
        # (and the middleware ctx) sees the resumed policy, not the transient one.
        if session_id and self._session_manager.get(session_id) is None:
            await self._adopt_stored_workflow(session_id)

        # --- Provider change detection (different provider = must create new session) ---
        if provider != self._current_provider:
            self._current_provider = provider
            if self._current_session_id:
                await self._session_manager.close(self._current_session_id)
                self._current_session_id = None

        # --- Model change detection ---
        if model == "default":
            model = None
        if model != self._current_model:
            self._current_model = model
            if self._current_session_id:
                await self._session_manager.close(self._current_session_id)
                self._current_session_id = None

        # --- Middleware pipeline ---
        processed = content
        if self._middleware:
            ctx = SendContext(
                workflow=self._workflow_name,
                session_id=self._current_session_id,
                provider=provider,
                model=model,
            )
            for mw in self._middleware:
                result = await mw.before_send(processed, ctx)
                if isinstance(result, RejectResult):
                    yield ErrorEvent(
                        text=result.reason,
                        session_id=self._current_session_id or "",
                    )
                    return
                processed = result

        prompt = processed

        # --- Per-turn tool scope (PERM-2 / D2): a per-send tool_scope narrows
        # THIS turn (absent → ctor default) as a ToolScope-stage INPUT. A changed
        # scope needs a NEW session (SDK disallowed_tools is set-once at create).
        active_scope = tool_scope if tool_scope is not None else self._tool_scope
        if active_scope != self._active_tool_scope:
            self._active_tool_scope = active_scope
            if self._current_session_id:
                await self._session_manager.close(self._current_session_id)
                self._current_session_id = None

        # --- Image handling ---
        prompt, temp_image_dir = prepare_image_prompt(prompt, images)

        logger.info(
            "[prompt] provider=%s model=%s | final prompt:\n%s",
            self._current_provider, self._current_model,
            prompt[:500],
        )

        # --- Compute deny list (baseline + tool scope) ---
        deny_list = compute_turn_deny_list(self._deny_baseline, self._active_tool_scope)

        # --- Cancel in-flight stream ---
        if self._stream_task and not self._stream_task.done():
            self._stream_task.cancel()
            if self._current_session_id:
                old = self._session_manager.get(self._current_session_id)
                if old:
                    await old.stop()

        # --- Guarded restore (persistence active): rebuild/no-op before session use ---
        turn_repo_path = self._repo_path
        turn_provider_kwargs: dict = {}
        if self._persist_active:
            turn_repo_path, turn_provider_kwargs = await prepare_persisted_turn(
                self._persist_cfg,
                self._persist_backend,
                self._user_id,
                self._task_id,
                self._current_provider,
            )
            self._repo_path = turn_repo_path  # keep in sync for register/workspace_path

        # --- Per-turn provider kwargs: managed key (B8) + codex opt-in --------
        turn_provider_kwargs = assemble_turn_provider_kwargs(
            turn_provider_kwargs,
            self._current_provider,
            auth_env=self._auth_env,
            codex_allow_ungated=self._codex_allow_ungated_custom_tools,
            provider_config=self._provider_config,
            telemetry=self._telemetry,
            audit=self._audit,
            safety_hooks=self._safety_hooks,
            durable_defer=self._durable_defer,
            permission_checker=self._permission_checker,
            continuation=self._continuation,
        )

        # --- 3-way session lookup: client ID → orchestrator current → DB resume → create ---
        session, is_resumed, self._current_session_id, resumed_event = (
            await resolve_turn_session(
                session_manager=self._session_manager,
                session_id=session_id,
                current_session_id=self._current_session_id,
                provider=self._current_provider,
                model=self._current_model,
                can_use_tool=self._can_use_tool,
                disallowed_tools=deny_list,
                system_prompt=self._system_prompt,
                custom_tools=self._custom_tools,
                repo_path=turn_repo_path,
                provider_kwargs=turn_provider_kwargs,
            )
        )
        if resumed_event is not None:
            yield resumed_event

        # --- Stream response via queue (decouples provider pace from consumer) ---
        queue: asyncio.Queue[OrchestratorEvent | None] = asyncio.Queue()
        self._tool_event_queue = queue
        message_handler = get_message_handler(self._current_provider)

        async def _run_stream() -> None:
            nonlocal session
            sid = session.session_id
            registered = is_resumed or (
                sid is not None
                and self._session_manager.get(sid) is not None
            )
            run_started = time.monotonic()
            watchdog: ClockWatchdog | None = None
            try:
                # --- pre-flight Governor checkpoint (B17): stop here bounds the
                # run before the provider's send() is ever iterated (3d's
                # reservation reject → no first run is unbounded).
                pre = await checkpoint_stop(
                    self._governor, "pre_flight", Usage(), 0.0, sid,
                )
                if pre is not None:
                    await queue.put(pre)
                    return
                # --- arm the wall-clock time bound (B18). No-op without a
                # Governor; on a clock_tick stop it cooperatively interrupts the
                # session and records the StoppedEvent for the post-loop check.
                watchdog = ClockWatchdog(
                    self._governor, session, run_started,
                    self._clock_tick_interval_s,
                ).start()
                async for sdk_msg in session.send(prompt):
                    # Session ID is unknown until the SDK sends its first message
                    if not registered and session.session_id:
                        sid = session.session_id
                        self._current_session_id = sid
                        registered = True
                        await self._session_manager.register(
                            session,
                            provider=self._current_provider,
                            workspace_path=str(Path(self._repo_path).resolve()),
                            workflow=self._workflow_name,
                        )
                        await queue.put(SessionCreatedEvent(session_id=sid))

                    # --- transform + forward the message, running the mid_stream
                    # and turn_boundary Governor checkpoints (B17/B20a). A returned
                    # StoppedEvent halts the loop (cooperative — the session is
                    # stopped; the turn's output already shipped).
                    stop_ev = await process_provider_message(
                        sdk_msg,
                        message_handler=message_handler,
                        tag_sid=sid or "",
                        queue=queue,
                        governor=self._governor,
                        run_started=run_started,
                        sid=sid,
                    )
                    if stop_ev is not None:
                        await queue.put(stop_ev)
                        await session.stop()
                        return

                # --- deadline breach mid-stream (B18): the watchdog interrupted
                # the turn cooperatively; emit its StoppedEvent, not a completion.
                if watchdog.pending is not None:
                    await queue.put(watchdog.pending)
                    return

                # JSONL discovery + persistence
                await finalize_jsonl_path(
                    session, sid, self._session_manager._index,
                )

                await queue.put(CompletionEvent(session_id=sid or ""))
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("Stream error for session %s", sid)
                await queue.put(ErrorEvent(
                    text="Stream error", session_id=sid or "",
                ))
            finally:
                if watchdog is not None:
                    await watchdog.aclose()
                if temp_image_dir:
                    shutil.rmtree(temp_image_dir, ignore_errors=True)
                await queue.put(None)  # sentinel

        self._stream_task = asyncio.create_task(_run_stream())

        # Drain + OUTPUT pass (M4 3b-2 / SAFE-1); empty pipeline ⇒ pass-through.
        async for event in drain_with_output_pass(queue, self._output_middleware, self):
            yield event
        self._tool_event_queue = None

        # --- Snapshot after the turn's workspace files are written (persistence) ---
        # NOTE: this runs even when the turn's stream errored — intentional
        # (latest-overwrites; don't lose partial work). A snapshot does NOT
        # imply the turn succeeded.
        if self._persist_active:
            snapshot_error = await snapshot_turn(
                self._persist_cfg,
                self._persist_backend,
                self._user_id,
                self._task_id,
            )
            if snapshot_error:
                yield ErrorEvent(
                    text=snapshot_error,
                    session_id=self._current_session_id or "",
                )

    # ------------------------------------------------------------------
    # resume_session
    # ------------------------------------------------------------------

    async def resume_session(self, session_id: str) -> str:
        """Resume a previous session. Returns the session ID."""
        # SESS-2 / N9: rebuild the surface from the persisted workflow, never
        # from this orchestrator's transient in-memory state.
        await self._adopt_stored_workflow(session_id)
        new_sid, _session = await self._session_manager.resume(
            session_id=session_id,
            repo_path=self._repo_path,
            can_use_tool=self._can_use_tool,
            provider=self._current_provider,
            model=self._current_model,
            disallowed_tools=self._deny_baseline,
            system_prompt=self._system_prompt,
            custom_tools=self._custom_tools or None,
        )
        self._current_session_id = new_sid
        return new_sid

    async def _adopt_stored_workflow(self, session_id: str) -> None:
        """Rebuild the bound workflow + checker from the persisted ``workflow``
        column (SESS-2 / N9) so a resume reconstitutes the exact surface from
        durable storage. No-op when no DB row exists (init-bound surface kept).
        """
        found, stored_workflow = await read_stored_workflow(
            self._session_manager, session_id,
        )
        if not found:
            return
        self._workflow_name = stored_workflow
        self._permission_checker = build_permission_checker(
            self._repo_path, stored_workflow,
        )

    # ------------------------------------------------------------------
    # abort
    # ------------------------------------------------------------------

    async def abort(self) -> None:
        """Cancel the in-flight stream and stop the current session."""
        if self._stream_task and not self._stream_task.done():
            self._stream_task.cancel()
        if self._current_session_id:
            session = self._session_manager.get(self._current_session_id)
            if session:
                await session.stop()

    # ------------------------------------------------------------------
    # check_session_status
    # ------------------------------------------------------------------

    def check_session_status(self, session_id: str) -> bool:
        """Check if a session is active. Updates internal tracking."""
        is_active = bool(session_id and self._session_manager.get(session_id))
        if is_active:
            self._current_session_id = session_id
        return is_active

    # ------------------------------------------------------------------
    # close
    # ------------------------------------------------------------------

    async def close(self) -> None:
        """Close current session if any."""
        if self._stream_task and not self._stream_task.done():
            self._stream_task.cancel()
        if self._current_session_id:
            await self._session_manager.close(self._current_session_id)
            self._current_session_id = None
