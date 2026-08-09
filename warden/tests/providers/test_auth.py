"""Tests for providers.auth — the provider auth resolution seam.

Deterministic: env is controlled with monkeypatch (or passed explicitly), so no
real credential or Keychain access is involved.
"""

import os

import pytest

from warden.providers.auth import (
    PROVIDER_AUTH_VARS,
    auth_hint,
    is_authed,
    resolve_auth,
)

_CLAUDE_VARS = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")
_ALL_VARS = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "OPENAI_API_KEY")


@pytest.fixture
def clean_env(monkeypatch):
    """Ensure none of the auth vars leak in from the real environment."""
    for var in _ALL_VARS:
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


# --- resolve_auth (via os.environ / monkeypatch) --------------------------


def test_resolve_auth_returns_only_present_vars(clean_env):
    clean_env.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-tok")
    assert resolve_auth("claude-cli") == {"CLAUDE_CODE_OAUTH_TOKEN": "oauth-tok"}


def test_resolve_auth_returns_both_claude_vars_when_set(clean_env):
    clean_env.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-tok")
    clean_env.setenv("ANTHROPIC_API_KEY", "anthropic-key")
    assert resolve_auth("claude") == {
        "CLAUDE_CODE_OAUTH_TOKEN": "oauth-tok",
        "ANTHROPIC_API_KEY": "anthropic-key",
    }


def test_resolve_auth_picks_whichever_claude_var_is_set(clean_env):
    clean_env.setenv("ANTHROPIC_API_KEY", "anthropic-key")
    assert resolve_auth("claude") == {"ANTHROPIC_API_KEY": "anthropic-key"}


def test_resolve_auth_empty_when_none_set(clean_env):
    assert resolve_auth("claude-cli") == {}
    assert resolve_auth("codex") == {}


def test_resolve_auth_codex_uses_openai_key(clean_env):
    clean_env.setenv("OPENAI_API_KEY", "openai-key")
    assert resolve_auth("codex") == {"OPENAI_API_KEY": "openai-key"}


def test_resolve_auth_openharness_is_empty(clean_env):
    clean_env.setenv("OPENAI_API_KEY", "openai-key")
    assert resolve_auth("openharness") == {}


def test_resolve_auth_unknown_provider_is_empty(clean_env):
    clean_env.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-tok")
    assert resolve_auth("mystery") == {}


def test_resolve_auth_ignores_empty_string_value(clean_env):
    clean_env.setenv("CLAUDE_CODE_OAUTH_TOKEN", "")
    assert resolve_auth("claude-cli") == {}


# --- resolve_auth with an explicit env mapping (pure, no os.environ) -------


def test_resolve_auth_reads_explicit_env_without_touching_os_environ(clean_env):
    before = dict(os.environ)
    passed_env = {"CLAUDE_CODE_OAUTH_TOKEN": "from-dict"}
    result = resolve_auth("claude-cli", env=passed_env)
    assert result == {"CLAUDE_CODE_OAUTH_TOKEN": "from-dict"}
    # os.environ was never consulted nor mutated.
    assert dict(os.environ) == before


def test_resolve_auth_explicit_env_overrides_os_environ(clean_env):
    clean_env.setenv("OPENAI_API_KEY", "from-os")
    # An explicit env that lacks the var yields {} even though os.environ has it.
    assert resolve_auth("codex", env={}) == {}


def test_resolve_auth_does_not_mutate_passed_env(clean_env):
    passed_env = {"OPENAI_API_KEY": "k"}
    snapshot = dict(passed_env)
    resolve_auth("codex", env=passed_env)
    assert passed_env == snapshot


# --- is_authed ------------------------------------------------------------


def test_is_authed_true_when_var_set(clean_env):
    clean_env.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-tok")
    assert is_authed("claude-cli") is True


def test_is_authed_false_when_no_var(clean_env, tmp_path, monkeypatch):
    assert is_authed("claude-cli") is False
    # N4 fix: codex is also authed via an on-disk ChatGPT-OAuth auth.json. Point
    # CODEX_HOME at an empty dir AND neutralize the ~/.codex fallback so this
    # asserts the truly-no-auth case (the dev host may have ~/.codex/auth.json).
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "nonexistent"))
    monkeypatch.setattr(
        "warden.providers.auth.Path.home", lambda: tmp_path / "no_home"
    )
    assert is_authed("codex") is False


def test_is_authed_openharness_always_true(clean_env):
    assert is_authed("openharness") is True


def test_is_authed_reads_explicit_env(clean_env, tmp_path, monkeypatch):
    assert is_authed("codex", env={"OPENAI_API_KEY": "k"}) is True
    # N4 fix: with no API key AND no auth.json (empty CODEX_HOME + neutralized
    # ~/.codex fallback), codex is unauthed.
    monkeypatch.setattr(
        "warden.providers.auth.Path.home", lambda: tmp_path / "no_home"
    )
    assert is_authed("codex", env={"CODEX_HOME": str(tmp_path / "nonexistent")}) is False


def test_is_authed_unknown_provider_false(clean_env):
    clean_env.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-tok")
    assert is_authed("mystery") is False


# --- auth_hint ------------------------------------------------------------


def test_auth_hint_claude_names_both_vars():
    hint = auth_hint("claude")
    assert "CLAUDE_CODE_OAUTH_TOKEN" in hint
    assert "ANTHROPIC_API_KEY" in hint


def test_auth_hint_claude_cli_names_both_vars():
    hint = auth_hint("claude-cli")
    assert "CLAUDE_CODE_OAUTH_TOKEN" in hint
    assert "ANTHROPIC_API_KEY" in hint


def test_auth_hint_codex_names_openai_key():
    assert "OPENAI_API_KEY" in auth_hint("codex")


def test_auth_hint_openharness_mentions_local():
    hint = auth_hint("openharness").lower()
    assert hint
    assert "ollama" in hint or "local" in hint


def test_auth_hint_always_non_empty():
    for provider in list(PROVIDER_AUTH_VARS) + ["mystery"]:
        assert auth_hint(provider)
