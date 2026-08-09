"""EXT-C3 — the Postgres durable ``run_events`` log (multi-replica shared backend).

The :class:`~warden.harness_api.event_log.RunEventLog` (aiosqlite) mirrors
every run event into an append-only log **local to one container**. A fleet of
replicas behind a load balancer needs that log in a **shared** store: a run whose
events were written on replica A must be replayable / resumable by ``GET /runs/{id}``
served on replica B. This backend keeps the identical monotonic-``seq`` + idempotent
+ replayable contract, but stores rows in Postgres, so ``replay`` /
``reconstruct_view`` are authoritative live reads of shared state — any replica
serves/resumes any run.

**local backend is fine for a single container; Postgres is required to run more than
one replica.** (The single-container default stays :class:`RunEventLog`; this is
selected only when ``state.backend == "postgres"``.)

Mirrors ``postgres_run_registry.py``: ``asyncpg`` is imported LAZILY and is an OPTIONAL
extra (``uv sync --extra postgres``), so this module imports WITHOUT the driver
installed and the hermetic suite never needs a DB. It is exercised on the Docker bed +
the opt-in live-Postgres test, not in the default unit suite.
"""

from __future__ import annotations

import json
from typing import Any

from warden.harness_api.schemas import Event, RunView

_ASYNCPG_HINT = (
    "PostgresRunEventLog requires asyncpg. Install the optional extra: "
    "`uv sync --extra postgres` (or `pip install 'engines-harness[postgres]'`)."
)

# --- DDL: idempotent, mirrors the sqlite ``run_events`` table -----------------
# ``data`` is JSONB (round-trips a dict cleanly); ``seq`` is BIGINT for headroom.

_DDL = """
CREATE TABLE IF NOT EXISTS run_events (
    run_id     TEXT NOT NULL,
    seq        BIGINT NOT NULL,
    type       TEXT NOT NULL,
    session_id TEXT,
    data       JSONB NOT NULL,
    at         TEXT NOT NULL,
    PRIMARY KEY (run_id, seq)
);
"""


def _require_asyncpg() -> Any:
    """Import ``asyncpg`` lazily with a clear install hint (LAW 4: no silent failure)."""
    try:
        import asyncpg  # noqa: PLC0415  (lazy by design — optional extra)
    except ImportError as exc:  # pragma: no cover - exercised on the bed
        raise ImportError(_ASYNCPG_HINT) from exc
    return asyncpg


def _decode_data(raw: Any) -> dict:
    """asyncpg may return a JSONB column as a decoded dict or a raw string depending
    on codec registration; accept both so replay reconstructs the original dict."""
    if isinstance(raw, str):
        return json.loads(raw)
    return raw or {}


