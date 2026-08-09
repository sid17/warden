"""M2 3d — the Postgres reservation ledger (multi-process, row-lock atomic).

The in-memory ledger (:class:`~warden.harness_api.governance.ledger.InMemoryReservationLedger`)
serializes reservations within ONE process. A multi-process / multi-replica deploy needs the
atomicity to live in the DB: this ledger holds the reservation critical section as a Postgres
transaction with ``SELECT … FOR UPDATE`` on the tenant's committed row, so concurrent
``reserve()`` calls across processes serialize on the row lock exactly as the in-memory lock
serializes them in-process — the one that would cross zero is rejected, no double-spend (GOV-4).

``asyncpg`` is imported LAZILY (inside ``__init__``/methods) and is an OPTIONAL extra
(``pip install 'engines-harness[postgres]'`` / ``uv sync --extra postgres``): this module
must import WITHOUT ``asyncpg`` installed so the hermetic in-memory tests never require a DB
driver. It is exercised on the Docker bed, not in the unit suite; there are deliberately no
live-DB tests here.

Schema (mirrors :class:`~warden.harness_api.governance.ledger.Reservation`):
  * ``reservations`` — one row per reservation.
  * ``tenant_budgets`` — one row per tenant carrying the in-flight ``committed`` USD; this is
    the row locked ``FOR UPDATE``. The DURABLE opening balance is passed IN (GOV-5, from the
    product DB via the :class:`~warden.harness_api.governance.ledger.BalanceSource`);
    this table tracks only in-flight holds, exactly like the in-memory ledger's ``_committed``.
"""

from __future__ import annotations

import time
from typing import Any

from warden.harness_api.governance.ledger import Reservation

_ASYNCPG_HINT = (
    "PostgresReservationLedger requires asyncpg. Install the optional extra: "
    "`uv sync --extra postgres` (or `pip install 'engines-harness[postgres]'`)."
)

# --- DDL: idempotent, mirrors the Reservation dataclass -----------------------

_DDL = """
CREATE TABLE IF NOT EXISTS tenant_budgets (
    id         TEXT PRIMARY KEY,
    committed  DOUBLE PRECISION NOT NULL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS reservations (
    reservation_id    TEXT PRIMARY KEY,
    user_id           TEXT NOT NULL,
    task_id           TEXT NOT NULL,
    tenant_budget_id  TEXT NOT NULL,
    reserved_cost_usd DOUBLE PRECISION,      -- NULL ⇒ time_only (unpriced provider)
    actual_cost_usd   DOUBLE PRECISION,
    status            TEXT NOT NULL,          -- reserved|settled|released|expired
    deadline_at       DOUBLE PRECISION,
    created_at        DOUBLE PRECISION NOT NULL,
    settled_at        DOUBLE PRECISION,
    provider          TEXT NOT NULL,
    pricing_shape     TEXT NOT NULL           -- usd|time_only
);
"""


def _require_asyncpg() -> Any:
    """Import ``asyncpg`` lazily with a clear install hint (LAW 4: no silent failure)."""
    try:
        import asyncpg  # noqa: PLC0415  (lazy by design — optional extra)
    except ImportError as exc:  # pragma: no cover - exercised on the bed
        raise ImportError(_ASYNCPG_HINT) from exc
    return asyncpg


