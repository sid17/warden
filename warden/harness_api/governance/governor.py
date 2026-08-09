"""M2 3d — the concrete Governor: policy + reservation, wired to the seam.

Two objects, one job each:

  * :class:`GovernorService` — the shared, long-lived resolver (one per process). It
    holds the collaborators the *engine never sees* (GOV-1): a
    :class:`~warden.harness_api.credentials.resolver.CredentialResolver`
    (credentials — the typed ``AuthResolver`` in production, ``KeyRegistry`` in
    legacy tests), the pricing table, the :class:`ReservationLedger`, the
    :class:`BalanceSource`, and the tier-level :class:`GovernancePolicy`. Its
    :meth:`resolve` consolidates — in ONE step — everything a run needs: the auth env,
    the resolved policy (tier→task→run), the durable opening balance, and the computed
    worst-case reservation price. It returns a per-run :class:`RunGovernor`.

  * :class:`RunGovernor` — one per run, implements the engine's ``Governor.check``
    seam contract. ``pre_flight`` makes the reservation (the N10 fix: reject a run that
    cannot be afforded BEFORE it starts). ``turn_boundary`` COMMITS the turn's cost and
    enforces the caps; ``mid_stream`` / ``tool_gate`` are PROVISIONAL tripwires (check
    without committing); ``clock_tick`` is a time-only breach check.

Money unit is float USD (see :mod:`.ledger`).
"""

from __future__ import annotations

from collections.abc import Mapping

from warden.harness_api.credentials.resolver import CredentialResolver
from warden.harness_api.governance.pricing import (
    DEFAULT_PRICING,
    cost_usd,
)
from warden.harness_api.governance.billing import (
    BillingBackend,
    UsageRecord,
)
from warden.harness_api.governance.ledger import (
    BalanceSource,
    Reservation,
    ReservationLedger,
)
from warden.harness_api.governance.policy import (
    GovernancePolicy,
    resolve_policy,
    worst_case_usd,
)
from warden.schemas.usage import Usage
from warden.seams.governor import CONTINUE, Checkpoint, Stop, Verdict

# Default estimate for prompt-side tokens when the caller has no better number.
# Folded into the worst-case reservation so a large-context run is bounded up front.
_DEFAULT_INPUT_TOKENS_EST = 8_000


