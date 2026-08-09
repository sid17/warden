"""ToolScope — per-mode tool restriction config."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ToolScope:
    """Per-mode tool restriction config.

    Supports whitelist (``allowed``) or blacklist (``denied``) mode.
    When both are ``None``, all tools are permitted.
    """

    allowed: list[str] | None = None
    denied: list[str] | None = None

    def is_allowed(self, tool_name: str) -> bool:
        # Whitelist wins over blacklist; if neither is set, everything passes
        if self.allowed is not None:
            return tool_name in self.allowed
        if self.denied is not None:
            return tool_name not in self.denied
        return True

    def to_disallowed_tools(self, all_tools: list[str]) -> list[str]:
        """Convert to disallowed_tools list for provider creation."""
        # Inverts whitelist to blacklist format that the SDK expects
        if self.allowed is not None:
            return [t for t in all_tools if t not in self.allowed]
        if self.denied is not None:
            return list(self.denied)
        return []

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ToolScope):
            return NotImplemented
        return self.allowed == other.allowed and self.denied == other.denied

    def __hash__(self) -> int:
        allowed = tuple(self.allowed) if self.allowed is not None else None
        denied = tuple(self.denied) if self.denied is not None else None
        return hash((allowed, denied))
