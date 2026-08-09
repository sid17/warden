"""M2 3g.2a — the Governor switchboard: build a GovernorService from Settings.

Every governance knob is config-driven, not hand-wired (GOV-1: the Governor's nouns
— caps, ledger, balance/billing backend — are assembled here from the typed
:class:`~warden.harness_api.config.HarnessApiConfig`, never read off
``os.environ`` directly). :func:`build_governor_service` is the single construction
point the Runner calls when no ``governor_service`` was explicitly injected:

  * ``governance.enabled is False`` ⇒ ``(None, None)`` — the ungoverned path
    (KeyRegistry auth + stateless per-turn pricing, no budget gate) runs (GOV-2).
  * enabled ⇒ construct the :class:`GovernorService` from the config's backends and
    the tier-level :class:`GovernancePolicy`, plus the durable
    :class:`JsonlTaskPolicyStore` (the *task* cap layer) when a state dir is available.

The JSONL-backed pieces need one ``await load()`` at startup to replay their file into
memory; that is the caller's job via :func:`init_governance` (the Runner calls it in
its existing ``init()``).
"""

from __future__ import annotations

from pathlib import Path

from warden.harness_api.config import HarnessApiConfig
from warden.harness_api.credentials.keys import KeyRegistry
from warden.harness_api.credentials.resolver import CredentialResolver
from warden.harness_api.governance.pricing import build_pricing
from warden.harness_api.governance.billing import NullBillingBackend
from warden.harness_api.governance.governor import GovernorService
from warden.harness_api.governance.jsonl_ledger import JsonlBalanceLedger
from warden.harness_api.governance.ledger import InMemoryReservationLedger
from warden.harness_api.governance.policy import GovernancePolicy
from warden.harness_api.governance.task_policy_store import (
    JsonlTaskPolicyStore,
)

# A tenant-scoped Postgres ledger is a per-deploy dependency (row-lock atomic across
# processes) that pulls in ``asyncpg``. We do NOT construct it here so this factory
# stays import-light and hermetic; a Postgres deploy injects a
# ``PostgresReservationLedger`` explicitly via ``governor_service=``.
_POSTGRES_HINT = (
    "governance.ledger_backend='postgres' is not built by the config switchboard "
    "(it would pull in asyncpg). Construct a PostgresReservationLedger and inject a "
    "GovernorService explicitly via Runner(governor_service=...)."
)


def _state_dir(cfg: HarnessApiConfig) -> Path:
    """Where the JSONL governance files live.

    Honors ``cfg.governance.state_dir`` when set; otherwise falls back next to the
    engine's persistence session DB (the same dir the durable ``run_events`` log
    derives — one storage dir holds the control-plane state), and ``data/`` when no
    session DB is configured (mirrors ``runner._event_log_path``).
    """
    if cfg.governance.state_dir:
        return Path(cfg.governance.state_dir)
    session_db = getattr(cfg.engine.persistence, "session_db_path", None)
    return Path(session_db).parent if session_db else Path("data")


def build_governor_service(
    cfg: HarnessApiConfig,
    auth_resolver: CredentialResolver | None = None,
) -> tuple[GovernorService | None, JsonlTaskPolicyStore | None]:
    """Construct the (GovernorService, JsonlTaskPolicyStore) pair from config.

    Reads governance knobs off the passed ``cfg`` (G1: never ``os.environ``).
    Returns ``(None, None)`` when governance is disabled — the ungoverned path (GOV-2).
    The returned JSONL-backed pieces are UNLOADED; the caller runs
    :func:`init_governance` at startup to replay their files.

    ``auth_resolver`` (pre-03 3d/3e): the Governor delegates the credential half of
    ``resolve()`` to this typed resolver. The Runner passes the SAME instance it uses
    for the ungoverned path, so credentials resolve one way. When ``None`` (direct
    callers / tests), a default is built via the ``KeyRegistry`` legacy adapter — the
    config-driven switchboard lives in ``credentials/config.build_auth_resolver`` and is
    NOT imported here (it would cycle: it imports this module's sibling).
    """
    gov = cfg.governance
    if not gov.enabled:
        return None, None

    if auth_resolver is None:
        auth_resolver = KeyRegistry.from_keys_config(cfg.keys).to_auth_resolver()
    table = build_pricing(cfg.spend.pricing_json)

    if gov.ledger_backend == "postgres":
        raise NotImplementedError(_POSTGRES_HINT)
    ledger = InMemoryReservationLedger()

    state_dir = _state_dir(cfg)
    task_policy_store: JsonlTaskPolicyStore | None = None
    if gov.balance_backend == "jsonl":
        # JsonlBalanceLedger is BOTH a BalanceSource AND a BillingBackend, so one
        # object serves both roles (durable opening balance + settle→debit sink).
        backend = JsonlBalanceLedger(state_dir / "balance.jsonl")
        task_policy_store = JsonlTaskPolicyStore(state_dir / "task_policies.jsonl")
    else:  # "null"
        # NullBillingBackend is also both roles (uncapped balance, no-op meter).
        backend = NullBillingBackend()

    tier_policy = GovernancePolicy(
        cost_cap_usd=gov.default_cost_cap_usd,
        deadline_s=gov.default_deadline_s,
        max_turns=gov.default_max_turns,
    )

    # NOTE (M2 3g.2a scope): ``hard_cap_out`` / ``input_tokens_est`` are validated
    # config fields but not yet threaded through ``run_wiring.resolve_run_governor``
    # (which still passes the ``HARD_CAP_OUT`` constant). Threading the configurable
    # values is a deliberate follow-up sub-step; do NOT break run_wiring here.
    service = GovernorService(
        key_registry=auth_resolver,
        ledger=ledger,
        balance_source=backend,
        table=table,
        tier_policy=tier_policy,
        billing=backend,
        allow_uncapped=gov.allow_uncapped,
    )
    return service, task_policy_store


async def init_governance(
    service: GovernorService | None,
    task_policy_store: JsonlTaskPolicyStore | None,
) -> None:
    """Replay the JSONL-backed governance state into memory (call once at startup).

    Loads the durable balance ledger (when the service's backend is JSONL-backed)
    and the task-policy store. Idempotent — a second call re-folds the same files.
    No-op when governance is disabled (both ``None``).
    """
    if service is not None:
        backend = getattr(service, "_balance_source", None)
        if isinstance(backend, JsonlBalanceLedger):
            await backend.load()
    if task_policy_store is not None:
        await task_policy_store.load()


__all__ = ["build_governor_service", "init_governance"]