class RunGovernor:
    """Per-run Governor implementing the ``seams.governor.Governor.check`` contract.

    Holds the resolved policy, the ledger + reservation inputs, and the run's
    accumulated committed cost. Semantics per checkpoint are documented on
    :meth:`check`.
    """

    def __init__(
        self,
        *,
        policy: GovernancePolicy,
        ledger: ReservationLedger,
        user_id: str,
        task_id: str,
        tenant_budget_id: str,
        provider: str,
        model: str | None,
        table: Mapping[str, tuple[float, float]],
        worst_case_usd: float | None,
        opening_balance_usd: float,
        auth_env: dict[str, str] | None,
        deadline_at: float | None = None,
        billing: BillingBackend | None = None,
        pausable: bool = False,
        allow_uncapped: bool = False,
    ) -> None:
        self.policy = policy
        self.auth_env = auth_env
        self.opening_balance_usd = opening_balance_usd
        # Overdraft semantics (3g.2b): when True, money stops (reason="budget") are
        # suppressed — admit even at zero/negative balance and never stop for spend —
        # but the spend is STILL metered so the balance goes negative. deadline /
        # max_turns are safety rails, NOT budget, and always enforce when set.
        self.allow_uncapped = allow_uncapped
        # Public: the billing entity + whether a budget stop can PAUSE (GOV-6) rather
        # than hard-fail (True iff a top-up path exists for this tenant). The Runner
        # reads ``pausable`` + ``tenant_budget_id`` at terminal-emit time.
        self.tenant_budget_id = tenant_budget_id
        self.pausable = pausable
        self._ledger = ledger
        self._user_id = user_id
        self._task_id = task_id
        self._tenant_budget_id = tenant_budget_id
        self._provider = provider
        self._model = model
        self._table = table
        self._worst_case_usd = worst_case_usd
        self._deadline_at = deadline_at
        self._billing = billing
        # Committed run cost across COMPLETED turns (turn_boundary sums into this).
        self._run_cost_usd = 0.0
        # Completed-turn count for the universal turn-cap bound (max_turns).
        self._turns = 0
        self._reservation: Reservation | None = None
        # Idempotency guard for settle(): the ledger settle is a no-op past the first,
        # but the billing meter would otherwise double-debit a durable balance on a
        # second settle — so guard the WHOLE settle, not just the ledger call.
        self._settled = False

    @property
    def reservation(self) -> Reservation | None:
        """The reservation made at ``pre_flight`` (``None`` until then / if rejected)."""
        return self._reservation

    @property
    def run_cost_usd(self) -> float:
        """USD committed across completed turns this run."""
        return self._run_cost_usd

    def price_usage(self, usage: Usage) -> float:
        """Price one turn's usage: trust a provider-supplied cost, else compute it."""
        if usage.cost_usd:
            return usage.cost_usd
        return cost_usd(
            self._model,
            {
                "input_tokens": usage.input,
                "output_tokens": usage.output,
                "cache_read_input_tokens": usage.cached,
            },
            self._table,
        )

    def _time_breached(self, elapsed_s: float) -> bool:
        return self.policy.deadline_s is not None and elapsed_s >= self.policy.deadline_s

    async def check(
        self, checkpoint: Checkpoint, usage: Usage, elapsed_s: float,
    ) -> Verdict:
        """The seam callback. Verdict per checkpoint:

        * ``pre_flight`` — make the reservation (worst case vs. opening balance). No
          headroom / already at cap ⇒ ``Stop("budget")``; else store it and CONTINUE.
        * ``turn_boundary`` — COMMIT this turn's cost; ``>= cost_cap`` ⇒ ``Stop("budget")``;
          then count the turn — ``>= max_turns`` ⇒ ``Stop("max_turns")`` (the universal
          turn-cap bound); else a deadline breach ⇒ ``Stop("deadline")``, else CONTINUE.
        * ``mid_stream`` / ``tool_gate`` — PROVISIONAL: check ``committed + this turn``
          against the cap WITHOUT committing (the tripwire); time-breach too; else CONTINUE.
        * ``clock_tick`` — time only: deadline breach ⇒ ``Stop("deadline")``, else CONTINUE.
        """
        if checkpoint == "pre_flight":
            return await self._pre_flight()

        if checkpoint == "turn_boundary":
            self._run_cost_usd += self.price_usage(usage)
            if (
                not self.allow_uncapped
                and self.policy.cost_cap_usd is not None
                and self._run_cost_usd >= self.policy.cost_cap_usd
            ):
                return Stop(reason="budget")
            self._turns += 1
            if (
                self.policy.max_turns is not None
                and self._turns >= self.policy.max_turns
            ):
                return Stop(reason="max_turns")
            if self._time_breached(elapsed_s):
                return Stop(reason="deadline")
            return CONTINUE

        if checkpoint in ("mid_stream", "tool_gate"):
            provisional = self._run_cost_usd + self.price_usage(usage)
            if (
                not self.allow_uncapped
                and self.policy.cost_cap_usd is not None
                and provisional >= self.policy.cost_cap_usd
            ):
                # Mid-turn stop: the run DID generate up to here, so capture the
                # incurred (partial-turn) cost — settle then debits what was
                # actually spent, not zero. (A CONTINUE stays provisional: it does
                # NOT commit, so the terminal turn_boundary is the sole committer.)
                self._run_cost_usd = provisional
                return Stop(reason="budget")
            if self._time_breached(elapsed_s):
                return Stop(reason="deadline")
            return CONTINUE

        if checkpoint == "clock_tick":
            if self._time_breached(elapsed_s):
                return Stop(reason="deadline")
            return CONTINUE

        return CONTINUE

    async def _pre_flight(self) -> Verdict:
        """Reserve the worst case before the provider is ever called (N10 fix).

        A run whose worst case exceeds the remaining balance, or a tenant already at
        or over cap (no headroom), is rejected here — never admitted allow-first.
        With an all-``None`` policy and an unknown worst case, there is nothing to
        reserve against and the run is admitted (GOV-2 passthrough) — but we still
        reserve whenever a balance headroom is knowable so leaks are swept later.
        """
        reservation = await self._ledger.reserve(
            user_id=self._user_id,
            task_id=self._task_id,
            tenant_budget_id=self._tenant_budget_id,
            worst_case_usd=self._worst_case_usd,
            opening_balance_usd=self.opening_balance_usd,
            deadline_at=self._deadline_at,
            provider=self._provider,
        )
        if reservation is None:
            # No headroom. Enforce path ⇒ reject as a money stop. Overdraft path
            # (allow_uncapped) ⇒ admit with no hold (_reservation stays None); the
            # spend is still metered at settle so the balance goes negative.
            if not self.allow_uncapped:
                return Stop(reason="budget")
            return CONTINUE
        self._reservation = reservation
        return CONTINUE

    async def settle(self, actual_cost_usd: float | None = None) -> None:
        """Settle the run to the actual cost (default: committed run cost).

        Idempotent via the ``_settled`` guard. Metering and reservation settlement are
        INDEPENDENT: the billing meter ALWAYS fires when a billing backend is present —
        even for an overdraft admit that held no reservation (``_reservation is None``),
        so the spend is tracked and the durable balance goes negative (3g.2b). The
        ledger settle only runs when a hold exists (an unreserved / overdraft run has
        nothing to swap). The meter is a downstream signal (meter → signal, GOV-5);
        ``record_usage`` swallows its own transport errors, but we still guard here so a
        billing outage that DID raise cannot break the run's settle/terminal path.
        """
        if self._settled:
            return
        self._settled = True
        actual = actual_cost_usd if actual_cost_usd is not None else self._run_cost_usd
        if self._reservation is not None:
            await self._ledger.settle(self._reservation.reservation_id, actual)
        if self._billing is not None:
            record = UsageRecord(
                tenant_budget_id=self._tenant_budget_id,
                user_id=self._user_id,
                task_id=self._task_id,
                cost_usd=actual,
                provider=self._provider,
                model=self._model,
            )
            await self._billing.record_usage(record)


