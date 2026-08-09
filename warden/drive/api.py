"""ChatAPI — Python-native interface to the orchestration layer.

Driven by one typed :class:`HarnessConfig`: the 18 constructor knobs of the old
signature collapsed into the config object (a clean break — see
``docs/config-plan.md``). ``config/build.py`` turns the declarative config into
the runtime seam objects the orchestrator wires; ``repo_path`` stays a separate
argument because it is the per-instance working directory (and is replaced by the
restored task dir when persistence is active).
"""

from collections.abc import AsyncGenerator
from pathlib import Path

from warden.config.build import (
    apply_workflow_event_map,
    apply_workflow_middleware,
    build_middleware,
    build_permission_handler,
    build_persistence,
    build_tool_scope,
    resolve_model,
)
from warden.config.models import HarnessConfig
from warden.orchestrator.orchestrator import Orchestrator
from warden.safety.middleware.input.canary import (
    DEFAULT_CANARY,
    plant_canary,
)
from warden.safety.middleware.output.middleware import (
    CanaryOutputMiddleware,
)
from warden.orchestrator.session.db import SessionDB
from warden.orchestrator.session.index import SessionIndex
from warden.orchestrator.session.manager import SessionManager
from warden.schemas.events import OrchestratorEvent
from warden.workspace import ensure_restored
from warden.workspace.workflow.loader import load_workflow


