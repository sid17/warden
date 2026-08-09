"""pre-03 M0 · 3a — the typed ``AuthMethod`` union + ``ResolvedCredential`` (AUTH-5).

Fail-first keystone tests for the credential *model* (no store, no policy, no
injection yet — those are 3b/3c/3e). What is locked here:

  * ``ResolvedCredential`` exposes an auth **mode** + a **fingerprint** but carries
    **no raw secret** — the value never lands on the safe descriptor (LangFuse
    ``Safe*`` shape). Secrets stay **by reference** (env-var *names*) on the
    ``AuthMethod``; the value is peeked once to fingerprint, never stored.
  * The mode projection is the **single classifier** — it agrees, string-for-string,
    with ``providers/auth.py``'s ``describe_auth`` mode vocabulary
    (``oauth`` / ``api-key`` / ``file`` / ``none``), so there is no parallel classifier.
  * ``get_secret`` is a **prefix-dispatched** resolver (``os.environ/NAME`` and the
    bare-name form) so config is committable and a future Vault backend is a prefix.

Hermetic + in-process; no store, no filesystem beyond a session-file existence probe.
"""

from __future__ import annotations

from warden.harness_api.credentials.methods import (
    ApiKey,
    Inherit,
    OAuthToken,
    ResolvedCredential,
    SessionFile,
    get_secret,
    mode_of,
)
from warden.providers.auth import describe_auth

_SECRET = "sk-ant-supersecret-value-1234"


# === get_secret — prefix dispatch + bare name ================================

def test_get_secret_resolves_os_environ_prefix_and_bare_name() -> None:
    src = {"ANTHROPIC_API_KEY": _SECRET}
    assert get_secret("os.environ/ANTHROPIC_API_KEY", src) == _SECRET
    assert get_secret("ANTHROPIC_API_KEY", src) == _SECRET


def test_get_secret_missing_ref_is_none() -> None:
    assert get_secret("ANTHROPIC_API_KEY", {}) is None
    assert get_secret("os.environ/NOPE", {}) is None


# === ResolvedCredential — no raw secret past the boundary ====================

def test_resolved_credential_carries_no_raw_secret() -> None:
    """An api-key credential resolves to a SAFE descriptor: the secret VALUE
    appears nowhere on it (not in a field, not in its repr) — only a fingerprint."""
    method = ApiKey(var="ANTHROPIC_API_KEY", key_ref="ANTHROPIC_API_KEY")
    rc = ResolvedCredential.from_method(
        "claude", method, secrets={"ANTHROPIC_API_KEY": _SECRET}
    )
    assert rc.authed is True
    assert rc.fingerprint == "…1234"
    # The secret must not leak — not on any field value, not in the repr.
    assert _SECRET not in repr(rc)
    for value in vars(rc).values():
        assert _SECRET not in repr(value)


def test_authed_false_and_no_fingerprint_when_secret_absent() -> None:
    method = ApiKey(var="ANTHROPIC_API_KEY", key_ref="ANTHROPIC_API_KEY")
    rc = ResolvedCredential.from_method("claude", method, secrets={})
    assert rc.authed is False
    assert rc.fingerprint == ""


# === mode projection == the single classifier (agrees with describe_auth) ====

def test_mode_of_covers_every_variant() -> None:
    assert mode_of(OAuthToken(var="CLAUDE_CODE_OAUTH_TOKEN", token_ref="T")) == "oauth"
    assert mode_of(ApiKey(var="ANTHROPIC_API_KEY", key_ref="K")) == "api-key"
    assert mode_of(SessionFile(home_var="CODEX_HOME", path_ref="~/.codex")) == "file"
    assert mode_of(Inherit()) == "none"


def test_describe_projection_matches_providers_auth_vocab() -> None:
    """The mode strings the union projects are the SAME vocabulary
    ``providers/auth.py describe_auth`` emits — one classifier, not two."""
    oauth_rc = ResolvedCredential.from_method(
        "claude",
        OAuthToken(var="CLAUDE_CODE_OAUTH_TOKEN", token_ref="CLAUDE_CODE_OAUTH_TOKEN"),
        secrets={"CLAUDE_CODE_OAUTH_TOKEN": "tok-oauth-abcd"},
    )
    live = describe_auth("claude", {"CLAUDE_CODE_OAUTH_TOKEN": "tok-oauth-abcd"})
    assert oauth_rc.describe()["mode"] == live["mode"] == "oauth"

    key_rc = ResolvedCredential.from_method(
        "claude",
        ApiKey(var="ANTHROPIC_API_KEY", key_ref="ANTHROPIC_API_KEY"),
        secrets={"ANTHROPIC_API_KEY": _SECRET},
    )
    live_key = describe_auth("claude", {"ANTHROPIC_API_KEY": _SECRET})
    assert key_rc.describe()["mode"] == live_key["mode"] == "api-key"


def test_describe_is_safe_shape() -> None:
    """``describe()`` is the read surface: provider/mode/fingerprint/authed only,
    never a secret."""
    rc = ResolvedCredential.from_method(
        "claude",
        ApiKey(var="ANTHROPIC_API_KEY", key_ref="ANTHROPIC_API_KEY"),
        secrets={"ANTHROPIC_API_KEY": _SECRET},
    )
    d = rc.describe()
    assert set(d) == {"provider", "mode", "fingerprint", "authed"}
    assert _SECRET not in repr(d)


# === SessionFile — presence probe, tag fingerprint (no secret) ===============

def test_session_file_authed_on_existing_path(tmp_path) -> None:
    auth_json = tmp_path / "auth.json"
    auth_json.write_text("{}", encoding="utf-8")
    method = SessionFile(home_var="CODEX_HOME", path_ref=str(tmp_path))
    rc = ResolvedCredential.from_method("codex", method, secrets={})
    assert rc.authed is True
    assert rc.describe()["mode"] == "file"


def test_session_file_not_authed_when_missing(tmp_path) -> None:
    method = SessionFile(home_var="CODEX_HOME", path_ref=str(tmp_path / "absent"))
    rc = ResolvedCredential.from_method("codex", method, secrets={})
    assert rc.authed is False
