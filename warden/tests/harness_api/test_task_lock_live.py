"""EXT-C3b — the distributed (user, task) lock against a LIVE Postgres (opt-in).

Proves the cross-replica mutex the whole multi-replica story rests on: two independent
lock instances (distinct owners = two "replicas") sharing one Postgres never both hold a
``(user, task)``; a lease renews while held; an EXPIRED lease is stealable; and a
superseded owner is fenced (its renew/release can't clobber the new holder).

Opt-in: skipped unless ``WARDEN_TEST_POSTGRES_DSN`` is set. Short TTLs keep it fast:

    WARDEN_TEST_POSTGRES_DSN=postgresql://warden:warden@localhost:5432/warden_test \\
      uv run --no-sync python -m pytest \\
      warden/tests/harness_api/test_task_lock_live.py -q
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import uuid

import pytest

from warden.harness_api.task_lock import PostgresTaskLock

_DSN = os.environ.get("WARDEN_TEST_POSTGRES_DSN")

pytestmark = pytest.mark.skipif(
    not _DSN, reason="set WARDEN_TEST_POSTGRES_DSN to run the C3b lock tests"
)


def _key() -> tuple[str, str]:
    """A unique (user, task) per test so parallel runs never collide."""
    u = f"c3b-{uuid.uuid4().hex[:8]}"
    return u, "course"


async def _cleanup(lock: PostgresTaskLock, user_id: str) -> None:
    await lock._ensure()  # noqa: SLF001
    async with lock._pool.acquire() as conn:  # noqa: SLF001
        await conn.execute("DELETE FROM task_leases WHERE user_id = $1", user_id)


def test_live_two_replicas_never_both_hold():
    """Two owners contend on one key: the second blocks until the first releases, so the
    two critical sections never overlap."""
    async def _run():
        u, t = _key()
        a = PostgresTaskLock(_DSN, lease_ttl_s=30, heartbeat_s=5, poll_s=0.02)
        b = PostgresTaskLock(_DSN, lease_ttl_s=30, heartbeat_s=5, poll_s=0.02)
        order: list[str] = []
        try:
            async def worker(lock, tag):
                async with lock.hold(u, t):
                    order.append(f"enter-{tag}")
                    await asyncio.sleep(0.15)
                    order.append(f"exit-{tag}")

            await asyncio.gather(worker(a, "a"), worker(b, "b"))
            # no interleave: first entrant fully exits before the second enters
            assert order[0].startswith("enter") and order[1].startswith("exit")
            assert order[1][5:] == order[0][6:]  # same tag exits before other enters
            assert order[2].startswith("enter") and order[3].startswith("exit")
        finally:
            await _cleanup(a, u)
            await a.close()
            await b.close()

    asyncio.run(_run())


def test_live_lease_loss_cancels_the_running_hold():
    """The HIGH-severity fix: a lease is advisory, so if it is lost mid-hold (stolen
    after an expiry the heartbeat failed to prevent), the run must STOP — not keep
    executing work another replica now owns. We simulate the steal by reassigning the
    lease row to another owner; A's next heartbeat renew returns False → A's run task is
    cancelled. Without the fix A would run on, double-executing the (user,task)."""
    async def _run():
        u, t = _key()
        a = PostgresTaskLock(
            _DSN, lease_ttl_s=30, heartbeat_s=0.2, poll_s=0.02, renew_retry_s=0.1
        )
        b = PostgresTaskLock(_DSN)
        cancelled = {"v": False}
        try:
            async def a_run():
                try:
                    async with a.hold(u, t):
                        await asyncio.sleep(10)  # long-running work
                except asyncio.CancelledError:
                    cancelled["v"] = True
                    raise

            task = asyncio.create_task(a_run())
            await asyncio.sleep(0.2)             # let A claim + start executing
            await b._ensure()                    # noqa: SLF001
            # Forcibly hand the lease row to B (deterministic "steal"): A's next
            # owner-guarded renew (WHERE owner_id=A) now matches 0 rows.
            async with b._pool.acquire() as conn:  # noqa: SLF001
                await conn.execute(
                    "UPDATE task_leases SET owner_id = $2, "
                    "lease_expires_at = now() + interval '30 seconds' "
                    "WHERE user_id = $1 AND task_id = $3",
                    u, b._owner, t,             # noqa: SLF001
                )
            await asyncio.sleep(0.6)             # A's heartbeat ticks, sees loss, cancels
            assert cancelled["v"] is True        # the run was fenced at the work level
            with contextlib.suppress(asyncio.CancelledError):
                await task
        finally:
            await _cleanup(a, u)
            await a.close()
            await b.close()

    asyncio.run(_run())


def test_live_different_keys_do_not_block():
    """Two owners on DIFFERENT keys hold concurrently (the mutex is per key)."""
    async def _run():
        u1, t = _key()
        u2, _ = _key()
        a = PostgresTaskLock(_DSN, poll_s=0.02)
        b = PostgresTaskLock(_DSN, poll_s=0.02)
        both = asyncio.Event()
        n = {"v": 0}
        try:
            async def worker(lock, user):
                async with lock.hold(user, t):
                    n["v"] += 1
                    if n["v"] == 2:
                        both.set()
                    await asyncio.wait_for(both.wait(), timeout=2.0)

            await asyncio.gather(worker(a, u1), worker(b, u2))
            assert n["v"] == 2
        finally:
            await _cleanup(a, u1)
            await _cleanup(b, u2)
            await a.close()
            await b.close()

    asyncio.run(_run())


def test_live_expired_lease_is_stealable_and_old_owner_is_fenced():
    """A owns a short lease and stops renewing; after expiry B claims it. Then A's late
    renew + release are fenced (match 0 rows) — A cannot resurrect or delete B's lease."""
    async def _run():
        u, t = _key()
        # A: 1s lease, and we DON'T run its heartbeat loop (raw claim), so it expires.
        a = PostgresTaskLock(_DSN, lease_ttl_s=1.0, heartbeat_s=100, poll_s=0.02)
        b = PostgresTaskLock(_DSN, lease_ttl_s=30, heartbeat_s=5, poll_s=0.02)
        try:
            await a._ensure()                       # noqa: SLF001
            await b._ensure()                       # noqa: SLF001  (direct _try_claim needs the pool)
            assert await a._try_claim(u, t) is True  # noqa: SLF001  A holds
            assert await b._try_claim(u, t) is False  # noqa: SLF001  B blocked (unexpired)

            await asyncio.sleep(1.2)                 # let A's lease expire (DB clock)

            assert await b._try_claim(u, t) is True   # noqa: SLF001  B steals expired
            # Fencing: A is superseded — its renew + release now match nothing.
            assert await a._renew(u, t) is False      # noqa: SLF001
            await a._release(u, t)                    # noqa: SLF001  (owner-guarded no-op)
            # B still owns the key: a re-claim by B (already owner path is a fresh
            # unexpired lease) — prove the row is B's by renewing successfully.
            assert await b._renew(u, t) is True       # noqa: SLF001
        finally:
            await _cleanup(a, u)
            await a.close()
            await b.close()

    asyncio.run(_run())


def test_live_heartbeat_holds_lease_against_a_contender():
    """While A holds with a live heartbeat, a contender B cannot claim even past the raw
    TTL — the renew keeps A's ownership fresh."""
    async def _run():
        u, t = _key()
        a = PostgresTaskLock(_DSN, lease_ttl_s=1.0, heartbeat_s=0.3, poll_s=0.02)
        b = PostgresTaskLock(_DSN, lease_ttl_s=1.0, heartbeat_s=0.3, poll_s=0.02)
        try:
            await b._ensure()                        # noqa: SLF001  (direct _try_claim needs the pool)
            async with a.hold(u, t):                 # heartbeat loop renews every 0.3s
                await asyncio.sleep(1.4)             # well past the 1.0s raw TTL
                assert await b._try_claim(u, t) is False  # noqa: SLF001  still A's
            # once A exits (releases), B can claim immediately
            assert await b._try_claim(u, t) is True   # noqa: SLF001
        finally:
            await _cleanup(a, u)
            await a.close()
            await b.close()

    asyncio.run(_run())
