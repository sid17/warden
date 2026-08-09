"""Permission handler protocol for transport-agnostic permission checks."""

from dataclasses import dataclass
from typing import Protocol


@dataclass
class PermissionDecision:
    """Result of a permission check.

    ``updated_input`` (pre-07b / 3a): an optional mutated tool input the handler
    may return on allow — round-tripped into ``PermissionResultAllow(updated_input=…)``
    so a resumed/injected decision can also rewrite the call's args (the field
    standard: Claude ``updatedInput`` / OpenAI Agents SDK mutated args).
    """

    allowed: bool
    source: str = ""
    reason: str = ""
    always: bool = False
    updated_input: dict | None = None


class PermissionHandler(Protocol):
    """How the transport handles permission requests and user questions."""

    async def request_permission(
        self, tool_name: str, tool_input: dict, reason: str,
        tool_use_id: str | None = None,
    ) -> PermissionDecision:
        """Ask for permission. Blocks until decision. Returns allow/deny.

        ``tool_use_id`` (pre-07b / 3a) identifies the exact pending call so a
        durable transport can key, pause, and resume it by injecting the
        decision back into that call (never a nudge). ``None`` when the
        provider's gate carries no id.
        """
        ...

    async def ask_user_question(
        self, questions: list[dict],
    ) -> dict:
        """Forward AskUserQuestion to the user. Blocks until answered."""
        ...


class AutoAllowHandler:
    """For CLI/scripts — auto-allow everything."""

    async def request_permission(
        self, tool_name: str, tool_input: dict, reason: str,
        tool_use_id: str | None = None,
    ) -> PermissionDecision:
        return PermissionDecision(allowed=True, source="auto")

    async def ask_user_question(self, questions: list[dict]) -> dict:
        return {"result": {}}


class CLIPermissionHandler:
    """For CLI — prompt in terminal."""

    async def request_permission(
        self, tool_name: str, tool_input: dict, reason: str,
        tool_use_id: str | None = None,
    ) -> PermissionDecision:
        answer = input(f"Allow {tool_name}? [y/N] ")
        allowed = answer.strip().lower() in ("y", "yes")
        return PermissionDecision(
            allowed=allowed, source="cli",
            reason="" if allowed else "Denied by user",
        )

    async def ask_user_question(self, questions: list[dict]) -> dict:
        results = {}
        for q in questions:
            answer = input(f"{q.get('question', '?')} > ")
            results[q.get("question", "")] = answer
        return {"result": results}
