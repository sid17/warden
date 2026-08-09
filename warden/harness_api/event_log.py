"""C2 / N1 — the durable, append-only ``run_events`` log.

The control plane mirrors **every** run event out of the sandbox into an on-disk,
append-only log so a run's history survives process teardown — the foundation
history / resume / HITL / governance all build on. Today the runner's ``seq`` is
in-memory only (``_RunState.last_seq``); a crash loses the stream.

Contract:
- **Monotonic ``seq`` per run**, ``PRIMARY KEY (run_id, seq)``.
- **Idempotent**: re-writing an existing ``(run_id, seq)`` is a no-op (``INSERT OR
  IGNORE``), so a retried emit or a replayed reconnect never duplicates.
- **Replayable**: ``replay(run_id, after_seq)`` returns everything with
  ``seq > after_seq`` in order — a reconnecting consumer replays from ``last_seq+1``.

Async SQLite (aiosqlite), same idiom as ``orchestrator.session.db.SessionDB``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

import aiosqlite

from warden.harness_api.schemas import Event, RunView

if TYPE_CHECKING:
    from warden.harness_api.config import HarnessApiConfig
    from warden.harness_api.postgres_event_log import PostgresRunEventLog

logger = logging.getLogger(__name__)

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS run_events (
    run_id     TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    type       TEXT NOT NULL,
    session_id TEXT,
    data       TEXT NOT NULL,
    at         TEXT NOT NULL,
    PRIMARY KEY (run_id, seq)
)
"""


class RunEventLog:
    """Append-only durable store for run events (one row per ``(run_id, seq)``)."""

    def __init__(self, db_path: Path | str) -> None:
        self._db_path = Path(db_path)
        self._conn: aiosqlite.Connection | None = None

    async def init(self) -> None:
        """Open the connection (WAL), create the table. Idempotent."""
        if self._conn is not None:
            return
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(str(self._db_path))
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute(_CREATE_TABLE)
        await self._conn.commit()
        logger.info("RunEventLog initialized at %s", self._db_path)

    async def append(self, event: Event) -> bool:
        """Durably record one event. Returns True if written, False if a duplicate.

        Idempotent on ``(run_id, seq)`` via ``INSERT OR IGNORE`` — a re-emit or a
        replayed reconnect is a safe no-op, never a duplicate row.
        """
        assert self._conn is not None, "RunEventLog.init() not called"
        cur = await self._conn.execute(
            "INSERT OR IGNORE INTO run_events "
            "(run_id, seq, type, session_id, data, at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                event.run_id,
                event.seq,
                event.type,
                event.session_id,
                json.dumps(event.data),
                event.at,
            ),
        )
        await self._conn.commit()
        return cur.rowcount > 0

    async def replay(self, run_id: str, after_seq: int = 0) -> list[Event]:
        """Return events for ``run_id`` with ``seq > after_seq``, in ``seq`` order.

        A reconnecting consumer passes its last-seen seq to resume exactly where it
        left off (``after_seq=last_seq`` → the stream continues at ``last_seq+1``).
        """
        assert self._conn is not None, "RunEventLog.init() not called"
        cur = await self._conn.execute(
            "SELECT run_id, seq, type, session_id, data, at FROM run_events "
            "WHERE run_id = ? AND seq > ? ORDER BY seq ASC",
            (run_id, after_seq),
        )
        rows = await cur.fetchall()
        return [
            Event(
                run_id=r[0],
                seq=r[1],
                type=r[2],
                session_id=r[3],
                data=json.loads(r[4]),
                at=r[5],
            )
            for r in rows
        ]

    async def reconstruct_view(
        self, run_id: str, *, user_id: str | None = None,
    ) -> RunView | None:
        """EXT-C1 — rebuild a :class:`RunView` from the durable event log alone.

        The registry supplies identity (``run_id → user/task``); *state* is derived
        here so it never drifts: replay every event and fold it into a status. Returns
        ``None`` if the run has no events (unknown / never emitted).

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
        A stamped ``permission_request.data.revise_round`` (emit-time convenience mirror)
        is derived from this same count and never authoritative — a stored counter can
        drift across crash/resume, so the read is always recomputed here.
        """
        assert self._conn is not None, "RunEventLog.init() not called"
        cur = await self._conn.execute(
            "SELECT data FROM run_events "
            "WHERE run_id = ? AND type = 'permission_request' ORDER BY seq ASC",
            (run_id,),
        )
        rows = await cur.fetchall()
        return sum(
            1 for (raw,) in rows if json.loads(raw).get("tool_name") == tool_name
        )

    async def last_seq(self, run_id: str) -> int:
        """Highest ``seq`` recorded for a run (0 if none) — the replay cursor."""
        assert self._conn is not None, "RunEventLog.init() not called"
        cur = await self._conn.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM run_events WHERE run_id = ?",
            (run_id,),
        )
        row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None


def build_event_log(
    cfg: "HarnessApiConfig",
) -> "RunEventLog | PostgresRunEventLog":
    """Construct the durable event log from the typed config (EXT-C3 tier switch).

    local backend is fine for a single container; Postgres is required to run more than
    one replica. ``state.backend == "postgres"`` ⇒ the shared
    :class:`PostgresRunEventLog` (DSN-deferred; the caller runs ``init()`` at startup to
    connect). Otherwise the process-local aiosqlite :class:`RunEventLog` at the same
    on-disk path the runner uses. Both are returned UNCONNECTED.
    """
    if cfg.state.is_postgres:
        # Imported lazily so the default (local) path never imports the asyncpg-backed
        # module; construction is DSN-deferred (init() connects at startup).
        from warden.harness_api.postgres_event_log import PostgresRunEventLog

        return PostgresRunEventLog(dsn=cfg.state.dsn)
    # Reuse the runner's path logic (module-level there); lazy-imported to avoid a
    # circular import (runner imports this module).
    from warden.harness_api.runner import _event_log_path

    return RunEventLog(_event_log_path(cfg.engine))
