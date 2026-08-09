"""Managed multi-key registry (harness-owned, shared across products).

The operator holds several provider keys (one per user *tier*) and a map of
``user_id → key + budget``. This module resolves, per run, the auth env vars to
inject into the provider subprocess — the value the ``auth_env`` seam
(``config.auth.auth_env`` → ``ChatAPI`` → ``home_env`` → ``ClaudeCliSession``
subprocess ``env=``) carries so each concurrent run uses its user's key with no
``os.environ`` bleed.

**Secrets are never baked.** The config holds only ``secret_env`` — the name of
an env var that carries the actual key at runtime (mounted / injected by the
container). The config itself (key ids, tiers, user map, budgets) is safe to
commit; the secrets are not in it.

Config source (first found wins):
  1. ``MANAGED_KEYS_JSON`` — inline JSON.
  2. ``MANAGED_KEYS_FILE`` — path to a mounted JSON file.
  3. none → an empty registry (``auth_env_for`` returns ``None`` → runs inherit
     the launching process credential, the single-key dev/default behavior).

Config shape::

    {
      "keys": {
        "anthropic-standard": {"provider": "claude", "tier": "standard",
                                "secret_env": "ANTHROPIC_KEY_STANDARD"},
        "anthropic-pro":      {"provider": "claude", "tier": "pro",
                                "secret_env": "ANTHROPIC_KEY_PRO"}
      },
      "users": {
        "u1": {"key_id": "anthropic-standard", "budget_usd": 5.0},
        "u2": {"key_id": "anthropic-pro",      "budget_usd": 50.0}
      },
      "default_key_id": "anthropic-standard",
      "default_budget_usd": 5.0
    }
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from warden.harness_api.config import KeysConfig
    from warden.harness_api.credentials.resolver import AuthResolver

# Which env var a provider's key must be injected as, and the fallback if a key
# entry does not name its own ``auth_var``.
_DEFAULT_AUTH_VAR: dict[str, str] = {
    "claude": "ANTHROPIC_API_KEY",
    "claude-cli": "ANTHROPIC_API_KEY",
    "codex": "OPENAI_API_KEY",
}


@dataclass(frozen=True)
class ManagedKey:
    """One operator-held key. ``secret_env`` names the env var holding the actual
    secret at runtime — the secret is never stored on this object at config time
    (resolved lazily in :meth:`KeyRegistry.auth_env_for`)."""

    key_id: str
    provider: str
    tier: str
    secret_env: str
    auth_var: str  # the env var name the subprocess must see the key under


class KeyRegistry:
    """Resolves ``user_id → the auth env to inject`` and ``user_id → budget``.

    Pure w.r.t. ``os.environ``: it never mutates it. Secrets are read from the
    provided ``secrets`` mapping (defaults to ``os.environ``) only at resolve
    time, so rotating a secret env var takes effect on the next run.
    """

    def __init__(
        self,
        keys: Mapping[str, ManagedKey],
        users: Mapping[str, dict],
        *,
        default_key_id: str | None = None,
        default_budget_usd: float | None = None,
        secrets: Mapping[str, str] | None = None,
    ) -> None:
        self._keys = dict(keys)
        self._users = dict(users)
        self._default_key_id = default_key_id
        self._default_budget_usd = default_budget_usd
        # Secret *source*; defaults to os.environ (the container env seam).
        self._secrets = secrets if secrets is not None else os.environ

    # --- construction -----------------------------------------------------

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "KeyRegistry":
        """Build from ``MANAGED_KEYS_JSON`` / ``MANAGED_KEYS_FILE`` in ``env``
        (defaults to ``os.environ``), which is also the secret source."""
        src = env if env is not None else os.environ
        return cls._from_raw(
            src.get("MANAGED_KEYS_JSON"), src.get("MANAGED_KEYS_FILE"), secrets=src
        )

    @classmethod
    def from_keys_config(
        cls, keys: "KeysConfig", secrets: Mapping[str, str] | None = None
    ) -> "KeyRegistry":
        """Build from the typed Axis-2 ``KeysConfig`` (managed-key JSON/file),
        resolving secrets from the live process env (``secrets`` defaults to
        ``os.environ``). The engine never sees this — it is the account layer."""
        return cls._from_raw(
            keys.managed_keys_json,
            keys.managed_keys_file,
            secrets=secrets if secrets is not None else os.environ,
        )

    @classmethod
    def _from_raw(
        cls,
        raw_json: str | None,
        file_path: str | None,
        *,
        secrets: Mapping[str, str],
    ) -> "KeyRegistry":
        """Shared loader: inline JSON wins over a file path.

        Missing config → an empty registry (every ``auth_env_for`` returns
        ``None``, i.e. inherit the launching credential). Malformed config is a
        hard error (LAW 4: never silently ignore) so a bad deploy fails loudly.
        """
        raw = raw_json
        if not raw and file_path:
            raw = Path(file_path).read_text(encoding="utf-8")
        if not raw:
            return cls(keys={}, users={}, secrets=secrets)
        try:
            cfg = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid managed-keys config JSON: {exc}") from exc
        return cls.from_config(cfg, secrets=secrets)

    @classmethod
    def from_config(
        cls, cfg: Mapping, secrets: Mapping[str, str] | None = None
    ) -> "KeyRegistry":
        keys: dict[str, ManagedKey] = {}
        for key_id, entry in (cfg.get("keys") or {}).items():
            provider = entry["provider"]
            keys[key_id] = ManagedKey(
                key_id=key_id,
                provider=provider,
                tier=entry.get("tier", "default"),
                secret_env=entry["secret_env"],
                auth_var=entry.get("auth_var")
                or _DEFAULT_AUTH_VAR.get(provider, "ANTHROPIC_API_KEY"),
            )
        return cls(
            keys=keys,
            users=cfg.get("users") or {},
            default_key_id=cfg.get("default_key_id"),
            default_budget_usd=cfg.get("default_budget_usd"),
            secrets=secrets,
        )

    # --- resolution -------------------------------------------------------

    def _key_id_for(self, user_id: str) -> str | None:
        user = self._users.get(user_id)
        if user and user.get("key_id"):
            return user["key_id"]
        return self._default_key_id

    def has_keys(self) -> bool:
        """True when any managed key is configured (else runs inherit env)."""
        return bool(self._keys)

    def auth_env_for(self, user_id: str, provider: str) -> dict[str, str] | None:
        """Return ``{auth_var: secret}`` for this user+provider, or ``None``.

        ``None`` means "no managed key applies" — the caller should pass no
        ``auth_env`` and let the subprocess inherit the launching credential.

        Raises:
            ValueError: a key is mapped but its ``secret_env`` is unset at
                runtime (a misconfiguration we must not paper over — LAW 4).
        """
        key_id = self._key_id_for(user_id)
        if key_id is None:
            return None
        key = self._keys.get(key_id)
        if key is None:
            raise ValueError(
                f"user {user_id!r} maps to unknown key_id {key_id!r}"
            )
        if key.provider != provider:
            # The user's key is for a different provider than this run requests.
            # Fall back to inheritance rather than inject a mismatched key.
            return None
        secret = self._secrets.get(key.secret_env)
        if not secret:
            raise ValueError(
                f"key {key_id!r} secret env {key.secret_env!r} is not set"
            )
        return {key.auth_var: secret}

    # --- legacy adapter: present as the typed AuthResolver (pre-03 3d) ----

    def to_auth_resolver(self, policy=None) -> "AuthResolver":
        """Adapt this flat managed-key registry into a typed
        :class:`~warden.harness_api.credentials.resolver.AuthResolver`.

        Each ``user → key_id`` mapping seeds one ``(user, key.provider)`` **api-key**
        record in an in-memory store (managed keys are operator-provisioned secrets);
        ``default_key_id`` becomes the per-provider fallback (the unmapped-user default
        and the OAuth-downgrade target). The resolver shares this registry's secret
        source, so resolution is behavior-identical to :meth:`auth_env_for` for every
        managed user — the AUTH-4 typed path, back-compatible. ``policy`` defaults to
        the open gate (``"*"``); a deploy overrides it via the config switchboard (3e).
        """
        from warden.harness_api.credentials.methods import ApiKey
        from warden.harness_api.credentials.resolver import (
            AuthPolicy,
            AuthResolver,
        )
        from warden.harness_api.credentials.store import (
            CredentialRecord,
            InMemoryCredentialStore,
        )

        store = InMemoryCredentialStore()
        records: list[CredentialRecord] = []
        for user_id, user in self._users.items():
            key = self._keys.get(user.get("key_id")) if user.get("key_id") else None
            if key is None:
                continue
            records.append(CredentialRecord(
                user_id=user_id, provider=key.provider, auth_method="api_key",
                secret_ref=key.secret_env, var=key.auth_var, tier=key.tier,
            ))
        store.seed(records)

        fallback: dict[str, ApiKey] = {}
        default = self._keys.get(self._default_key_id) if self._default_key_id else None
        if default is not None:
            fallback[default.provider] = ApiKey(
                var=default.auth_var, key_ref=default.secret_env
            )
        return AuthResolver(
            store=store,
            policy=policy or AuthPolicy(),
            fallback_methods=fallback,
            secrets=self._secrets,
        )

    def budget_for(self, user_id: str) -> float | None:
        """Per-user spend cap in USD, or ``None`` for uncapped."""
        user = self._users.get(user_id)
        if user and user.get("budget_usd") is not None:
            return float(user["budget_usd"])
        return self._default_budget_usd
