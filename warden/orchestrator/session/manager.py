"""Manages active agent sessions and their lifecycle."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from warden.providers import AgentProvider, create_session
from warden.orchestrator.session.index import SessionIndex

if TYPE_CHECKING:
    from warden.harness_api.config import HarnessApiConfig

logger = logging.getLogger(__name__)


class SessionManager:
    """Manages active agent sessions and their lifecycle.

    Session IDs are owned by the provider SDK (Claude SDK session_id,
    Codex thread_id). They are unknown at creation time and captured
    from the first streaming message. Use register() after the ID is known.
    """

    def __init__(self, index: SessionIndex | None = None):
        self._sessions: dict[str, AgentProvider] = {}
        # Backward-compatible default: no arg ⇒ the process-local sqlite index,
        # exactly as before. The Postgres path is reached only via ``from_config``.
        self._index = index or SessionIndex()

    @classmethod
    def from_config(cls, cfg: "HarnessApiConfig") -> "SessionManager":
        """Build a cfg-aware manager: the shared-state tier switch selects the index's
        store (local ``SessionDB`` vs shared ``PostgresSessionStore`` when
        ``state.backend == "postgres"``). ``SessionManager()`` stays local-default."""
        return cls(index=SessionIndex.from_config(cfg))

    async def init(self) -> None:
        """Initialize the session index (creates DB table)."""
        await self._index.init()

    async def create(
        self,
        repo_path: Path,
        can_use_tool: Any = None,
        provider: str = "claude",
        model: str | None = None,
        disallowed_tools: list[str] | None = None,
        system_prompt: str | None = None,
        custom_tools: list | None = None,
        provider_kwargs: dict | None = None,
    ) -> AgentProvider:
        """Create and start a new agent session.

        Returns the session object. session.session_id is None until
        the first streaming message captures it from the SDK.
        Call register() after the ID is known.

        ``provider_kwargs`` is a generic passthrough merged into the provider
        constructor kwargs (e.g. ``claude_config_dir`` / ``codex_home`` for the
        per-task session home). Default None → behavior unchanged.
        """
        kwargs: dict[str, Any] = dict(
            repo_path=repo_path,
            can_use_tool=can_use_tool,
            model=model,
            disallowed_tools=disallowed_tools,
        )
        if system_prompt is not None:
            kwargs["system_prompt"] = system_prompt
        if custom_tools is not None:
            kwargs["custom_tools"] = custom_tools
        if provider_kwargs:
            kwargs.update(provider_kwargs)
        session = create_session(provider, **kwargs)
        await session.start()
        logger.info("Created session (provider=%s, id pending)", provider)
        return session

    async def register(
        self,
        session: AgentProvider,
        provider: str = "claude",
        workspace_path: str = "",
        workflow: str | None = None,
    ) -> None:
        """Register a session after its ID has been captured from the SDK.

        ``workflow`` is the session's init-bound workflow (N9) — persisted so a
        resume rebuilds the permission surface from it. ``None`` = no bound
        workflow (permissive default).
        """
        sid = session.session_id
        if not sid:
            raise ValueError("Cannot register session without session_id")
        self._sessions[sid] = session
        # Upsert: if the session already exists in DB (resume case), update it
        existing = await self._index.get(sid)
        if existing:
            await self._index.update_status(sid, "active")
        else:
            await self._index.register(
                sid, workflow,
                provider=provider,
                workspace_path=workspace_path,
                jsonl_path=session.jsonl_path,
            )
        logger.info("Registered session %s", sid)

    def get(self, session_id: str) -> AgentProvider | None:
        """Get an active session by ID."""
        return self._sessions.get(session_id)

    async def list_sessions(self, workspace_path: str) -> list[dict]:
        """List sessions for a workspace."""
        return await self._index.list_sessions(workspace_path)

    async def resume(
        self,
        session_id: str,
        repo_path: Path,
        can_use_tool: Any = None,
        provider: str = "claude",
        model: str | None = None,
        disallowed_tools: list[str] | None = None,
        system_prompt: str | None = None,
        custom_tools: list | None = None,
        provider_kwargs: dict | None = None,
    ) -> tuple[str, AgentProvider]:
        """Resume a previous session by its ID.

        Reuses the original session_id — no new ID generated.

        ``provider_kwargs`` is a generic passthrough merged into the provider
        constructor kwargs (e.g. ``claude_config_dir`` / ``codex_home`` for the
        per-task session home). Default None → behavior unchanged.
        """
        original = await self._index.get(session_id)
        if original is None:
            raise ValueError(f"Session {session_id} not found in index")

        kwargs: dict[str, Any] = dict(
            repo_path=repo_path,
            can_use_tool=can_use_tool,
            model=model,
            resume_session_id=session_id,
            disallowed_tools=disallowed_tools,
        )
        if system_prompt is not None:
            kwargs["system_prompt"] = system_prompt
        if custom_tools is not None:
            kwargs["custom_tools"] = custom_tools
        if provider_kwargs:
            kwargs.update(provider_kwargs)
        session = create_session(provider, **kwargs)
        await session.start()
        # session.session_id == session_id (set from resume_session_id)
        self._sessions[session_id] = session
        await self._index.update_status(session_id, "active")
        logger.info("Resumed session %s", session_id)
        return session_id, session

    async def close(self, session_id: str) -> None:
        """Close a session and update the index."""
        session = self._sessions.pop(session_id, None)
        if session:
            await session.close()
            await self._index.update_status(session_id, "closed")
            logger.info("Closed session %s", session_id)

    async def close_all(self) -> None:
        """Close all active sessions (for shutdown)."""
        for sid in list(self._sessions.keys()):
            await self.close(sid)

    async def close_index(self) -> None:
        """Close the underlying session-index DB connection.

        Call on final shutdown (after ``close_all``) so the aiosqlite worker
        thread is torn down inside the running event loop — otherwise it lingers
        and raises "Event loop is closed" at interpreter exit.
        """
        await self._index.close()
