"""EXT-C3 — the Postgres run registry against a LIVE Postgres (opt-in).

Skipped unless ``WARDEN_TEST_POSTGRES_DSN`` is set — the default hermetic suite stays
DB-free (like the ledger). Run it locally against a throwaway DB to prove the real SQL:

    WARDEN_TEST_POSTGRES_DSN=postgresql://warden:warden@localhost:5432/warden \\
      uv run --no-sync python -m pytest \\
      warden/tests/harness_api/test_postgres_run_registry_live.py -q

Uses a unique ``run_id`` per test (uuid4) and deletes its own rows, so it is safe to
point at a shared DB without polluting it. The Docker bed gate is the fuller proof
(a real replica serving/resuming another replica's run).
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

from warden.harness_api.postgres_run_registry import PostgresRunRegistry
from warden.harness_api.run_registry import RunIdentity

_DSN = os.environ.get("WARDEN_TEST_POSTGRES_DSN")

pytestmark = pytest.mark.skipif(
    not _DSN, reason="set WARDEN_TEST_POSTGRES_DSN to run the live Postgres tests"
)


async def _cleanup(reg: PostgresRunRegistry, run_id: str) -> None:
    async with reg._pool.acquire() as conn:  # noqa: SLF001 (test teardown)
        await conn.execute("DELETE FROM run_identities WHERE run_id = $1", run_id)


def test_live_round_trip_put_get():
    async def _run():
        reg = await PostgresRunRegistry.connect(_DSN)
        run_id = f"c3-{uuid.uuid4()}"
        try:
            assert await reg.get(run_id) is None  # absent before put
            ident = RunIdentity(run_id, "u1", "course", "2026-07-29T00:00:00Z")
            await reg.put(ident)
            got = await reg.get(run_id)
            assert got is not None
            assert (got.user_id, got.task_id) == ("u1", "course")
            assert got.created_at == "2026-07-29T00:00:00Z"
        finally:
            await _cleanup(reg, run_id)
            await reg.close()

    asyncio.run(_run())


def test_live_put_is_append_only_idempotent():
    """A re-put of the same run_id is a no-op (immutable identity, ON CONFLICT DO
    NOTHING) — the first writer's user/task wins."""
    async def _run():
        reg = await PostgresRunRegistry.connect(_DSN)
        run_id = f"c3-{uuid.uuid4()}"
        try:
            await reg.put(RunIdentity(run_id, "u1", "course", "t0"))
            await reg.put(RunIdentity(run_id, "u2", "other", "t1"))  # ignored
            got = await reg.get(run_id)
            assert (got.user_id, got.task_id) == ("u1", "course")
        finally:
            await _cleanup(reg, run_id)
            await reg.close()

    asyncio.run(_run())


def test_live_cross_instance_visibility():
    """The multi-replica invariant: a run written by instance A is read by a SEPARATE
    instance B (fresh pool) — i.e. the read is live shared state, not a local cache."""
    async def _run():
        writer = await PostgresRunRegistry.connect(_DSN)
        reader = await PostgresRunRegistry.connect(_DSN)  # a distinct "replica"
        run_id = f"c3-{uuid.uuid4()}"
        try:
            await writer.put(RunIdentity(run_id, "uX", "taskX", "t0"))
            got = await reader.get(run_id)  # reader never saw the put locally
            assert got is not None and got.user_id == "uX"
        finally:
            await _cleanup(writer, run_id)
            await writer.close()
            await reader.close()

    asyncio.run(_run())
