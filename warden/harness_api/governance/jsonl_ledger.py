"""M2 3g.1 — a durable, append-only JSONL credit balance ledger (GOV-5).

The reservation ledger (:mod:`.ledger`) holds only the *in-flight* holds of the
current process; the DURABLE remaining balance of record must survive a restart. In
a real deploy that balance lives in the product DB (a :class:`BillingBackend` such as
Lago). :class:`JsonlBalanceLedger` is the DEFAULT durable backend for deploys with no
external billing platform: a single append-only JSONL file, event-sourced, that
implements BOTH the :class:`~warden.harness_api.governance.ledger.BalanceSource`
Protocol (durable opening balance) AND the
:class:`~warden.harness_api.governance.billing.BillingBackend` Protocol
(the settle→debit usage sink + a credit/top-up path).

Design (LOCKED):

  * **Append-only, event-sourced.** The file is NEVER rewritten. Each mutation appends
    ONE JSON line — a ``credit`` or a ``debit`` event. The in-memory balance map is a
    materialized *fold* replayed from the file on :meth:`load`. A crash can only lose
    the last (partial) line, never corrupt prior state.
  * **Single-writer within one process.** One :class:`asyncio.Lock` guards the
    append+fold critical section, so concurrent credits/debits serialize (no lost
    updates). Multi-process safety is the future DB swap — out of scope here (no file
    locking).
  * **Idempotent credit.** ``credit`` is a no-op past the first for a given
    ``txn_id`` (a repeated recharge cannot double-credit). Seen txn ids are persisted
    in the credit events, so replay stays idempotent.

Money unit: **float USD** throughout (consistent with the ledger + billing seam).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

from warden.harness_api.governance.billing import UsageRecord

logger = logging.getLogger(__name__)


class JsonlBalanceLedger:
    """A durable per-tenant credit balance backed by an append-only JSONL file.

    Implements both :class:`BalanceSource` (``opening_balance_usd``) and
    :class:`BillingBackend` (``record_usage`` / ``opening_balance_usd`` /
    ``supports_topup``), plus a :meth:`credit` top-up path. The in-memory balance is a
    fold over the file's events, replayed once at startup via :meth:`load`.
    """

    def __init__(self, path: Path, *, supports_topup: bool = True) -> None:
        self._path = Path(path)
        self._supports_topup = supports_topup
        self._lock = asyncio.Lock()
        # Materialized fold: current balance per tenant, and the credit txn ids
        # already applied (for idempotent recharge across restarts).
        self._balances: dict[str, float] = {}
        self._seen_txns: set[str] = set()

    async def load(self) -> None:
        """Replay the JSONL file, folding events into memory (idempotent).

        Safe to call once at startup. A missing file ⇒ an empty ledger (balance 0).
        A corrupt/partial last line is logged and skipped (LAW 4: one bad tail line
        must not crash the whole ledger); all prior events still fold.
        """
        async with self._lock:
            self._balances = {}
            self._seen_txns = set()
            if not self._path.exists():
                return
            with self._path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except (ValueError, json.JSONDecodeError):
                        logger.warning(
                            "JsonlBalanceLedger: skipping corrupt line in %s: %r",
                            self._path,
                            line[:200],
                        )
                        continue
                    self._apply(event)

    def _apply(self, event: dict) -> None:
        """Fold one already-parsed event into the in-memory state."""
        etype = event.get("type")
        tenant = event.get("tenant")
        if etype == "credit":
            txn_id = event.get("txn_id")
            if txn_id is not None and txn_id in self._seen_txns:
                return  # idempotent: a repeated txn_id was already applied
            if txn_id is not None:
                self._seen_txns.add(txn_id)
            usd = float(event.get("usd", 0.0))
            self._balances[tenant] = self._balances.get(tenant, 0.0) + usd
        elif etype == "debit":
            usd = float(event.get("usd", 0.0))
            self._balances[tenant] = self._balances.get(tenant, 0.0) - usd
        # Unknown event types are ignored (forward-compat: a newer writer may add
        # event kinds a reader does not yet understand).

    def _append(self, event: dict) -> None:
        """Append one event as a JSON line, creating parent dirs on first write."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event) + "\n")

    async def credit(self, tenant_budget_id: str, usd: float, txn_id: str) -> None:
        """Credit a tenant's balance (a top-up), idempotent on ``txn_id``.

        A repeated ``txn_id`` is a no-op past the first (durably, via the persisted
        seen-set replayed on :meth:`load`). Append then fold, under the lock.
        """
        async with self._lock:
            if txn_id in self._seen_txns:
                return  # already applied — idempotent recharge
            event = {
                "type": "credit",
                "tenant": tenant_budget_id,
                "usd": float(usd),
                "txn_id": txn_id,
                "ts": time.time(),
            }
            self._append(event)
            self._apply(event)

    async def record_usage(self, record: UsageRecord) -> None:
        """The :class:`BillingBackend` debit — the settle→debit path.

        Appends a debit event (``usd = record.cost_usd``, ``ref = record.task_id``)
        and decrements the tenant balance. Unlike ``NullBillingBackend`` this DOES
        persist. Only genuine I/O errors are swallowed + logged (a debit must not fail
        a run that already ran); a normal debit succeeds.
        """
        try:
            async with self._lock:
                event = {
                    "type": "debit",
                    "tenant": record.tenant_budget_id,
                    "usd": float(record.cost_usd),
                    "ref": record.task_id,
                    "provider": record.provider,
                    "model": record.model or "",
                    "ts": time.time(),
                }
                self._append(event)
                self._apply(event)
        except OSError:
            logger.warning(
                "JsonlBalanceLedger.record_usage I/O error for tenant %s (swallowed)",
                record.tenant_budget_id,
                exc_info=True,
            )

    async def opening_balance_usd(self, tenant_budget_id: str) -> float:
        """The current materialized balance (0.0 for an unknown tenant)."""
        return self._balances.get(tenant_budget_id, 0.0)

    async def supports_topup(self, tenant_budget_id: str) -> bool:
        """Whether this ledger offers a top-up path (the configured flag).

        Defaults to ``True`` — a JSONL ledger HAS a credit path.
        """
        return self._supports_topup


__all__ = ["JsonlBalanceLedger"]
