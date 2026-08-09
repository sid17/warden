"""pre-03 M0 · 3c — the ``AuthResolver``: the single "which credential + allowed?" step.

Answers, for a ``(user_id, provider)``: which typed :data:`AuthMethod`, is its secret
present, and is the user **allowed** to use it — returning the safe
:class:`ResolvedCredential`. Policy lives on the identity (an
``oauth_allowed_users`` allow-list), checked HERE at resolve, never on the secret
(the field consensus). A non-whitelisted user whose stored credential is OAuth is
either **downgraded** to a managed API key (when a per-provider fallback exists) or
**rejected** with a typed reason (``oauth_not_permitted_for_user``), per
``on_oauth_denied``. Default ``"*"`` never restricts — single-tenant is unchanged.

:meth:`AuthResolver.auth_env_for` is the drop-in the Governor (3d) and the ungoverned
Runner call: same signature + ``{var: secret} | None`` shape as
``KeyRegistry.auth_env_for``, so governed and ungoverned runs resolve credentials the
SAME way. The store is the typed primary source; the per-provider ``fallback_methods``
carry the operator's managed default (default-key / downgrade target).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

from warden.harness_api.credentials.injection import injection_env
from warden.harness_api.credentials.methods import (
    AuthMethod,
    Inherit,
    OAuthToken,
    ResolvedCredential,
)
from warden.harness_api.credentials.store import CredentialStore

OAUTH_NOT_PERMITTED = "oauth_not_permitted_for_user"


@runtime_checkable
class CredentialResolver(Protocol):
    """The credential contract the Governor + ungoverned Runner depend on — the
    ``{var: secret} | None`` env overlay to inject for a ``(user_id, provider)``.

    Both the legacy :class:`~warden.harness_api.credentials.keys.KeyRegistry`
    and the typed :class:`AuthResolver` satisfy it, so a run resolves credentials the
    SAME way whether the caller holds the old registry (tests) or the unified resolver
    (production). ``None`` ⇒ inherit the launching process credential.
    """

    def auth_env_for(self, user_id: str, provider: str) -> dict[str, str] | None: ...


@dataclass(frozen=True)
class AuthPolicy:
    """The resolve-time policy gate. ``oauth_allowed_users`` is an allow-list of
    ``user_id`` (or the literal ``"*"`` = everyone). A user outside it resolving to
    OAuth is handled per ``on_oauth_denied``: ``downgrade`` to a managed api-key when
    one exists, else ``reject``. Shaped to hold sibling policies (allowed providers /
    modes per user) when a real consumer appears (LAW 5) — not before."""

    oauth_allowed_users: tuple[str, ...] | Literal["*"] = "*"
    on_oauth_denied: Literal["downgrade", "reject"] = "downgrade"

    def oauth_permitted(self, user_id: str) -> bool:
        if self.oauth_allowed_users == "*":
            return True
        return user_id in self.oauth_allowed_users


@dataclass
class AuthResolver:
    """Resolve ``(user_id, provider) → ResolvedCredential`` against a store + policy.

    ``store`` is the typed primary source; ``policy`` gates OAuth; ``fallback_methods``
    maps a provider to the operator's managed default method (used for an unmapped user
    and as the OAuth-downgrade target); ``secrets`` is the value source for
    fingerprint/injection (``None`` ⇒ live :data:`os.environ`).
    """

    store: CredentialStore
    policy: AuthPolicy = field(default_factory=AuthPolicy)
    fallback_methods: Mapping[str, AuthMethod] = field(default_factory=dict)
    secrets: Mapping[str, str] | None = None

    def resolve(self, user_id: str, provider: str) -> ResolvedCredential:
        """The single resolution step (see module docstring)."""
        record = self.store.get(user_id, provider)
        if record is not None:
            method = record.to_method()
            if isinstance(method, OAuthToken) and not self.policy.oauth_permitted(user_id):
                return self._deny_oauth(provider)
            return ResolvedCredential.from_method(provider, method, self.secrets)
        fallback = self.fallback_methods.get(provider)
        method = fallback if fallback is not None else Inherit()
        return ResolvedCredential.from_method(provider, method, self.secrets)

    def _deny_oauth(self, provider: str) -> ResolvedCredential:
        """OAuth denied by policy: downgrade to a managed api-key if one exists, else
        reject (never inject the OAuth token the user isn't permitted to use)."""
        if self.policy.on_oauth_denied == "downgrade":
            fallback = self.fallback_methods.get(provider)
            if fallback is not None:
                return ResolvedCredential.from_method(provider, fallback, self.secrets)
        return ResolvedCredential(
            provider=provider,
            method=Inherit(),
            fingerprint="",
            authed=False,
            denied_reason=OAUTH_NOT_PERMITTED,
        )

    def auth_env_for(self, user_id: str, provider: str) -> dict[str, str] | None:
        """Drop-in for ``KeyRegistry.auth_env_for``: the ``{var: secret} | None``
        overlay to inject (``None`` ⇒ inherit). Governed + ungoverned runs call this."""
        return injection_env(self.resolve(user_id, provider).method, self.secrets)


__all__ = ["AuthPolicy", "AuthResolver", "CredentialResolver", "OAUTH_NOT_PERMITTED"]
