"""Phase 1 provider-contract unit tests.

Cover the widened contract's mechanism close: reject-unknown-kwargs guardrail,
the strip-then-inject auth helper, the seven declared capability flags, the
AUTH-FIX, and the factory C1 guard. No network / no real SDK turn.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from warden.providers import create_session
from warden.providers.auth import (
    PROVIDER_AUTH_VARS,
    is_authed,
    resolve_auth,
)
from warden.providers.base_provider import BaseProvider
from warden.providers.claude.session import ClaudeSession
from warden.providers.codex.session import CodexSession
from warden.providers.openharness.session import OpenHarnessSession
from warden.seams.custom_tools import CustomTool


# ---------------------------------------------------------------------------
# reject-unknown-kwargs guardrail
# ---------------------------------------------------------------------------

def test_claude_rejects_unknown_kwargs() -> None:
    with pytest.raises(TypeError, match="unknown kwargs"):
        ClaudeSession(repo_path=Path("."), bogus_kwarg=1)


def test_codex_rejects_unknown_kwargs() -> None:
    with pytest.raises(TypeError, match="unknown kwargs"):
        CodexSession(repo_path=Path("."), bogus_kwarg=1)


def test_openharness_rejects_unknown_kwargs() -> None:
    with pytest.raises(TypeError, match="unknown kwargs"):
        OpenHarnessSession(repo_path=Path("."), bogus_kwarg=1)


def test_factory_rejects_unknown_kwargs() -> None:
    with pytest.raises(TypeError):
        create_session("claude", repo_path=Path("."), bogus_kwarg=1)


# ---------------------------------------------------------------------------
# strip-then-inject auth helper (test BaseProvider.apply_auth_env directly)
# ---------------------------------------------------------------------------

def test_apply_auth_env_strips_inherited_then_injects() -> None:
    env = {
        "CLAUDE_CODE_OAUTH_TOKEN": "inherited-oauth",
        "ANTHROPIC_AUTH_TOKEN": "inherited-bearer",
        "ANTHROPIC_API_KEY": "inherited-key",
        "PATH": "/usr/bin",
    }
    out = BaseProvider.apply_auth_env(
        env, "claude", {"ANTHROPIC_API_KEY": "sk-injected"}
    )
    # Every inherited Claude credential is stripped first...
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in out
    assert "ANTHROPIC_AUTH_TOKEN" not in out
    # ...then only the injected key remains; unrelated vars are untouched.
    assert out["ANTHROPIC_API_KEY"] == "sk-injected"
    assert out["PATH"] == "/usr/bin"


def test_apply_auth_env_noop_when_none() -> None:
    env = {"CLAUDE_CODE_OAUTH_TOKEN": "x", "ANTHROPIC_API_KEY": "y"}
    out = BaseProvider.apply_auth_env(env, "claude", None)
    assert out == {"CLAUDE_CODE_OAUTH_TOKEN": "x", "ANTHROPIC_API_KEY": "y"}


def test_apply_auth_env_does_not_touch_os_environ() -> None:
    import os

    marker = "WARDEN_CONTRACT_TEST_VAR"
    os.environ[marker] = "keep-me"
    try:
        env = {**os.environ}
        BaseProvider.apply_auth_env(env, "claude", {"ANTHROPIC_API_KEY": "sk-x"})
        assert os.environ.get(marker) == "keep-me"
        assert marker in os.environ
    finally:
        os.environ.pop(marker, None)


def test_claude_stores_auth_env_and_config_dir() -> None:
    sess = ClaudeSession(
        repo_path=Path("."),
        auth_env={"ANTHROPIC_API_KEY": "sk-x"},
        claude_config_dir=Path("/tmp/cfg"),
    )
    assert sess._auth_env == {"ANTHROPIC_API_KEY": "sk-x"}
    assert sess._claude_config_dir == Path("/tmp/cfg").resolve()


# ---------------------------------------------------------------------------
# capability flags (design-coverage §4)
# ---------------------------------------------------------------------------

def test_claude_capability_flags() -> None:
    s = ClaudeSession(repo_path=Path("."))
    assert s.crash_isolated is True
    assert s.hard_kill_tier == "cooperative"
    assert s.cost_visibility == "mid_turn"
    assert s.compaction == "native"
    assert s.supports_hard_deadline is False
    assert s.custom_tool_delivery == "in_proc_list"
    assert s.perm_tier == "arg_level"


def test_codex_capability_flags() -> None:
    s = CodexSession(repo_path=Path("."))
    assert s.crash_isolated is True
    assert s.hard_kill_tier == "os"
    assert s.cost_visibility == "coarse"
    assert s.compaction == "harness_driven"
    assert s.supports_hard_deadline is True
    assert s.custom_tool_delivery == "none"
    assert s.perm_tier == "none"


def test_openharness_capability_flags() -> None:
    s = OpenHarnessSession(repo_path=Path("."))
    assert s.crash_isolated is False
    assert s.hard_kill_tier == "none"
    assert s.cost_visibility == "terminal"
    assert s.compaction == "harness_driven"
    assert s.supports_hard_deadline is False
    assert s.custom_tool_delivery == "in_proc_list"
    assert s.perm_tier == "arg_level"  # Phase 2: B15 closed (PRE_TOOL_USE hook)


# ---------------------------------------------------------------------------
# AUTH-FIX — ANTHROPIC_AUTH_TOKEN now in the claude strip tuples
# ---------------------------------------------------------------------------

def test_authfix_anthropic_auth_token_in_claude_tuples() -> None:
    assert "ANTHROPIC_AUTH_TOKEN" in PROVIDER_AUTH_VARS["claude"]
    assert "ANTHROPIC_AUTH_TOKEN" in PROVIDER_AUTH_VARS["claude-cli"]
    # Index [0] stays the preferred OAuth token (used for messaging).
    assert PROVIDER_AUTH_VARS["claude"][0] == "CLAUDE_CODE_OAUTH_TOKEN"


def test_authfix_resolve_and_is_authed_still_sane() -> None:
    # An inherited bearer token now resolves for claude...
    got = resolve_auth("claude", {"ANTHROPIC_AUTH_TOKEN": "bearer"})
    assert got == {"ANTHROPIC_AUTH_TOKEN": "bearer"}
    assert is_authed("claude", {"ANTHROPIC_AUTH_TOKEN": "bearer"}) is True
    # ...and an empty env is still unauthed for claude.
    assert is_authed("claude", {}) is False
    # openharness stays always-authed (local Ollama).
    assert is_authed("openharness", {}) is True


# ---------------------------------------------------------------------------
# factory C1 guard
# ---------------------------------------------------------------------------

_TOOL = CustomTool(
    name="save_note",
    description="save a note",
    input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
    handler=lambda text="": "ok",
)


def test_factory_c1_guard_codex_raises() -> None:
    # Codex still cannot inject harness tools (D6) — consume-or-error.
    with pytest.raises(NotImplementedError):
        create_session("codex", repo_path=Path("."), custom_tools=[_TOOL])


def test_factory_claude_and_openharness_consume() -> None:
    # Both in-process providers now CONSUME custom tools (Phase 1 claude,
    # Phase 2 openharness) — never silently drop.
    for provider in ("claude", "openharness"):
        sess = create_session(provider, repo_path=Path("."), custom_tools=[_TOOL])
        assert _TOOL in sess._custom_tools, f"{provider} must consume the tool."


# --- C11: normalize_event default (kind-keyed dict -> typed MessageEvent) ----


def test_normalize_event_maps_kind_dict_to_message_event() -> None:
    from warden.schemas.events import MessageEvent

    base = BaseProvider()
    raw = {"kind": "text", "text": "hi", "sessionId": "s1", "id": "x", "timestamp": 1}
    ev = base.normalize_event(raw)
    assert isinstance(ev, MessageEvent)
    assert ev.kind == "text"
    assert ev.session_id == "s1"
    # kind/sessionId/id/timestamp are stripped from content; the payload remains.
    assert ev.content == {"text": "hi"}


def test_normalize_event_passthrough_non_kind_and_typed() -> None:
    base = BaseProvider()
    # No "kind" key -> unchanged (fail-soft, never drops).
    assert base.normalize_event({"foo": 1}) == {"foo": 1}
    sentinel = object()
    assert base.normalize_event(sentinel) is sentinel


# --- C11: install_hooks generalized (Claude routes through the seam) ---------


class _Opts:
    """Minimal stand-in for the SDK options object (just a .hooks attribute)."""

    def __init__(self, hooks=None):
        self.hooks = hooks


def test_claude_install_hooks_noop_when_audit_disabled(monkeypatch) -> None:
    from warden.providers.claude.session import ClaudeSession

    monkeypatch.delenv("AUDIT_ENABLED", raising=False)
    sess = ClaudeSession(repo_path=Path("."))
    opts = _Opts(hooks=None)
    sess.install_hooks(opts)
    assert opts.hooks is None  # disabled -> untouched


def test_claude_install_hooks_merges_when_enabled(monkeypatch) -> None:
    from warden.providers.base_provider import BaseProvider
    from warden.providers.claude.session import ClaudeSession

    from warden.config.models import AuditConfig

    # It must be a real override, not the base no-op (C11 generalization).
    assert ClaudeSession.install_hooks is not BaseProvider.install_hooks

    # M5 3a-1: gate is now config-first (AuditConfig.enabled), not AUDIT_ENABLED
    # env; build_audit_hooks now takes run_id/log_dir kwargs (closurized at build).
    monkeypatch.setattr(
        "warden.observability.audit.claude_sdk_hooks.build_audit_hooks",
        lambda run_id=None, log_dir=None: {"PreToolUse": ["audit_matcher"]},
    )
    sess = ClaudeSession(repo_path=Path("."), audit=AuditConfig(enabled=True))

    # From no existing hooks -> installed.
    opts = _Opts(hooks=None)
    sess.install_hooks(opts)
    assert opts.hooks == {"PreToolUse": ["audit_matcher"]}

    # Merges with (appends to) existing matchers, doesn't clobber.
    opts2 = _Opts(hooks={"PreToolUse": ["existing"]})
    sess.install_hooks(opts2)
    assert opts2.hooks["PreToolUse"] == ["existing", "audit_matcher"]