class GovernorService:
    """Shared resolver (one per process): turns a run request into a RunGovernor.

    Holds the credential resolver, pricing table, ledger, balance source, and the
    tier-level policy default. :meth:`resolve` is the single consolidation step the
    Runner calls (3e); it exposes the resolved ``auth_env`` and ``opening_balance_usd``
    on the returned :class:`RunGovernor` so the Runner can inject the credential.

    The credential half is DELEGATED to the injected ``CredentialResolver`` (pre-03 3d):
    :meth:`resolve` calls ``auth_env_for`` on it — the SAME resolver the ungoverned
    Runner path calls — so governed and ungoverned runs resolve credentials one way.
    """

    def __init__(
        self,
        *,
        key_registry: CredentialResolver,
        ledger: ReservationLedger,
        balance_source: BalanceSource | None = None,
        table: Mapping[str, tuple[float, float]] | None = None,
        tier_policy: GovernancePolicy | None = None,
        billing: BillingBackend | None = None,
        allow_uncapped: bool = False,
    ) -> None:
        self._keys = key_registry
        self._ledger = ledger
        self._billing = billing
        self._allow_uncapped = allow_uncapped
        # A BillingBackend is also a BalanceSource (GOV-5): when a billing backend is
        # given but no explicit balance_source, the billing backend serves both roles
        # (one object reports durable credit AND receives the metered usage). An
        # explicit balance_source still wins so existing callers are unbroken.
        if balance_source is None:
            if billing is None:
                raise ValueError(
                    "GovernorService needs a balance_source or a billing backend"
                )
            balance_source = billing
        self._balance_source = balance_source
        self._table = dict(table) if table is not None else dict(DEFAULT_PRICING)
        self._tier_policy = tier_policy

    async def resolve(
        self,
        *,
        user_id: str,
        task_id: str,
        provider: str,
        model: str | None,
        tenant_budget_id: str | None = None,
        requested_max_out: int | None = None,
        input_tokens_est: int = _DEFAULT_INPUT_TOKENS_EST,
        model_max_out: int | None = None,
        hard_cap_out: int | None = None,
        task_policy: GovernancePolicy | None = None,
        run_policy: GovernancePolicy | None = None,
        deadline_at: float | None = None,
    ) -> RunGovernor:
        """Consolidate credential + policy + balance + worst-case into a RunGovernor.

        ``tenant_budget_id`` defaults to ``user_id`` (one budget per user) when the
        caller does not carry a distinct billing entity.
        """
        tenant = tenant_budget_id if tenant_budget_id is not None else user_id
        auth_env = self._keys.auth_env_for(user_id, provider)
        policy = resolve_policy(self._tier_policy, task_policy, run_policy)
        opening_balance = await self._balance_source.opening_balance_usd(tenant)
        # Pause-not-fail (GOV-6): a budget stop can PAUSE (await a top-up) only when
        # the tenant's billing backend has a credit-grant path. No billing ⇒ False.
        pausable = (
            await self._billing.supports_topup(tenant)
            if self._billing is not None
            else False
        )
        token_worst = worst_case_usd(
            input_tokens_est=input_tokens_est,
            requested_max_out=requested_max_out,
            model_max_out=model_max_out,
            hard_cap_out=hard_cap_out,
            model=model,
            table=self._table,
        )
        # Reserve the run's ENFORCED ceiling: when a cost cap is set that is the
        # run's max spend (the RunGovernor stops it there), so holding the cap makes
        # N concurrent runs each hold their own ceiling — no collective overshoot
        # (GOV-4 at run granularity). Absent a cap, fall back to the token worst
        # case (None ⇒ a time/turn-only reservation for an unpriced provider).
        worst = policy.cost_cap_usd if policy.cost_cap_usd is not None else token_worst
        return RunGovernor(
            policy=policy,
            ledger=self._ledger,
            user_id=user_id,
            task_id=task_id,
            tenant_budget_id=tenant,
            provider=provider,
            model=model,
            table=self._table,
            worst_case_usd=worst,
            opening_balance_usd=opening_balance,
            auth_env=auth_env,
            deadline_at=deadline_at,
            billing=self._billing,
            pausable=pausable,
            allow_uncapped=self._allow_uncapped,
        )


__all__ = ["GovernorService", "RunGovernor"]
