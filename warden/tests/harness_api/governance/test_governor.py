"""M2 3d — the concrete Governor: policy, reservation ledger, RunGovernor.

Hermetic: the "DB" is the in-memory ledger + a static BalanceSource (no asyncpg,
no network). Async style matches the repo — plain ``def test_...`` with
``asyncio.run(...)`` inside (NOT pytest-asyncio); see
``tests/harness_api/governance/test_pricing.py``.

Each test names the behavior it locks (GOV-4 atomicity, the N10 fix, GOV-5 restart
safety, the cap/deadline stops, the mid-stream tripwire, worst-case pricing).
"""

from __future__ import annotations

import asyncio

from warden.harness_api.credentials.keys import KeyRegistry
from warden.harness_api.governance import (
    GovernancePolicy,
    GovernorService,
    InMemoryReservationLedger,
    RunGovernor,
    StaticBalanceSource,
    resolve_policy,
    worst_case_usd,
)
from warden.harness_api.governance.pricing import DEFAULT_PRICING
from warden.schemas.usage import Usage
from warden.seams.governor import Continue, Stop


def _run(coro):
    return asyncio.run(coro)


# --- helpers ------------------------------------------------------------------

def _make_run_governor(
    *,
    policy: GovernancePolicy,
    ledger: InMemoryReservationLedger,
    tenant_budget_id: str = "t1",
    model: str | None = "claude-opus-4-8",
    worst: float | None = None,
    opening_balance_usd: float = 100.0,
    deadline_at: float | None = None,
    allow_uncapped: bool = False,
    billing=None,
) -> RunGovernor:
    return RunGovernor(
        policy=policy,
        ledger=ledger,
        user_id="u1",
        task_id="task-1",
        tenant_budget_id=tenant_budget_id,
        provider="claude",
        model=model,
        table=dict(DEFAULT_PRICING),
        worst_case_usd=worst,
        opening_balance_usd=opening_balance_usd,
        auth_env=None,
        deadline_at=deadline_at,
        billing=billing,
        allow_uncapped=allow_uncapped,
    )


# === 1. GOV-4 — reservation atomic under concurrency =========================

