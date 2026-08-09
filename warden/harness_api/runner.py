"""The concurrent execution engine behind the Runs API.

One ``POST /runs`` becomes one background asyncio task that:
  1. acquires the per-``(user, task)`` lock (serialize one run per key) then a global
     ``Semaphore(N)`` slot (bound concurrency across ``(user, task)`` pairs),
  2. picks the user's managed key and threads it as ``auth_env`` into one
     ``ChatAPI`` per run (subprocess-isolated via ``claude-cli``),
  3. adapts each ``OrchestratorEvent`` to a typed, ``seq``-stamped :class:`Event`
     and delivers it through the run's egress adapter,
  4. records usage/cost, then emits exactly one terminal ``result``/``error``.

The run registry is in-memory (ephemeral, per-process), but every emitted event is
ALSO mirrored to a durable append-only ``run_events`` log (C2) so a run's history
survives process teardown and a reconnecting consumer can replay from its last seq.
The workspace/session persist so resume still works.

The ``Runner`` class is composed from mixins (one per cohesive method group; see
the ``_runner_*`` modules); ``__init__`` here builds all shared state ONCE and the
MRO merges the mixins into the exact same runtime class. The stateless data +
helpers live in ``_run_state`` (imported below and re-exported so the historical
``harness_api.runner`` import surface is preserved).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import httpx

from warden.drive.api import ChatAPI
from warden.harness_api.config import HarnessApiConfig
from warden.harness_api.credentials.keys import KeyRegistry
from warden.harness_api.credentials.config import (
    build_auth_resolver,
    init_auth,
)
from warden.harness_api.governance.pricing import build_pricing
from warden.harness_api.governance import (
    GovernorService,
    build_governor_service,
    init_governance,
)
from warden.harness_api.governance.task_policy_store import (
    JsonlTaskPolicyStore,
)
from warden.harness_api.egress import SseEgress
from warden.harness_api.event_log import RunEventLog, build_event_log
from warden.harness_api.task_lock import build_task_lock
from warden.harness_api.run_registry import (
    RunRegistry,
    build_run_registry,
    init_run_registry,
)
from warden.harness_api.schemas import RunSpec

# --- preserved public import surface (moved to _run_state; re-exported here so the
# historical ``from warden.harness_api.runner import …`` sites keep working:
# event_log.py imports ``_event_log_path``; tests import ``_RunState`` + the
# ``_DURABLE_RESUME_*`` / ``_DUPLICATE_REVISE_REASON`` vocabulary). ------------------
from warden.harness_api._run_state import (  # noqa: F401
    _DUPLICATE_REVISE_REASON,
    _DURABLE_HTTP_UNSUPPORTED,
    _DURABLE_RESUME_CONTINUATION,
    _DURABLE_RESUME_DENIED,
    _DURABLE_RESUME_REVISE,
    _RunState,
    _durable_http_unsupported_reason,
    _event_log_path,
    _now,
)
from warden.harness_api._runner_exec import _ExecMixin
from warden.harness_api._runner_hitl import _HitlMixin
from warden.harness_api._runner_query import _QueryMixin
from warden.harness_api._runner_submit import _SubmitMixin

logger = logging.getLogger(__name__)

# Factory that builds the per-run agent. Overridable in tests (mock skill).
ChatApiFactory = Callable[[RunSpec, dict[str, str] | None], object]


def _default_factory(cfg: HarnessApiConfig) -> ChatApiFactory:
    """Build a real ``ChatAPI`` per run, threading the managed key as auth_env.

    Deep-copies the executor-wide engine config per run and overlays the
    ``RunSpec``'s per-run choices (provider/model/user/task) + the resolved
    managed key. The persistence/workspace knobs (base_dir, state_root,
    session_db, backend, s3) come straight from the shared engine config — this
    is what let ``RunnerConfig`` (which duplicated them) be deleted.

    EXT-C3: takes the full ``HarnessApiConfig`` (not just ``engine``) so, when the
    ``state.backend`` tier switch is ``postgres``, it injects a shared Postgres-backed
    ``SessionManager`` into the ChatAPI — the fleet resumes sessions across replicas.
    (local backend is fine for a single container; Postgres is required for a fleet.)
    """
    engine = cfg.engine

    def factory(spec: RunSpec, auth_env: dict[str, str] | None) -> ChatAPI:
        config = engine.model_copy(deep=True)
        config.provider.provider = spec.provider
        config.provider.model = spec.model
        config.auth.auth_env = auth_env
        config.workspace.user_id = spec.user_id
        config.workspace.task_id = spec.task_id
        session_manager = None
        if cfg.state.is_postgres:
            # Lazy import + build only on the shared-backend path (keeps the local
            # default free of the asyncpg-backed session store).
            from warden.orchestrator.session.manager import SessionManager

            session_manager = SessionManager.from_config(cfg)
        return ChatAPI(
            config,
            # base_dir; replaced by the restored task dir on init().
            repo_path=config.workspace.base_dir,
            # Workflow is init-bound session identity (SESS-1): part of the run
            # spec, threaded at construction — not a mutable per-send input.
            workflow=spec.input.get("workflow"),
            session_manager=session_manager,
        )

    return factory


class Runner(_SubmitMixin, _QueryMixin, _HitlMixin, _ExecMixin):
    """Owns the semaphore, per-task locks, run registry, and SSE buffers.

    The bodies of the ~30 methods live on the four mixins above; this class builds
    all the shared state ONCE in ``__init__`` (which the mixins reference via
    ``self``), and holds the two lifecycle methods (``init``/``aclose``).
    """

    def __init__(
        self,
        config: HarnessApiConfig | None = None,
        *,
        keys: KeyRegistry | None = None,
        chat_api_factory: ChatApiFactory | None = None,
        webhook_client: "httpx.AsyncClient | None" = None,
        event_log: RunEventLog | None = None,
        governor_service: GovernorService | None = None,
        run_registry: RunRegistry | None = None,
    ) -> None:
        self._cfg = config or HarnessApiConfig()
        # M2 3e-2: optional shared resource Governor. None ⇒ the ungoverned path
        # (KeyRegistry auth + stateless per-turn pricing, NO budget gate — uncapped)
        # (GOV-2). When set, resolve() owns auth + reservation and
        # the RunGovernor is wired into the ChatAPI/Orchestrator per run.
        #
        # Account layer (Axis-2): keys built from the config slices; secrets resolve
        # live from the process env inside the registry.
        self._keys = keys or KeyRegistry.from_keys_config(self._cfg.keys)
        # pre-03 3e: the typed AuthResolver from the config switchboard (store backend +
        # policy gate). This ONE instance is both the ungoverned auth path AND what the
        # Governor delegates to, so governed and ungoverned runs resolve credentials one
        # way (AUTH-9). An explicit legacy ``keys=`` still seeds via its adapter.
        self._auth_resolver = (
            keys.to_auth_resolver() if keys is not None
            else build_auth_resolver(self._cfg)
        )
        # M2 3g.2a: an explicit ``governor_service=`` (tests / manual wiring) still
        # wins. When none is injected, the config switchboard decides: governance
        # disabled ⇒ (None, None) ⇒ ungoverned (unchanged); enabled ⇒ a service +
        # durable task-policy store built from the typed config, sharing the resolver.
        self._governor_service = governor_service
        self._task_policy_store: JsonlTaskPolicyStore | None = None
        if governor_service is None:
            self._governor_service, self._task_policy_store = build_governor_service(
                self._cfg, self._auth_resolver
            )
        # Stateless price table (3g.2b): the retired SpendTracker's accumulation +
        # budget gate moved to the Governor's reservation ledger. The Runner keeps
        # only the price *table* to cost each turn's usage into ``state.cost_usd``.
        self._table = build_pricing(self._cfg.spend.pricing_json)
        self._factory = chat_api_factory or _default_factory(self._cfg)
        self._webhook_client = webhook_client
        self._sem = asyncio.Semaphore(self._cfg.engine.concurrency.max_concurrent)
        # EXT-C3b: the (user, task) mutex — in-process locally, a distributed Postgres
        # claim+lease when the state tier switch is postgres (so two replicas never run
        # one (user, task) concurrently). Same `async with lock.hold(u, t):` surface.
        self._task_lock = build_task_lock(self._cfg)
        self._runs: dict[str, _RunState] = {}
        self._sse: dict[str, SseEgress] = {}
        # C2: durable append-only run-events log. Built here (path derived from the
        # engine's persistence config) but opened lazily under a lock so it can be
        # constructed off-loop; DI-overridable for tests.
        self._event_log = event_log or build_event_log(self._cfg)
        self._event_log_ready = False
        self._event_log_lock = asyncio.Lock()
        # EXT-C1: durable run-identity index (run_id → user/task), so GET /runs/{id}
        # + the file read survive a restart. Config-selected (memory=ephemeral /
        # jsonl=durable); DI-overridable for tests. State is derived from the event
        # log — this holds identity only.
        self._run_registry = run_registry or build_run_registry(self._cfg)

    # --- lifecycle -------------------------------------------------------

    async def init(self) -> None:
        """Open the durable event log + replay governance state (call once at app
        startup; idempotent)."""
        await self._ensure_event_log()
        # EXT-C1: replay the durable run-identity registry (jsonl backend) into
        # memory. No-op for the memory backend / an explicitly-injected registry.
        await init_run_registry(self._run_registry)
        # M2 3g.2a: replay the JSONL-backed governance state (durable balance +
        # task-policy store) when the config switchboard built them. No-op for an
        # explicitly-injected service or the ungoverned path.
        await init_governance(self._governor_service, self._task_policy_store)
        # pre-03 3e: replay the durable credential store (jsonl backend) into memory.
        # No-op for the memory backend / an explicitly-injected legacy registry.
        await init_auth(self._auth_resolver)

    async def aclose(self) -> None:
        """Cancel any in-flight runs and close the durable log (for shutdown)."""
        for state in self._runs.values():
            if state.task and not state.task.done():
                state.task.cancel()
            # M6: also cancel a paused run's pending SLA timer so it doesn't outlive
            # the Runner (or fire during teardown).
            if state.sla_task and not state.sla_task.done():
                state.sla_task.cancel()
        await self._event_log.close()
        # EXT-C3b: release the distributed lock's pool (no-op for the in-process lock).
        await self._task_lock.close()
