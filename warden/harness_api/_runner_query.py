"""Query / read / lifecycle-inspection methods for :class:`Runner`.

Composed into ``Runner`` via the MRO; assumes ``Runner.__init__`` state
(``self._runs``, ``self._run_registry``, ``self._event_log``, ``self._sse``,
``self._cfg``, ``self._ensure_event_log`` etc.).
"""

from __future__ import annotations

import asyncio

from warden.harness_api.egress import SseEgress
from warden.harness_api.run_registry import RunIdentity  # noqa: F401
from warden.harness_api.schemas import Event, RunView


class _QueryMixin:
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

    async def identity_of(self, run_id: str) -> tuple[str, str] | None:
        """Resolve ``run_id → (user_id, task_id)`` — in-memory hit, else the durable
        registry (EXT-C1). ``None`` if the run is unknown to both. Used by A1's
        file-read to build the ``(user, task)`` archive key. ``async`` because the
        Postgres registry (EXT-C3) reads the shared row live (any replica → any run)."""
        state = self._runs.get(run_id)
        if state is not None:
            return (state.user_id, state.task_id)
        identity = await self._run_registry.get(run_id)
        return (identity.user_id, identity.task_id) if identity is not None else None

    async def read_run_file(self, run_id: str, path: str) -> bytes:
        """EXT-A1 — return the bytes of one confined file from the run's snapshot.

        Reads from the **persistence snapshot** (authoritative after teardown), not
        the live task folder (which may be gone). The ``(user, task)`` are resolved
        from the run itself (in-memory or the durable registry), so the bytes served
        are always that run's own box; ``path`` is confined (``..``/NUL/escape
        rejected) by the backend. Credential files are excluded from every snapshot,
        so a secret is unreachable.

        Raises:
            ValueError: ``path`` fails the confinement check (→ route 400).
            FileNotFoundError: unknown run, no snapshot yet, or missing member (→ 404).
        """
        from warden.config.build import build_persistence
        from warden.persistence.keys import archive_key

        identity = await self.identity_of(run_id)
        if identity is None:
            raise FileNotFoundError(f"unknown run {run_id}")
        user_id, task_id = identity
        runtime_cfg, backend = build_persistence(
            self._cfg.engine.persistence, self._cfg.engine.workspace
        )
        key = archive_key(runtime_cfg, user_id, task_id)
        # A run with no completed turn has no tarball yet → 404 (not an error).
        if not await backend.exists(key):
            raise FileNotFoundError(
                f"no snapshot for run {run_id} (no completed turn yet)"
            )
        return await backend.read_file(key, path)

    async def owner_of(self, run_id: str) -> str | None:
        """The ``user_id`` that owns a run, for the per-user authorization check
        (EXT-T1b), or ``None`` if the run is unknown.

        In-memory registry hit → ``_RunState.user_id``. After a restart the in-memory
        map is empty → fall back to the durable ``RunRegistry`` (EXT-C1). A miss in
        both → ``None`` (the route then 404s). ``async`` because the Postgres registry
        (EXT-C3) reads the shared row live so any replica authorizes any run.
        """
        state = self._runs.get(run_id)
        if state is not None:
            return state.user_id
        identity = await self._run_registry.get(run_id)
        return identity.user_id if identity is not None else None

    async def get_durable(self, run_id: str) -> RunView | None:
        """EXT-C1 — ``GET /runs/{id}`` that survives a restart.

        In-memory hit → the live snapshot (:meth:`get`). Miss → resolve identity from
        the durable ``RunRegistry`` and reconstruct *state* from the event log; the
        registry stays identity-only, the event log stays the single source of state.
        A ``run_id`` unknown to both the registry AND the event log → ``None`` (404).
        """
        live = self.get(run_id)
        if live is not None:
            return live
        identity = await self._run_registry.get(run_id)
        if identity is None:
            return None
        await self._ensure_event_log()
        return await self._event_log.reconstruct_view(
            run_id, user_id=identity.user_id
        )

    def sse_for(self, run_id: str) -> SseEgress | None:
        """The SSE buffer for a run, so ``GET /runs/{id}/events`` can stream it."""
        return self._sse.get(run_id)

    async def replay(self, run_id: str, after_seq: int = 0) -> list[Event]:
        """Durable history for a run with ``seq > after_seq`` (reconnect/replay).

        Survives process teardown (unlike the in-memory registry), so a consumer
        that dropped the SSE stream resumes exactly at ``last_seq+1``.
        """
        await self._ensure_event_log()
        return await self._event_log.replay(run_id, after_seq)

    def task_for(self, run_id: str) -> asyncio.Task | None:
        """The background task for a run (awaited by tests / graceful drain)."""
        state = self._runs.get(run_id)
        return state.task if state else None

    async def cancel(self, run_id: str) -> bool:
        state = self._runs.get(run_id)
        if state is None or state.task is None or state.task.done():
            return False
        state.task.cancel()
        return True
