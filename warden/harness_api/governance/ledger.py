"""M2 3d — the reservation ledger (the N10 fix's durable core).

The N10 bug: the retired ``SpendTracker.over_budget`` gate was *allow-first* — it only
rejected a user whose ALREADY-accumulated spend had crossed the
cap, so the very first run of any size is always admitted (a single oversized run defeats
the budget). The reservation model fixes this by **reserving the worst-case cost BEFORE
the call** and settling to the actual afterward: a run that cannot be afforded up front is
rejected up front.

A :class:`ReservationLedger` is the atomic accountant. Its unit of contention is one
``tenant_budget_id`` (the billing entity): concurrent ``reserve()`` calls on the same
tenant SERIALIZE, and the one that would cross the remaining balance to zero is rejected —
no double-spend (GOV-4).

The durable opening balance is INJECTED (a :class:`BalanceSource`, GOV-5): the ledger holds
only the *in-flight* holds for this process; the remaining budget of record lives in the
product DB, so a process restart cannot zero a tenant's prior spend.

Two implementations:
  * :class:`InMemoryReservationLedger` — the default, an ``asyncio.Lock`` making the
    read-remaining → decrement critical section atomic. Hermetic; needs no DB driver.
  * :class:`~warden.harness_api.governance.postgres_ledger.PostgresReservationLedger`
    — a ``SELECT … FOR UPDATE`` row-lock ledger for multi-process deploys (lazy ``asyncpg``).

Money unit: **float USD** throughout (consistent with ``cost_usd``, which gives
sub-cent precision). The design doc's "cents" is a future billing-serialization
detail, not this ledger's unit.

Reservation granularity: **run-level worst-case held up front**, settled to actual after.
The finer alternative — re-reserving per turn as the run's true cost is revealed — is
deferred; it would let a run start on a small hold and top up, at the cost of more ledger
round-trips and a mid-run rejection path. Run-level is coarser but simpler and never
mid-run rejects a run it already admitted.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

ReservationStatus = Literal["reserved", "settled", "released", "expired"]


@dataclass
class Reservation:
    """One reservation's full record (mirrors the Postgres ``reservations`` row).

    ``reserved_cost_usd is None`` marks a *time/turn-only* reservation for an
    unpriced provider (a local/Ollama model): it holds no dollars but still occupies
    a slot so the run is admitted only while the tenant has headroom.
    """

    reservation_id: str
    user_id: str
    task_id: str
    tenant_budget_id: str
    reserved_cost_usd: float | None
    actual_cost_usd: float | None
    status: ReservationStatus
    deadline_at: float | None
    created_at: float
    settled_at: float | None
    provider: str
    pricing_shape: str  # "usd" (a dollar hold) or "time_only" (unpriced provider)


@runtime_checkable
class BalanceSource(Protocol):
    """The DURABLE remaining budget read from the product DB (GOV-5).

    Injected so a fresh process (after a restart) sees a tenant's real remaining
    budget, never a zeroed in-memory total. The ledger subtracts its own in-flight
    holds from THIS number to compute headroom.
    """

    async def opening_balance_usd(self, tenant_budget_id: str) -> float: ...


@dataclass
class StaticBalanceSource:
    """A trivial :class:`BalanceSource` double: a fixed ``tenant → USD`` map.

    Used in tests and as a stand-in when the durable balance is a constant (a
    tenant not present ⇒ ``0.0`` remaining). Real deploys inject a DB-backed source.
    """

    balances: dict[str, float] = field(default_factory=dict)

    async def opening_balance_usd(self, tenant_budget_id: str) -> float:
        return self.balances.get(tenant_budget_id, 0.0)


@runtime_checkable
class ReservationLedger(Protocol):
    """Atomic worst-case reservation accounting per ``tenant_budget_id``."""

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
        """Atomically hold ``min(remaining, worst_case_usd)`` for one run.

        ``remaining = opening_balance_usd - committed(tenant)``. Returns the
        :class:`Reservation` on success, or ``None`` when there is no headroom
        (``remaining <= 0``) → the caller emits ``stop(reason="budget")``. A
        ``worst_case_usd=None`` (unpriced) reservation still succeeds while
        ``remaining > 0`` but holds no dollars (``pricing_shape="time_only"``).
        """
        ...

    async def settle(self, reservation_id: str, actual_cost_usd: float) -> None:
        """Replace the hold with the actual cost (idempotent).

        Adjusts committed by ``actual - held``, sets status ``settled`` +
        ``settled_at``. Calling past the first settle is a no-op.
        """
        ...

    async def release(self, reservation_id: str) -> None:
        """Release the whole hold (``committed -= held``), status ``released``. Idempotent."""
        ...

    async def sweep(self, now: float) -> int:
        """Reclaim every still-``reserved`` reservation past its deadline (crash backstop).

        For each reservation with ``deadline_at is not None and deadline_at <= now``:
        release its hold and set status ``expired``. Returns the count swept. Leaked
        holds (a run that crashed without settling) are the #1 reservation failure mode.
        """
        ...


class InMemoryReservationLedger:
    """Default ledger: a single ``asyncio.Lock`` makes reserve atomic (GOV-4).

    Concurrent ``reserve()`` on one ``tenant_budget_id`` serialize through the lock,
    so the read-remaining → decrement is a critical section: N callers race, and the
    one that would cross zero gets ``None`` — no double-spend. ``committed`` per tenant
    is the sum of active holds plus settled actuals.

    Ephemeral by design: the durable balance is the injected
    :class:`BalanceSource`; this only tracks in-flight holds for the current process.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        # Per-tenant committed USD (active holds + settled actuals).
        self._committed: dict[str, float] = {}
        # All reservations by id (kept after settle/release for idempotency + audit).
        self._reservations: dict[str, Reservation] = {}
        # Monotonic id counter (avoids uuid4 randomness — deterministic ids).
        self._seq = 0

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
        import time

        async with self._lock:
            committed = self._committed.get(tenant_budget_id, 0.0)
            remaining = opening_balance_usd - committed
            if remaining <= 0:
                return None  # no headroom ⇒ caller stops with reason="budget"

            if worst_case_usd is None:
                held = 0.0
                pricing_shape = "time_only"
            else:
                # Never hold more than what remains: a worst case larger than the
                # balance still admits the run but caps the hold at the balance.
                held = min(remaining, worst_case_usd)
                pricing_shape = "usd"

            self._seq += 1
            reservation_id = f"resv-{self._seq}"
            now = time.time()
            reservation = Reservation(
                reservation_id=reservation_id,
                user_id=user_id,
                task_id=task_id,
                tenant_budget_id=tenant_budget_id,
                reserved_cost_usd=None if worst_case_usd is None else held,
                actual_cost_usd=None,
                status="reserved",
                deadline_at=deadline_at,
                created_at=now,
                settled_at=None,
                provider=provider,
                pricing_shape=pricing_shape,
            )
            self._reservations[reservation_id] = reservation
            self._committed[tenant_budget_id] = committed + held
            return reservation

    async def settle(self, reservation_id: str, actual_cost_usd: float) -> None:
        import time

        async with self._lock:
            reservation = self._reservations.get(reservation_id)
            if reservation is None or reservation.status != "reserved":
                return  # idempotent: only a live reservation settles
            held = reservation.reserved_cost_usd or 0.0
            # Swap the hold for the actual on the tenant's committed total.
            self._committed[reservation.tenant_budget_id] = (
                self._committed.get(reservation.tenant_budget_id, 0.0)
                - held
                + actual_cost_usd
            )
            reservation.actual_cost_usd = actual_cost_usd
            reservation.status = "settled"
            reservation.settled_at = time.time()

    async def release(self, reservation_id: str) -> None:
        async with self._lock:
            reservation = self._reservations.get(reservation_id)
            if reservation is None or reservation.status != "reserved":
                return  # idempotent
            held = reservation.reserved_cost_usd or 0.0
            self._committed[reservation.tenant_budget_id] = (
                self._committed.get(reservation.tenant_budget_id, 0.0) - held
            )
            reservation.status = "released"

    async def sweep(self, now: float) -> int:
        swept = 0
        async with self._lock:
            for reservation in self._reservations.values():
                if reservation.status != "reserved":
                    continue
                if reservation.deadline_at is None or reservation.deadline_at > now:
                    continue
                held = reservation.reserved_cost_usd or 0.0
                self._committed[reservation.tenant_budget_id] = (
                    self._committed.get(reservation.tenant_budget_id, 0.0) - held
                )
                reservation.status = "expired"
                swept += 1
        return swept


__all__ = [
    "ReservationStatus",
    "Reservation",
    "BalanceSource",
    "StaticBalanceSource",
    "ReservationLedger",
    "InMemoryReservationLedger",
]
