"""M2 3f — the swappable billing backend + meter-on-settle.

Hermetic, ``asyncio.run(...)`` style (no pytest-asyncio, no live HTTP). Exercises the
in-memory + null backends, that ``RunGovernor.settle()`` meters the committed run cost
to the billing backend, and that the Lago/Polar skeletons construct + swallow a
transport error injected via a fake httpx client (they are otherwise only exercised
against a live backend on the bed/prod).
"""

import asyncio

from warden.harness_api.credentials.keys import KeyRegistry
from warden.harness_api.governance import (
    GovernorService,
    InMemoryBillingBackend,
    InMemoryReservationLedger,
    LagoBillingBackend,
    NullBillingBackend,
    PolarBillingBackend,
    UsageRecord,
)

_KEYS = KeyRegistry.from_config(
    {
        "keys": {"k1": {"provider": "claude", "secret_env": "S1"}},
        "users": {"u1": {"key_id": "k1", "budget_usd": 100.0}},
    },
    secrets={"S1": "sk-1"},
)


# --- InMemoryBillingBackend: record decrements, grant credits, topup toggles ---

def test_inmemory_records_and_decrements():
    async def _run():
        backend = InMemoryBillingBackend({"t1": 10.0})
        assert await backend.opening_balance_usd("t1") == 10.0
        await backend.record_usage(
            UsageRecord(
                tenant_budget_id="t1", user_id="u1", task_id="task",
                cost_usd=2.5, provider="claude", model="claude-opus-4-8",
            )
        )
        assert len(backend.records) == 1
        assert backend.records[0].cost_usd == 2.5
        assert await backend.opening_balance_usd("t1") == 7.5

    asyncio.run(_run())


def test_inmemory_grant_and_topup_toggle():
    async def _run():
        backend = InMemoryBillingBackend(
            {"t1": 1.0}, supports_topup=False,
            topup_by_tenant={"paid": True},
        )
        assert await backend.supports_topup("t1") is False  # global default
        assert await backend.supports_topup("paid") is True  # per-tenant override
        backend.grant("t1", 5.0)
        assert await backend.opening_balance_usd("t1") == 6.0

    asyncio.run(_run())


# --- NullBillingBackend: no-op, uncapped, no topup ------------------------

def test_null_backend_uncapped_no_topup():
    async def _run():
        backend = NullBillingBackend()
        assert await backend.opening_balance_usd("anyone") == float("inf")
        assert await backend.supports_topup("anyone") is False
        # record_usage no-ops and does not raise.
        await backend.record_usage(
            UsageRecord(
                tenant_budget_id="t", user_id="u", task_id="k",
                cost_usd=9.0, provider="claude", model=None,
            )
        )

    asyncio.run(_run())


def test_null_backend_configured_constant_balance():
    async def _run():
        backend = NullBillingBackend(opening_balance_usd=42.0)
        assert await backend.opening_balance_usd("t") == 42.0

    asyncio.run(_run())


# --- RunGovernor.settle() meters a UsageRecord to the billing backend ------

def test_settle_meters_committed_cost_to_billing():
    async def _run():
        billing = InMemoryBillingBackend({"u1": 100.0}, supports_topup=True)
        service = GovernorService(
            key_registry=_KEYS,
            ledger=InMemoryReservationLedger(),
            billing=billing,  # serves as balance_source too (GOV-5)
        )
        gov = await service.resolve(
            user_id="u1", task_id="course_A",
            provider="claude", model="claude-opus-4-8",
        )
        assert gov.pausable is True  # supports_topup True ⇒ pausable
        assert gov.tenant_budget_id == "u1"
        # Make the reservation, then commit a turn cost by settling to an explicit
        # actual — the meter must record that exact cost.
        await gov.check("pre_flight", _usage(), 0.0)
        await gov.settle(actual_cost_usd=0.0375)
        assert len(billing.records) == 1
        rec = billing.records[0]
        assert rec.cost_usd == 0.0375
        assert rec.tenant_budget_id == "u1"
        assert rec.provider == "claude"

    asyncio.run(_run())


def test_settle_without_billing_does_not_meter():
    async def _run():
        from warden.harness_api.governance import StaticBalanceSource

        service = GovernorService(
            key_registry=_KEYS,
            ledger=InMemoryReservationLedger(),
            balance_source=StaticBalanceSource({"u1": 100.0}),
        )
        gov = await service.resolve(
            user_id="u1", task_id="c", provider="claude", model="claude-opus-4-8",
        )
        assert gov.pausable is False  # no billing ⇒ never pausable
        await gov.check("pre_flight", _usage(), 0.0)
        # No billing backend present ⇒ settle just hits the ledger, nothing to assert
        # beyond it not raising.
        await gov.settle(actual_cost_usd=0.01)

    asyncio.run(_run())


# --- Lago/Polar skeletons: construct + swallow a transport error -----------

class _RaisingClient:
    """A fake httpx client whose calls raise — proves the meter swallows."""

    async def post(self, *args, **kwargs):
        raise RuntimeError("transport down")

    async def get(self, *args, **kwargs):
        raise RuntimeError("transport down")


def test_lago_constructs_and_swallows_transport_error():
    async def _run():
        backend = LagoBillingBackend(
            base_url="https://lago.example", api_key="key",
            client=_RaisingClient(),
        )
        assert await backend.supports_topup("t") is True
        # record_usage must NOT propagate the transport error (meter never gates).
        await backend.record_usage(
            UsageRecord(
                tenant_budget_id="t", user_id="u", task_id="k",
                cost_usd=1.0, provider="claude", model="m",
            )
        )
        # opening_balance falls back to 0.0 on a read error.
        assert await backend.opening_balance_usd("t") == 0.0

    asyncio.run(_run())


def test_polar_constructs_and_swallows_transport_error():
    async def _run():
        backend = PolarBillingBackend(
            base_url="https://polar.example", api_key="key",
            client=_RaisingClient(),
        )
        assert await backend.supports_topup("t") is True
        await backend.record_usage(
            UsageRecord(
                tenant_budget_id="t", user_id="u", task_id="k",
                cost_usd=1.0, provider="claude", model="m",
            )
        )
        assert await backend.opening_balance_usd("t") == 0.0

    asyncio.run(_run())


def _usage():
    from warden.schemas.usage import Usage

    return Usage()