class ChatAPI:
    """Python-native interface to the orchestration layer.

    Usage::

        from warden.config import get_harness_config

        api = ChatAPI(get_harness_config(), repo_path="/path/to/workspace")
        await api.init()

        async for event in api.send("Explain chapter 3"):
            if isinstance(event, MessageEvent):
                print(event.content.get("text", ""), end="")

        await api.close()

    The app holds the whole ``HarnessConfig``; each module below receives only
    its own slice (built here via ``config/build.py``).
    """

    def __init__(
        self,
        config: HarnessConfig,
        *,
        repo_path: str | Path,
        workflow: str | None = None,
        session_manager: "SessionManager | None" = None,
    ) -> None:
        self._config = config
        self._repo_path = Path(repo_path)
        # Workflow is INIT-BOUND session identity (SESS-1): fixed at construction,
        # threaded into the Orchestrator. ``send(workflow=...)`` may only re-state
        # it — a different name raises WorkflowMismatchError.
        self._workflow = workflow

        # Effective provider/model for send() (model default resolved per provider).
        self._provider = config.provider.provider
        self._model = resolve_model(config.provider)

        # Runtime seam objects, built from the declarative config.
        self._permission_handler = build_permission_handler(config.permissions)
        self._tool_scope = build_tool_scope(config.permissions)
        # SAFE-5: the workflow's safety policy TRAVELS WITH THE MANIFEST — merge
        # its declared middleware into a LOCAL effective config (never mutate the
        # passed-in config) before building the pipeline. Fail-soft on a MISSING
        # workflow (None → no-op merge); a present-but-broken one raises
        # WorkflowLoadError here (fail-closed, exactly like permissions).
        wf = (
            load_workflow(self._repo_path, self._workflow)
            if self._workflow
            else None
        )
        effective_middleware = apply_workflow_middleware(
            config.middleware, wf.middleware if wf else None
        )
        input_middleware, output_middleware = build_middleware(
            effective_middleware, config.safety
        )
        self._middleware = input_middleware
        # M4 3b-2 (SAFE-1): the output pass is threaded into the orchestrator so
        # BOTH drive paths inherit it. Empty unless enable_output_middleware is on.
        self._output_middleware = output_middleware
        # EXT-P1/A2 (E4): the workflow's event_tool_map travels with the manifest
        # (same discipline as middleware/permissions) — merged into the effective
        # custom-tools config so any consumer reads one resolved map.
        self._custom_tools_config = apply_workflow_event_map(
            config.custom_tools, wf.event_tool_map if wf else None
        )
        self._custom_tools = self._custom_tools_config.tools
        self._system_prompt = config.safety.system_prompt

        # SAFE-4: canary backstop. Opt-in and INDEPENDENT of the output-middleware
        # master switch — it rides the output pass whenever enabled. Plant a token
        # in the system prompt the provider sends, and append a per-chunk canary
        # checker so verbatim system-prompt leakage is CUT at egress. No-op when
        # disabled (system prompt + output list unchanged).
        if config.safety.enable_canary:
            token = config.safety.canary_token or DEFAULT_CANARY
            self._system_prompt = plant_canary(self._system_prompt, token)
            self._output_middleware = [
                *self._output_middleware,
                CanaryOutputMiddleware(token),
            ]

        # Per-run managed key as subprocess auth env vars (e.g. a per-user
        # ANTHROPIC_API_KEY). None => inherit the launching process credential.
        self._auth_env = config.auth.auth_env
        self._user_id = config.workspace.user_id
        self._task_id = config.workspace.task_id

        # EXT-C3: the multi-replica unit (the Runner, which owns the HarnessApiConfig
        # carrying the ``state.backend`` tier switch) may inject a shared-backend
        # ``SessionManager`` — e.g. a Postgres-backed one via ``SessionManager.from_config``
        # — so a fresh replica resumes sessions it did not create. When none is injected,
        # fall back to the local sqlite backend (a shared ``session_db_path`` still gives
        # the single-container / shared-volume behavior; None ⇒ per-process default).
        if session_manager is not None:
            self._session_manager = session_manager
        else:
            session_db_path = config.persistence.session_db_path
            if session_db_path is not None:
                self._session_manager = SessionManager(
                    index=SessionIndex(SessionDB(Path(session_db_path)))
                )
            else:
                self._session_manager = SessionManager()
        self._orchestrator: Orchestrator | None = None
        # Optional resource Governor (M2). None ⇒ ungoverned (unchanged behavior).
        # Set via set_governor() BEFORE init(); threaded into the Orchestrator there.
        self._governor = None

    def set_governor(self, governor) -> None:
        """Wire a per-run Governor into the orchestrator (call BEFORE ``init()``).

        ``governor`` implements the ``seams.governor.Governor.check`` contract. When
        left unset the run is ungoverned (GOV-2) — exactly today's behavior.
        """
        self._governor = governor

    def set_permission_handler(self, handler) -> None:
        """Override the permission handler for this run (call BEFORE ``init()``).

        M6: the durable HTTP transport builds a per-run ``PermissionHandler`` and
        injects it here — the config-declared ``durable_http`` kind resolves to a
        fail-closed placeholder at construction (``build_permission_handler``), which
        the Runner replaces before ``init()`` threads it into the orchestrator. Used
        for the OpenHarness/Codex durable eject (``DurableDeferHandler`` on
        ``can_use_tool``). Same lifecycle as ``set_governor``.
        """
        self._permission_handler = handler

    def set_durable_defer(self, durable_defer) -> None:
        """Wire the Claude native-defer durable config for this run (BEFORE ``init()``).

        M6 exact-id path: for Claude, the durable eject is the SDK-native
        ``permissionDecision:"defer"`` PreToolUse hook (``config.safety.durable_defer``)
        — a resume re-fires the hook for the SAME ``tool_use_id`` (exact-inject, no
        regeneration), unlike the OpenHarness/Codex re-drive. ``init()`` reads
        ``self._config.safety.durable_defer``, so the Runner sets the per-run store
        root here before init threads it into the orchestrator/Claude session.
        """
        self._config.safety.durable_defer = durable_defer

    def _require_init(self) -> Orchestrator:
        """Return the orchestrator or raise if init() hasn't been called."""
        if self._orchestrator is None:
            raise RuntimeError("Call init() first")
        return self._orchestrator

    async def _setup_persistence(self) -> dict:
        """Build persistence config/backend and restore the task folder.

        Returns the Orchestrator persistence kwargs. Empty dict when ``task_id``
        is not set (persistence off — behaves exactly as before). The backend
        selection (``local``/``s3``) lives in ``build_persistence``; swapping
        stores is a config change, not a code change here.
        """
        if self._task_id is None:
            return {}
        cfg, backend = build_persistence(
            self._config.persistence, self._config.workspace
        )
        td = await ensure_restored(cfg, backend, self._user_id, self._task_id)
        td.mkdir(parents=True, exist_ok=True)
        self._repo_path = td
        return {
            "persist_cfg": cfg,
            "persist_backend": backend,
            "user_id": self._user_id,
            "task_id": self._task_id,
        }

    async def init(self) -> None:
        """Initialize DB and create orchestrator."""
        await self._session_manager.init()
        persist_kwargs = await self._setup_persistence()
        self._orchestrator = Orchestrator(
            session_manager=self._session_manager,
            repo_path=self._repo_path,
            permission_handler=self._permission_handler,
            workflow=self._workflow,
            tool_scope=self._tool_scope,
            custom_tools=self._custom_tools,
            middleware=self._middleware,
            output_middleware=self._output_middleware,
            system_prompt=self._system_prompt,
            auth_env=self._auth_env,
            codex_allow_ungated_custom_tools=(
                self._config.provider.codex_allow_ungated_custom_tools
            ),
            provider_config=self._config.provider,
            telemetry=self._config.observability.telemetry,
            audit=self._config.observability.audit,
            safety_hooks=self._config.safety.path_hook,
            durable_defer=self._config.safety.durable_defer,
            continuation=self._config.continuation,
            governor=self._governor,
            **persist_kwargs,
        )

    async def send(
        self,
        content: str,
        *,
        session_id: str | None = None,
        workflow: str | None = None,
    ) -> AsyncGenerator[OrchestratorEvent, None]:
        """Send a message, yield events."""
        # Thin pass-through: maps friendly param names to orchestrator's internal API
        orchestrator = self._require_init()
        async for event in orchestrator.send_message(
            content=content,
            provider=self._provider,
            model=self._model,
            session_id=session_id,
            workflow=workflow,
        ):
            yield event

    async def resume(self, session_id: str) -> str:
        """Resume a previous session. Returns the session ID."""
        orchestrator = self._require_init()
        return await orchestrator.resume_session(session_id)

    async def list_sessions(self, workspace_path: str | None = None) -> list[dict]:
        """List sessions, newest first (C14 — the public entry point).

        ``workspace_path`` filters to one workspace; ``None`` lists across all
        workspaces. Callers (the CLI, a future UI) go through here instead of
        reaching the private ``_session_manager._index``.
        """
        return await self._session_manager._index.list_sessions(workspace_path)

    async def close(self) -> None:
        """Close the orchestrator and all sessions."""
        orchestrator = self._require_init()
        await orchestrator.close()
        await self._session_manager.close_all()
        await self._session_manager.close_index()
