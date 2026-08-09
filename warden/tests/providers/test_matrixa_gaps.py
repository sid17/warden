"""Matrix-A code-truth gap tests (Task 1.2 validation — REPORT, not fix).

These pin the audit hypotheses N3/N4/B15/B2-C1 to the LIVE code so the matrix
cells that are code-evident are proven without burning API spend. They are
written to PASS while the gap EXISTS (i.e. they assert the buggy/dropped
behavior). When the Task-1.5 fixes land, these tests should be inverted/removed.

Anchors re-confirmed against live code on branch refactor/harness-config:
  - N3  codex swallows auth_env      → providers/codex/session.py:24,74-76
  - N4  is_authed('codex') OAuth-blind → providers/auth.py:20,43-48
  - B15 hook forwards REAL tool_input → providers/openharness/permission_bridge.py (build_permission_hook)
  - B2/C1 custom tools: claude + openharness CONSUME; codex ERRORS (D6)
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from warden.providers import create_session
from warden.providers.auth import is_authed, resolve_auth
from warden.providers.codex.session import CodexSession
from warden.providers.claude.session import ClaudeSession
from warden.providers.openharness.permission_bridge import (
    build_permission_hook,
)
from warden.seams.custom_tools import CustomTool


# ===========================================================================
# N3 — Codex silently swallows auth_env (runs on the WRONG inherited key)
# ===========================================================================

def test_n3_codex_session_accepts_but_drops_auth_env() -> None:
    """CodexSession.__init__ takes auth_env only via **kwargs and never stores
    or uses it. Confirms N3: a per-run managed OPENAI_API_KEY is discarded, so
    codex runs on whatever key it inherits, with no error."""
    sess = CodexSession(
        repo_path=Path("."),
        auth_env={"OPENAI_API_KEY": "sk-managed-per-run-key"},
    )
    # The session exposes NO attribute holding the injected key.
    attrs = {k: v for k, v in vars(sess).items()}
    key_bearing = [
        k for k, v in attrs.items()
        if isinstance(v, dict) and "OPENAI_API_KEY" in v
    ]
    assert key_bearing == [], (
        f"N3 REFUTED: codex now stores auth_env somewhere ({key_bearing}). "
        "Re-check the matrix cell."
    )
    # And there is no auth_env attribute at all.
    assert not hasattr(sess, "_auth_env"), "N3 REFUTED: codex grew _auth_env."
    assert not hasattr(sess, "auth_env"), "N3 REFUTED: codex grew auth_env."


def test_n3_codex_env_only_sets_codex_home_not_openai_key() -> None:
    """The env dict codex builds for its subprocess (session.py:74-76) sets only
    CODEX_HOME — never OPENAI_API_KEY from auth_env. We assert the source shape:
    the only env mutation is CODEX_HOME."""
    import inspect

    src = inspect.getsource(CodexSession.send)
    # The subprocess env is built as {**os.environ, "CODEX_HOME": ...} and there
    # is no reference to auth_env or an injected OPENAI_API_KEY in send().
    assert "CODEX_HOME" in src
    assert "auth_env" not in src, (
        "N3 REFUTED: codex send() now references auth_env — the injection may "
        "have been wired. Re-check the matrix cell."
    )


# ===========================================================================
# N4 — is_authed('codex') now inspects auth.json (OAuth) — FIXED (Phase 3)
# ===========================================================================
# INVERTED from the former OAuth-blind assertion: is_authed('codex') is True when
# EITHER an OPENAI_API_KEY is set OR a ChatGPT-OAuth auth.json exists on disk
# (CODEX_HOME/auth.json or ~/.codex/auth.json). resolve_auth stays pure; the
# filesystem probe lives only in is_authed (via _codex_auth_present).

def test_n4_is_authed_codex_true_when_only_oauth_present(tmp_path) -> None:
    """is_authed('codex') returns True for an OAuth-only env (auth.json present on
    disk under CODEX_HOME) even with NO OPENAI_API_KEY (N4 fix)."""
    (tmp_path / "auth.json").write_text("{}")
    env_oauth_only = {"CODEX_HOME": str(tmp_path)}  # no OPENAI_API_KEY
    assert is_authed("codex", env_oauth_only) is True, (
        "N4 FIX REGRESSION: is_authed('codex') must inspect CODEX_HOME/auth.json."
    )
    # Still True the moment an API key appears (the env-based path).
    assert is_authed("codex", {"OPENAI_API_KEY": "sk-x"}) is True


def test_n4_is_authed_codex_false_when_no_key_and_no_auth_json(tmp_path) -> None:
    """With neither an OPENAI_API_KEY nor an auth.json under a (nonexistent)
    CODEX_HOME, is_authed('codex') is False — the probe is real, not a blanket
    True. (~/.codex/auth.json may exist on the dev host, so we point CODEX_HOME
    at an empty dir to isolate the probe.)"""
    import unittest.mock as mock

    env = {"CODEX_HOME": str(tmp_path / "nonexistent")}  # no auth.json, no key
    # Neutralize the ~/.codex/auth.json fallback so the probe is deterministic.
    with mock.patch.object(Path, "home", return_value=tmp_path / "no_home"):
        assert is_authed("codex", env) is False, (
            "N4: is_authed('codex') must be False with no key and no auth.json."
        )


def test_n4_resolve_auth_codex_only_knows_openai_api_key() -> None:
    """The provider auth var table maps codex to only OPENAI_API_KEY — no OAuth
    path — so resolve_auth cannot surface an OAuth credential."""
    got = resolve_auth("codex", {"CODEX_HOME": "/x", "OPENAI_API_KEY": "sk-y"})
    assert got == {"OPENAI_API_KEY": "sk-y"}
    got_oauth_only = resolve_auth("codex", {"CODEX_HOME": "/x"})
    assert got_oauth_only == {}, "N4 REFUTED: resolve_auth surfaced an OAuth path."


# ===========================================================================
# B15 — OpenHarness PRE_TOOL_USE hook forwards REAL tool_input (arg-level, CLOSED)
# ===========================================================================
# B15 is now FIXED: the orchestrator decision routes through a PRE_TOOL_USE hook
# (build_permission_hook) that receives the full {tool_name, tool_input} — not
# the args-blind 2-arg permission_prompt. These rows were INVERTED from the
# former {}-forwarding assertion.

def test_b15_hook_forwards_real_tool_input() -> None:
    """The PRE_TOOL_USE hook forwards the REAL tool_input (path/command), so an
    arg/path-level rule CAN fire. Inverted from the old {} assertion."""
    seen: dict = {}

    async def spy_can_use_tool(tool_name, tool_input, context):
        seen["tool_name"] = tool_name
        seen["tool_input"] = tool_input

        class _Allow:
            behavior = "allow"

        return _Allow()

    hook = build_permission_hook(spy_can_use_tool)
    payload = {
        "tool_name": "write_file",
        "tool_input": {"path": "secret.txt", "content": "x"},
    }
    result = asyncio.run(hook(payload))

    assert result.blocked is False, "allow decision must not block"
    assert seen["tool_name"] == "write_file"
    assert seen["tool_input"] == {"path": "secret.txt", "content": "x"}, (
        "B15 REGRESSION: the hook must forward the REAL tool_input, not {}."
    )


def test_b15_hook_name_and_arg_level_deny_block() -> None:
    """A deny decision fails closed via the hook, and a raising callback also
    blocks (fail-closed). This closes the arg-level half of the B15 cell."""
    async def deny_can_use_tool(tool_name, tool_input, context):
        class _Deny:
            behavior = "deny"
            message = "nope"

        return _Deny()

    hook = build_permission_hook(deny_can_use_tool)
    res = asyncio.run(hook({"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}}))
    assert res.blocked is True, "deny should block (fail-closed)."

    async def boom(tool_name, tool_input, context):
        raise RuntimeError("boom")

    hook2 = build_permission_hook(boom)
    res2 = asyncio.run(hook2({"tool_name": "Bash", "tool_input": {}}))
    assert res2.blocked is True, "exception must fail closed (block)."


# ===========================================================================
# B2 / C1 — Custom tools: Claude CONSUMES (Phase 1); Codex ERRORS (not drops)
# ===========================================================================
# These rows were INVERTED in Phase 1: Claude SDK now consumes custom tools
# (TOOL-1 satisfied) and Codex raises consume-or-error instead of silently
# dropping. Keep N3/N4/B15 above unchanged (Phases 2/3).

_C1_TOOL = CustomTool(
    name="save_note",
    description="save a note",
    input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
    handler=lambda text="": "ok",
)


def test_claude_session_consumes_custom_tools() -> None:
    """ClaudeSession now STORES custom_tools (TOOL-1: consume, never silent
    drop) — inverted from the former drop assertion."""
    sess = ClaudeSession(repo_path=Path("."), custom_tools=[_C1_TOOL])
    assert hasattr(sess, "_custom_tools"), "C1: ClaudeSession must grow _custom_tools."
    assert _C1_TOOL in sess._custom_tools, "C1: the passed custom tool must be stored."


def test_c1_codex_session_errors_on_custom_tools() -> None:
    """CodexSession CANNOT consume custom tools yet, so it ERRORS
    (consume-or-error) rather than silently dropping (belt-and-suspenders
    alongside the factory C1 guard)."""
    import pytest

    with pytest.raises(NotImplementedError):
        CodexSession(repo_path=Path("."), custom_tools=[_C1_TOOL])


def test_openharness_session_consumes_custom_tools() -> None:
    """OpenHarnessSession now STORES custom_tools (Phase 2 — consume, never
    drop). Inverted from the former NotImplementedError raise."""
    sess = create_session("openharness", repo_path=Path("."), custom_tools=[_C1_TOOL])
    assert _C1_TOOL in sess._custom_tools, "OpenHarness must consume the custom tool."


def test_c1_factory_claude_openharness_consume_codex_raises() -> None:
    """Via the factory: claude + openharness consume the tool; codex raises the
    C1 guard (NotImplementedError) rather than dropping."""
    import pytest

    sess = create_session("claude", repo_path=Path("."), custom_tools=[_C1_TOOL])
    assert _C1_TOOL in sess._custom_tools, "C1: claude must consume via the factory."

    oh = create_session("openharness", repo_path=Path("."), custom_tools=[_C1_TOOL])
    assert _C1_TOOL in oh._custom_tools, "C1: openharness must consume via the factory."

    with pytest.raises(NotImplementedError):
        create_session("codex", repo_path=Path("."), custom_tools=[_C1_TOOL])
