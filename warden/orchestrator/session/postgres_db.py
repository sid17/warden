"""EXT-C3 — the Postgres session-metadata store (multi-replica shared backend).

:class:`~warden.orchestrator.session.db.SessionDB` durably records the
``session_id → (provider, workspace, jsonl, workflow, …)`` index in an embedded
sqlite file **local to one container**. A fleet of replicas behind a load balancer
needs that index in a **shared** store: a session registered on replica A must be
listable / resumable by a request served on replica B. This backend keeps the
identical public surface (``SessionIndex`` duck-types it) but stores rows in
Postgres, so every read is an authoritative live read of shared state — any replica
lists / resumes any session.

**local backend is fine for a single container; Postgres is required to run more than
one replica.** (The single-container default stays :class:`SessionDB`; this is
selected only when ``state.backend == "postgres"``.)

Mirrors ``harness_api/postgres_run_registry.py``: ``asyncpg`` is imported LAZILY and
is an OPTIONAL extra (``uv sync --extra postgres``), so this module imports WITHOUT
the driver installed and the hermetic suite never needs a DB. It is exercised on the
Docker bed + the opt-in live-Postgres test, not in the default unit suite.
"""

from __future__ import annotations

import time
from typing import Any

_ASYNCPG_HINT = (
    "PostgresSessionStore requires asyncpg. Install the optional extra: "
    "`uv sync --extra postgres` (or `pip install 'engines-harness[postgres]'`)."
)

# --- DDL: idempotent, mirrors the sqlite ``sessions`` table -------------------
# ``is_archived`` is a native BOOLEAN here (sqlite stores it as INTEGER 0/1); the
# row builder returns a Python bool either way so the dict shape is identical.

_DDL = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id     TEXT PRIMARY KEY,
    provider       TEXT NOT NULL,
    workspace_path TEXT NOT NULL,
    jsonl_path     TEXT,
    display_name   TEXT,
    workflow       TEXT,
    is_archived    BOOLEAN NOT NULL DEFAULT FALSE,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);
