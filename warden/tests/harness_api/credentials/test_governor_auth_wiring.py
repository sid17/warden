"""pre-03 M0 · 3d — the Governor + ungoverned Runner call the AuthResolver (AUTH-9).

Governed and ungoverned runs must resolve credentials the SAME way. This locks:

  * ``GovernorService.resolve()`` obtains its ``auth_env`` from the injected
    :class:`AuthResolver` (a ``CredentialResolver``), not a bespoke path — the
    ``RunGovernor.auth_env`` equals ``resolver.auth_env_for(user, provider)``.
  * ``KeyRegistry.to_auth_resolver()`` (the legacy adapter) is behavior-identical to
    the old ``KeyRegistry.auth_env_for`` for every managed user — so the swap is a
    no-op for existing deploys (back-compat), while newly enabling the typed path.
  * Built from the same config, the governed resolver and the ungoverned Runner's
    resolver yield identical injection — one credential path.
"""

from __future__ import annotations

import asyncio

from warden.harness_api.credentials.keys import KeyRegistry
from warden.harness_api.credentials.resolver import AuthResolver
from warden.harness_api.credentials.store import (
    CredentialRecord,
    InMemoryCredentialStore,
)
from warden.harness_api.governance.billing import NullBillingBackend
from warden.harness_api.governance.governor import GovernorService
from warden.harness_api.governance.ledger import InMemoryReservationLedger
from warden.harness_api.governance.policy import GovernancePolicy


def _run(coro):
    return asyncio.run(coro)


_CFG = {
    "keys": {
        "anthropic-standard": {"provider": "claude", "tier": "standard",
                               "secret_env": "ANTHROPIC_KEY_STANDARD"},
        "anthropic-pro": {"provider": "claude", "tier": "pro",
                          "secret_env": "ANTHROPIC_KEY_PRO"},
    },
    "users": {
        "u1": {"key_id": "anthropic-standard"},
        "u2": {"key_id": "anthropic-pro"},
    },
    "default_key_id": "anthropic-standard",
}
_SECRETS = {"ANTHROPIC_KEY_STANDARD": "sk-standard", "ANTHROPIC_KEY_PRO": "sk-pro"}


# === the Governor delegates the credential half to the AuthResolver =========

def test_governor_resolve_uses_auth_resolver() -> None:
    async def _test() -> None:
        store = InMemoryCredentialStore()
        await store.load()
        await store.put(CredentialRecord(
            user_id="u1", provider="codex", auth_method="api_key",
            secret_ref="U1_OPENAI",
        ))
        resolver = AuthResolver(store, secrets={"U1_OPENAI": "sk-u1-openai"})
        service = GovernorService(
            key_registry=resolver,  # a CredentialResolver, not a bare KeyRegistry
            ledger=InMemoryReservationLedger(),
            billing=NullBillingBackend(),
            tier_policy=GovernancePolicy(cost_cap_usd=1000.0),
        )
        gov = await service.resolve(
            user_id="u1", task_id="t", provider="codex", model="gpt-5",
        )
        assert gov.auth_env == resolver.auth_env_for("u1", "codex")
        assert gov.auth_env == {"OPENAI_API_KEY": "sk-u1-openai"}

    _run(_test())


# === legacy adapter parity: to_auth_resolver == old auth_env_for ============

def test_legacy_adapter_parity() -> None:
    """The typed resolver resolves every managed user exactly as the flat registry
    did — including provider-mismatch inherit and the default-key stranger fallback."""
    reg = KeyRegistry.from_config(_CFG, secrets=_SECRETS)
    resolver = reg.to_auth_resolver()
    for user in ("u1", "u2", "stranger"):
        assert resolver.auth_env_for(user, "claude") == reg.auth_env_for(user, "claude")
    # provider mismatch inherits under both (u1's key is claude, run is codex).
    assert resolver.auth_env_for("u1", "codex") == reg.auth_env_for("u1", "codex") is None


def test_empty_registry_adapter_inherits() -> None:
    resolver = KeyRegistry(keys={}, users={}).to_auth_resolver()
    assert resolver.auth_env_for("u1", "claude") is None


# === governed + ungoverned built from same config → identical injection =====

def test_governed_and_ungoverned_same_injection() -> None:
    governed = KeyRegistry.from_config(_CFG, secrets=_SECRETS).to_auth_resolver()
    ungoverned = KeyRegistry.from_config(_CFG, secrets=_SECRETS).to_auth_resolver()
    for user in ("u1", "u2", "stranger"):
        assert governed.auth_env_for(user, "claude") == ungoverned.auth_env_for(user, "claude")
