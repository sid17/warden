"""Session index backed by SessionDB (local sqlite) or a shared Postgres store."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from warden.orchestrator.session.db import SessionDB, build_session_store

if TYPE_CHECKING:
    from warden.harness_api.config import HarnessApiConfig

logger = logging.getLogger(__name__)


class SessionIndex:
    """Session index wrapping a session store (sqlite ``SessionDB`` or the shared
    ``PostgresSessionStore``, which duck-types the same surface)."""

    def __init__(self, db: SessionDB | None = None) -> None:
        # Backward-compatible default: no arg ⇒ the process-local sqlite SessionDB,
        # exactly as before. The Postgres path is reached only via ``from_config``.
        self._db = db or SessionDB()

    @classmethod
    def from_config(cls, cfg: "HarnessApiConfig") -> "SessionIndex":
        """Build a cfg-aware index: the shared-state tier switch selects the store.

        ``state.backend == "postgres"`` ⇒ the shared ``PostgresSessionStore``
        (required to run more than one replica); otherwise the local ``SessionDB``
        default — identical to ``SessionIndex()``. The store is returned unconnected;
        ``SessionIndex.init()`` runs its ``init()`` at startup."""
        return cls(build_session_store(cfg))

    async def init(self) -> None:
        """Initialize the underlying database."""
        await self._db.init()

    async def register(
        self,
        session_id: str,
        workflow: str | None,
        provider: str = "claude",
        workspace_path: str = "",
        jsonl_path: str | None = None,
    ) -> None:
        """Register a new session.

        ``workflow`` is persisted as a durable column (N9) so resume rebuilds
        the permission surface from it; ``None`` (no bound workflow) is stored
        as-is. The display name falls back to ``chat`` only for presentation.
        """
        display_name = f"{workflow or 'chat'} — {session_id[:8]}"
        await self._db.register(
            session_id=session_id,
            provider=provider,
            workspace_path=workspace_path,
            jsonl_path=jsonl_path,
            display_name=display_name,
            workflow=workflow,
        )

    async def update_status(self, session_id: str, status: str) -> None:
        """Update session status (maps 'closed' to is_archived=True)."""
        is_archived = status == "closed"
        await self._db.update_status(session_id, is_archived)

    async def set_archived(self, session_id: str, archived: bool = True) -> None:
        """Archive or unarchive a session."""
        await self._db.update_status(session_id, archived)

    async def update_jsonl_path(self, session_id: str, jsonl_path: str) -> None:
        """Update the JSONL path for a session."""
        await self._db.update_jsonl_path(session_id, jsonl_path)

    async def list_sessions(self, workspace_path: str | None = None) -> list[dict]:
        """List sessions, optionally filtered by workspace_path.

        ``workspace_path=None`` lists sessions across ALL workspaces (newest
        first) — previously a stub that returned ``[]`` (C13).
        """
        if workspace_path:
            return await self._db.list_by_workspace(workspace_path)
        return await self._db.list_all()

    async def get(self, session_id: str) -> dict | None:
        """Get a single session."""
        return await self._db.get(session_id)

    async def close(self) -> None:
        """Close the underlying database."""
        await self._db.close()
