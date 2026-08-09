"""Async SQLite wrapper for session metadata persistence."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

import aiosqlite

if TYPE_CHECKING:  # avoid a runtime import of the Axis-2 config (circular / heavy)
    from warden.harness_api.config import HarnessApiConfig

logger = logging.getLogger(__name__)

# The default session index lives in the harness engine's own data dir
# (``warden/data/`` — the git-tracked ``.gitkeep`` home), NOT a sibling
# ``engines/data/``. db.py is at warden/orchestrator/session/db.py, so
# THREE parents reach ``warden``; a fourth (the old bug) overshot to
# ``engines/`` and split the index across two files ("my session disappeared").
_DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "sessions.db"

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id   TEXT PRIMARY KEY,
    provider     TEXT NOT NULL,
    workspace_path TEXT NOT NULL,
    jsonl_path   TEXT,
    display_name TEXT,
    workflow     TEXT,
    is_archived  INTEGER DEFAULT 0,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
)
"""

# The columns a row SELECT returns, in order. Kept in ONE place so the row
# dict builder and every query agree — the ``workflow`` column (N9) is read on
# resume to rebuild the session's permission surface from durable storage.
_ROW_COLUMNS = (
    "session_id", "provider", "workspace_path", "jsonl_path", "display_name",
    "workflow", "is_archived", "created_at", "updated_at",
)
_ROW_SELECT = ", ".join(_ROW_COLUMNS)


def _row_to_dict(r: tuple) -> dict:
    """Map a ``_ROW_SELECT`` tuple to a session dict (single source of shape)."""
    d = dict(zip(_ROW_COLUMNS, r))
    d["is_archived"] = bool(d["is_archived"])
    return d


class SessionDB:
    """Async SQLite store for session metadata."""

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path or _DEFAULT_DB_PATH
        self._conn: aiosqlite.Connection | None = None

    async def init(self) -> None:
        """Open connection, enable WAL mode, create table, run migrations."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(str(self._db_path))
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute(_CREATE_TABLE)
        await self._migrate()
        await self._conn.commit()
        logger.info("SessionDB initialized at %s", self._db_path)

    async def _migrate(self) -> None:
        """Idempotently add columns missing from a pre-existing DB.

        ``CREATE TABLE IF NOT EXISTS`` never alters an existing table, so a DB
        created before the ``workflow`` column (N9) would lack it. Add it in
        place rather than dropping rows — resume reads it to rebuild policy.
        """
        cursor = await self._conn.execute("PRAGMA table_info(sessions)")
        cols = {row[1] for row in await cursor.fetchall()}
        if "workflow" not in cols:
            await self._conn.execute("ALTER TABLE sessions ADD COLUMN workflow TEXT")

    async def register(
        self,
        session_id: str,
        provider: str,
        workspace_path: str,
        jsonl_path: str | None = None,
        display_name: str | None = None,
        workflow: str | None = None,
    ) -> None:
        """Insert a new session row.

        ``workflow`` is the init-bound workflow this session was created under
        (N9) — persisted so a resume in a fresh process rebuilds the exact
        permission surface from durable storage, not transient memory.
        """
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        await self._conn.execute(
            "INSERT INTO sessions (session_id, provider, workspace_path, "
            "jsonl_path, display_name, workflow, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (session_id, provider, workspace_path, jsonl_path, display_name,
             workflow, now, now),
        )
        await self._conn.commit()

    async def update_status(self, session_id: str, is_archived: bool) -> None:
        """Update the is_archived flag."""
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        await self._conn.execute(
            "UPDATE sessions SET is_archived = ?, updated_at = ? WHERE session_id = ?",
            (int(is_archived), now, session_id),
        )
        await self._conn.commit()

    async def update_jsonl_path(self, session_id: str, jsonl_path: str) -> None:
        """Update the JSONL transcript path for a session."""
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        await self._conn.execute(
            "UPDATE sessions SET jsonl_path = ?, updated_at = ? WHERE session_id = ?",
            (jsonl_path, now, session_id),
        )
        await self._conn.commit()

    async def list_by_workspace(self, workspace_path: str) -> list[dict]:
        """List sessions for a workspace, ordered by updated_at DESC."""
        cursor = await self._conn.execute(
            f"SELECT {_ROW_SELECT} FROM sessions WHERE workspace_path = ? "
            "AND is_archived = 0 ORDER BY updated_at DESC",
            (workspace_path,),
        )
        return [_row_to_dict(r) for r in await cursor.fetchall()]

    async def list_all(self, include_archived: bool = False) -> list[dict]:
        """List sessions across ALL workspaces, newest first.

        The unfiltered companion to ``list_by_workspace`` — used by the
        "all workspaces" listing (``SessionIndex.list_sessions(None)``). Archived
        sessions are excluded by default, matching the per-workspace query.
        """
        where = "" if include_archived else "WHERE is_archived = 0"
        cursor = await self._conn.execute(
            f"SELECT {_ROW_SELECT} FROM sessions {where} ORDER BY updated_at DESC"
        )
        return [_row_to_dict(r) for r in await cursor.fetchall()]

    async def get(self, session_id: str) -> dict | None:
        """Get a single session by ID."""
        cursor = await self._conn.execute(
            f"SELECT {_ROW_SELECT} FROM sessions WHERE session_id = ?",
            (session_id,),
        )
        r = await cursor.fetchone()
        return _row_to_dict(r) if r is not None else None

    async def close(self) -> None:
        """Close the database connection."""
        if self._conn:
            await self._conn.close()
            self._conn = None


def build_session_store(cfg: "HarnessApiConfig"):
    """Construct the session-metadata store from the shared-state tier switch.

    ``state.backend == "postgres"`` ⇒ the shared
    :class:`~warden.orchestrator.session.postgres_db.PostgresSessionStore`
    (local backend is fine for a single container; Postgres is required to run more
    than one replica). Otherwise the process-local :class:`SessionDB` at its default
    path. The Postgres store is returned UNCONNECTED (DSN-deferred); the caller runs
    ``init()`` at startup to connect + ensure the schema, exactly like ``SessionDB``.
    """
    if cfg.state.is_postgres:
        # Imported lazily so the default (local) path never imports the asyncpg-backed
        # module; construction is DSN-deferred (init() connects at startup).
        from warden.orchestrator.session.postgres_db import (  # noqa: PLC0415
            PostgresSessionStore,
        )

        return PostgresSessionStore(dsn=cfg.state.dsn)
    return SessionDB()
