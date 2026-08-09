"""M2 3f — the swappable billing backend (own the ledger, RENT the billing).

The reservation ledger (:mod:`.ledger`) is the harness's OWN in-flight accountant:
it holds worst-case reservations for the current process and settles them to actuals.
A **billing backend** is a different thing entirely — it is the *durable* product-side
credit system (self-hosted Lago, hosted Polar, or a null/dev double), and the harness
merely RENTS it: it is a swappable dependency of the :class:`~warden.harness_api.governance.governor.GovernorService`,
**never of the engine** (GOV-1).

A billing backend plays TWO roles:

  1. A durable **usage sink** — after a run settles, its committed cost is metered to
     the billing system via :meth:`BillingBackend.record_usage`. This is **meter → signal
     only**: it NEVER gates in the request path. The authoritative in-flight accounting
     is the reservation ledger's ``settle``; the meter is a downstream signal that the
     billing platform totals for invoicing/credit-drawdown. An implementation therefore
     catches and logs its own transport errors and NEVER raises into the run path — a
     billing outage must not fail a run that already ran.

  2. A **BalanceSource** (GOV-5) — a billing backend can report a tenant's durable
     remaining credit via :meth:`BillingBackend.opening_balance_usd`, so ONE object can
     be handed to the Governor as both its ``billing`` sink AND its ``balance_source``.

Pause-not-fail (GOV-6) hangs off :meth:`BillingBackend.supports_topup`: when a tenant's
billing platform has a credit-grant path, a budget stop can PAUSE (await a top-up) instead
of hard-failing — the run is re-admittable once the durable balance is restored.

Money unit: **float USD** throughout (consistent with the ledger and ``cost_usd``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@dataclass
class UsageRecord:
    """One settled run's cost, metered to the durable billing system.

    Assembled by :meth:`~warden.harness_api.governance.governor.RunGovernor.settle`
    from the run's identity + committed cost, then handed to
    :meth:`BillingBackend.record_usage`.
    """

    tenant_budget_id: str
    user_id: str
    task_id: str
    cost_usd: float
    provider: str
    model: str | None


@runtime_checkable
class BillingBackend(Protocol):
    """The durable product-side billing system (also a ``BalanceSource``).

    Implementations own the transport to a real credit platform. All three methods
    are async so a backend may make a network round-trip without changing the seam.
    """

    async def record_usage(self, record: UsageRecord) -> None:
        """Meter a settled run's cost to the durable billing system.

        Fire-and-forget from the caller's view: an implementation MUST catch + log
        its own transport errors and NEVER raise into the run path (meter → signal,
        never gates). The reservation ledger's ``settle`` is the authoritative in-flight
        accounting; this is the downstream signal the billing platform totals.
        """
        ...

    async def opening_balance_usd(self, tenant_budget_id: str) -> float:
        """The durable remaining credit for a tenant (GOV-5).

        Lets a :class:`BillingBackend` be passed directly as the
        :class:`~warden.harness_api.governance.governor.GovernorService`'s
        ``balance_source`` — one object serving both roles.
        """
        ...

    async def supports_topup(self, tenant_budget_id: str) -> bool:
        """Whether a top-up / credit-grant path exists for this tenant (GOV-6).

        Drives pause-not-fail: a budget stop can PAUSE (await a top-up) rather than
        hard-fail only when the tenant's billing platform can be topped up.
        """
        ...


class NullBillingBackend:
    """The default backend: uncapped, no-op, no top-up.

    Ungoverned / hobby deploys use this. ``record_usage`` no-ops (nothing to meter);
    ``opening_balance_usd`` returns a configured constant that defaults to
    ``float("inf")`` — an "uncapped" balance so a Null backend NEVER blocks a run at
    the reservation (there is always headroom); ``supports_topup`` is ``False`` (a
    Null backend has no credit-grant path, so a budget stop can never pause).
    """

    def __init__(self, opening_balance_usd: float = float("inf")) -> None:
        self._opening = opening_balance_usd

    async def record_usage(self, record: UsageRecord) -> None:
        return None

    async def opening_balance_usd(self, tenant_budget_id: str) -> float:
        return self._opening

    async def supports_topup(self, tenant_budget_id: str) -> bool:
        return False


class InMemoryBillingBackend:
    """Dev/test double: an in-process credit ledger with a top-up path.

    Holds per-tenant ``balances`` and an append log of ``records``. ``record_usage``
    appends the record AND decrements the tenant's balance by ``cost_usd`` (modeling a
    credit drawdown); ``opening_balance_usd`` returns the current balance;
    ``supports_topup`` returns a configurable bool (a per-tenant override, else the
    global default). :meth:`grant` models a top-up by crediting a tenant.
    """

    def __init__(
        self,
        balances: dict[str, float] | None = None,
        *,
        supports_topup: bool = False,
        topup_by_tenant: dict[str, bool] | None = None,
    ) -> None:
        self.balances: dict[str, float] = dict(balances) if balances else {}
        self.records: list[UsageRecord] = []
        self._topup_default = supports_topup
        self._topup_by_tenant: dict[str, bool] = (
            dict(topup_by_tenant) if topup_by_tenant else {}
        )

    async def record_usage(self, record: UsageRecord) -> None:
        self.records.append(record)
        current = self.balances.get(record.tenant_budget_id, 0.0)
        self.balances[record.tenant_budget_id] = current - record.cost_usd

    async def opening_balance_usd(self, tenant_budget_id: str) -> float:
        return self.balances.get(tenant_budget_id, 0.0)

    async def supports_topup(self, tenant_budget_id: str) -> bool:
        return self._topup_by_tenant.get(tenant_budget_id, self._topup_default)

    def grant(self, tenant_budget_id: str, usd: float) -> None:
        """Credit a tenant's balance (models a top-up / credit grant)."""
        self.balances[tenant_budget_id] = (
            self.balances.get(tenant_budget_id, 0.0) + usd
        )