class PostgresReservationLedger:
    """Row-lock reservation ledger. Satisfies the ``ReservationLedger`` Protocol.

    Pass an ``asyncpg`` pool (or a dsn to build one via :meth:`connect`). All atomicity
    comes from ``SELECT … FOR UPDATE`` on the tenant's ``committed`` row inside a
    transaction, so reserve is a serialized read-modify-write across processes.
    """

    def __init__(self, pool: Any) -> None:
        # Fail loudly at construction if the driver is absent even when a pool-like
        # object was handed in — keeps the "must be importable without asyncpg" rule
        # (the import here is lazy) while never running against a half-present driver.
        _require_asyncpg()
        self._pool = pool

    @classmethod
    async def connect(cls, dsn: str, *, min_size: int = 1, max_size: int = 10) -> "PostgresReservationLedger":
        """Build a pool from a DSN and ensure the schema exists."""
        asyncpg = _require_asyncpg()
        pool = await asyncpg.create_pool(dsn, min_size=min_size, max_size=max_size)
        ledger = cls(pool)
        await ledger.ensure_schema()
        return ledger

    async def ensure_schema(self) -> None:
        """Create the ``reservations`` + ``tenant_budgets`` tables if absent."""
        async with self._pool.acquire() as conn:
            await conn.execute(_DDL)

    async def reserve(
        self,
        *,
        user_id: str,
        task_id: str,
        tenant_budget_id: str,
        worst_case_usd: float | None,
        opening_balance_usd: float,
        deadline_at: float | None,
        provider: str,
    ) -> Reservation | None:
        now = time.time()
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                # Lock (or create) the tenant's committed row for the duration of the
                # transaction so a concurrent reserve blocks here rather than racing.
                await conn.execute(
                    "INSERT INTO tenant_budgets (id, committed) VALUES ($1, 0.0) "
                    "ON CONFLICT (id) DO NOTHING",
                    tenant_budget_id,
                )
                committed = await conn.fetchval(
                    "SELECT committed FROM tenant_budgets WHERE id = $1 FOR UPDATE",
                    tenant_budget_id,
                )
                committed = float(committed or 0.0)
                remaining = opening_balance_usd - committed
                if remaining <= 0:
                    return None  # no headroom ⇒ caller stops with reason="budget"

                if worst_case_usd is None:
                    held: float = 0.0
                    reserved_cost_usd: float | None = None
                    pricing_shape = "time_only"
                else:
                    held = min(remaining, worst_case_usd)
                    reserved_cost_usd = held
                    pricing_shape = "usd"

                # A DB-side unique id: sequence-free, collision-proof under the row lock.
                reservation_id = await conn.fetchval("SELECT gen_random_uuid()::text")

                await conn.execute(
                    "UPDATE tenant_budgets SET committed = committed + $1 WHERE id = $2",
                    held,
                    tenant_budget_id,
                )
                await conn.execute(
                    "INSERT INTO reservations (reservation_id, user_id, task_id, "
                    "tenant_budget_id, reserved_cost_usd, actual_cost_usd, status, "
                    "deadline_at, created_at, settled_at, provider, pricing_shape) "
                    "VALUES ($1,$2,$3,$4,$5,NULL,'reserved',$6,$7,NULL,$8,$9)",
                    reservation_id, user_id, task_id, tenant_budget_id,
                    reserved_cost_usd, deadline_at, now, provider, pricing_shape,
                )
                return Reservation(
                    reservation_id=reservation_id,
                    user_id=user_id,
                    task_id=task_id,
                    tenant_budget_id=tenant_budget_id,
                    reserved_cost_usd=reserved_cost_usd,
                    actual_cost_usd=None,
                    status="reserved",
                    deadline_at=deadline_at,
                    created_at=now,
                    settled_at=None,
                    provider=provider,
                    pricing_shape=pricing_shape,
                )

    async def settle(self, reservation_id: str, actual_cost_usd: float) -> None:
        now = time.time()
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                # Only a still-'reserved' row settles ⇒ idempotent (a second call finds
                # no matching row and updates nothing).
                row = await conn.fetchrow(
                    "SELECT tenant_budget_id, reserved_cost_usd FROM reservations "
                    "WHERE reservation_id = $1 AND status = 'reserved' FOR UPDATE",
                    reservation_id,
                )
                if row is None:
                    return
                held = float(row["reserved_cost_usd"] or 0.0)
                await conn.execute(
                    "UPDATE tenant_budgets SET committed = committed - $1 + $2 WHERE id = $3",
                    held, actual_cost_usd, row["tenant_budget_id"],
                )
                await conn.execute(
                    "UPDATE reservations SET status = 'settled', actual_cost_usd = $1, "
                    "settled_at = $2 WHERE reservation_id = $3",
                    actual_cost_usd, now, reservation_id,
                )

    async def release(self, reservation_id: str) -> None:
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT tenant_budget_id, reserved_cost_usd FROM reservations "
                    "WHERE reservation_id = $1 AND status = 'reserved' FOR UPDATE",
                    reservation_id,
                )
                if row is None:
                    return  # idempotent
                held = float(row["reserved_cost_usd"] or 0.0)
                await conn.execute(
                    "UPDATE tenant_budgets SET committed = committed - $1 WHERE id = $2",
                    held, row["tenant_budget_id"],
                )
                await conn.execute(
                    "UPDATE reservations SET status = 'released' WHERE reservation_id = $1",
                    reservation_id,
                )

    async def sweep(self, now: float) -> int:
        """Reclaim expired-but-still-reserved holds (the crash backstop)."""
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                rows = await conn.fetch(
                    "SELECT reservation_id, tenant_budget_id, reserved_cost_usd "
                    "FROM reservations WHERE status = 'reserved' "
                    "AND deadline_at IS NOT NULL AND deadline_at <= $1 FOR UPDATE",
                    now,
                )
                for row in rows:
                    held = float(row["reserved_cost_usd"] or 0.0)
                    await conn.execute(
                        "UPDATE tenant_budgets SET committed = committed - $1 WHERE id = $2",
                        held, row["tenant_budget_id"],
                    )
                    await conn.execute(
                        "UPDATE reservations SET status = 'expired' "
                        "WHERE reservation_id = $1",
                        row["reservation_id"],
                    )
                return len(rows)


__all__ = ["PostgresReservationLedger"]
