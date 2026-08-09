"""The Governor — resource governance (cost + time), Axis-2 policy (M2).

Holds the *nouns* the engine's Governor seam (``seams/governor.py``) never sees:
the caps, the reservation ledger, the per-provider enforcement tiers. Optional
and pluggable (GOV-2) — no Governor ⇒ the harness runs ungoverned.

3b landed the time bound's provider guard (:mod:`.deadline`); 3d lands the full
Governor: policy (:mod:`.policy`), the reservation ledger (:mod:`.ledger`,
:mod:`.postgres_ledger`), and the concrete :class:`GovernorService` /
:class:`RunGovernor` wired to the seam (:mod:`.governor`).
"""

from warden.harness_api.governance.billing import (
    BillingBackend,
    InMemoryBillingBackend,
    LagoBillingBackend,
    NullBillingBackend,
    PolarBillingBackend,
    UsageRecord,
)
from warden.harness_api.governance.config import (
    build_governor_service,
    init_governance,
)
from warden.harness_api.governance.deadline import (
    DeadlineUnsupportedError,
    assert_deadline_supported,
)
from warden.harness_api.governance.governor import (
    GovernorService,
    RunGovernor,
)
from warden.harness_api.governance.jsonl_ledger import (
    JsonlBalanceLedger,
)
from warden.harness_api.governance.ledger import (
    BalanceSource,
    InMemoryReservationLedger,
    Reservation,
    ReservationLedger,
    ReservationStatus,
    StaticBalanceSource,
)
from warden.harness_api.governance.policy import (
    GovernancePolicy,
    resolve_policy,
    worst_case_usd,
)
from warden.harness_api.governance.postgres_ledger import (
    PostgresReservationLedger,
)
from warden.harness_api.governance.task_policy_store import (
    JsonlTaskPolicyStore,
)

__all__ = [
    # billing (3f)
    "BillingBackend",
    "UsageRecord",
    "NullBillingBackend",
    "InMemoryBillingBackend",
    "LagoBillingBackend",
    "PolarBillingBackend",
    # config switchboard (3g.2a)
    "build_governor_service",
    "init_governance",
    # deadline (3b)
    "DeadlineUnsupportedError",
    "assert_deadline_supported",
    # policy (3d)
    "GovernancePolicy",
    "resolve_policy",
    "worst_case_usd",
    # ledger (3d)
    "ReservationStatus",
    "Reservation",
    "BalanceSource",
    "StaticBalanceSource",
    "ReservationLedger",
    "InMemoryReservationLedger",
    "PostgresReservationLedger",
    # durable JSONL stores (3g.1)
    "JsonlBalanceLedger",
    "JsonlTaskPolicyStore",
    # governor (3d)
    "GovernorService",
    "RunGovernor",
]
