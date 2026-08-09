"""C7 (describe_auth) + C8 (preflight) — auth reported by mode/fingerprint, never key.

Hermetic: drives the pure ``providers.auth`` helpers with explicit env maps, plus
asserts the finalized provider classes delegate to them.
"""

from __future__ import annotations

from warden.providers.auth import describe_auth, preflight


# --- describe_auth: mode + fingerprint, never the raw secret ---------------

def test_claude_oauth_fingerprint_no_key_leak() -> None:
    d = describe_auth("claude", {"CLAUDE_CODE_OAUTH_TOKEN": "sk-oauth-verysecret-ABCD"})
    assert d["mode"] == "oauth" and d["authed"] is True
    assert d["fingerprint"] == "…ABCD"
    # The raw secret must never appear anywhere in the descriptor.
    assert "verysecret" not in str(d)


def test_claude_api_key_mode() -> None:
    d = describe_auth("claude", {"ANTHROPIC_API_KEY": "sk-ant-1234"})
    assert d["mode"] == "api-key" and d["fingerprint"] == "…1234"


def test_claude_none_when_unset() -> None:
    d = describe_auth("claude", {})
    assert d["mode"] == "none" and d["authed"] is False


def test_codex_api_key_vs_file(tmp_path, monkeypatch) -> None:
    assert describe_auth("codex", {"OPENAI_API_KEY": "sk-openai-9999"})["mode"] == "api-key"
    # File lane: CODEX_HOME/auth.json present, no OPENAI_API_KEY.
    (tmp_path / "auth.json").write_text("{}")
    d = describe_auth("codex", {"CODEX_HOME": str(tmp_path)})
    assert d["mode"] == "file" and d["authed"] is True
    assert d["fingerprint"] == "codex-auth.json"


def test_openharness_local_no_credential() -> None:
    d = describe_auth("openharness", {})
    assert d["mode"] == "none" and d["authed"] is True
    assert d["fingerprint"] == "local-ollama"


# --- preflight (C8) --------------------------------------------------------

def test_preflight_ok_when_authed() -> None:
    r = preflight("claude", {"ANTHROPIC_API_KEY": "sk-x"})
    assert r["ok"] is True and r["reason"] == ""


def test_preflight_fails_with_hint_when_unset() -> None:
    r = preflight("claude", {})
    assert r["ok"] is False
    assert "no usable credential" in r["reason"]
    assert "CLAUDE_CODE_OAUTH_TOKEN" in r["hint"]


def test_preflight_openharness_always_ok() -> None:
    assert preflight("openharness", {})["ok"] is True


# --- provider delegation ---------------------------------------------------

def test_providers_override_describe_auth(monkeypatch) -> None:
    """The finalized providers override the BaseProvider no-op to delegate."""
    from warden.providers.base_provider import BaseProvider
    from warden.providers.claude.session import ClaudeSession
    from warden.providers.codex.sdk_session import CodexSdkSession
    from warden.providers.openharness.session import OpenHarnessSession

    for cls in (ClaudeSession, CodexSdkSession, OpenHarnessSession):
        assert cls.describe_auth is not BaseProvider.describe_auth, (
            f"{cls.__name__} must override describe_auth (C7)"
        )
