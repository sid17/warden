"""pre-03 M0 · 3b — the JSONL ``CredentialStore`` keyed by ``(user, provider)``.

Fixes AUTH-4 (one-key-per-user) natively: the store is keyed by
``(user_id, provider)``, so one user holding a Claude OAuth token **and** a Codex
key is the ordinary case, not a mismatch that silently inherits the process cred.

Mirrors the governance JSONL stores (``test_jsonl_stores.py``): append-only,
replay-on-``load``, latest-write-wins fold, one bad tail line skipped, single-writer
lock. Async style is the repo's — plain ``def test_...`` with ``asyncio.run`` inside.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from warden.harness_api.credentials.methods import (
    ApiKey,
    OAuthToken,
    SessionFile,
)
from warden.harness_api.credentials.store import (
    CredentialRecord,
    InMemoryCredentialStore,
    JsonlCredentialStore,
    legacy_records_from_config,
)


def _run(coro):
    return asyncio.run(coro)


_OAUTH = CredentialRecord(
    user_id="u1", provider="claude", auth_method="oauth",
    secret_ref="U1_CLAUDE_OAUTH",
)
_KEY = CredentialRecord(
    user_id="u1", provider="codex", auth_method="api_key",
    secret_ref="U1_OPENAI_KEY",
)


# === AUTH-4: one user, different credential per provider =====================

def test_multi_cred_per_user_native() -> None:
    """u1 holds a Claude OAuth token AND a Codex key at once — each resolves to
    its own typed method (the flat-key_id registry could not express this)."""

    async def _test() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonlCredentialStore(Path(tmp) / "credentials.jsonl")
            await store.load()
            await store.put(_OAUTH)
            await store.put(_KEY)

            claude = store.get("u1", "claude")
            codex = store.get("u1", "codex")
            assert isinstance(claude.to_method(), OAuthToken)
            assert isinstance(codex.to_method(), ApiKey)
            # Injection vars default from the provider's precedence list.
            assert claude.to_method().var == "CLAUDE_CODE_OAUTH_TOKEN"
            assert codex.to_method().var == "OPENAI_API_KEY"

    _run(_test())


def test_empty_store_returns_none_for_any_key() -> None:
    """An empty store ⇒ get is None everywhere ⇒ resolve inherits the process cred."""

    async def _test() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonlCredentialStore(Path(tmp) / "credentials.jsonl")
            await store.load()
            assert store.get("u1", "claude") is None

    _run(_test())


def test_latest_write_wins() -> None:
    async def _test() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonlCredentialStore(Path(tmp) / "credentials.jsonl")
            await store.load()
            await store.put(_OAUTH)
            newer = CredentialRecord(
                user_id="u1", provider="claude", auth_method="api_key",
                secret_ref="U1_ANTHROPIC_KEY",
            )
            await store.put(newer)
            assert store.get("u1", "claude").auth_method == "api_key"

    _run(_test())


def test_remove_deletes_the_record() -> None:
    async def _test() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonlCredentialStore(Path(tmp) / "credentials.jsonl")
            await store.load()
            await store.put(_OAUTH)
            await store.remove("u1", "claude")
            assert store.get("u1", "claude") is None
            # remove of an absent key is a no-op (no raise).
            await store.remove("u1", "claude")

    _run(_test())


# === AUTH-7: JSONL durability (restart replay + corrupt tail) ===============

def test_records_survive_restart() -> None:
    async def _test() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "credentials.jsonl"
            first = JsonlCredentialStore(path)
            await first.load()
            await first.put(_OAUTH)
            await first.put(_KEY)
            await first.remove("u1", "codex")

            fresh = JsonlCredentialStore(path)
            await fresh.load()
            assert fresh.get("u1", "claude").auth_method == "oauth"
            assert fresh.get("u1", "codex") is None  # remove persisted

    _run(_test())


def test_corrupt_tail_line_tolerated() -> None:
    async def _test() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "credentials.jsonl"
            first = JsonlCredentialStore(path)
            await first.load()
            await first.put(_OAUTH)
            with path.open("a", encoding="utf-8") as handle:
                handle.write('{"type": "put", "user_id": "u1",\n')  # torn write

            fresh = JsonlCredentialStore(path)
            await fresh.load()  # must not raise
            assert fresh.get("u1", "claude").auth_method == "oauth"

    _run(_test())


def test_concurrent_puts_no_lost_updates() -> None:
    async def _test() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "credentials.jsonl"
            store = JsonlCredentialStore(path)
            await store.load()
            n = 20
            await asyncio.gather(*[
                store.put(CredentialRecord(
                    user_id=f"u{i}", provider="claude", auth_method="api_key",
                    secret_ref=f"K{i}",
                ))
                for i in range(n)
            ])
            fresh = JsonlCredentialStore(path)
            await fresh.load()
            assert all(fresh.get(f"u{i}", "claude") is not None for i in range(n))

    _run(_test())


# === in-memory store (the "memory" backend tier) ============================

def test_in_memory_store_roundtrip() -> None:
    async def _test() -> None:
        store = InMemoryCredentialStore()
        await store.load()
        await store.put(_OAUTH)
        assert store.get("u1", "claude").to_method().var == "CLAUDE_CODE_OAUTH_TOKEN"
        await store.remove("u1", "claude")
        assert store.get("u1", "claude") is None

    _run(_test())


# === legacy adapter: MANAGED_KEYS blob → (user, provider) records ===========

def test_legacy_records_from_config_maps_users() -> None:
    """A flat MANAGED_KEYS blob (user → one key_id) materializes as one
    ``(user, key.provider)`` api-key record per mapped user — back-compat."""
    cfg = {
        "keys": {
            "anthropic-standard": {
                "provider": "claude", "tier": "standard",
                "secret_env": "ANTHROPIC_KEY_STANDARD",
            },
        },
        "users": {"u1": {"key_id": "anthropic-standard"}},
    }
    records = legacy_records_from_config(cfg)
    assert len(records) == 1
    rec = records[0]
    assert (rec.user_id, rec.provider, rec.auth_method) == ("u1", "claude", "api_key")
    assert rec.secret_ref == "ANTHROPIC_KEY_STANDARD"
    method = rec.to_method()
    assert isinstance(method, ApiKey) and method.var == "ANTHROPIC_API_KEY"


def test_legacy_custom_auth_var_preserved() -> None:
    """A managed key whose auth_var is an OAuth var keeps injecting under it."""
    cfg = {
        "keys": {
            "k": {"provider": "claude", "secret_env": "S",
                  "auth_var": "CLAUDE_CODE_OAUTH_TOKEN"},
        },
        "users": {"u": {"key_id": "k"}},
    }
    rec = legacy_records_from_config(cfg)[0]
    assert rec.to_method().var == "CLAUDE_CODE_OAUTH_TOKEN"


def test_session_file_record_to_method() -> None:
    rec = CredentialRecord(
        user_id="u1", provider="codex", auth_method="session_file",
        secret_ref="~/.codex",
    )
    method = rec.to_method()
    assert isinstance(method, SessionFile)
    assert method.home_var == "CODEX_HOME"
    assert method.path_ref == "~/.codex"