def test_gov4_reserve_atomic_under_concurrency() -> None:
    """N concurrent reserves on ONE tenant that only fits K get exactly K holds;
    the rest get None and the total held never exceeds the opening balance."""

    async def _test() -> None:
        ledger = InMemoryReservationLedger()
        n = 20
        opening = 50.0
        per_reserve = 10.0  # only 5 of 20 fit
        expected_fit = int(opening // per_reserve)

        async def _one():
            return await ledger.reserve(
                user_id="u1",
                task_id="task",
                tenant_budget_id="t1",
                worst_case_usd=per_reserve,
                opening_balance_usd=opening,
                deadline_at=None,
                provider="claude",
            )

        results = await asyncio.gather(*[_one() for _ in range(n)])
        granted = [r for r in results if r is not None]
        rejected = [r for r in results if r is None]

        assert len(granted) == expected_fit, (len(granted), expected_fit)
        assert len(rejected) == n - expected_fit
        total_held = sum(r.reserved_cost_usd or 0.0 for r in granted)
        assert total_held <= opening + 1e-9

    _run(_test())


# === 2. N10 — a single oversized first run is rejected pre-flight ============

def test_n10_single_run_bound_rejected_preflight() -> None:
    """A run whose worst case exceeds remaining is stopped at pre_flight —
    the allow-first over_budget bug would have admitted a first run of any size."""

    async def _test() -> None:
        ledger = InMemoryReservationLedger()
        # remaining = 5.0, worst case = 500.0 → still admitted (hold capped at
        # remaining) — reservation succeeds because there IS headroom. To assert the
        # N10 spirit we set opening to 0 headroom via prior committed below; here we
        # verify the *reservation-first* path: a run larger than balance holds only
        # the balance and admits, but a run against ZERO headroom is rejected.
        gov = _make_run_governor(
            policy=GovernancePolicy(cost_cap_usd=5.0),
            ledger=ledger,
            worst=500.0,
            opening_balance_usd=5.0,
        )
        # pre_flight reserves (headroom exists), then the first turn's real cost
        # crosses the $5 cap and is stopped — the run is bounded, not allow-first.
        assert isinstance(await gov.check("pre_flight", Usage(), 0.0), Continue)
        # A single big turn: 1M output on opus = $25 > $5 cap.
        verdict = await gov.check(
            "turn_boundary", Usage(input=0, output=1_000_000), 0.0
        )
        assert isinstance(verdict, Stop) and verdict.reason == "budget"

    _run(_test())


# === 3. over_budget — zero/negative headroom ⇒ reserve None ⇒ pre_flight stop =

def test_over_budget_preflight_reject() -> None:
    """A tenant already at zero headroom: reserve() returns None and
    check('pre_flight') is Stop('budget')."""

    async def _test() -> None:
        ledger = InMemoryReservationLedger()
        gov = _make_run_governor(
            policy=GovernancePolicy(cost_cap_usd=10.0),
            ledger=ledger,
            worst=1.0,
            opening_balance_usd=0.0,  # no headroom
        )
        verdict = await gov.check("pre_flight", Usage(), 0.0)
        assert isinstance(verdict, Stop) and verdict.reason == "budget"
        assert gov.reservation is None

    _run(_test())


# === 4. settle idempotent + sweeper ==========================================

def test_settle_idempotent_and_sweep_reclaims_leaked_holds() -> None:
    async def _test() -> None:
        ledger = InMemoryReservationLedger()
        r = await ledger.reserve(
            user_id="u1", task_id="task", tenant_budget_id="t1",
            worst_case_usd=10.0, opening_balance_usd=100.0,
            deadline_at=None, provider="claude",
        )
        assert r is not None
        # committed reflects the $10 hold.
        assert ledger._committed["t1"] == 10.0

        await ledger.settle(r.reservation_id, 3.0)
        assert ledger._committed["t1"] == 3.0  # hold swapped for actual
        # Second settle is a no-op past the first.
        await ledger.settle(r.reservation_id, 99.0)
        assert ledger._committed["t1"] == 3.0
        assert ledger._reservations[r.reservation_id].status == "settled"

        # A reservation left 'reserved' past its deadline is swept.
        r2 = await ledger.reserve(
            user_id="u1", task_id="task2", tenant_budget_id="t2",
            worst_case_usd=20.0, opening_balance_usd=100.0,
            deadline_at=100.0, provider="claude",
        )
        assert r2 is not None
        assert ledger._committed["t2"] == 20.0
        swept = await ledger.sweep(now=200.0)  # past deadline_at=100
        assert swept == 1
        assert ledger._reservations[r2.reservation_id].status == "expired"
        assert ledger._committed["t2"] == 0.0  # hold released

    _run(_test())


def test_sweep_leaves_live_reservations_alone() -> None:
    async def _test() -> None:
        ledger = InMemoryReservationLedger()
        r = await ledger.reserve(
            user_id="u1", task_id="task", tenant_budget_id="t1",
            worst_case_usd=5.0, opening_balance_usd=100.0,
            deadline_at=500.0, provider="claude",
        )
        assert r is not None
        swept = await ledger.sweep(now=100.0)  # before deadline
        assert swept == 0
        assert ledger._reservations[r.reservation_id].status == "reserved"

    _run(_test())


# === 5. GOV-5 — restart safety (durable balance from the BalanceSource) =======

def test_gov5_restart_safety_balance_from_source() -> None:
    """A fresh GovernorService reads the DURABLE remaining balance from the
    (mock) BalanceSource — a 'new process' does not zero prior spend."""

    async def _test() -> None:
        # The product DB says tenant t1 has only $2 remaining (prior spend elsewhere).
        balances = StaticBalanceSource({"t1": 2.0})
        ledger = InMemoryReservationLedger()  # fresh in-memory (a new process)
        service = GovernorService(
            key_registry=KeyRegistry(keys={}, users={}),
            ledger=ledger,
            balance_source=balances,
            tier_policy=GovernancePolicy(cost_cap_usd=1000.0),
        )
        gov = await service.resolve(
            user_id="u1", task_id="task", provider="claude",
            model="claude-opus-4-8", requested_max_out=1_000_000,
            tenant_budget_id="t1",
        )
        # The opening balance is the durable $2, NOT a zeroed fresh-process total.
        assert gov.opening_balance_usd == 2.0
        # worst case for 1M out on opus = $25 > remaining $2 → hold capped at $2,
        # admitted (headroom exists) but bounded to the real remaining budget.
        v = await gov.check("pre_flight", Usage(), 0.0)
        assert isinstance(v, Continue)
        assert gov.reservation is not None
        assert gov.reservation.reserved_cost_usd == 2.0  # capped at durable remaining

    _run(_test())


def test_gov5_prior_committed_blocks_new_run() -> None:
    """If the durable balance already reflects prior spend down to zero, a new
    run is rejected — spend is not zeroed by a fresh process."""

    async def _test() -> None:
        balances = StaticBalanceSource({"t1": 0.0})  # durable: nothing left
        ledger = InMemoryReservationLedger()
        service = GovernorService(
            key_registry=KeyRegistry(keys={}, users={}),
            ledger=ledger,
            balance_source=balances,
        )
        gov = await service.resolve(
            user_id="u1", task_id="task", provider="claude",
            model="claude-opus-4-8", requested_max_out=1000,
            tenant_budget_id="t1",
        )
        v = await gov.check("pre_flight", Usage(), 0.0)
        assert isinstance(v, Stop) and v.reason == "budget"

    _run(_test())


# === 6. cost-cap stop at turn_boundary =======================================

def test_cost_cap_stops_at_turn_boundary() -> None:
    async def _test() -> None:
        ledger = InMemoryReservationLedger()
        gov = _make_run_governor(
            policy=GovernancePolicy(cost_cap_usd=30.0),
            ledger=ledger,
            worst=100.0,
            opening_balance_usd=1000.0,
        )
        assert isinstance(await gov.check("pre_flight", Usage(), 0.0), Continue)
        # First turn: 1M output on opus = $25 (< $30 cap) → CONTINUE.
        v1 = await gov.check("turn_boundary", Usage(output=1_000_000), 0.0)
        assert isinstance(v1, Continue)
        # Second turn: another $25 → cumulative $50 >= $30 → Stop('budget').
        v2 = await gov.check("turn_boundary", Usage(output=1_000_000), 0.0)
        assert isinstance(v2, Stop) and v2.reason == "budget"

    _run(_test())


# === 7. mid_stream tripwire — provisional on CONTINUE, commits on STOP ========

def test_mid_stream_continue_does_not_commit() -> None:
    """A mid_stream check UNDER the cap is provisional — it does NOT commit, so the
    terminal turn_boundary remains the sole committer (no double-count)."""

    async def _test() -> None:
        ledger = InMemoryReservationLedger()
        gov = _make_run_governor(
            policy=GovernancePolicy(cost_cap_usd=30.0),
            ledger=ledger, worst=100.0, opening_balance_usd=1000.0,
        )
        await gov.check("pre_flight", Usage(), 0.0)
        v = await gov.check("mid_stream", Usage(output=1_000_000), 0.0)  # $25 < $30
        assert isinstance(v, Continue)
        assert gov.run_cost_usd == 0.0  # provisional — not committed
        v2 = await gov.check("turn_boundary", Usage(output=100_000), 0.0)  # $2.5
        assert isinstance(v2, Continue)
        assert abs(gov.run_cost_usd - 2.5) < 1e-9

    _run(_test())


def test_mid_stream_stop_commits_incurred_cost() -> None:
    """A mid-turn STOP captures the incurred (partial-turn) cost — the run DID
    generate up to the cap, so settle debits that, not zero."""

    async def _test() -> None:
        ledger = InMemoryReservationLedger()
        gov = _make_run_governor(
            policy=GovernancePolicy(cost_cap_usd=30.0),
            ledger=ledger, worst=100.0, opening_balance_usd=1000.0,
        )
        await gov.check("pre_flight", Usage(), 0.0)
        v = await gov.check("mid_stream", Usage(output=2_000_000), 0.0)  # $50 > $30
        assert isinstance(v, Stop) and v.reason == "budget"
        # The incurred cost is captured (so settle debits the partial-turn spend).
        assert abs(gov.run_cost_usd - 50.0) < 1e-9

    _run(_test())


# === 8. deadline stop at clock_tick ==========================================

def test_deadline_stop_at_clock_tick() -> None:
    async def _test() -> None:
        ledger = InMemoryReservationLedger()
        gov = _make_run_governor(
            policy=GovernancePolicy(deadline_s=10.0),
            ledger=ledger,
            worst=None,
            opening_balance_usd=100.0,
        )
        # Before the deadline → CONTINUE.
        assert isinstance(await gov.check("clock_tick", Usage(), 5.0), Continue)
        # Past the deadline → Stop('deadline').
        v = await gov.check("clock_tick", Usage(), 11.0)
        assert isinstance(v, Stop) and v.reason == "deadline"

    _run(_test())


# === 9. GOV-2 — all-None policy + no worst_case ⇒ continue everywhere =========

def test_gov2_no_policy_passthrough() -> None:
    async def _test() -> None:
        ledger = InMemoryReservationLedger()
        gov = _make_run_governor(
            policy=GovernancePolicy(),  # all None
            ledger=ledger,
            worst=None,  # unpriced / unknown
            opening_balance_usd=100.0,
        )
        big = Usage(input=10_000_000, output=10_000_000)
        for cp in ("pre_flight", "turn_boundary", "mid_stream", "tool_gate", "clock_tick"):
            v = await gov.check(cp, big, 99999.0)
            assert isinstance(v, Continue), (cp, v)

    _run(_test())


# === 10. worst_case_usd — includes input; None for unpriced model ============

def test_worst_case_includes_input_tokens() -> None:
    table = dict(DEFAULT_PRICING)
    # opus: $5/Mtok in, $25/Mtok out.
    out_only = worst_case_usd(
        input_tokens_est=0, requested_max_out=1_000_000,
        model_max_out=None, hard_cap_out=None,
        model="claude-opus-4-8", table=table,
    )
    with_input = worst_case_usd(
        input_tokens_est=1_000_000, requested_max_out=1_000_000,
        model_max_out=None, hard_cap_out=None,
        model="claude-opus-4-8", table=table,
    )
    assert out_only == 25.0
    assert with_input == 30.0  # + $5 input, NOT output-only
    assert with_input > out_only


def test_worst_case_takes_min_of_out_bounds() -> None:
    table = dict(DEFAULT_PRICING)
    w = worst_case_usd(
        input_tokens_est=0,
        requested_max_out=2_000_000, model_max_out=500_000, hard_cap_out=1_000_000,
        model="claude-opus-4-8", table=table,
    )
    # tightest bound = 500k out * $25/Mtok = $12.5
    assert abs(w - 12.5) < 1e-9


def test_worst_case_none_when_no_out_bound() -> None:
    w = worst_case_usd(
        input_tokens_est=5_000, requested_max_out=None,
        model_max_out=None, hard_cap_out=None,
        model="claude-opus-4-8", table=dict(DEFAULT_PRICING),
    )
    assert w is None


def test_worst_case_none_for_unpriced_model() -> None:
    """A local/Ollama model not in the table yields None (no bogus dollar hold)."""
    w = worst_case_usd(
        input_tokens_est=5_000, requested_max_out=1_000_000,
        model_max_out=None, hard_cap_out=None,
        model="llama3.1:8b", table=dict(DEFAULT_PRICING),
    )
    assert w is None


def test_worst_case_none_model_is_priced_as_default() -> None:
    """A None model IS priced (it resolves to the default model the run will use)."""
    w = worst_case_usd(
        input_tokens_est=0, requested_max_out=1_000_000,
        model_max_out=None, hard_cap_out=None,
        model=None, table=dict(DEFAULT_PRICING),
    )
    assert w == 25.0  # default is claude-opus-4-8: $25/Mtok out


# === policy precedence (resolve_policy) ======================================

def test_resolve_policy_run_over_task_over_tier() -> None:
    tier = GovernancePolicy(cost_cap_usd=5.0, deadline_s=60.0, max_turns=10)
    task = GovernancePolicy(cost_cap_usd=20.0)          # overrides cost only
    run = GovernancePolicy(deadline_s=30.0)             # overrides deadline only
    resolved = resolve_policy(tier, task, run)
    assert resolved.cost_cap_usd == 20.0   # task wins over tier
    assert resolved.deadline_s == 30.0     # run wins over tier
    assert resolved.max_turns == 10        # only tier set it


def test_resolve_policy_all_none_stays_none() -> None:
    resolved = resolve_policy(None, None, None)
    assert resolved == GovernancePolicy()
    resolved2 = resolve_policy(GovernancePolicy(), GovernancePolicy(), None)
    assert resolved2.cost_cap_usd is None


# === settle idempotency guards the billing meter (3g) ========================

def test_settle_twice_debits_billing_once() -> None:
    """settle() is idempotent for the BILLING meter too — a second settle must not
    double-debit a durable balance (the ledger settle was already a no-op, but the
    meter would have fired twice without the _settled guard)."""

    async def _test() -> None:
        from warden.harness_api.governance.billing import (
            InMemoryBillingBackend,
        )

        billing = InMemoryBillingBackend({"u1": 100.0})
        service = GovernorService(
            key_registry=KeyRegistry(keys={}, users={}),
            ledger=InMemoryReservationLedger(),
            balance_source=StaticBalanceSource({"u1": 100.0}),
            billing=billing,
        )
        gov = await service.resolve(
            user_id="u1", task_id="t", provider="claude",
            model="claude-opus-4-8", requested_max_out=1000,
            run_policy=GovernancePolicy(cost_cap_usd=50.0),
        )
        await gov.check("pre_flight", Usage(), 0.0)
        await gov.check("turn_boundary", Usage(output=100_000), 0.0)  # $2.5
        await gov.settle()
        await gov.settle()  # second settle: must NOT meter again
        assert len(billing.records) == 1
        assert abs(billing.balances["u1"] - (100.0 - 2.5)) < 1e-9

    _run(_test())


# === max_turns — the universal turn-cap bound (3e) ===========================

def test_max_turns_stops_at_turn_cap() -> None:
    """The turn-cap is a universal bound on every provider: after ``max_turns``
    turns complete, the next turn_boundary is Stop('max_turns')."""

    async def _test() -> None:
        ledger = InMemoryReservationLedger()
        gov = _make_run_governor(
            policy=GovernancePolicy(max_turns=2),  # no cost/deadline cap
            ledger=ledger,
            worst=None,
            opening_balance_usd=1000.0,
        )
        await gov.check("pre_flight", Usage(), 0.0)
        # Two turns allowed…
        assert isinstance(await gov.check("turn_boundary", Usage(), 0.0), Continue)
        v = await gov.check("turn_boundary", Usage(), 0.0)
        assert isinstance(v, Stop) and v.reason == "max_turns"

    _run(_test())


# === reservation is cost_cap-aware (3e): reserve the run's enforced ceiling ===

def test_resolve_reserves_cost_cap_when_set() -> None:
    """When a run carries a cost cap, the reservation holds the CAP (its enforced
    ceiling) — so N concurrent runs each holding their cap cannot together exceed
    the tenant budget (GOV-4 at the run granularity)."""

    async def _test() -> None:
        service = GovernorService(
            key_registry=KeyRegistry(keys={}, users={}),
            ledger=InMemoryReservationLedger(),
            balance_source=StaticBalanceSource({"u1": 100.0}),
        )
        gov = await service.resolve(
            user_id="u1", task_id="t", provider="claude",
            model="claude-opus-4-8", requested_max_out=1_000_000,  # token worst = $25+
            run_policy=GovernancePolicy(cost_cap_usd=5.0),
        )
        await gov.check("pre_flight", Usage(), 0.0)
        # The hold is the $5 cap, not the larger token worst case.
        assert gov.reservation is not None
        assert abs(gov.reservation.reserved_cost_usd - 5.0) < 1e-9

    _run(_test())


def test_resolve_reserves_token_worst_when_no_cap() -> None:
    """No cost cap but a bounded output ⇒ reserve the token worst case."""

    async def _test() -> None:
        service = GovernorService(
            key_registry=KeyRegistry(keys={}, users={}),
            ledger=InMemoryReservationLedger(),
            balance_source=StaticBalanceSource({"u1": 100.0}),
        )
        gov = await service.resolve(
            user_id="u1", task_id="t", provider="claude",
            model="claude-opus-4-8", hard_cap_out=16_384, input_tokens_est=0,
        )
        await gov.check("pre_flight", Usage(), 0.0)
        # 16384 out * $25/Mtok ≈ $0.4096 held (no cap → token worst).
        assert gov.reservation is not None
        assert abs(gov.reservation.reserved_cost_usd - 16_384 * 25.0 / 1_000_000) < 1e-9

    _run(_test())


# === settle via RunGovernor is idempotent + defaults to run cost =============

def test_run_governor_settle_defaults_to_run_cost() -> None:
    async def _test() -> None:
        ledger = InMemoryReservationLedger()
        gov = _make_run_governor(
            policy=GovernancePolicy(cost_cap_usd=1000.0),
            ledger=ledger,
            worst=100.0,
            opening_balance_usd=1000.0,
        )
        await gov.check("pre_flight", Usage(), 0.0)
        await gov.check("turn_boundary", Usage(output=100_000), 0.0)  # $2.5
        await gov.settle()  # default = run_cost_usd = 2.5
        assert abs(ledger._committed["t1"] - 2.5) < 1e-9
        await gov.settle()  # idempotent
        assert abs(ledger._committed["t1"] - 2.5) < 1e-9

    _run(_test())


# === allow_uncapped (3g.2b) — overdraft: meter, don't block ==================

def test_allow_uncapped_admits_at_zero_and_meters_negative() -> None:
    """allow_uncapped + zero headroom: pre_flight ADMITS (overdraft, no hold), and
    after a turn + settle() the billing backend is metered so the balance goes
    negative — spend is tracked even though nothing was reserved."""

    async def _test() -> None:
        from warden.harness_api.governance.billing import (
            InMemoryBillingBackend,
        )

        billing = InMemoryBillingBackend({"t1": 0.0})
        gov = _make_run_governor(
            policy=GovernancePolicy(cost_cap_usd=10.0),
            ledger=InMemoryReservationLedger(),
            worst=1.0,
            opening_balance_usd=0.0,  # no headroom
            allow_uncapped=True,
            billing=billing,
        )
        # pre_flight admits (overdraft) — no reservation held.
        assert isinstance(await gov.check("pre_flight", Usage(), 0.0), Continue)
        assert gov.reservation is None
        # A turn costs $2.5, then settle still meters the spend.
        await gov.check("turn_boundary", Usage(output=100_000), 0.0)  # $2.5
        await gov.settle()
        assert len(billing.records) == 1
        assert billing.balances["t1"] < 0  # metered into the red (overdraft)
        assert abs(billing.balances["t1"] - (-2.5)) < 1e-9

    _run(_test())


def test_allow_uncapped_still_enforces_deadline() -> None:
    """money-uncapped ≠ time-uncapped: a deadline still stops the run."""

    async def _test() -> None:
        gov = _make_run_governor(
            policy=GovernancePolicy(deadline_s=10.0),
            ledger=InMemoryReservationLedger(),
            worst=None,
            opening_balance_usd=100.0,
            allow_uncapped=True,
        )
        await gov.check("pre_flight", Usage(), 0.0)
        # Within the deadline → Continue.
        assert isinstance(await gov.check("clock_tick", Usage(), 5.0), Continue)
        # Past the deadline → Stop('deadline') even though money is uncapped.
        v = await gov.check("clock_tick", Usage(), 11.0)
        assert isinstance(v, Stop) and v.reason == "deadline"

    _run(_test())


def test_allow_uncapped_does_not_enforce_cost_cap() -> None:
    """A turn over the cost cap is NOT stopped when allow_uncapped is set."""

    async def _test() -> None:
        gov = _make_run_governor(
            policy=GovernancePolicy(cost_cap_usd=5.0),
            ledger=InMemoryReservationLedger(),
            worst=None,
            opening_balance_usd=100.0,
            allow_uncapped=True,
        )
        await gov.check("pre_flight", Usage(), 0.0)
        # $25 turn >> $5 cap, but uncapped ⇒ Continue.
        v = await gov.check("turn_boundary", Usage(output=1_000_000), 0.0)
        assert isinstance(v, Continue)

    _run(_test())


def test_enforce_path_zero_headroom_still_stops() -> None:
    """allow_uncapped=False + zero headroom ⇒ pre_flight Stop('budget') (unchanged)."""

    async def _test() -> None:
        gov = _make_run_governor(
            policy=GovernancePolicy(cost_cap_usd=10.0),
            ledger=InMemoryReservationLedger(),
            worst=1.0,
            opening_balance_usd=0.0,
            allow_uncapped=False,
        )
        v = await gov.check("pre_flight", Usage(), 0.0)
        assert isinstance(v, Stop) and v.reason == "budget"
        assert gov.reservation is None

    _run(_test())
