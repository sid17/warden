"""EXT-C3 — the Postgres run-identity registry (multi-replica shared backend).

The :class:`~warden.harness_api.run_registry.JsonlRunRegistry` durably
records ``run_id → (user_id, task_id, created_at)`` in a file that is **local to one
container**. A fleet of replicas behind a load balancer needs that mapping in a
**shared** store: a run created on replica A must be resolvable by ``GET /runs/{id}``
served on replica B. This backend keeps the identical identity-only + append-only
contract but stores rows in Postgres, so ``get`` is an authoritative live read of
shared state (not a per-process cache) — any replica resolves any run.

**local backend is fine for a single container; Postgres is required to run more than
one replica.** (The single-container default stays :class:`JsonlRunRegistry`; this is
selected only when ``state.backend == "postgres"``.)

Mirrors ``governance/postgres_ledger.py``: ``asyncpg`` is imported LAZILY and is an
OPTIONAL extra (``uv sync --extra postgres``), so this module imports WITHOUT the
driver installed and the hermetic suite never needs a DB. It is exercised on the
Docker bed + the opt-in live-Postgres test, not in the default unit suite.
"""

from __future__ import annotations

from typing import Any

from warden.harness_api.run_registry import RunIdentity

_ASYNCPG_HINT = (
    "PostgresRunRegistry requires asyncpg. Install the optional extra: "
    "`uv sync --extra postgres` (or `pip install 'engines-harness[postgres]'`)."
)

# --- DDL: idempotent, mirrors the RunIdentity dataclass -----------------------

_DDL = """
CREATE TABLE IF NOT EXISTS run_identities (
    run_id      TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    task_id     TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    spec_json   TEXT              -- EXT-C3c: immutable RunSpec for cross-replica cold-resume
);
"""

# EXT-C3c: add the column to a table created before C3c (idempotent, in-place — never
# drops rows). ``CREATE TABLE IF NOT EXISTS`` won't alter an existing table.
_MIGRATE_SPEC_JSON = (
    "ALTER TABLE run_identities ADD COLUMN IF NOT EXISTS spec_json TEXT"
)


def _require_asyncpg() -> Any:
    """Import ``asyncpg`` lazily with a clear install hint (LAW 4: no silent failure)."""
    try:
        import asyncpg  # noqa: PLC0415  (lazy by design — optional extra)
    except ImportError as exc:  # pragma: no cover - exercised on the bed
        raise ImportError(_ASYNCPG_HINT) from exc
    return asyncpg


class PostgresRunRegistry:
    """Shared ``run_id → RunIdentity`` index. Satisfies the ``RunRegistry`` Protocol.

    Identity-only + append-only, exactly like the JSONL backend: a ``put`` is an
    ``INSERT … ON CONFLICT DO NOTHING`` (records are immutable, so a resume-time
    re-put is a harmless no-op), and ``get`` reads the shared row live so a replica
    resolves runs created by any other replica.
    """

    def __init__(
        self, pool: Any = None, *, dsn: str | None = None,
        min_size: int = 1, max_size: int = 10,
    ) -> None:
        # Fail loudly at construction if the driver is absent even when a pool-like
        # object was handed in (the import here is lazy) — mirrors the ledger. A pool
        # may be injected directly (tests), or a ``dsn`` deferred for :meth:`load` to
        # connect at startup (the ``build_run_registry`` sync-build / async-load path).
        _require_asyncpg()
        self._pool = pool
        self._dsn = dsn
        self._min_size = min_size
        self._max_size = max_size

    @classmethod
    async def connect(
        cls, dsn: str, *, min_size: int = 1, max_size: int = 10
    ) -> "PostgresRunRegistry":
        """Build a pool from a DSN and ensure the schema exists (eager connect)."""
        registry = cls(dsn=dsn, min_size=min_size, max_size=max_size)
        await registry.load()
        return registry

    async def ensure_schema(self) -> None:
        """Create the ``run_identities`` table if absent + migrate the spec_json column
        (idempotent)."""
        async with self._pool.acquire() as conn:
            await conn.execute(_DDL)
            await conn.execute(_MIGRATE_SPEC_JSON)

    async def load(self) -> None:
        """Connect the pool (if deferred) and ensure the schema. Idempotent — a second
        call is a no-op once the pool exists. ``get`` reads Postgres live, so there is
        no local cache to replay; this only establishes the shared connection."""
        if self._pool is not None:
            return
        if not self._dsn:
            raise ValueError(
                "PostgresRunRegistry needs a DSN (state.dsn / WARDEN_POSTGRES_DSN) "
                "to connect — none was provided."
            )
        asyncpg = _require_asyncpg()
        self._pool = await asyncpg.create_pool(
            self._dsn, min_size=self._min_size, max_size=self._max_size
        )
        await self.ensure_schema()

    async def get(self, run_id: str) -> RunIdentity | None:
        """Authoritative live read of the shared identity row (any replica → any run)."""
        assert self._pool is not None, "PostgresRunRegistry.load() not called"
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT run_id, user_id, task_id, created_at, spec_json "
                "FROM run_identities WHERE run_id = $1",
                run_id,
            )
        if row is None:
            return None
        return RunIdentity(
            run_id=row["run_id"],
            user_id=row["user_id"],
            task_id=row["task_id"],
            created_at=row["created_at"],
            spec_json=row["spec_json"],
        )

    async def put(self, identity: RunIdentity) -> None:
        """Append the identity row. Immutable ⇒ ``ON CONFLICT DO NOTHING`` (a re-put of
        the same ``run_id`` is a no-op, matching the JSONL backend's idempotent fold)."""
        assert self._pool is not None, "PostgresRunRegistry.load() not called"
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO run_identities "
                "(run_id, user_id, task_id, created_at, spec_json) "
                "VALUES ($1, $2, $3, $4, $5) ON CONFLICT (run_id) DO NOTHING",
                identity.run_id,
                identity.user_id,
                identity.task_id,
                identity.created_at,
                identity.spec_json,
            )

    async def close(self) -> None:
        """Close the connection pool (the fleet-teardown hook)."""
        await self._pool.close()


__all__ = ["PostgresRunRegistry"]
