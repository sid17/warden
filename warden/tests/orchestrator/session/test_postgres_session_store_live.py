"""EXT-C3 — the Postgres session store against a LIVE Postgres (opt-in).

Skipped unless ``WARDEN_TEST_POSTGRES_DSN`` is set — the default hermetic suite stays
DB-free (like the run registry). Run it locally against a throwaway DB to prove the
real SQL:

    WARDEN_TEST_POSTGRES_DSN=postgresql://warden:warden@localhost:5432/warden_test \\
      uv run --no-sync python -m pytest \\
      warden/tests/orchestrator/session/test_postgres_session_store_live.py -q

Uses uuid-unique ``session_id`` / ``workspace_path`` per test and deletes its own rows,
so it is safe to point at a shared DB without polluting it. The Docker bed gate is the
fuller proof (a real replica listing/resuming another replica's session).
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

from warden.orchestrator.session.postgres_db import PostgresSessionStore

_DSN = os.environ.get("WARDEN_TEST_POSTGRES_DSN")

pytestmark = pytest.mark.skipif(
    not _DSN, reason="set WARDEN_TEST_POSTGRES_DSN to run the live Postgres tests"
)


async def _cleanup(store: PostgresSessionStore, *session_ids: str) -> None:
    async with store._pool.acquire() as conn:  # noqa: SLF001 (test teardown)
        for sid in session_ids:
            await conn.execute("DELETE FROM sessions WHERE session_id = $1", sid)


def test_live_register_get_round_trip():
    """register → get returns a dict of the exact SessionDB shape; is_archived is bool."""
    async def _run():
        store = await PostgresSessionStore.connect(_DSN)
        sid = f"c3-sess-{uuid.uuid4()}"
        try:
            assert await store.get(sid) is None  # absent before register
            await store.register(
                sid,
                provider="claude",
                workspace_path="/ws/a",
                jsonl_path="/logs/a.jsonl",
                display_name="chat — c3",
                workflow="course",
            )
            got = await store.get(sid)
            assert got is not None
            assert set(got.keys()) == {
                "session_id", "provider", "workspace_path", "jsonl_path",
                "display_name", "workflow", "is_archived", "created_at", "updated_at",
            }
            assert got["session_id"] == sid
            assert got["provider"] == "claude"
            assert got["workspace_path"] == "/ws/a"
            assert got["jsonl_path"] == "/logs/a.jsonl"
            assert got["display_name"] == "chat — c3"
            assert got["workflow"] == "course"
            assert got["is_archived"] is False  # bool, not 0/1
            assert isinstance(got["created_at"], str)
            assert isinstance(got["updated_at"], str)
        finally:
            await _cleanup(store, sid)
            await store.close()

    asyncio.run(_run())


def test_live_update_status_and_jsonl_path():
    """update_status flips is_archived (bool); update_jsonl_path rewrites the path."""
    async def _run():
        store = await PostgresSessionStore.connect(_DSN)
        sid = f"c3-sess-{uuid.uuid4()}"
        try:
            await store.register(sid, provider="codex", workspace_path="/ws/b")
            await store.update_status(sid, True)
            got = await store.get(sid)
            assert got["is_archived"] is True

            await store.update_status(sid, False)
            assert (await store.get(sid))["is_archived"] is False

            await store.update_jsonl_path(sid, "/logs/b-new.jsonl")
            assert (await store.get(sid))["jsonl_path"] == "/logs/b-new.jsonl"
        finally:
            await _cleanup(store, sid)
            await store.close()

    asyncio.run(_run())


def test_live_list_by_workspace_and_list_all_ordering():
    """list_by_workspace filters by workspace + excludes archived, newest first;
    list_all spans workspaces. Ordering is updated_at DESC."""
    async def _run():
        store = await PostgresSessionStore.connect(_DSN)
        ws = f"/ws/{uuid.uuid4()}"  # unique workspace so we own the whole result set
        s1 = f"c3-sess-{uuid.uuid4()}"
        s2 = f"c3-sess-{uuid.uuid4()}"
        s3 = f"c3-sess-{uuid.uuid4()}"  # archived → excluded
        try:
            await store.register(s1, provider="claude", workspace_path=ws)
            await asyncio.sleep(1.0)  # timestamps are second-granular; force s2 newer
            await store.register(s2, provider="claude", workspace_path=ws)
            await store.register(s3, provider="claude", workspace_path=ws)
            await store.update_status(s3, True)  # archive s3

            by_ws = await store.list_by_workspace(ws)
            ids = [r["session_id"] for r in by_ws]
            assert s3 not in ids  # archived excluded
            assert set(ids) == {s1, s2}
            # updated_at DESC: s2 (registered later) precedes s1.
            assert ids.index(s2) < ids.index(s1)

            all_rows = await store.list_all()
            all_ids = {r["session_id"] for r in all_rows}
            assert {s1, s2} <= all_ids
            assert s3 not in all_ids  # archived excluded by default

            # include_archived=True surfaces s3.
            assert s3 in {r["session_id"] for r in await store.list_all(True)}
        finally:
            await _cleanup(store, s1, s2, s3)
            await store.close()

    asyncio.run(_run())


def test_live_cross_instance_visibility():
    """The multi-replica invariant: a session registered by instance A is read by a
    SEPARATE instance B (fresh pool) — the read is live shared state, not a cache."""
    async def _run():
        writer = await PostgresSessionStore.connect(_DSN)
        reader = await PostgresSessionStore.connect(_DSN)  # a distinct "replica"
        sid = f"c3-sess-{uuid.uuid4()}"
        try:
            await writer.register(
                sid, provider="claude", workspace_path="/ws/x", workflow="wf"
            )
            got = await reader.get(sid)  # reader never saw the register locally
            assert got is not None and got["workflow"] == "wf"
        finally:
            await _cleanup(writer, sid)
            await writer.close()
            await reader.close()

    asyncio.run(_run())
