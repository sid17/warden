"""Tests for the shared session-index seam (Step 3 of persistence).

Proves that pointing two SEPARATE SessionManager instances at the SAME SQLite
db_path makes a session registered by one visible to the other — so a fresh
process/container can resume a session it did not create. Also proves that
without sharing (different db paths) the row is NOT visible.

Uses real SQLite via tmp_path (no mocking). Async methods are driven with
asyncio.run(...) inside sync tests, matching the repo convention (no
pytest-asyncio configured). Each manager's whole lifecycle runs inside one
coroutine so its aiosqlite connection lives and dies within a single event
loop. Follows the style of
orchestrator/tests/persistence/test_local_backend.py.
"""

import asyncio
from pathlib import Path

from warden.orchestrator.session.db import SessionDB
from warden.orchestrator.session.index import SessionIndex
from warden.orchestrator.session.manager import SessionManager


def _manager(db_path: Path) -> SessionManager:
    """Build a SessionManager pointed at a specific SQLite path."""
    return SessionManager(index=SessionIndex(SessionDB(db_path)))


async def _register(db_path: Path, sid: str, workspace_path: str) -> None:
    """Register a synthetic session row via a fresh manager, then close it."""
    manager = _manager(db_path)
    await manager.init()
    try:
        await manager._index.register(
            sid,
            "chat",
            provider="claude",
            workspace_path=workspace_path,
            jsonl_path=None,
        )
    finally:
        await manager._index.close()


async def _read(db_path: Path, sid: str, workspace_path: str) -> tuple:
    """Open a FRESH manager on db_path and read back the row + workspace list."""
    manager = _manager(db_path)
    await manager.init()
    try:
        row = await manager._index.get(sid)
        sessions = await manager.list_sessions(workspace_path)
        return row, sessions
    finally:
        await manager._index.close()


def test_shared_db_path_makes_session_visible_to_fresh_manager(tmp_path):
    # Both managers share ONE db file — a fresh process would see the same file.
    db_path = tmp_path / "sessions.db"
    workspace_path = str((tmp_path / "workspaces" / "u" / "t").resolve())
    sid = "session-abc-123"

    # Manager A registers, then closes (simulating process exit).
    asyncio.run(_register(db_path, sid, workspace_path))

    # Manager B is a FRESH instance pointed at the same db_path.
    row, sessions = asyncio.run(_read(db_path, sid, workspace_path))

    # Cross-process visibility: the row is present by session id.
    assert row is not None
    assert row["session_id"] == sid
    assert row["provider"] == "claude"
    assert row["workspace_path"] == workspace_path

    # And it is found by the ABSOLUTE workspace path.
    assert [s["session_id"] for s in sessions] == [sid]


def test_separate_db_paths_do_not_share_sessions(tmp_path):
    # Two DIFFERENT db files — the default per-process behavior. The row
    # registered against one must NOT be visible via the other.
    db_a = tmp_path / "a" / "sessions.db"
    db_b = tmp_path / "b" / "sessions.db"
    workspace_path = str((tmp_path / "workspaces" / "u" / "t").resolve())
    sid = "session-xyz-789"

    asyncio.run(_register(db_a, sid, workspace_path))

    row, sessions = asyncio.run(_read(db_b, sid, workspace_path))

    assert row is None
    assert sessions == []
