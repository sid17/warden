"""EXT-C1 — the durable run-identity registry.

A tiny durable index ``run_id → (user_id, task_id, created_at)`` so ``GET /runs/{id}``
and E1's ``GET /runs/{id}/file`` survive a harness redeploy. The in-memory run
registry (``Runner._runs``) is ephemeral: on restart it is empty and both the status
lookup and the per-user ownership check lose the run. The durable ``run_events`` log
survives, but it has **no user/task columns** — so event-derivation alone cannot
resolve identity. This registry supplies exactly that missing mapping.

**Identity-only + append-only (decision 1).** A :class:`RunIdentity` is written once
at submit and never mutated; status/usage/cost are NOT stored here — they are derived
from the event log on demand (``RunEventLog.reconstruct_view``). The event log stays
the single source of run *state*; this registry is a pure identity index (no status
drift). Because records are immutable, the JSONL is append-only with no compaction.

**Backends (decision 2), mirroring the governance ledger / credential-store trio.**
:class:`InMemoryRunRegistry` (the ``memory`` backend — today's ephemeral behavior) +
:class:`JsonlRunRegistry` (durable ``runs.jsonl``, replayed on :meth:`load`). A
``PostgresRunRegistry`` slots in later as a sibling impl without touching callers.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from warden.harness_api.config import HarnessApiConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RunIdentity:
    """The durable identity of one run — written once at submit, never mutated.

    Just enough to resolve a ``run_id`` back to its owning ``(user_id, task_id)``
    after a restart (the mapping E1's file-read + ownership check consume). State
    (status/usage/cost) is derived from the event log, not stored here.

    ``spec_json`` (EXT-C3c) is the immutable, write-once serialized :class:`RunSpec` —
    the ONE piece of run context not reconstructable from the event log. It lets a
    replica that never held the run in memory rebuild its execution context and
    **cold-resume** a paused run (durable-HITL confirm on another container). ``None``
    for the ``memory`` backend / pre-C3c records; present only to enable cross-replica
    resume. Still identity-like: written once at submit, never mutated (no status drift).
    """

    run_id: str
    user_id: str
    task_id: str
    created_at: str
    spec_json: str | None = None


@runtime_checkable
class RunRegistry(Protocol):
    """The identity index the Runner writes to at submit and reads on a cache-miss.

    ``get`` is **async** (EXT-C3): the local backends resolve from an in-memory map,
    but the Postgres backend must read the *shared* row live so a replica resolves a
    run created by any other replica — a sync signature would force a stale per-process
    cache. The two callers (``owner_of`` / ``identity_of``) are already on async paths.
    """

    async def load(self) -> None: ...
    async def get(self, run_id: str) -> RunIdentity | None: ...
    async def put(self, identity: RunIdentity) -> None: ...


class InMemoryRunRegistry:
    """Ephemeral ``run_id → RunIdentity`` index (the ``memory`` backend tier).

    No durability — identical read/write surface to the JSONL store, used for tests
    and the opt-out (non-durable) default. Restart loses everything (today's behavior).
    """

    def __init__(self) -> None:
        self._records: dict[str, RunIdentity] = {}

    async def load(self) -> None:
        """No-op (nothing durable to replay); present for Protocol parity."""
        return None

    async def get(self, run_id: str) -> RunIdentity | None:
        return self._records.get(run_id)

    async def put(self, identity: RunIdentity) -> None:
        self._records[identity.run_id] = identity


class JsonlRunRegistry:
    """Durable ``run_id → RunIdentity`` index backed by an append-only ``runs.jsonl``
    (mirrors :class:`~warden.harness_api.credentials.store.JsonlCredentialStore`).

    A ``put`` appends one JSON line and folds it into memory; :meth:`load` replays the
    file on startup. Records are identity-only + immutable, so — unlike the balance
    ledger — there is **no** latest-wins compaction: a re-``put`` of the same ``run_id``
    is a harmless no-op (the fold is idempotent). A single :class:`asyncio.Lock`
    serializes writers; a corrupt/partial last line is logged and skipped (LAW 4).
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._lock = asyncio.Lock()
        self._records: dict[str, RunIdentity] = {}

    async def load(self) -> None:
        """Replay the JSONL file into memory (idempotent). Missing file ⇒ empty."""
        async with self._lock:
            self._records = {}
            if not self._path.exists():
                return
            with self._path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except (ValueError, json.JSONDecodeError):
                        logger.warning(
                            "JsonlRunRegistry: skipping corrupt line in %s: %r",
                            self._path, line[:200],
                        )
                        continue
                    self._apply(data)

    def _apply(self, data: dict) -> None:
        """Fold one already-parsed record into the in-memory map."""
        run_id = data.get("run_id")
        if not run_id:
            return
        try:
            self._records[run_id] = RunIdentity(
                run_id=run_id,
                user_id=data["user_id"],
                task_id=data["task_id"],
                created_at=data.get("created_at", ""),
                spec_json=data.get("spec_json"),
            )
        except KeyError:
            logger.warning(
                "JsonlRunRegistry: skipping record missing fields: %r", data
            )

    async def get(self, run_id: str) -> RunIdentity | None:
        return self._records.get(run_id)

    async def put(self, identity: RunIdentity) -> None:
        """Append + fold the identity record (no-op if the run_id is already known —
        records are immutable, so a resume-time re-put never appends a duplicate)."""
        async with self._lock:
            if identity.run_id in self._records:
                return
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(asdict(identity)) + "\n")
            self._apply(asdict(identity))


# --- switchboard (mirrors credentials/config.build_auth_resolver + init_auth) ---


def _registry_state_dir(cfg: "HarnessApiConfig") -> Path:
    """Where ``runs.jsonl`` lives — ``cfg.run_registry.state_dir`` when set, else the
    control-plane state dir (next to the session DB / ``run_events.db``), falling back
    to ``data/`` (mirrors ``credentials/config._auth_state_dir``)."""
    if cfg.run_registry.state_dir:
        return Path(cfg.run_registry.state_dir)
    session_db = getattr(cfg.engine.persistence, "session_db_path", None)
    return Path(session_db).parent if session_db else Path("data")


def build_run_registry(cfg: "HarnessApiConfig") -> RunRegistry:
    """Construct the :class:`RunRegistry` from the typed config.

    The shared-state tier switch wins first: ``state.backend == "postgres"`` ⇒ the
    shared :class:`PostgresRunRegistry` (local backend is fine for a single container;
    Postgres is required to run more than one replica). Otherwise the process-local
    backends: ``memory`` (default) ⇒ ephemeral (today's behavior — opt-in durability);
    ``jsonl`` ⇒ the durable ``runs.jsonl``. All are returned UNLOADED; the caller runs
    :func:`init_run_registry` at startup to replay/connect.
    """
    if cfg.state.is_postgres:
        # Imported lazily so the default (local) path never imports the asyncpg-backed
        # module; construction is DSN-deferred (load() connects at startup).
        from warden.harness_api.postgres_run_registry import (
            PostgresRunRegistry,
        )

        return PostgresRunRegistry(dsn=cfg.state.dsn)
    if cfg.run_registry.store_backend == "jsonl":
        return JsonlRunRegistry(_registry_state_dir(cfg) / "runs.jsonl")
    return InMemoryRunRegistry()


async def init_run_registry(registry: RunRegistry) -> None:
    """Replay/connect the durable registry (call once at startup; idempotent).
    No-op for the in-memory backend; ``load()`` replays JSONL or connects Postgres."""
    if not isinstance(registry, InMemoryRunRegistry):
        await registry.load()


__all__ = [
    "RunIdentity",
    "RunRegistry",
    "InMemoryRunRegistry",
    "JsonlRunRegistry",
    "build_run_registry",
    "init_run_registry",
]
