"""M2 3g.1 — the durable JSONL governance stores (balance ledger + task policy).

Hermetic: the "DB" is an append-only JSONL file under a ``TemporaryDirectory`` (no
network, no external billing platform). Async style matches the repo — plain
``def test_...`` with ``asyncio.run(...)`` inside (NOT pytest-asyncio); see
``tests/harness_api/governance/test_governor.py``.

Each test names the behavior it locks (credit/debit fold, idempotent recharge, GOV-5
restart durability, corrupt-tail tolerance, concurrent-append safety, topup flag,
task-policy register/get/remove + replay).
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from warden.harness_api.governance import (
    GovernancePolicy,
    JsonlBalanceLedger,
    JsonlTaskPolicyStore,
    UsageRecord,
)


def _run(coro):
    return asyncio.run(coro)


def _usage(tenant: str, cost: float, task_id: str = "task-1") -> UsageRecord:
    return UsageRecord(
        tenant_budget_id=tenant,
        user_id="u1",
        task_id=task_id,
        cost_usd=cost,
        provider="claude",
        model="claude-opus-4-8",
    )


# === 1. credit / debit + balance ============================================

def test_credit_then_debit_reflects_balance() -> None:
    """credit $10, record_usage a $3 UsageRecord ⇒ balance $7."""

    async def _test() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "balances.jsonl"
            ledger = JsonlBalanceLedger(path)
            await ledger.load()
            await ledger.credit("t1", 10.0, txn_id="grant-1")
            await ledger.record_usage(_usage("t1", 3.0))
            assert abs(await ledger.opening_balance_usd("t1") - 7.0) < 1e-9

    _run(_test())


# === 2. idempotent credit ===================================================

def test_credit_idempotent_on_txn_id() -> None:
    """Same txn_id twice ⇒ credited once."""

    async def _test() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "balances.jsonl"
            ledger = JsonlBalanceLedger(path)
            await ledger.load()
            await ledger.credit("t1", 10.0, txn_id="grant-1")
            await ledger.credit("t1", 10.0, txn_id="grant-1")  # repeat = no-op
            assert abs(await ledger.opening_balance_usd("t1") - 10.0) < 1e-9

    _run(_test())


def test_credit_idempotent_survives_restart() -> None:
    """A repeated txn_id is a no-op even across a fresh load (persisted seen-set)."""

    async def _test() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "balances.jsonl"
            first = JsonlBalanceLedger(path)
            await first.load()
            await first.credit("t1", 10.0, txn_id="grant-1")

            fresh = JsonlBalanceLedger(path)
            await fresh.load()
            await fresh.credit("t1", 10.0, txn_id="grant-1")  # dup seen on replay
            assert abs(await fresh.opening_balance_usd("t1") - 10.0) < 1e-9

    _run(_test())


# === 3. GOV-5 restart replay (durable balance) ==============================

def test_balance_survives_restart() -> None:
    """A NEW ledger on the SAME path sees the real balance after load (GOV-5)."""

    async def _test() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "balances.jsonl"
            first = JsonlBalanceLedger(path)
            await first.load()
            await first.credit("t1", 20.0, txn_id="grant-1")
            await first.record_usage(_usage("t1", 5.0))

            # A "fresh process": a brand-new ledger object on the same file.
            fresh = JsonlBalanceLedger(path)
            await fresh.load()
            assert abs(await fresh.opening_balance_usd("t1") - 15.0) < 1e-9

    _run(_test())


# === 4. corrupt tail line tolerated =========================================

def test_corrupt_tail_line_tolerated() -> None:
    """A garbage trailing line is skipped; prior events still fold, no crash."""

    async def _test() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "balances.jsonl"
            first = JsonlBalanceLedger(path)
            await first.load()
            await first.credit("t1", 12.0, txn_id="grant-1")

            # Append a corrupt (non-JSON) line — simulate a torn write on crash.
            with path.open("a", encoding="utf-8") as handle:
                handle.write('{"type": "credit", "tenant": "t1", "usd": 999,\n')

            fresh = JsonlBalanceLedger(path)
            await fresh.load()  # must not raise
            assert abs(await fresh.opening_balance_usd("t1") - 12.0) < 1e-9

    _run(_test())


# === 5. concurrent credits don't lose updates ===============================

def test_concurrent_credits_no_lost_updates() -> None:
    """N concurrent credits (distinct txn_ids) ⇒ balance == sum (lock serializes)."""

    async def _test() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "balances.jsonl"
            ledger = JsonlBalanceLedger(path)
            await ledger.load()
            n = 25
            await asyncio.gather(
                *[ledger.credit("t1", 2.0, txn_id=f"grant-{i}") for i in range(n)]
            )
            assert abs(await ledger.opening_balance_usd("t1") - n * 2.0) < 1e-9

            # And the durable file agrees on a fresh replay.
            fresh = JsonlBalanceLedger(path)
            await fresh.load()
            assert abs(await fresh.opening_balance_usd("t1") - n * 2.0) < 1e-9

    _run(_test())


# === 6. supports_topup flag honored =========================================

def test_supports_topup_flag_honored() -> None:
    async def _test() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "balances.jsonl"
            default = JsonlBalanceLedger(path)
            assert await default.supports_topup("t1") is True  # default True

            no_topup = JsonlBalanceLedger(
                Path(tmp) / "b2.jsonl", supports_topup=False
            )
            assert await no_topup.supports_topup("t1") is False

    _run(_test())


def test_unknown_tenant_balance_is_zero() -> None:
    async def _test() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = JsonlBalanceLedger(Path(tmp) / "balances.jsonl")
            await ledger.load()
            assert await ledger.opening_balance_usd("nobody") == 0.0

    _run(_test())


# === 7. task policy register / get / remove + restart replay ================

def test_task_policy_register_get_remove() -> None:
    async def _test() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "task_policies.jsonl"
            store = JsonlTaskPolicyStore(path)
            await store.load()
            assert store.get("task-1") is None

            policy = GovernancePolicy(cost_cap_usd=5.0, deadline_s=60.0, max_turns=10)
            await store.register("task-1", policy)
            assert store.get("task-1") == policy

            await store.remove("task-1")
            assert store.get("task-1") is None
            # remove of an absent key is a no-op (no raise).
            await store.remove("task-1")

    _run(_test())


def test_task_policy_register_overrides_earlier() -> None:
    async def _test() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "task_policies.jsonl"
            store = JsonlTaskPolicyStore(path)
            await store.load()
            await store.register("task-1", GovernancePolicy(cost_cap_usd=5.0))
            await store.register("task-1", GovernancePolicy(cost_cap_usd=9.0))
            got = store.get("task-1")
            assert got is not None and got.cost_cap_usd == 9.0

    _run(_test())


def test_task_policy_survives_restart() -> None:
    """register on one instance, load on a fresh instance ⇒ get returns it;
    a persisted remove also survives replay."""

    async def _test() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "task_policies.jsonl"
            first = JsonlTaskPolicyStore(path)
            await first.load()
            policy = GovernancePolicy(cost_cap_usd=7.5, deadline_s=None, max_turns=3)
            await first.register("task-keep", policy)
            await first.register("task-drop", GovernancePolicy(cost_cap_usd=1.0))
            await first.remove("task-drop")

            fresh = JsonlTaskPolicyStore(path)
            await fresh.load()
            got = fresh.get("task-keep")
            assert got is not None
            assert got.cost_cap_usd == 7.5
            assert got.deadline_s is None
            assert got.max_turns == 3
            assert fresh.get("task-drop") is None  # remove persisted

    _run(_test())


def test_task_policy_corrupt_tail_tolerated() -> None:
    async def _test() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "task_policies.jsonl"
            first = JsonlTaskPolicyStore(path)
            await first.load()
            await first.register("task-1", GovernancePolicy(max_turns=4))
            with path.open("a", encoding="utf-8") as handle:
                handle.write("not json at all\n")

            fresh = JsonlTaskPolicyStore(path)
            await fresh.load()  # must not raise
            got = fresh.get("task-1")
            assert got is not None and got.max_turns == 4

    _run(_test())
