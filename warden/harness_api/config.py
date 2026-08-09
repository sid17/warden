"""Axis-2 config — the account/billing/transport layer over the engine.

``HarnessApiConfig`` **composes** the whole engine :class:`HarnessConfig` and adds
the surface the engine must never see: managed keys and per-user spend/pricing.
Because it reuses the engine config, the old ``RunnerConfig`` — which *duplicated*
engine knobs (concurrency, base_dir, state_root, backend, s3) — is deleted; those
knobs now come from ``config.engine``.

Three tiers, mirroring the engine's ``config/`` package:
- ``HarnessApiSettings`` — the flat env layer (``MANAGED_KEYS_*`` / ``PRICING_JSON``).
- ``HarnessApiConfig``   — the nested declarative surface (engine + keys + spend).
- consumers (``Runner``) read ``get_harness_api_config()`` and build the runtime
  ``KeyRegistry`` from the ``keys`` slice + the pricing table from the ``spend`` slice.

Transport knobs (host/port, webhook timeout/backoff) are intentionally NOT modeled
here: nothing in-process binds host/port (the server entrypoint passes them to
uvicorn) and the webhook micro-timeouts stay module constants per config-plan
decision #3. Add a ``TransportConfig`` slice only when a real in-code consumer
appears.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from warden.config import get_harness_config
from warden.config.base_settings import HarnessBaseSettings
from warden.config.models import HarnessConfig


# --- env layer ---------------------------------------------------------------


class HarnessApiSettings(HarnessBaseSettings):
    """Flat env layer for the Axis-2 account/billing surface (kept off the engine
    ``HarnessSettings`` so the engine stays account-agnostic — §8)."""

    managed_keys_json: str | None = Field(
        default=None, validation_alias=AliasChoices("MANAGED_KEYS_JSON")
    )
    managed_keys_file: str | None = Field(
        default=None, validation_alias=AliasChoices("MANAGED_KEYS_FILE")
    )
    pricing_json: str | None = Field(
        default=None, validation_alias=AliasChoices("PRICING_JSON")
    )

    # --- governance (M2 3g.2a/3g.2b): the Governor switchboard --------------
    # ON by default (3g.2b) ⇒ governance meters every run through the reservation
    # ledger. Paired with ``governance_allow_uncapped=True`` (below), the net
    # default is: governance on, spend tracked, nothing blocked (overdraft admit).
    # Set ``GOVERNANCE_ENABLED=false`` for the fully ungoverned path (GOV-2).
    governance_enabled: bool = Field(
        default=True, validation_alias=AliasChoices("GOVERNANCE_ENABLED")
    )
    # allow_uncapped gates ONLY money stops (reason="budget"): True ⇒ admit even at
    # zero/negative balance (overdraft) and never stop for MONEY, but STILL meter the
    # spend so the balance goes negative. deadline / max_turns ALWAYS enforce when set.
    governance_allow_uncapped: bool = Field(
        default=True, validation_alias=AliasChoices("GOVERNANCE_ALLOW_UNCAPPED")
    )
    governance_ledger_backend: str = Field(
        default="memory", validation_alias=AliasChoices("GOVERNANCE_LEDGER_BACKEND")
    )
    governance_balance_backend: str = Field(
        default="jsonl", validation_alias=AliasChoices("GOVERNANCE_BALANCE_BACKEND")
    )
    governance_state_dir: str | None = Field(
        default=None, validation_alias=AliasChoices("GOVERNANCE_STATE_DIR")
    )
    governance_default_cost_cap_usd: float | None = Field(
        default=None,
        validation_alias=AliasChoices("GOVERNANCE_DEFAULT_COST_CAP_USD"),
    )
    governance_default_deadline_s: float | None = Field(
        default=None, validation_alias=AliasChoices("GOVERNANCE_DEFAULT_DEADLINE_S")
    )
    governance_default_max_turns: int | None = Field(
        default=None, validation_alias=AliasChoices("GOVERNANCE_DEFAULT_MAX_TURNS")
    )
    governance_hard_cap_out: int = Field(
        default=16384, validation_alias=AliasChoices("GOVERNANCE_HARD_CAP_OUT")
    )
    governance_input_tokens_est: int = Field(
        default=8000, validation_alias=AliasChoices("GOVERNANCE_INPUT_TOKENS_EST")
    )

    # --- auth (pre-03 M0): the credential resolve→inject switchboard --------
    # store_backend "memory" (default) seeds the legacy MANAGED_KEYS blob in-process
    # (current behavior); "jsonl" is the durable per-(user,provider) credential store.
    # oauth_allowed_users is the policy gate — "*" (default) or a comma-separated list.
    auth_store_backend: str = Field(
        default="memory", validation_alias=AliasChoices("AUTH_STORE_BACKEND")
    )
    auth_state_dir: str | None = Field(
        default=None, validation_alias=AliasChoices("AUTH_STATE_DIR")
    )
    auth_oauth_allowed_users: str | None = Field(
        default=None, validation_alias=AliasChoices("AUTH_OAUTH_ALLOWED_USERS")
    )
    auth_on_oauth_denied: str = Field(
        default="downgrade", validation_alias=AliasChoices("AUTH_ON_OAUTH_DENIED")
    )

    # --- run registry (EXT-C1): durable run identity ------------------------
    # store_backend "memory" (default) = ephemeral (today's behavior); "jsonl" is
    # the durable append-only run_id→(user,task) index so GET /runs/{id} + the file
    # read survive a restart. Config-selected, mirroring GOVERNANCE_LEDGER_BACKEND.
    run_registry_store_backend: str = Field(
        default="memory",
        validation_alias=AliasChoices("RUN_REGISTRY_STORE_BACKEND"),
    )
    run_registry_state_dir: str | None = Field(
        default=None, validation_alias=AliasChoices("RUN_REGISTRY_STATE_DIR")
    )

    # --- caller auth (EXT-T1a): the per-service token registry --------------
    # Distinct from MANAGED_KEYS (which key runs the model) — this authenticates
    # WHICH BACKEND is calling the Runs API (x-service-token). Same inline-wins
    # load pattern as MANAGED_KEYS_*; empty ⇒ open (single-tenant dev default).
    service_tokens_json: str | None = Field(
        default=None, validation_alias=AliasChoices("SERVICE_TOKENS_JSON")
    )
    service_tokens_file: str | None = Field(
        default=None, validation_alias=AliasChoices("SERVICE_TOKENS_FILE")
    )

    # --- durable HITL (M6): the pause SLA -----------------------------------
    # How long a durable tool-confirmation ask may stay unanswered before it
    # EXPIRES (a clean, product-synced terminal + ``permission_expired`` event —
    # never a silent deny, never a pinned run). A UX knob, not a resource one (the
    # cold path already freed the worker).
    #
    # ``<= 0`` ⇒ **indefinite**: the ask stays durably parked forever (no timer
    # armed) and resumes whenever the human returns — minutes, hours, or after a
    # process restart. This is the correct model for an INTERACTIVE (human) gate
    # (mirrors LangGraph ``interrupt()`` / a Temporal ``wait_condition`` with no
    # timeout: durability is the persisted checkpoint, not a countdown). A positive
    # value suits UNATTENDED/automated review (an abandoned ask self-cleans to
    # ``expired`` — recoverable via retry, not a silent failure).
    hitl_sla_seconds: float = Field(
        default=60.0, validation_alias=AliasChoices("HITL_SLA_SECONDS")
    )

    # --- shared state backend (EXT-C3: multi-replica) -----------------------
    # The ONE tier switch that moves every durable store (run registry, event log,
    # sessions, HITL defer) between its process-local backend and shared Postgres.
    # "local" (default) = the lightweight JSONL / embedded-sqlite / filesystem stores:
    #   correct + zero-dependency for a SINGLE container. "postgres" = the shared
    #   backend REQUIRED to run more than one replica behind a load balancer (no store
    #   is process-local, so any replica can serve/resume any run). Flip this one env
    #   to convert a single container into a fleet member.
    state_backend: str = Field(
        default="local", validation_alias=AliasChoices("WARDEN_STATE_BACKEND")
    )
    # The Postgres DSN every shared store connects to when state_backend="postgres".
    # Falls back to DATABASE_URL (the repo-wide Postgres DSN) so a deploy that already
    # sets DATABASE_URL needs no harness-specific var. Ignored when state_backend="local".
    state_postgres_dsn: str | None = Field(
        default=None,
        validation_alias=AliasChoices("WARDEN_POSTGRES_DSN", "DATABASE_URL"),
    )


@lru_cache
def get_harness_api_settings() -> "HarnessApiSettings":
    return HarnessApiSettings()


# --- config layer ------------------------------------------------------------


class KeysConfig(BaseModel):
    """Managed-key config: the JSON (inline or file path) that maps
    ``user_id → key + budget`` and names each key's ``secret_env``. The secrets
    themselves are never here — they resolve from the live process env at run
    time (see ``credentials/keys.py``)."""

    managed_keys_json: str | None = None
    managed_keys_file: str | None = None


class SpendConfig(BaseModel):
    """Per-user spend/pricing config. ``pricing_json`` overrides the default
    price table (same shape as ``DEFAULT_PRICING``) so a deploy can correct rates
    without a code change."""

    pricing_json: str | None = None


class GovernanceConfig(BaseModel):
    """The Governor switchboard (M2 3g.2a): every governance knob, config-driven.

    ``enabled`` is the master switch — ``False`` ⇒ the ungoverned path is used
    unchanged (GOV-2). ``allow_uncapped`` (default ``True``) gates only MONEY stops:
    when set, a run is admitted even at zero/negative balance (overdraft) and never
    stopped for spend, but the spend is STILL metered (balance goes negative);
    ``deadline`` and ``max_turns`` always enforce when set. The backend fields select
    the concrete ledger + durable
    balance/billing backend the Governor is built from; the ``default_*`` fields
    are the *tier* :class:`~warden.harness_api.governance.policy.GovernancePolicy`
    (the operator default, the least-specific cap layer). ``ledger_backend`` accepts
    the ``postgres`` string but the factory does NOT construct it (no ``asyncpg``
    import here) — a Postgres deploy injects a ``PostgresReservationLedger`` explicitly.
    """

    enabled: bool = True
    allow_uncapped: bool = True
    ledger_backend: Literal["memory", "postgres"] = "memory"
    balance_backend: Literal["jsonl", "null"] = "jsonl"
    state_dir: str | None = None
    default_cost_cap_usd: float | None = None
    default_deadline_s: float | None = None
    default_max_turns: int | None = None
    hard_cap_out: int = 16384
    input_tokens_est: int = 8000


class AuthConfig(BaseModel):
    """The auth switchboard (pre-03 M0 · 3e): the credential resolve→inject knobs.

    ``store_backend`` selects the credential store — ``memory`` (default) seeds the
    legacy ``MANAGED_KEYS`` blob in-process (current single/managed behavior); ``jsonl``
    is the durable append-only per-``(user, provider)`` store (``credentials.jsonl`` in
    ``state_dir``, next to ``balance.jsonl``). ``oauth_allowed_users`` is the policy
    gate (``"*"`` = everyone, else an allow-list); ``on_oauth_denied`` chooses
    ``downgrade`` (to a managed api-key) vs ``reject`` for a non-whitelisted user.
    Managed-key config is NOT duplicated here — it lives in ``keys`` (``KeysConfig``).
    """

    store_backend: Literal["memory", "jsonl"] = "memory"
    state_dir: str | None = None
    oauth_allowed_users: list[str] | Literal["*"] = "*"
    on_oauth_denied: Literal["downgrade", "reject"] = "downgrade"


class RunRegistryConfig(BaseModel):
    """Durable run-identity switchboard (EXT-C1). ``store_backend`` selects the
    registry: ``memory`` (default) is ephemeral (today's behavior); ``jsonl`` is the
    durable append-only ``runs.jsonl`` (in ``state_dir``, beside ``run_events.db``) so
    ``run_id → (user, task)`` survives a restart. Identity-only + append-only — run
    *state* is derived from the event log, never stored here."""

    store_backend: Literal["memory", "jsonl"] = "memory"
    state_dir: str | None = None


class CallerAuthConfig(BaseModel):
    """The caller-authentication switchboard (EXT-T1a): the ``service_name → token``
    map that gates the Runs API. ``service_tokens_json`` (inline) wins over
    ``service_tokens_file`` (a mounted path); both unset ⇒ an empty registry, which
    is **open** (single-tenant dev default). The tokens ARE secrets, so a real deploy
    mounts them (never commits them); the map shape is the only thing modeled here.
    Distinct from ``keys`` (which resolves the provider credential, §10)."""

    service_tokens_json: str | None = None
    service_tokens_file: str | None = None


class HitlConfig(BaseModel):
    """Durable HITL (M6) transport knobs. ``sla_seconds`` bounds how long a paused
    ask may wait before it EXPIRES (a clean, product-synced terminal — never a silent
    deny, never a pinned run). ``None`` ⇒ **indefinite**: no timer is armed and the
    ask stays durably parked until the human returns (the interactive-gate model —
    resumes across a process restart). A positive value suits unattended review (an
    abandoned ask self-cleans to ``expired``, recoverable via retry)."""

    sla_seconds: float | None = 60.0


class StateBackendConfig(BaseModel):
    """The shared-state tier switch (EXT-C3: multi-replica). ``backend`` selects where
    every durable store lives: ``local`` (default) is the process-local backend
    (JSONL / embedded sqlite / filesystem) — correct and dependency-free for a **single
    container**; ``postgres`` is the shared backend **required to run more than one
    replica** behind a load balancer, so no run is owned by one process. ``dsn`` is the
    Postgres DSN the shared stores connect to (ignored when ``local``). A single flag,
    read by every store's ``build_*`` switch, so one env moves the whole fleet."""

    backend: Literal["local", "postgres"] = "local"
    dsn: str | None = None

    @property
    def is_postgres(self) -> bool:
        return self.backend == "postgres"


class HarnessApiConfig(BaseModel):
    """The Axis-2 config: the engine config plus the account/billing layer."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    engine: HarnessConfig = Field(default_factory=HarnessConfig)
    keys: KeysConfig = Field(default_factory=KeysConfig)
    spend: SpendConfig = Field(default_factory=SpendConfig)
    governance: GovernanceConfig = Field(default_factory=GovernanceConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    caller_auth: CallerAuthConfig = Field(default_factory=CallerAuthConfig)
    run_registry: RunRegistryConfig = Field(default_factory=RunRegistryConfig)
    hitl: HitlConfig = Field(default_factory=HitlConfig)
    state: StateBackendConfig = Field(default_factory=StateBackendConfig)


def get_harness_api_config() -> HarnessApiConfig:
    """The single read point for the Axis-2 config: the engine config (from
    ``get_harness_config()``) plus the account/billing slices from the env layer."""
    s = get_harness_api_settings()
    return HarnessApiConfig(
        engine=get_harness_config(),
        keys=KeysConfig(
            managed_keys_json=s.managed_keys_json,
            managed_keys_file=s.managed_keys_file,
        ),
        spend=SpendConfig(pricing_json=s.pricing_json),
        governance=GovernanceConfig(
            enabled=s.governance_enabled,
            allow_uncapped=s.governance_allow_uncapped,
            ledger_backend=s.governance_ledger_backend,
            balance_backend=s.governance_balance_backend,
            state_dir=s.governance_state_dir,
            default_cost_cap_usd=s.governance_default_cost_cap_usd,
            default_deadline_s=s.governance_default_deadline_s,
            default_max_turns=s.governance_default_max_turns,
            hard_cap_out=s.governance_hard_cap_out,
            input_tokens_est=s.governance_input_tokens_est,
        ),
        auth=AuthConfig(
            store_backend=s.auth_store_backend,  # type: ignore[arg-type]
            state_dir=s.auth_state_dir,
            oauth_allowed_users=_parse_allowed_users(s.auth_oauth_allowed_users),
            on_oauth_denied=s.auth_on_oauth_denied,  # type: ignore[arg-type]
        ),
        caller_auth=CallerAuthConfig(
            service_tokens_json=s.service_tokens_json,
            service_tokens_file=s.service_tokens_file,
        ),
        run_registry=RunRegistryConfig(
            store_backend=s.run_registry_store_backend,  # type: ignore[arg-type]
            state_dir=s.run_registry_state_dir,
        ),
        # ``<= 0`` from the env means indefinite (no auto-expiry) → None.
        hitl=HitlConfig(
            sla_seconds=s.hitl_sla_seconds if s.hitl_sla_seconds > 0 else None
        ),
        state=StateBackendConfig(
            backend=s.state_backend,  # type: ignore[arg-type]
            dsn=s.state_postgres_dsn,
        ),
    )


def _parse_allowed_users(raw: str | None) -> list[str] | Literal["*"]:
    """Env is a comma-separated allow-list or ``"*"`` (default); parse to the typed
    field. Empty / unset / ``"*"`` ⇒ no restriction."""
    if not raw or raw.strip() == "*":
        return "*"
    return [u.strip() for u in raw.split(",") if u.strip()]
