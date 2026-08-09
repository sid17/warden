from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from warden.safety.permissions.sensitive_paths import (
    _extract_path,
    check_sensitive,
)

if TYPE_CHECKING:
    from warden.workspace.workflow.permissions import FileAccess, Permissions, ToolAccess


class PermissionMode(Enum):
    CONFIRM = "confirm"
    READ_ONLY = "read_only"
    AUTO = "auto"


_YAML_MODE_MAP: dict[str, PermissionMode] = {
    "default": PermissionMode.CONFIRM,
    "strict": PermissionMode.CONFIRM,
    "read_only": PermissionMode.READ_ONLY,
    "auto": PermissionMode.AUTO,
}

_WRITE_TOOLS = frozenset({"Write", "Edit", "Bash"})


@dataclass(frozen=True)
class PermissionDecision:
    allowed: bool
    requires_confirmation: bool = False
    reason: str = ""
    source: str = ""


READ_ONLY_TOOLS = frozenset({
    "Read", "Grep", "Glob", "WebSearch", "WebFetch",
    "TaskCreate", "TaskUpdate", "TaskGet", "TaskList",
})


class PermissionChecker:
    """Evaluate tool permissions with a priority chain."""

    def __init__(
        self,
        mode: PermissionMode = PermissionMode.CONFIRM,
        file_access: FileAccess | None = None,
        tool_access: ToolAccess | None = None,
        workspace_root: Path | str | None = None,
    ) -> None:
        self.mode = mode
        self._file_access = file_access
        self._tool_access = tool_access
        self._remembered: dict[str, set[str]] = {}
        # The run's workspace root (cwd). The SDK passes tools ABSOLUTE file paths
        # (e.g. /app/data/workspaces/<user>/<task>/courses/x.md), but workflow
        # file_access globs are workspace-RELATIVE (courses/**). Without this,
        # fnmatch(absolute, "courses/**") never matches → every file op is denied
        # once the checker is enforced (the file_access boundary was silently
        # non-functional). We relativize an absolute path under this root before
        # glob-matching. None ⇒ no relativization (backward-compatible: tests that
        # pass relative paths are unaffected).
        self._workspace_root = Path(workspace_root) if workspace_root else None

    @classmethod
    def from_workflow_permissions(
        cls,
        permissions: Permissions | None,
        workspace_root: Path | str | None = None,
    ) -> PermissionChecker:
        """Create a PermissionChecker from workflow YAML permissions config.

        ``workspace_root`` (the run's cwd) lets ``_check_file_access`` relativize the
        absolute paths the SDK sends before matching the workspace-relative globs.
        """
        if permissions is None:
            return cls(workspace_root=workspace_root)
        mode = _YAML_MODE_MAP.get(permissions.mode, PermissionMode.CONFIRM)
        return cls(
            mode=mode,
            file_access=permissions.file_access,
            tool_access=permissions.tool_access,
            workspace_root=workspace_root,
        )

    def evaluate(self, tool_name: str, tool_input: dict) -> PermissionDecision:
        """Priority chain: sensitive → tool_access.deny → remembered → tool_access.allow → file_access → read-only → mode."""
        # 1. Sensitive path deny (always, regardless of mode)
        if check_sensitive(tool_name, tool_input):
            return PermissionDecision(allowed=False, reason="Sensitive path", source="sensitive_path")

        # 2. tool_access.deny — hard deny from workflow config
        tool_deny = self._check_tool_deny(tool_name)
        if tool_deny is not None:
            return tool_deny

        # 2b. tool_access.confirm (EXT-G1) — escalate to the human. Placed after
        # deny (a hard deny aborts) and before allow, so "allow-all-except-[names]"
        # (mode: auto + confirm:[...]) defers exactly the named tools while the
        # others auto-allow. Everything downstream (escalate → durable defer →
        # pause → resume) already exists.
        tool_confirm = self._check_tool_confirm(tool_name)
        if tool_confirm is not None:
            return tool_confirm

        # 3. Session "always allow" cache
        if self.is_remembered(tool_name, tool_input):
            return PermissionDecision(allowed=True, source="remembered")

        # 4. tool_access.allow — auto-allow from workflow config
        tool_allow = self._check_tool_allow(tool_name)
        if tool_allow is not None:
            return tool_allow

        # 5. File access glob check (before read-only auto-allow so globs can restrict reads)
        file_decision = self._check_file_access(tool_name, tool_input)
        if file_decision is not None:
            return file_decision

        # 6. Read-only tools auto-allow
        if tool_name in READ_ONLY_TOOLS:
            return PermissionDecision(allowed=True, source="read_only")

        # 7. Mode-based default
        if self.mode == PermissionMode.AUTO:
            return PermissionDecision(allowed=True, source="mode")
        if self.mode == PermissionMode.READ_ONLY:
            return PermissionDecision(
                allowed=False, reason="Read-only mode: mutating tools denied", source="mode",
            )
        # CONFIRM mode
        return PermissionDecision(allowed=False, requires_confirmation=True, source="mode")

    def _check_tool_deny(self, tool_name: str) -> PermissionDecision | None:
        """Check if tool is denied by workflow tool_access config."""
        if not self._tool_access or not self._tool_access.deny:
            return None
        if tool_name in self._tool_access.deny:
            return PermissionDecision(
                allowed=False,
                reason=f"Tool '{tool_name}' denied by workflow permissions",
                source="tool_access",
            )
        return None

    def _check_tool_allow(self, tool_name: str) -> PermissionDecision | None:
        """Check if tool is explicitly allowed by workflow tool_access config."""
        if not self._tool_access or not self._tool_access.allow:
            return None
        if tool_name in self._tool_access.allow:
            return PermissionDecision(
                allowed=True,
                reason=f"Tool '{tool_name}' allowed by workflow permissions",
                source="tool_access",
            )
        return None

    def _check_tool_confirm(self, tool_name: str) -> PermissionDecision | None:
        """EXT-G1 — escalate a workflow-``confirm``-listed tool to the human.

        Returns a ``requires_confirmation`` decision (routed to the durable HITL
        handler by ``permission_surface``), or ``None`` if the tool is not listed.
        Mirrors ``_check_tool_allow``."""
        if not self._tool_access or not self._tool_access.confirm:
            return None
        if tool_name in self._tool_access.confirm:
            return PermissionDecision(
                allowed=False,
                requires_confirmation=True,
                reason=f"Tool '{tool_name}' requires confirmation by workflow permissions",
                source="tool_access",
            )
        return None

    def _relativize(self, path: str) -> str:
        """Return ``path`` relative to the workspace root when it is an absolute
        path *under* that root; otherwise return it unchanged.

        The SDK sends absolute file paths but globs are workspace-relative, so an
        absolute path must be relativized before ``fnmatch``. Crucially, the
        workspace root itself may be a workspace-RELATIVE config path (e.g.
        ``data/workspaces/<user>/<task>``) while the tool path is ABSOLUTE
        (``/app/data/workspaces/<user>/<task>/courses/x`` — the CLI resolved the
        session cwd against the process cwd). So normalize the root to absolute the
        SAME way (``os.path.abspath`` prepends the process cwd without following
        symlinks) before ``relative_to``. A path outside the workspace (e.g.
        ``/etc/passwd``) is left absolute — it won't match a workspace glob, and
        the sensitive-path check (step 1) already guards the dangerous ones."""
        root = self._workspace_root
        if root is None:
            return path
        p = Path(path)
        if not p.is_absolute():
            return path  # already workspace-relative — match the globs as-is
        root_abs = Path(os.path.abspath(root))
        try:
            return str(p.relative_to(root_abs))
        except ValueError:
            return path  # not under the workspace root — keep absolute

    def _check_file_access(self, tool_name: str, tool_input: dict) -> PermissionDecision | None:
        """Check file access globs. Returns None if no restriction applies."""
        if not self._file_access:
            return None

        path = _extract_path(tool_name, tool_input)
        if not path:
            return None
        path = self._relativize(path)

        is_write = tool_name in _WRITE_TOOLS
        globs = self._file_access.write if is_write else self._file_access.read

        if not globs:
            return None  # empty list = no restriction

        for pattern in globs:
            if fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(path.split("/")[-1], pattern):
                return None  # matched — allowed, fall through to mode

        return PermissionDecision(
            allowed=False,
            reason="File access denied by workflow permissions",
            source="file_access",
        )

    def remember(self, tool_name: str, pattern: str = "*") -> None:
        """Remember an 'always allow' for a tool."""
        if tool_name not in self._remembered:
            self._remembered[tool_name] = set()
        self._remembered[tool_name].add(pattern)

    def is_remembered(self, tool_name: str, tool_input: dict) -> bool:
        """Check if a tool is in the 'always allow' cache."""
        patterns = self._remembered.get(tool_name)
        if not patterns:
            return False
        if "*" in patterns:
            return True
        # Check prefix patterns like "npm:*"
        cmd = str(tool_input.get("command", ""))
        for pat in patterns:
            if pat.endswith(":*"):
                prefix = pat[:-2]
                if cmd.startswith(prefix) or cmd.split()[0] == prefix if cmd else False:
                    return True
        return False
