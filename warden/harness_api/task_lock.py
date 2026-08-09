"""EXT-C3b — the ``(user, task)`` lock: in-process today, distributed for a fleet.

The Runner serializes concurrent runs of one ``(user_id, task_id)`` ("one course at a
time") with a per-key :class:`asyncio.Lock` — correct within ONE process, but a fleet
of replicas behind a load balancer needs the mutex to span processes: two replicas that
each receive a submit for the SAME ``(user, task)`` must not run it concurrently.

This seam has two impls behind one ``async with lock.hold(user, task):`` surface:

* :class:`InProcessTaskLock` — the ``local`` backend: today's per-key ``asyncio.Lock``,
  byte-for-byte the current behavior (a single container needs no Postgres).
* :class:`PostgresTaskLock` — the ``postgres`` backend: a **claim + lease** in a shared
  ``task_leases`` table. Acquisition is a conditional upsert that only takes a key whose
  existing lease is EXPIRED (judged by the **database** clock, never a replica's wall
  clock), so exactly one replica holds a ``(user, task)`` at a time; a background
  heartbeat renews the lease while the run executes; exit deletes it. The in-process
  lock is KEPT as a same-replica fast path (the lease is the cross-replica authority;
  the local lock just avoids two same-replica runs hammering the DB).

local backend is fine for a single container; Postgres is required to run more than one
replica. ``asyncpg`` is a lazy OPTIONAL extra (``uv sync --extra postgres``), so this
module imports without the driver and the hermetic suite never needs a DB.

Correctness notes (the lease gotchas):
* **Never hold a transaction for the whole run.** The lock is a lease COLUMN we renew,
  not a held ``FOR UPDATE`` — a long-open txn pins Postgres and dies under PgBouncer.
* **Owner-guarded renew/release (fencing).** Heartbeat and release both filter
  ``owner_id = me``: if this owner's lease expired and another replica legitimately
  stole the key, this owner's later renew/delete matches zero rows — it can neither
  resurrect nor clobber the new owner. That is the C3c fencing primitive.
* **One clock.** Expiry is ``now()`` evaluated in Postgres, so replica clock skew never
  decides ownership.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
import uuid
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from warden.harness_api.config import HarnessApiConfig

logger = logging.getLogger(__name__)

_ASYNCPG_HINT = (
    "PostgresTaskLock requires asyncpg. Install the optional extra: "
    "`uv sync --extra postgres` (or `pip install 'engines-harness[postgres]'`)."
)

_DDL = """
CREATE TABLE IF NOT EXISTS task_leases (
    user_id          TEXT NOT NULL,
    task_id          TEXT NOT NULL,
    owner_id         TEXT NOT NULL,
    lease_expires_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (user_id, task_id)
);
"""


def _require_asyncpg() -> Any:
    """Import ``asyncpg`` lazily with a clear install hint (LAW 4: no silent failure)."""
    try:
        import asyncpg  # noqa: PLC0415  (lazy by design — optional extra)
    except ImportError as exc:  # pragma: no cover - exercised on the bed
        raise ImportError(_ASYNCPG_HINT) from exc
    return asyncpg


def _new_owner_id() -> str:
    """A process-unique owner id: host + pid + a random suffix (distinct per Runner)."""
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


class InProcessTaskLock:
    """The ``local`` backend: a per-``(user, task)`` :class:`asyncio.Lock`.

    ``hold(user, task)`` returns the key's lock, which is itself an async context
    manager — so ``async with lock.hold(u, t):`` is exactly today's serialization.
    """

    def __init__(self) -> None:
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    def hold(self, user_id: str, task_id: str) -> asyncio.Lock:
        key = (user_id, task_id)
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    async def close(self) -> None:
        """No-op (nothing durable); present for interface parity with the PG lock."""
        return None


class PostgresTaskLock:
    """The ``postgres`` backend: a distributed ``(user, task)`` claim + lease.

    Built DSN-deferred (the pool connects lazily on first :meth:`hold`, matching the
    other shared stores). Each instance carries a unique ``owner_id`` so the lease is
    fenceable. Tunables: ``lease_ttl_s`` (how long a lease is valid without a renew),
    ``heartbeat_s`` (renew cadence — must be < ttl), ``poll_s`` (how often a blocked
    acquirer re-attempts the claim).
    """

    def __init__(
        self,
        dsn: str | None,
        *,
        owner_id: str | None = None,
        lease_ttl_s: float = 30.0,
        heartbeat_s: float = 10.0,
        poll_s: float = 0.5,
        renew_retry_s: float = 1.0,
        max_renew_failures: int = 3,
        min_size: int = 1,
        max_size: int = 10,
    ) -> None:
        _require_asyncpg()
        if not dsn:
            raise ValueError(
                "PostgresTaskLock needs a DSN (state.dsn / WARDEN_POSTGRES_DSN) "
                "to connect — none was provided."
            )
        self._dsn = dsn
        self._owner = owner_id or _new_owner_id()
        self._lease_ttl_s = lease_ttl_s
        self._heartbeat_s = heartbeat_s
        self._poll_s = poll_s
        # On a FAILED renew, retry after this short backoff rather than waiting a full
        # heartbeat cycle — so a transient DB blip doesn't burn the TTL slack. After
        # ``max_renew_failures`` consecutive failures (or a renew that returns False =
        # the lease was stolen after expiry), the lease is treated as LOST.
        self._renew_retry_s = renew_retry_s
        self._max_renew_failures = max_renew_failures
        self._min_size = min_size
        self._max_size = max_size
        self._pool: Any = None
        self._connect_lock = asyncio.Lock()
        # Same-replica fast path: two runs of one (user,task) on THIS replica serialize
        # locally without touching the DB. The lease is the cross-replica authority.
        self._local = InProcessTaskLock()

    @property
    def owner_id(self) -> str:
        return self._owner

    async def _ensure(self) -> None:
        """Connect the pool + create the table once (idempotent, race-safe)."""
        if self._pool is not None:
            return
        async with self._connect_lock:
            if self._pool is not None:
                return
            asyncpg = _require_asyncpg()
            pool = await asyncpg.create_pool(
                self._dsn, min_size=self._min_size, max_size=self._max_size
            )
            async with pool.acquire() as conn:
                await conn.execute(_DDL)
            self._pool = pool

    async def _try_claim(self, user_id: str, task_id: str) -> bool:
        """One atomic attempt to claim ``(user, task)``. Returns True iff this owner now
        holds the lease.

        The conditional upsert takes the key when it is FREE (fresh insert) or its
        current lease is EXPIRED (``DO UPDATE … WHERE lease_expires_at < now()``); a
        live lease held by another owner makes the ``DO UPDATE`` match no row, so
        ``RETURNING`` yields nothing and we back off. ``now()`` is the DB clock.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO task_leases (user_id, task_id, owner_id, lease_expires_at) "
                "VALUES ($1, $2, $3, now() + make_interval(secs => $4)) "
                "ON CONFLICT (user_id, task_id) DO UPDATE "
                "  SET owner_id = EXCLUDED.owner_id, "
                "      lease_expires_at = EXCLUDED.lease_expires_at "
                "  WHERE task_leases.lease_expires_at < now() "
                "RETURNING owner_id",
                user_id, task_id, self._owner, self._lease_ttl_s,
            )
        return row is not None and row["owner_id"] == self._owner

    async def _renew(self, user_id: str, task_id: str) -> bool:
        """Extend this owner's lease. Owner-guarded (fencing): matches 0 rows — and
        returns False — if the lease was stolen after expiry, so a slow owner learns it
        has been superseded and does not resurrect a lease it no longer holds."""
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE task_leases "
                "SET lease_expires_at = now() + make_interval(secs => $4) "
                "WHERE user_id = $1 AND task_id = $2 AND owner_id = $3",
                user_id, task_id, self._owner, self._lease_ttl_s,
            )
        return result.endswith(" 1")  # "UPDATE 1" ⇒ still ours

    async def _release(self, user_id: str, task_id: str) -> None:
        """Drop this owner's lease. Owner-guarded so a superseded owner never deletes the
        new holder's lease."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM task_leases "
                "WHERE user_id = $1 AND task_id = $2 AND owner_id = $3",
                user_id, task_id, self._owner,
            )

    def hold(self, user_id: str, task_id: str) -> "_LeaseHold":
        """Async CM: local lock → cross-replica claim (blocking-poll) → heartbeat;
        exit cancels the heartbeat, releases the lease, then the local lock."""
        return _LeaseHold(self, user_id, task_id)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None


class _LeaseHold:
    """The async context manager returned by :meth:`PostgresTaskLock.hold`.

    Owns the heartbeat and — critically — **reacts to lease loss**. A lease is an
    ADVISORY hold: renewing it keeps ownership, but nothing about the run body stops
    if the lease is stolen after an expiry the heartbeat failed to prevent (a
    multi-cycle DB stall / event-loop starvation). So the heartbeat here cancels the
    run's own task the moment it learns the lease is no longer ours — turning the lease
    into a real mutual exclusion over the WORK: the superseded replica aborts, the new
    holder proceeds (the run resumes there from durable state), and the two never
    execute one ``(user, task)`` concurrently.
    """

    def __init__(self, lock: PostgresTaskLock, user_id: str, task_id: str) -> None:
        self._lock = lock
        self._user_id = user_id
        self._task_id = task_id
        self._local_cm: Any = None
        self._hb: asyncio.Task | None = None
        self._owner_task: asyncio.Task | None = None
        self.lost = False  # set True if the lease was lost mid-hold (observability)

    async def __aenter__(self) -> "_LeaseHold":
        # 1. same-replica fast path: hold the local lock first (cheap, no DB).
        self._local_cm = self._lock._local.hold(self._user_id, self._task_id)
        await self._local_cm.__aenter__()
        try:
            # 2. cross-replica claim: poll until the lease is ours (free or expired).
            await self._lock._ensure()
            while not await self._lock._try_claim(self._user_id, self._task_id):
                await asyncio.sleep(self._lock._poll_s)
            # 3. keep it alive while the run executes; the heartbeat cancels THIS task
            # (the run body) if it ever loses the lease.
            self._owner_task = asyncio.current_task()
            self._hb = asyncio.create_task(self._heartbeat())
        except BaseException:
            # never leave the local lock held if the claim path failed
            await self._local_cm.__aexit__(None, None, None)
            raise
        return self

    async def _heartbeat(self) -> None:
        """Renew every ``heartbeat_s``; on loss, cancel the run task (fencing the work).

        A renew that returns ``False`` means the row is no longer ours (the lease was
        stolen after expiry) — an immediate, definite loss. A renew that RAISES is a
        transient blip: retry after a short backoff, and only declare loss after
        ``max_renew_failures`` consecutive failures (never silently forever — LAW 4).
        """
        lock = self._lock
        fails = 0
        while True:
            await asyncio.sleep(lock._heartbeat_s)
            try:
                still_ours = await lock._renew(self._user_id, self._task_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # transient — retry fast, don't burn the TTL slack
                fails += 1
                logger.warning(
                    "task lease renew failed (%d/%d) for (%s, %s): %s",
                    fails, lock._max_renew_failures,
                    self._user_id, self._task_id, exc,
                )
                if fails >= lock._max_renew_failures:
                    self._declare_lost("renew failed repeatedly")
                    return
                await asyncio.sleep(lock._renew_retry_s)
                continue
            fails = 0
            if not still_ours:
                self._declare_lost("lease superseded by another owner")
                return

    def _declare_lost(self, why: str) -> None:
        """Mark the lease lost and cancel the run task so it stops executing work it no
        longer owns (LAW 4: the loss is user-visible via the run's cancellation)."""
        self.lost = True
        logger.warning(
            "task lease LOST for (%s, %s): %s — cancelling the run",
            self._user_id, self._task_id, why,
        )
        if self._owner_task is not None and not self._owner_task.done():
            self._owner_task.cancel()

    async def __aexit__(self, *exc: Any) -> None:
        # Cancel + await the heartbeat FIRST, so no renew can fire after the release and
        # resurrect a lease we intend to drop.
        if self._hb is not None:
            self._hb.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._hb
            self._hb = None
        # Owner-guarded release: a no-op if the lease was already lost (someone else
        # owns the row now), so we never delete the new holder's lease.
        with contextlib.suppress(Exception):
            await self._lock._release(self._user_id, self._task_id)
        await self._local_cm.__aexit__(*exc)


def build_task_lock(
    cfg: "HarnessApiConfig", *, owner_id: str | None = None
) -> InProcessTaskLock | PostgresTaskLock:
    """Construct the ``(user, task)`` lock from the tier switch.

    local backend is fine for a single container; Postgres is required to run more than
    one replica. ``state.backend == "postgres"`` ⇒ the distributed
    :class:`PostgresTaskLock` (DSN-deferred; connects lazily on first hold). Otherwise
    the in-process :class:`InProcessTaskLock` (today's per-key ``asyncio.Lock``).
    """
    if cfg.state.is_postgres:
        return PostgresTaskLock(cfg.state.dsn, owner_id=owner_id)
    return InProcessTaskLock()


__all__ = [
    "InProcessTaskLock",
    "PostgresTaskLock",
    "build_task_lock",
]
