"""pre-03 M0 · 3e — type-dispatched injection (AUTH-8) + the AuthConfig switchboard.

Two parts of the final rung:

  * **AUTH-8 — injection dispatches on the typed method through ONE injector,
    strip-then-inject, no bleed.** ``apply_method`` composes ``injection_env`` (the
    overlay) with ``BaseProvider.apply_auth_env`` (strip inherited creds for the
    provider, then overlay). The load-bearing invariant: an ambient OAuth token must
    NOT shadow an injected API key (it is stripped first), two concurrent injections
    don't cross-contaminate, and ``os.environ`` is never mutated. File-mode
    (``SessionFile``) is a first-class code path here (``CODEX_HOME`` set), not bed-only.
  * **The switchboard** — ``build_auth_resolver(cfg)`` assembles the resolver from the
    typed ``Settings`` (policy gate + store backend), mirroring
    ``governance/config.py``. Default ``memory`` backend seeds the legacy managed keys
    (back-compat); ``jsonl`` backend is the durable multi-tenant store.
"""

from __future__ import annotations

import asyncio
import json
import os

from warden.harness_api.config import (
    AuthConfig,
    HarnessApiConfig,
    KeysConfig,
)
from warden.harness_api.credentials.config import (
    build_auth_resolver,
    init_auth,
)
from warden.harness_api.credentials.injection import apply_method
from warden.harness_api.credentials.methods import ApiKey, Inherit, SessionFile
from warden.harness_api.credentials.store import JsonlCredentialStore


# === AUTH-8 — strip-then-inject dispatch, no bleed ==========================

def test_ambient_oauth_stripped_before_key_injected() -> None:
    """The single load-bearing invariant: an inherited OAuth token cannot shadow an
    injected API key (the Claude transport prefers OAuth) — it is stripped first."""
    base = {"CLAUDE_CODE_OAUTH_TOKEN": "ambient-oauth", "PATH": "/usr/bin"}
    out = apply_method(
        dict(base), "claude",
        ApiKey(var="ANTHROPIC_API_KEY", key_ref="K"), secrets={"K": "sk-injected"},
    )
    assert out["ANTHROPIC_API_KEY"] == "sk-injected"
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in out  # stripped ⇒ cannot shadow
    assert out["PATH"] == "/usr/bin"  # unrelated env preserved


def test_two_concurrent_injections_no_bleed() -> None:
    """Two users injected into their own env copies don't cross-contaminate, and
    the process ``os.environ`` is never touched (OpenHands #3138 class of bug)."""
    before = dict(os.environ)
    env_a = apply_method(
        {"CLAUDE_CODE_OAUTH_TOKEN": "amb"}, "claude",
        ApiKey(var="ANTHROPIC_API_KEY", key_ref="KA"), secrets={"KA": "sk-a"},
    )
    env_b = apply_method(
        {"CLAUDE_CODE_OAUTH_TOKEN": "amb"}, "claude",
        ApiKey(var="ANTHROPIC_API_KEY", key_ref="KB"), secrets={"KB": "sk-b"},
    )
    assert env_a["ANTHROPIC_API_KEY"] == "sk-a"
    assert env_b["ANTHROPIC_API_KEY"] == "sk-b"
    assert env_a["ANTHROPIC_API_KEY"] != env_b["ANTHROPIC_API_KEY"]
    assert dict(os.environ) == before  # nothing bled to the process env


def test_session_file_injection_is_first_class(tmp_path) -> None:
    (tmp_path / "auth.json").write_text("{}", encoding="utf-8")
    out = apply_method(
        {"OPENAI_API_KEY": "ambient"}, "codex",
        SessionFile(home_var="CODEX_HOME", path_ref=str(tmp_path)), secrets={},
    )
    assert out["CODEX_HOME"] == str(tmp_path)
    assert "OPENAI_API_KEY" not in out  # stripped


def test_inherit_is_a_noop() -> None:
    base = {"OPENAI_API_KEY": "x", "PATH": "/bin"}
    assert apply_method(dict(base), "codex", Inherit(), secrets={}) == base


# === the AuthConfig switchboard =============================================

_MANAGED = {
    "keys": {"anthropic-standard": {"provider": "claude", "tier": "standard",
                                    "secret_env": "ANTHROPIC_KEY_STANDARD"}},
    "users": {"u1": {"key_id": "anthropic-standard"}},
    "default_key_id": "anthropic-standard",
}


def test_switchboard_applies_policy() -> None:
    cfg = HarnessApiConfig(auth=AuthConfig(
        oauth_allowed_users=["me"], on_oauth_denied="reject",
    ))
    resolver = build_auth_resolver(cfg)
    assert resolver.policy.oauth_allowed_users == ("me",)
    assert resolver.policy.on_oauth_denied == "reject"


def test_switchboard_memory_backend_seeds_managed_keys() -> None:
    cfg = HarnessApiConfig(
        keys=KeysConfig(managed_keys_json=json.dumps(_MANAGED)),
        auth=AuthConfig(store_backend="memory"),
    )
    resolver = build_auth_resolver(cfg)
    # The managed user resolves to a typed api-key method (secret presence aside).
    assert type(resolver.resolve("u1", "claude").method).__name__ == "ApiKey"
    # And default_key_id is the stranger fallback.
    assert type(resolver.resolve("stranger", "claude").method).__name__ == "ApiKey"


def test_switchboard_jsonl_backend_uses_durable_store(tmp_path) -> None:
    cfg = HarnessApiConfig(auth=AuthConfig(
        store_backend="jsonl", state_dir=str(tmp_path),
    ))
    resolver = build_auth_resolver(cfg)
    assert isinstance(resolver.store, JsonlCredentialStore)

    async def _test() -> None:
        await init_auth(resolver)  # loads the (empty) credentials.jsonl
        assert resolver.resolve("nobody", "claude").describe()["mode"] == "none"

    asyncio.run(_test())
