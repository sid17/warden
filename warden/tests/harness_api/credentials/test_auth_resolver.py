"""pre-03 M0 · 3c — the policy gate + ``AuthResolver.resolve`` (AUTH-6).

The resolver is the single "which credential, and is the user allowed to use it"
step. Policy lives on the identity (an ``oauth_allowed_users`` allow-list), checked
at resolve — a non-whitelisted user whose credential is OAuth is either **downgraded**
to a managed API key (if a fallback exists) or **rejected** with a typed reason,
per ``on_oauth_denied``. Default ``"*"`` never restricts (single-tenant unchanged).

``auth_env_for`` is the drop-in the Governor / ungoverned Runner call in 3d — same
signature/shape as ``KeyRegistry.auth_env_for`` (``{var: secret} | None``), so the
seam swap is additive.
"""

from __future__ import annotations

import asyncio

from warden.harness_api.credentials.methods import ApiKey, OAuthToken
from warden.harness_api.credentials.resolver import AuthPolicy, AuthResolver
from warden.harness_api.credentials.store import (
    CredentialRecord,
    InMemoryCredentialStore,
)


def _run(coro):
    return asyncio.run(coro)


_SECRETS = {
    "U1_CLAUDE_OAUTH": "tok-u1-oauth-aaaa",
    "MANAGED_ANTHROPIC": "sk-managed-bbbb",
    "U1_OPENAI": "sk-u1-openai-cccc",
}

_OAUTH = CredentialRecord(
    user_id="u1", provider="claude", auth_method="oauth", secret_ref="U1_CLAUDE_OAUTH",
)
_MANAGED_FALLBACK = {"claude": ApiKey(var="ANTHROPIC_API_KEY", key_ref="MANAGED_ANTHROPIC")}


async def _store_with(*records) -> InMemoryCredentialStore:
    store = InMemoryCredentialStore()
    await store.load()
    for rec in records:
        await store.put(rec)
    return store


# === default policy "*" never restricts ====================================

def test_default_policy_grants_oauth() -> None:
    async def _test() -> None:
        store = await _store_with(_OAUTH)
        resolver = AuthResolver(store, secrets=_SECRETS)  # default policy "*"
        rc = resolver.resolve("u1", "claude")
        assert isinstance(rc.method, OAuthToken)
        assert rc.describe()["mode"] == "oauth"
        assert rc.denied_reason is None

    _run(_test())


# === whitelist: allowed user keeps OAuth ====================================

def test_whitelisted_user_keeps_oauth() -> None:
    async def _test() -> None:
        store = await _store_with(_OAUTH)
        policy = AuthPolicy(oauth_allowed_users=("u1",))
        resolver = AuthResolver(store, policy, secrets=_SECRETS)
        rc = resolver.resolve("u1", "claude")
        assert isinstance(rc.method, OAuthToken)

    _run(_test())


# === whitelist: non-whitelisted user downgraded to managed api-key ==========

def test_non_whitelisted_user_downgraded() -> None:
    async def _test() -> None:
        store = await _store_with(
            CredentialRecord(user_id="u2", provider="claude",
                             auth_method="oauth", secret_ref="U1_CLAUDE_OAUTH"),
        )
        policy = AuthPolicy(oauth_allowed_users=("u1",), on_oauth_denied="downgrade")
        resolver = AuthResolver(store, policy, secrets=_SECRETS,
                                fallback_methods=_MANAGED_FALLBACK)
        rc = resolver.resolve("u2", "claude")
        assert isinstance(rc.method, ApiKey)  # downgraded, not the OAuth token
        assert rc.method.var == "ANTHROPIC_API_KEY"
        assert rc.denied_reason is None
        # And the injected env is the managed key, not the OAuth token.
        assert resolver.auth_env_for("u2", "claude") == {
            "ANTHROPIC_API_KEY": "sk-managed-bbbb"
        }

    _run(_test())


# === whitelist: non-whitelisted user rejected (typed reason) ================

def test_non_whitelisted_user_rejected() -> None:
    async def _test() -> None:
        store = await _store_with(
            CredentialRecord(user_id="u2", provider="claude",
                             auth_method="oauth", secret_ref="U1_CLAUDE_OAUTH"),
        )
        policy = AuthPolicy(oauth_allowed_users=("u1",), on_oauth_denied="reject")
        resolver = AuthResolver(store, policy, secrets=_SECRETS)
        rc = resolver.resolve("u2", "claude")
        assert rc.denied_reason == "oauth_not_permitted_for_user"
        assert rc.authed is False
        # A rejected credential injects nothing (does NOT fall back to OAuth).
        assert resolver.auth_env_for("u2", "claude") is None

    _run(_test())


def test_downgrade_with_no_fallback_rejects() -> None:
    """downgrade requested but no managed api-key available ⇒ reject, never leak
    the OAuth token the user isn't allowed to use."""

    async def _test() -> None:
        store = await _store_with(
            CredentialRecord(user_id="u2", provider="claude",
                             auth_method="oauth", secret_ref="U1_CLAUDE_OAUTH"),
        )
        policy = AuthPolicy(oauth_allowed_users=("u1",), on_oauth_denied="downgrade")
        resolver = AuthResolver(store, policy, secrets=_SECRETS)  # no fallback
        rc = resolver.resolve("u2", "claude")
        assert rc.denied_reason == "oauth_not_permitted_for_user"

    _run(_test())


# === no record → managed default fallback, else inherit =====================

def test_no_record_uses_fallback_then_inherit() -> None:
    async def _test() -> None:
        store = await _store_with()  # empty
        # With a per-provider managed default → api-key.
        resolver = AuthResolver(store, secrets=_SECRETS, fallback_methods=_MANAGED_FALLBACK)
        assert isinstance(resolver.resolve("stranger", "claude").method, ApiKey)
        # Without one → Inherit (auth_env None ⇒ process credential).
        bare = AuthResolver(store, secrets=_SECRETS)
        rc = bare.resolve("stranger", "claude")
        assert rc.describe()["mode"] == "none"
        assert bare.auth_env_for("stranger", "claude") is None

    _run(_test())


# === auth_env_for drop-in shape (matches KeyRegistry.auth_env_for) ==========

def test_auth_env_for_shapes() -> None:
    async def _test() -> None:
        store = await _store_with(
            _OAUTH,
            CredentialRecord(user_id="u1", provider="codex",
                             auth_method="api_key", secret_ref="U1_OPENAI"),
        )
        resolver = AuthResolver(store, secrets=_SECRETS)
        assert resolver.auth_env_for("u1", "claude") == {
            "CLAUDE_CODE_OAUTH_TOKEN": "tok-u1-oauth-aaaa"
        }
        assert resolver.auth_env_for("u1", "codex") == {"OPENAI_API_KEY": "sk-u1-openai-cccc"}

    _run(_test())


def test_session_file_auth_env_sets_home_var(tmp_path) -> None:
    async def _test() -> None:
        (tmp_path / "auth.json").write_text("{}", encoding="utf-8")
        store = await _store_with(
            CredentialRecord(user_id="u1", provider="codex",
                             auth_method="session_file", secret_ref=str(tmp_path)),
        )
        resolver = AuthResolver(store, secrets={})
        assert resolver.auth_env_for("u1", "codex") == {"CODEX_HOME": str(tmp_path)}

    _run(_test())