class LagoBillingBackend:
    """SKELETON — self-hosted Lago (the default real backend).

    Lago is the OSS metering/billing platform. Usage is metered as *events*
    (``precise_total_amount_cents``) against a customer's wallet; the wallet's
    ``ongoing_balance`` is the durable remaining credit. Credit grants (top-ups) are a
    first-class Lago concept, so ``supports_topup`` is ``True``.

    ``httpx`` is imported LAZILY inside methods (it is a harness dep, but keeping the
    import lazy means merely importing this module never touches the network stack and
    a backend that is never used costs nothing). Exercised against a LIVE Lago on the
    bed/prod — NOT in the unit suite; there are deliberately no live-HTTP tests here.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        client: "object | None" = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        # Optional injected httpx.AsyncClient (tests inject a fake that raises to
        # prove record_usage swallows transport errors). None ⇒ build one lazily.
        self._client = client

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"}

    async def _get_client(self):
        if self._client is not None:
            return self._client
        import httpx  # lazy: no network stack touched at import time

        self._client = httpx.AsyncClient()
        return self._client

    async def record_usage(self, record: UsageRecord) -> None:
        # Meter → signal: POST a usage event to Lago. Any transport error is logged
        # and SWALLOWED — a billing outage must never fail a run that already ran.
        try:
            client = await self._get_client()
            # Lago meters money as an event with precise_total_amount_cents.
            payload = {
                "event": {
                    "transaction_id": f"{record.task_id}-{record.tenant_budget_id}",
                    "external_subscription_id": record.tenant_budget_id,
                    "code": "harness_run",
                    "precise_total_amount_cents": str(record.cost_usd * 100.0),
                    "properties": {
                        "provider": record.provider,
                        "model": record.model or "",
                        "user_id": record.user_id,
                    },
                }
            }
            await client.post(
                f"{self._base_url}/api/v1/events",
                headers=self._headers(),
                json=payload,
            )
        except Exception:  # noqa: BLE001 - meter never gates the run path
            logger.warning(
                "Lago record_usage failed for tenant %s (swallowed)",
                record.tenant_budget_id,
                exc_info=True,
            )

    async def opening_balance_usd(self, tenant_budget_id: str) -> float:
        # GET the wallet's ongoing/available balance for the tenant. On any error we
        # fall back to 0.0 (no headroom) — a balance we cannot read is not credit we
        # can spend. (This DOES gate — but it is a read, not the meter path.)
        try:
            client = await self._get_client()
            resp = await client.get(
                f"{self._base_url}/api/v1/wallets",
                headers=self._headers(),
                params={"external_customer_id": tenant_budget_id},
            )
            data = resp.json()
            wallets = data.get("wallets") or []
            if not wallets:
                return 0.0
            wallet = wallets[0]
            # Lago exposes ongoing_balance (settled + in-flight) or balance.
            return float(wallet.get("ongoing_balance", wallet.get("balance", 0.0)))
        except Exception:  # noqa: BLE001
            logger.warning(
                "Lago opening_balance_usd failed for tenant %s", tenant_budget_id,
                exc_info=True,
            )
            return 0.0

    async def supports_topup(self, tenant_budget_id: str) -> bool:
        # Lago wallets have a first-class credit-grant / top-up path.
        return True


class PolarBillingBackend:
    """SKELETON — Polar (hosted Merchant-of-Record fallback).

    Polar is a hosted billing/MoR platform. Usage is metered by ingesting *meter
    events*; a customer's credit balance is the durable remaining credit. Credit grants
    exist, so ``supports_topup`` is ``True``.

    ``httpx`` is imported LAZILY (same rationale as :class:`LagoBillingBackend`).
    Exercised against LIVE Polar on the bed/prod — NOT in the unit suite.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        client: "object | None" = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._client = client

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"}

    async def _get_client(self):
        if self._client is not None:
            return self._client
        import httpx  # lazy

        self._client = httpx.AsyncClient()
        return self._client

    async def record_usage(self, record: UsageRecord) -> None:
        # Meter → signal: ingest a Polar meter event. Errors logged + swallowed.
        try:
            client = await self._get_client()
            payload = {
                "events": [
                    {
                        "name": "harness_run",
                        "external_customer_id": record.tenant_budget_id,
                        "metadata": {
                            "cost_usd": record.cost_usd,
                            "provider": record.provider,
                            "model": record.model or "",
                            "user_id": record.user_id,
                            "task_id": record.task_id,
                        },
                    }
                ]
            }
            await client.post(
                f"{self._base_url}/v1/events/ingest",
                headers=self._headers(),
                json=payload,
            )
        except Exception:  # noqa: BLE001 - meter never gates the run path
            logger.warning(
                "Polar record_usage failed for tenant %s (swallowed)",
                record.tenant_budget_id,
                exc_info=True,
            )

    async def opening_balance_usd(self, tenant_budget_id: str) -> float:
        # GET the customer's credit balance. On error fall back to 0.0.
        try:
            client = await self._get_client()
            resp = await client.get(
                f"{self._base_url}/v1/customers/{tenant_budget_id}/credit",
                headers=self._headers(),
            )
            data = resp.json()
            return float(data.get("balance", 0.0))
        except Exception:  # noqa: BLE001
            logger.warning(
                "Polar opening_balance_usd failed for tenant %s", tenant_budget_id,
                exc_info=True,
            )
            return 0.0

    async def supports_topup(self, tenant_budget_id: str) -> bool:
        # Polar customers have a credit-grant path.
        return True


__all__ = [
    "UsageRecord",
    "BillingBackend",
    "NullBillingBackend",
    "InMemoryBillingBackend",
    "LagoBillingBackend",
    "PolarBillingBackend",
]
