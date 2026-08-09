"""Tests for OpenHarness can_use_tool enforcement (security control).

Regression coverage for the FULL_AUTO permission bypass (docs/TODO.md #1,
orchestrator/TODO.md:9): the orchestrator's ``can_use_tool`` callback must be
honored so that a denied mutating tool is blocked BEFORE it executes.

These are hermetic — they build the bridge / inspect the QueryEngine config
without ever calling ``start()`` (no Ollama, no network). This engine's suite
uses ``asyncio.run(...)`` rather than pytest-asyncio.
"""

import asyncio

from openharness.permissions.checker import PermissionMode

from warden.providers.openharness.permission_bridge import (
    build_permission_prompt,
)


class _FakeAllow:
    behavior = "allow"


class _FakeDeny:
    behavior = "deny"
    message = "blocked by workflow rules"


class TestPermissionPromptBridge:
    """The bridge turns can_use_tool into an openharness PermissionPrompt."""

    def test_deny_result_blocks_tool(self):
        calls = []

        async def can_use_tool(tool_name, tool_input, context):
            calls.append(tool_name)
            return _FakeDeny()

        prompt = build_permission_prompt(can_use_tool)
        # openharness calls prompt(tool_name, reason) and executes the tool
        # ONLY if it returns True.
        confirmed = asyncio.run(prompt("write_file", "needs confirmation"))

        assert confirmed is False, "denied tool must NOT be confirmed for execution"
        assert calls == ["write_file"], "callback must actually be consulted"

    def test_allow_result_permits_tool(self):
        async def can_use_tool(tool_name, tool_input, context):
            return _FakeAllow()

        prompt = build_permission_prompt(can_use_tool)
        confirmed = asyncio.run(prompt("write_file", "needs confirmation"))

        assert confirmed is True

    def test_callback_receives_tool_name(self):
        seen = {}

        async def can_use_tool(tool_name, tool_input, context):
            seen["name"] = tool_name
            seen["input"] = tool_input
            return _FakeAllow()

        prompt = build_permission_prompt(can_use_tool)
        asyncio.run(prompt("bash", "reason"))

        assert seen["name"] == "bash"
        assert isinstance(seen["input"], dict)

    def test_unexpected_result_denies_fail_closed(self):
        """Any result that is not an explicit allow must fail closed (deny)."""

        async def can_use_tool(tool_name, tool_input, context):
            return object()  # no .behavior == "allow"

        prompt = build_permission_prompt(can_use_tool)
        confirmed = asyncio.run(prompt("write_file", "reason"))

        assert confirmed is False


class TestSessionWiring:
    """The session must wire can_use_tool through to the QueryEngine."""

    def _build_engine(self, monkeypatch, can_use_tool):
        """Construct the QueryEngine the way start() does, but without Ollama.

        We stub _check_ollama_health so no network call happens, and stub the
        OpenAI client so no real handshake occurs.
        """
        from warden.providers.openharness import session as sess_mod

        session = sess_mod.OpenHarnessSession(
            repo_path="/tmp", can_use_tool=can_use_tool,
        )

        async def _noop_health(self):
            return None

        monkeypatch.setattr(
            sess_mod.OpenHarnessSession, "_check_ollama_health", _noop_health,
        )
        # OpenAICompatibleClient does no I/O at construction, so it's safe.
        asyncio.run(session.start())
        return session

    def test_engine_not_full_auto_when_callback_present(self, monkeypatch):
        async def can_use_tool(tool_name, tool_input, context):
            from types import SimpleNamespace
            return SimpleNamespace(behavior="allow")

        session = self._build_engine(monkeypatch, can_use_tool)
        engine = session._engine

        # The permission checker must NOT be in FULL_AUTO — otherwise mutating
        # tools would be auto-allowed and never routed to our hook.
        assert engine._permission_checker._settings.mode != PermissionMode.FULL_AUTO

        # B15: the arg-level decision routes through a PRE_TOOL_USE hook (the
        # hook_executor). The permission_prompt is a NON-None auto-confirm that
        # only satisfies DEFAULT-mode's confirmation ceremony (the real gate is
        # the hook, which fires first and blocks denied tools before the checker).
        from warden.providers.openharness.permission_bridge import (
            PermissionHookExecutor,
        )

        assert engine._permission_prompt is not None
        # The auto-confirm prompt always approves (upstream ceremony only).
        assert asyncio.run(engine._permission_prompt("write_file", "x")) is True
        assert isinstance(engine._hook_executor, PermissionHookExecutor)

    def test_engine_hook_consults_callback_with_real_input(self, monkeypatch):
        from openharness.hooks.events import HookEvent

        recorded = []

        async def can_use_tool(tool_name, tool_input, context):
            from types import SimpleNamespace
            recorded.append((tool_name, dict(tool_input)))
            return SimpleNamespace(behavior="deny", message="nope")

        session = self._build_engine(monkeypatch, can_use_tool)
        engine = session._engine

        # Simulate the query loop's PRE_TOOL_USE dispatch for a mutating tool
        # carrying the REAL tool_input (the B15 seam).
        agg = asyncio.run(
            engine._hook_executor.execute(
                HookEvent.PRE_TOOL_USE,
                {
                    "tool_name": "write_file",
                    "tool_input": {"path": "x.txt", "content": "y"},
                },
            )
        )

        assert agg.blocked is True
        assert recorded == [("write_file", {"path": "x.txt", "content": "y"})]

    def test_no_callback_falls_back_to_full_auto(self, monkeypatch):
        """With no can_use_tool (e.g. standalone use), FULL_AUTO is retained
        and no hook/prompt gate is wired — the honest documented state."""
        session = self._build_engine(monkeypatch, None)
        engine = session._engine

        assert engine._permission_checker._settings.mode == PermissionMode.FULL_AUTO
        assert engine._permission_prompt is None
        # No permission gate when there's no policy to enforce (audit disabled).
        assert engine._hook_executor is None