"""

# The columns a row SELECT returns, in order — mirrors ``db._ROW_COLUMNS`` so the
# dict shape is byte-for-byte identical to the sqlite backend.
_ROW_COLUMNS = (
    "session_id", "provider", "workspace_path", "jsonl_path", "display_name",
    "workflow", "is_archived", "created_at", "updated_at",
)
_ROW_SELECT = ", ".join(_ROW_COLUMNS)


def _require_asyncpg() -> Any:
    """Import ``asyncpg`` lazily with a clear install hint (LAW 4: no silent failure)."""
    try:
        import asyncpg  # noqa: PLC0415  (lazy by design — optional extra)
    except ImportError as exc:  # pragma: no cover - exercised on the bed
        raise ImportError(_ASYNCPG_HINT) from exc
    return asyncpg


def _row_to_dict(row: Any) -> dict:
    """Map an asyncpg ``Record`` to a session dict (single source of shape).

    Matches ``db._row_to_dict``: ``is_archived`` is coerced to a Python ``bool``
    (Postgres already returns a bool, so this is a no-op guard that keeps the two
    backends' outputs identical for a sqlite INTEGER 0/1).
    """
    d = {col: row[col] for col in _ROW_COLUMNS}
    d["is_archived"] = bool(d["is_archived"])
    return d


class PostgresSessionStore:
    """Shared ``session_id → session-row`` store. Duck-types :class:`SessionDB`.

    Same public surface (method names, signatures, return shapes) as the sqlite
    backend, so :class:`~warden.orchestrator.session.index.SessionIndex`
    wraps either transparently. Built from a ``dsn`` and DSN-deferred: the pool is
    connected by :meth:`init` at startup (mirrors ``PostgresRunRegistry``), and
    every read is a live read of shared state so any replica lists any session.
    """

    def __init__(
        self, pool: Any = None, *, dsn: str | None = None,
        min_size: int = 1, max_size: int = 10,
    ) -> None:
        # Fail loudly at construction if the driver is absent even when a pool-like
        # object was handed in (the import here is lazy) — mirrors the registry. A
        # pool may be injected directly (tests), or a ``dsn`` deferred for
        # :meth:`init` to connect at startup (the ``build_session_store`` path).
        _require_asyncpg()
        self._pool = pool
        self._dsn = dsn
        self._min_size = min_size
        self._max_size = max_size

    @classmethod
    async def connect(
        cls, dsn: str, *, min_size: int = 1, max_size: int = 10
    ) -> "PostgresSessionStore":
        """Build a pool from a DSN and ensure the schema exists (eager connect)."""
        store = cls(dsn=dsn, min_size=min_size, max_size=max_size)
        await store.init()
        return store

    async def init(self) -> None:
        """Connect the pool (if deferred) and ensure the schema. Idempotent — a
        second call is a no-op once the pool exists. Mirrors ``SessionDB.init``:
        open + create table. There is no migration step — the DDL creates the full
        (post-``workflow``) schema; a shared Postgres store is fresh, not a legacy
        pre-``workflow`` DB to migrate in place."""
        if self._pool is not None:
            await self._ensure_schema()
            return
        if not self._dsn:
            raise ValueError(
                "PostgresSessionStore needs a DSN (state.dsn / WARDEN_POSTGRES_DSN) "
                "to connect — none was provided."
            )
        asyncpg = _require_asyncpg()
        self._pool = await asyncpg.create_pool(
            self._dsn, min_size=self._min_size, max_size=self._max_size
        )
        await self._ensure_schema()

    async def _ensure_schema(self) -> None:
        """Create the ``sessions`` table if absent (idempotent)."""
        assert self._pool is not None, "PostgresSessionStore.init() not called"
        async with self._pool.acquire() as conn:
            await conn.execute(_DDL)

    async def register(
        self,
        session_id: str,
        provider: str,
        workspace_path: str,
        jsonl_path: str | None = None,
        display_name: str | None = None,
        workflow: str | None = None,
    ) -> None:
        """Insert a new session row (mirrors ``SessionDB.register``).

        ``workflow`` is the init-bound workflow this session was created under (N9)
        — persisted so a resume in a fresh process (or on another replica) rebuilds
        the exact permission surface from durable shared storage, not transient
        memory."""
        assert self._pool is not None, "PostgresSessionStore.init() not called"
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO sessions (session_id, provider, workspace_path, "
                "jsonl_path, display_name, workflow, created_at, updated_at) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
                session_id, provider, workspace_path, jsonl_path, display_name,
                workflow, now, now,
            )

    async def update_status(self, session_id: str, is_archived: bool) -> None:
        """Update the is_archived flag (mirrors ``SessionDB.update_status``)."""
        assert self._pool is not None, "PostgresSessionStore.init() not called"
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE sessions SET is_archived = $1, updated_at = $2 "
                "WHERE session_id = $3",
                is_archived, now, session_id,
            )

    async def update_jsonl_path(self, session_id: str, jsonl_path: str) -> None:
        """Update the JSONL transcript path (mirrors ``SessionDB.update_jsonl_path``)."""
        assert self._pool is not None, "PostgresSessionStore.init() not called"
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE sessions SET jsonl_path = $1, updated_at = $2 "
                "WHERE session_id = $3",
                jsonl_path, now, session_id,
            )

    async def list_by_workspace(self, workspace_path: str) -> list[dict]:
        """List sessions for a workspace, ordered by updated_at DESC (mirrors sqlite:
        is_archived excluded)."""
        assert self._pool is not None, "PostgresSessionStore.init() not called"
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT {_ROW_SELECT} FROM sessions WHERE workspace_path = $1 "
                "AND is_archived = FALSE ORDER BY updated_at DESC",
                workspace_path,
            )
        return [_row_to_dict(r) for r in rows]

    async def list_all(self, include_archived: bool = False) -> list[dict]:
        """List sessions across ALL workspaces, newest first. Archived excluded by
        default, matching the per-workspace query (mirrors ``SessionDB.list_all``)."""
        assert self._pool is not None, "PostgresSessionStore.init() not called"
        where = "" if include_archived else "WHERE is_archived = FALSE"
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT {_ROW_SELECT} FROM sessions {where} ORDER BY updated_at DESC"
            )
        return [_row_to_dict(r) for r in rows]

    async def get(self, session_id: str) -> dict | None:
        """Get a single session by ID (mirrors ``SessionDB.get``)."""
        assert self._pool is not None, "PostgresSessionStore.init() not called"
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT {_ROW_SELECT} FROM sessions WHERE session_id = $1",
                session_id,
            )
        return _row_to_dict(row) if row is not None else None

    async def close(self) -> None:
        """Close the connection pool (the fleet-teardown hook)."""
        if self._pool is not None:
            await self._pool.close()
            self._pool = None


__all__ = ["PostgresSessionStore"]
