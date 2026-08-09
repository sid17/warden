"""pre-03 M0 · the ``--auth-typed`` bed gate driver — typed resolve → inject.

Proves the unified auth path end-to-end on the REAL code (``AuthResolver`` built by
the config switchboard from a durable ``credentials.jsonl``), for the three provider
shapes, plus the policy gate and AUTH-4 multi-cred and the no-``os.environ``-bleed
invariant:

  * a **whitelisted** user (``u_me``) resolves an ``OAuthToken`` for ``claude`` and
    injects ``CLAUDE_CODE_OAUTH_TOKEN``;
  * a **non-whitelisted** user (``u_other``) resolving OAuth is **downgraded** to the
    managed api-key by the policy gate;
  * the same ``u_me`` holds a **separate** Codex ``api_key`` record and injects
    ``OPENAI_API_KEY`` on ``codex`` (AUTH-4 — one user, different cred per provider);
  * two concurrent ``apply_method`` injections don't bleed, and an **ambient OAuth
    token is stripped** before an api-key is injected — ``os.environ`` is never touched.

Secrets are held **by reference** to (fake) env-var names, so this gate is GREEN with
**no live credential** — the bed run adds the real image and (optionally) a live turn
on the resolved credential. ``0`` = PASS (entrypoint prints ``GATE: PASS/FAIL``).

Run directly (host or container):
    python -m warden.tests.e2e.auth_typed_smoke
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

from warden.harness_api.config import AuthConfig, HarnessApiConfig, KeysConfig
from warden.harness_api.credentials.config import build_auth_resolver, init_auth
from warden.harness_api.credentials.injection import apply_method
from warden.harness_api.credentials.methods import ApiKey, OAuthToken
from warden.harness_api.credentials.store import (
    CredentialRecord,
    JsonlCredentialStore,
)

# Fake secret VALUES injected under their env-var NAMES (the by-reference model — in
# the real bed these are the host's OAuth token / managed key, injected at docker run).
_FAKE_SECRETS = {
    "ME_CLAUDE_OAUTH": "oauth-me-tok-1111",
    "OTHER_CLAUDE_OAUTH": "oauth-other-tok-2222",
    "ME_OPENAI": "sk-me-openai-3333",
    "MANAGED_ANTHROPIC": "sk-managed-anthropic-4444",
}

_MANAGED_KEYS = {
    "keys": {"managed-anthropic": {"provider": "claude",
                                   "secret_env": "MANAGED_ANTHROPIC"}},
    "users": {},
    "default_key_id": "managed-anthropic",  # the OAuth-downgrade / stranger fallback
}


def _fail(msg: str) -> int:
    print(f" AUTH-TYPED SMOKE: FAIL — {msg}")
    print("=" * 66)
    return 1


async def _seed_store(state_dir: Path) -> None:
    """Write the durable credentials.jsonl the resolver will replay."""
    store = JsonlCredentialStore(state_dir / "credentials.jsonl")
    await store.load()
    await store.put(CredentialRecord(
        user_id="u_me", provider="claude", auth_method="oauth",
        secret_ref="ME_CLAUDE_OAUTH"))
    await store.put(CredentialRecord(
        user_id="u_other", provider="claude", auth_method="oauth",
        secret_ref="OTHER_CLAUDE_OAUTH"))
    await store.put(CredentialRecord(  # AUTH-4: same user, a Codex api-key
        user_id="u_me", provider="codex", auth_method="api_key",
        secret_ref="ME_OPENAI"))


async def main() -> int:
    print("=" * 66)
    print(" pre-03 M0 — --auth-typed: typed resolve→inject + policy + AUTH-4 + no-bleed")
    print("=" * 66)
    # Fake secrets ambient (as if injected at `docker run`), by reference.
    for name, value in _FAKE_SECRETS.items():
        os.environ[name] = value
    env_before = dict(os.environ)

    with tempfile.TemporaryDirectory() as tmp:
        state_dir = Path(tmp)
        await _seed_store(state_dir)

        cfg = HarnessApiConfig(
            keys=KeysConfig(managed_keys_json=json.dumps(_MANAGED_KEYS)),
            auth=AuthConfig(
                store_backend="jsonl", state_dir=str(state_dir),
                oauth_allowed_users=["u_me"], on_oauth_denied="downgrade",
            ),
        )
        resolver = build_auth_resolver(cfg)
        await init_auth(resolver)  # replays credentials.jsonl

        # 1. whitelisted user → OAuth, injects the oauth token.
        me_claude = resolver.resolve("u_me", "claude")
        if not isinstance(me_claude.method, OAuthToken):
            return _fail(f"u_me/claude expected OAuthToken, got {me_claude.method!r}")
        if resolver.auth_env_for("u_me", "claude") != {
            "CLAUDE_CODE_OAUTH_TOKEN": "oauth-me-tok-1111"
        }:
            return _fail("u_me/claude did not inject CLAUDE_CODE_OAUTH_TOKEN")
        print("[ok] u_me/claude → OAuthToken → CLAUDE_CODE_OAUTH_TOKEN")

        # 2. non-whitelisted user → downgraded to the managed api-key (policy gate).
        other = resolver.resolve("u_other", "claude")
        if not isinstance(other.method, ApiKey) or other.denied_reason is not None:
            return _fail(f"u_other/claude expected downgraded ApiKey, got {other!r}")
        if resolver.auth_env_for("u_other", "claude") != {
            "ANTHROPIC_API_KEY": "sk-managed-anthropic-4444"
        }:
            return _fail("u_other/claude did not downgrade to the managed api-key")
        print("[ok] u_other/claude → downgraded ApiKey (policy gate)")

        # 3. AUTH-4: the same user holds a distinct Codex api-key.
        me_codex = resolver.resolve("u_me", "codex")
        if not isinstance(me_codex.method, ApiKey):
            return _fail(f"u_me/codex expected ApiKey, got {me_codex.method!r}")
        if resolver.auth_env_for("u_me", "codex") != {"OPENAI_API_KEY": "sk-me-openai-3333"}:
            return _fail("u_me/codex did not inject OPENAI_API_KEY")
        print("[ok] u_me/codex → ApiKey → OPENAI_API_KEY (AUTH-4 multi-cred)")

        # 4. no bleed: inject each resolved method into its own env; an ambient OAuth
        #    token must be stripped before an api-key lands, and os.environ untouched.
        base = {"CLAUDE_CODE_OAUTH_TOKEN": "ambient-poison", "PATH": "/usr/bin"}
        env_me = apply_method(dict(base), "claude", me_claude.method)
        env_other = apply_method(dict(base), "claude", other.method)
        if env_me.get("CLAUDE_CODE_OAUTH_TOKEN") != "oauth-me-tok-1111":
            return _fail("u_me injection did not overwrite the ambient token")
        if "CLAUDE_CODE_OAUTH_TOKEN" in env_other:
            return _fail("ambient OAuth token shadowed u_other's injected api-key")
        if env_other.get("ANTHROPIC_API_KEY") != "sk-managed-anthropic-4444":
            return _fail("u_other injection missing the managed api-key")
        if dict(os.environ) != env_before:
            return _fail("os.environ was mutated by injection (bleed)")
        print("[ok] no bleed: ambient token stripped, os.environ untouched")

    print("=" * 66)
    print(" AUTH-TYPED SMOKE: PASS — typed resolve→inject, policy, AUTH-4, no bleed.")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
