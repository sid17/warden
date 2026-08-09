"""pre-07b · 3a — the permission seam forwards ``tool_use_id`` and round-trips
``updated_input``.

The id exists at every provider's gate but the seam used to drop it. This is
the enabling fix for defer/resume: a permission consult must be IDENTIFIED so a
pending call can be keyed, paused, and resumed by injecting the decision back
into that exact call.

These tests exercise the seam directly (no live SDK): ``evaluate_tool_permission``
must accept a ``tool_use_id``, pass it to ``PermissionHandler.request_permission``,
and surface a handler ``updated_input`` on the returned ``PermissionResultAllow``.
"""

from __future__ import annotations

import asyncio

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

from warden.orchestrator.permission_surface import evaluate_tool_permission
from warden.safety.permissions.checker import PermissionDecision as CheckerDecision
from warden.seams.permissions import PermissionDecision


class _RecordingHandler:
    """Captures what request_permission was called with; returns a fixed answer."""

    def __init__(self, decision: PermissionDecision):
        self._decision = decision
        self.seen_tool_use_id: object = "UNSET"
        self.calls: list[tuple] = []

    async def request_permission(
        self, tool_name, tool_input, reason, tool_use_id=None,
    ) -> PermissionDecision:
        self.seen_tool_use_id = tool_use_id
        self.calls.append((tool_name, tool_use_id))
        return self._decision

    async def ask_user_question(self, questions):
        return {"result": {}}


class _ConfirmChecker:
    """A checker stub that always routes to the handler (requires_confirmation)."""

    def evaluate(self, tool_name, tool_input) -> CheckerDecision:
        return CheckerDecision(
            allowed=False, requires_confirmation=True, reason="needs ok", source="stub",
        )

    def remember(self, tool_name) -> None:
        pass


def _run(coro):
    return asyncio.run(coro)


def test_tool_use_id_forwarded_to_handler() -> None:
    handler = _RecordingHandler(PermissionDecision(allowed=True, source="test"))
    result = _run(
        evaluate_tool_permission(
            "Bash", {"command": "ls"},
            tool_scope=None,
            permission_handler=handler,
            permission_checker=_ConfirmChecker(),
            tool_event_queue=None,
            tool_use_id="toolu_012554abc",
        )
    )
    assert handler.seen_tool_use_id == "toolu_012554abc"
    assert isinstance(result, PermissionResultAllow)


def test_updated_input_round_trips_from_handler() -> None:
    handler = _RecordingHandler(
        PermissionDecision(
            allowed=True, source="test", updated_input={"command": "ls -la"},
        )
    )
    result = _run(
        evaluate_tool_permission(
            "Bash", {"command": "ls"},
            tool_scope=None,
            permission_handler=handler,
            permission_checker=_ConfirmChecker(),
            tool_event_queue=None,
            tool_use_id="toolu_x",
        )
    )
    assert isinstance(result, PermissionResultAllow)
    assert result.updated_input == {"command": "ls -la"}


def test_deny_still_blocks_and_carries_id() -> None:
    handler = _RecordingHandler(
        PermissionDecision(allowed=False, source="test", reason="user said no")
    )
    result = _run(
        evaluate_tool_permission(
            "Bash", {"command": "rm -rf /"},
            tool_scope=None,
            permission_handler=handler,
            permission_checker=_ConfirmChecker(),
            tool_event_queue=None,
            tool_use_id="toolu_y",
        )
    )
    assert isinstance(result, PermissionResultDeny)
    assert handler.seen_tool_use_id == "toolu_y"


def test_extract_tool_use_id_from_context_shapes() -> None:
    """The orchestrator helper pulls the id from each provider's context shape."""
    from warden.orchestrator.orchestrator import _extract_tool_use_id

    class _ClaudeCtx:  # ToolPermissionContext-like
        tool_use_id = "toolu_abc"

    assert _extract_tool_use_id(_ClaudeCtx()) == "toolu_abc"
    assert _extract_tool_use_id({"tool_use_id": "toolu_d"}) == "toolu_d"  # gate dict
    assert _extract_tool_use_id({"item_id": "item_9"}) == "item_9"  # codex
    assert _extract_tool_use_id({"tool_use_id": None}) is None  # OH fallback
    assert _extract_tool_use_id(None) is None
    assert _extract_tool_use_id(object()) is None  # unknown shape → None, no raise


def test_tool_use_id_defaults_to_none_when_absent() -> None:
    """Back-compat: callers that don't pass an id still work (id = None)."""
    handler = _RecordingHandler(PermissionDecision(allowed=True, source="test"))
    _run(
        evaluate_tool_permission(
            "Bash", {"command": "ls"},
            tool_scope=None,
            permission_handler=handler,
            permission_checker=_ConfirmChecker(),
            tool_event_queue=None,
        )
    )
    assert handler.seen_tool_use_id is None