class PostgresRunEventLog:
    """Shared append-only durable store for run events (one row per ``(run_id, seq)``).

    Same public surface as :class:`RunEventLog`, but rows live in Postgres so any
    replica replays/reconstructs any run's history. Idempotent on ``(run_id, seq)`` via
    ``INSERT … ON CONFLICT DO NOTHING`` — a re-emit or replayed reconnect is a safe
    no-op, never a duplicate row.
    """

    def __init__(
        self, pool: Any = None, *, dsn: str | None = None,
        min_size: int = 1, max_size: int = 10,
    ) -> None:
        # Fail loudly at construction if the driver is absent even when a pool-like
        # object was handed in (the import here is lazy) — mirrors the run registry. A
        # pool may be injected directly (tests), or a ``dsn`` deferred for :meth:`init`
        # to connect at startup (the ``build_event_log`` deferred-connect path).
        _require_asyncpg()
        self._pool = pool
        self._dsn = dsn
        self._min_size = min_size
        self._max_size = max_size

    @classmethod
    async def connect(
        cls, dsn: str, *, min_size: int = 1, max_size: int = 10
    ) -> "PostgresRunEventLog":
        """Build a pool from a DSN and ensure the schema exists (eager connect)."""
        log = cls(dsn=dsn, min_size=min_size, max_size=max_size)
        await log.init()
        return log

    async def init(self) -> None:
        """Connect the pool (if deferred) and create the table. Idempotent — a second
        call is a no-op once the pool exists. ``replay`` reads Postgres live, so there
        is no local cache; this only establishes the shared connection + schema."""
        if self._pool is not None:
            return
        if not self._dsn:
            raise ValueError(
                "PostgresRunEventLog needs a DSN (state.dsn / WARDEN_POSTGRES_DSN) "
                "to connect — none was provided."
            )
        asyncpg = _require_asyncpg()
        self._pool = await asyncpg.create_pool(
            self._dsn, min_size=self._min_size, max_size=self._max_size
        )
        async with self._pool.acquire() as conn:
            await conn.execute(_DDL)

    async def append(self, event: Event) -> bool:
        """Durably record one event. Returns True if written, False if a duplicate.

        Idempotent on ``(run_id, seq)`` via ``ON CONFLICT DO NOTHING`` — a re-emit or a
        replayed reconnect is a safe no-op, never a duplicate row. ``RETURNING`` yields
        a row only when the INSERT actually landed, so a missing row ⇒ the duplicate.
        """
        assert self._pool is not None, "PostgresRunEventLog.init() not called"
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO run_events "
                "(run_id, seq, type, session_id, data, at) "
                "VALUES ($1, $2, $3, $4, $5, $6) "
                "ON CONFLICT (run_id, seq) DO NOTHING RETURNING seq",
                event.run_id,
                event.seq,
                event.type,
                event.session_id,
                json.dumps(event.data),
                event.at,
            )
        return row is not None

    async def replay(self, run_id: str, after_seq: int = 0) -> list[Event]:
        """Return events for ``run_id`` with ``seq > after_seq``, in ``seq`` order.

        A reconnecting consumer passes its last-seen seq to resume exactly where it
        left off (``after_seq=last_seq`` → the stream continues at ``last_seq+1``).
        """
        assert self._pool is not None, "PostgresRunEventLog.init() not called"
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT run_id, seq, type, session_id, data, at FROM run_events "
                "WHERE run_id = $1 AND seq > $2 ORDER BY seq ASC",
                run_id,
                after_seq,
            )
        return [
            Event(
                run_id=r["run_id"],
                seq=r["seq"],
                type=r["type"],
                session_id=r["session_id"],
                data=_decode_data(r["data"]),
                at=r["at"],
            )
            for r in rows
        ]

    async def reconstruct_view(
        self, run_id: str, *, user_id: str | None = None,
    ) -> RunView | None:
        """EXT-C1 — rebuild a :class:`RunView` from the durable event log alone.

        Ports the EXACT status-derivation from :class:`RunEventLog`: replay every event
        and fold it into a status. Returns ``None`` if the run has no events.

        Status derivation (the only place run status is inferred, since only event
        ``type`` is stored, never a status column):
          * terminal ``result`` → ``succeeded``; ``error`` → ``error``;
            ``stopped`` → ``stopped``;
          * a trailing ``permission_request`` with no later ``permission_resolved``
            → ``requires_action`` (the durable-HITL pause);
          * none of the above → ``running``.
        """
        events = await self.replay(run_id, 0)
        if not events:
            return None
        session_id: str | None = None
        status = "running"
        usage: dict = {}
        cost_usd = 0.0
        error: str | None = None
        pending = False
        for e in events:
            if e.session_id:
                session_id = e.session_id
            if e.type == "permission_request":
                pending = True
            elif e.type == "permission_resolved":
                pending = False
            elif e.type == "result":
                status = "succeeded"
                usage = e.data.get("usage", {}) or {}
                cost_usd = e.data.get("cost_usd", 0.0) or 0.0
                pending = False
            elif e.type == "error":
                status = "error"
                error = e.data.get("reason")
                pending = False
            elif e.type == "stopped":
                status = "stopped"
                usage = e.data.get("usage", {}) or {}
                cost_usd = e.data.get("cost_usd", 0.0) or 0.0
                pending = False
        if pending:
            status = "requires_action"
        return RunView(
            run_id=run_id,
            status=status,  # type: ignore[arg-type]
            session_id=session_id,
            last_seq=events[-1].seq,
            usage=usage,
            cost_usd=cost_usd,
            error=error,
        )

    async def revise_round(self, run_id: str, tool_name: str) -> int:
        """E6 §3b — the number of gate pauses for a ``(run_id, tool_name)``, derived
        from the durable event log (NO new storage; the log is the source of truth).

        Counts ``permission_request`` events whose ``data.tool_name`` matches, ordered
        by ``seq``. On the first pause this is 1; after a revise→re-pause it is 2, etc.
        Always recomputed (a stored counter can drift across crash/resume).
        """
        assert self._pool is not None, "PostgresRunEventLog.init() not called"
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT data FROM run_events "
                "WHERE run_id = $1 AND type = 'permission_request' ORDER BY seq ASC",
                run_id,
            )
        return sum(
            1 for r in rows if _decode_data(r["data"]).get("tool_name") == tool_name
        )

    async def last_seq(self, run_id: str) -> int:
        """Highest ``seq`` recorded for a run (0 if none) — the replay cursor."""
        assert self._pool is not None, "PostgresRunEventLog.init() not called"
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT COALESCE(MAX(seq), 0) AS s FROM run_events WHERE run_id = $1",
                run_id,
            )
        return int(row["s"]) if row else 0

    async def close(self) -> None:
        """Close the connection pool (the fleet-teardown hook)."""
        if self._pool is not None:
            await self._pool.close()
            self._pool = None


__all__ = ["PostgresRunEventLog"]
