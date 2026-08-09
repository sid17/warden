"""Tests for the managed-key registry (per-run auth_env resolution)."""

import pytest

from warden.harness_api.credentials.keys import KeyRegistry


_CFG = {
    "keys": {
        "anthropic-standard": {
            "provider": "claude-cli",
            "tier": "standard",
            "secret_env": "ANTHROPIC_KEY_STANDARD",
        },
        "anthropic-pro": {
            "provider": "claude-cli",
            "tier": "pro",
            "secret_env": "ANTHROPIC_KEY_PRO",
        },
    },
    "users": {
        "u1": {"key_id": "anthropic-standard", "budget_usd": 5.0},
        "u2": {"key_id": "anthropic-pro", "budget_usd": 50.0},
    },
    "default_key_id": "anthropic-standard",
    "default_budget_usd": 5.0,
}

_SECRETS = {
    "ANTHROPIC_KEY_STANDARD": "sk-standard",
    "ANTHROPIC_KEY_PRO": "sk-pro",
}


def test_auth_env_resolves_user_key():
    reg = KeyRegistry.from_config(_CFG, secrets=_SECRETS)
    assert reg.auth_env_for("u1", "claude-cli") == {"ANTHROPIC_API_KEY": "sk-standard"}
    assert reg.auth_env_for("u2", "claude-cli") == {"ANTHROPIC_API_KEY": "sk-pro"}


def test_two_users_get_distinct_keys():
    """The isolation contract: two users resolve to different subprocess keys."""
    reg = KeyRegistry.from_config(_CFG, secrets=_SECRETS)
    assert reg.auth_env_for("u1", "claude-cli") != reg.auth_env_for("u2", "claude-cli")


def test_unknown_user_falls_back_to_default_key():
    reg = KeyRegistry.from_config(_CFG, secrets=_SECRETS)
    assert reg.auth_env_for("stranger", "claude-cli") == {"ANTHROPIC_API_KEY": "sk-standard"}


def test_no_managed_keys_returns_none():
    """Empty registry => inherit the launching credential (auth_env None)."""
    reg = KeyRegistry.from_config({}, secrets=_SECRETS)
    assert not reg.has_keys()
    assert reg.auth_env_for("u1", "claude-cli") is None


def test_provider_mismatch_returns_none():
    """A user's claude key is not injected for a codex run — inherit instead."""
    reg = KeyRegistry.from_config(_CFG, secrets=_SECRETS)
    assert reg.auth_env_for("u1", "codex") is None


def test_missing_secret_raises():
    reg = KeyRegistry.from_config(_CFG, secrets={})  # secret env not set
    with pytest.raises(ValueError, match="secret env"):
        reg.auth_env_for("u1", "claude-cli")


def test_budget_lookup():
    reg = KeyRegistry.from_config(_CFG, secrets=_SECRETS)
    assert reg.budget_for("u1") == 5.0
    assert reg.budget_for("u2") == 50.0
    assert reg.budget_for("stranger") == 5.0  # default_budget_usd


def test_from_env_empty_when_unconfigured():
    reg = KeyRegistry.from_env(env={})
    assert not reg.has_keys()


def test_from_env_reads_inline_json():
    import json

    env = {"MANAGED_KEYS_JSON": json.dumps(_CFG), **_SECRETS}
    reg = KeyRegistry.from_env(env=env)
    assert reg.auth_env_for("u1", "claude-cli") == {"ANTHROPIC_API_KEY": "sk-standard"}


def test_from_env_malformed_json_raises():
    with pytest.raises(ValueError, match="invalid managed-keys config"):
        KeyRegistry.from_env(env={"MANAGED_KEYS_JSON": "{not json"})


def test_custom_auth_var_honored():
    cfg = {
        "keys": {
            "k": {"provider": "claude-cli", "secret_env": "S", "auth_var": "CLAUDE_CODE_OAUTH_TOKEN"},
        },
        "users": {"u": {"key_id": "k"}},
    }
    reg = KeyRegistry.from_config(cfg, secrets={"S": "tok"})
    assert reg.auth_env_for("u", "claude-cli") == {"CLAUDE_CODE_OAUTH_TOKEN": "tok"}
