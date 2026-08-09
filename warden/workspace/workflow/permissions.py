"""Workflow permission schemas — what the agent can access."""

from __future__ import annotations

from pydantic import BaseModel


class FileAccess(BaseModel):
    """File-level read/write permission globs."""

    read: list[str] = []
    write: list[str] = []


class ToolAccess(BaseModel):
    """Tool-level allow/deny/confirm lists.

    ``confirm`` (EXT-G1) is the allow-all-except-one gate: paired with ``mode: auto``,
    every tool auto-allows EXCEPT the named ones, which escalate to the human (durable
    HITL pause → confirm → resume). A ``confirm`` tool must NOT also be in ``deny`` (a
    hard deny short-circuits and aborts instead of pausing)."""

    allow: list[str] = []
    deny: list[str] = []
    confirm: list[str] = []


class Permissions(BaseModel):
    """What the agent can access, enforced by PermissionChecker."""

    mode: str = "default"
    file_access: FileAccess | None = None
    tool_access: ToolAccess | None = None
