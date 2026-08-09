"""Tests for ToolScope — per-mode tool restriction config."""

import asyncio
from pathlib import Path

from warden.schemas.tool_scope import ToolScope


class TestIsAllowed:
    def test_whitelist_allows_listed_tool(self):
        scope = ToolScope(allowed=["Read", "Grep"])
        assert scope.is_allowed("Read") is True

    def test_whitelist_blocks_unlisted_tool(self):
        scope = ToolScope(allowed=["Read", "Grep"])
        assert scope.is_allowed("Write") is False

    def test_blacklist_allows_unlisted_tool(self):
        scope = ToolScope(denied=["Bash", "Write"])
        assert scope.is_allowed("Read") is True

    def test_blacklist_blocks_listed_tool(self):
        scope = ToolScope(denied=["Bash", "Write"])
        assert scope.is_allowed("Bash") is False

    def test_unrestricted_allows_anything(self):
        scope = ToolScope()
        assert scope.is_allowed("anything") is True


class TestToDisallowedTools:
    def test_whitelist_returns_complement(self):
        scope = ToolScope(allowed=["Read"])
        result = scope.to_disallowed_tools(["Read", "Write", "Bash"])
        assert sorted(result) == ["Bash", "Write"]

    def test_blacklist_returns_denied_list(self):
        scope = ToolScope(denied=["Bash"])
        result = scope.to_disallowed_tools(["Read", "Write", "Bash"])
        assert result == ["Bash"]

    def test_unrestricted_returns_empty(self):
        scope = ToolScope()
        result = scope.to_disallowed_tools(["Read", "Write", "Bash"])
        assert result == []


class TestEquality:
    def test_equal_allowed(self):
        assert ToolScope(allowed=["Read"]) == ToolScope(allowed=["Read"])

    def test_unequal_allowed(self):
        assert ToolScope(allowed=["Read"]) != ToolScope(allowed=["Write"])

    def test_allowed_vs_unrestricted(self):
        assert ToolScope(allowed=["Read"]) != ToolScope()

    def test_both_unrestricted(self):
        assert ToolScope() == ToolScope()

    def test_not_equal_to_non_toolscope(self):
        assert ToolScope() != "not a toolscope"


class TestOrchestratorToolScopeEnforcement:
    """Integration: Orchestrator._can_use_tool respects ToolScope."""

    def _make_orchestrator(self):
        from warden.orchestrator.orchestrator import Orchestrator
        from warden.orchestrator.session.manager import SessionManager

        sm = SessionManager()
        return Orchestrator(
            session_manager=sm,
            repo_path=Path("."),
            tool_scope=ToolScope(allowed=["Read"]),
        )

    def test_blocked_tool_returns_deny(self):
        from claude_agent_sdk import PermissionResultDeny

        async def _test():
            orch = self._make_orchestrator()
            result = await orch._can_use_tool("Write", {}, None)
            assert isinstance(result, PermissionResultDeny)
            assert "blocked by tool scope" in result.message

        asyncio.run(_test())

    def test_allowed_tool_passes_through(self):
        from claude_agent_sdk import PermissionResultDeny

        async def _test():
            orch = self._make_orchestrator()
            result = await orch._can_use_tool("Read", {}, None)
            # Should NOT be denied by tool scope — falls through to permission checker
            if isinstance(result, PermissionResultDeny):
                assert "blocked by tool scope" not in result.message

        asyncio.run(_test())
