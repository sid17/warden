"""C13/C14/C15 — session listing across workspaces + the single default DB path.

- C15: the default session index lives at ``warden/data/sessions.db``
  (the git-tracked home), not a sibling ``engines/data/`` — a split-brain that
  made sessions written by the default vanish from the intended path.
- C13: ``SessionIndex.list_sessions(None)`` lists across ALL workspaces (was a
  stub returning ``[]``).
- C14: ``ChatAPI.list_sessions`` is the public entry point (the CLI no longer
  reaches ``_session_manager._index``).

Real SQLite via tmp_path; ``asyncio.run`` per the repo convention.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from warden.orchestrator.session.db import SessionDB, _DEFAULT_DB_PATH
from warden.orchestrator.session.index import SessionIndex


# --- C15: one canonical default path --------------------------------------

def test_default_db_path_is_the_tracked_harness_data_dir() -> None:
    """Default DB is warden/data/sessions.db (the .gitkeep'd home)."""
    # Its parent dir must be the SAME dir that holds the tracked .gitkeep.
    assert _DEFAULT_DB_PATH.name == "sessions.db"
    assert _DEFAULT_DB_PATH.parent.name == "data"
    # The harness package root == db.py's ...orchestrator/session, three up.
    harness_root = Path(__file__).resolve().parents[2]  # tests/core -> harness
    assert _DEFAULT_DB_PATH == harness_root / "data" / "sessions.db", (
        f"default DB split-brain: {_DEFAULT_DB_PATH} != {harness_root/'data'/'sessions.db'}"
    )
    # Regression guard: must NOT be the sibling engines/data/ (the old 4-parent bug).
    assert _DEFAULT_DB_PATH.parent != harness_root.parent / "data"


# --- C13: list all workspaces ---------------------------------------------

def _register(index: SessionIndex, sid: str, ws: str) -> None:
    return index.register(sid, "chat", provider="claude", workspace_path=ws)


def test_list_sessions_none_returns_all_workspaces(tmp_path: Path) -> None:
    async def _run() -> None:
        idx = SessionIndex(SessionDB(tmp_path / "s.db"))
        await idx.init()
        try:
            await _register(idx, "s1", "/ws/a")
            await _register(idx, "s2", "/ws/b")
            await _register(idx, "s3", "/ws/a")

            all_sessions = await idx.list_sessions(None)  # was [] before C13
            assert {s["session_id"] for s in all_sessions} == {"s1", "s2", "s3"}

            # Filtered path still works and is a subset.
            ws_a = await idx.list_sessions("/ws/a")
            assert {s["session_id"] for s in ws_a} == {"s1", "s3"}
        finally:
            await idx.close()

    asyncio.run(_run())


def test_list_all_excludes_archived_by_default(tmp_path: Path) -> None:
    async def _run() -> None:
        idx = SessionIndex(SessionDB(tmp_path / "s.db"))
        await idx.init()
        try:
            await _register(idx, "live", "/ws/a")
            await _register(idx, "gone", "/ws/a")
            await idx.set_archived("gone", True)

            active = await idx.list_sessions(None)
            assert {s["session_id"] for s in active} == {"live"}

            with_archived = await idx._db.list_all(include_archived=True)
            assert {s["session_id"] for s in with_archived} == {"live", "gone"}
        finally:
            await idx.close()

    asyncio.run(_run())


# --- C14: public ChatAPI.list_sessions -------------------------------------

def test_chatapi_list_sessions_is_public(tmp_path: Path) -> None:
    """ChatAPI exposes list_sessions delegating to the index (no private reach)."""
    from warden.config.models import HarnessConfig
    from warden.drive.api import ChatAPI

    async def _run() -> None:
        cfg = HarnessConfig()
        cfg.persistence.session_db_path = str(tmp_path / "s.db")
        api = ChatAPI(cfg, repo_path=str(tmp_path))
        await api.init()
        try:
            await api._session_manager._index.register(
                "sid1", "chat", provider="claude", workspace_path="/ws/x"
            )
            listed = await api.list_sessions()  # public, no workspace filter
            assert [s["session_id"] for s in listed] == ["sid1"]
            assert await api.list_sessions("/ws/none") == []
        finally:
            await api.close()

    asyncio.run(_run())
