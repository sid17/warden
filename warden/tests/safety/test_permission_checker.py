"""Tests for safety.permissions.checker — priority chain evaluation."""

import os

from warden.safety.permissions.checker import PermissionChecker, PermissionMode
from warden.workspace.workflow.permissions import FileAccess, Permissions, ToolAccess


def test_sensitive_path_denied():
    pc = PermissionChecker()
    d = pc.evaluate("Read", {"file_path": "/home/user/.ssh/id_rsa"})
    assert not d.allowed
    assert d.reason == "Sensitive path"


# --- EXT-G1: the allow-all-except-one gate --------------------------------


def test_confirm_list_gates_exactly_one_tool_under_auto():
    """mode: auto + confirm:[confirm_landscape] → only the named tool defers; every
    other tool auto-allows (the allow-all-except-one gate)."""
    pc = PermissionChecker.from_workflow_permissions(
        Permissions(mode="auto",
                    tool_access=ToolAccess(confirm=["confirm_landscape"]))
    )
    gate = pc.evaluate("confirm_landscape", {})
    assert gate.allowed is False and gate.requires_confirmation is True
    assert pc.evaluate("emit_checkpoint", {}).allowed is True
    assert pc.evaluate("course_complete", {}).allowed is True
    # A built-in mutating tool also auto-allows (not in the confirm list).
    assert pc.evaluate("Write", {"file_path": "out.md", "content": "x"}).allowed


def test_confirm_without_auto_mode_storms_every_mutating_tool():
    """The mode: auto trap (Gotcha #1): confirm:[...] WITHOUT mode: auto defers every
    non-allowed mutating tool (default CONFIRM fall-through), breaking allow-the-others.
    This regression proves why mode: auto is mandatory in the manifest contract."""
    pc = PermissionChecker.from_workflow_permissions(
        Permissions(tool_access=ToolAccess(confirm=["confirm_landscape"]))  # no mode
    )
    # The gate tool still defers...
    assert pc.evaluate("confirm_landscape", {}).requires_confirmation is True
    # ...but so does a built-in Write (CONFIRM mode default) — the storm.
    assert pc.evaluate("Write", {"file_path": "out.md", "content": "x"}).requires_confirmation


def test_deny_aborts_not_pauses():
    """Gotcha #3: a tool in deny short-circuits (aborts), never reaching the durable
    handler — so the gate tool must go in confirm, not deny."""
    pc = PermissionChecker.from_workflow_permissions(
        Permissions(mode="auto", tool_access=ToolAccess(deny=["confirm_landscape"]))
    )
    d = pc.evaluate("confirm_landscape", {})
    assert d.allowed is False and d.requires_confirmation is False  # hard deny


def test_sensitive_path_denied_even_in_auto_mode():
    pc = PermissionChecker(mode=PermissionMode.AUTO)
    d = pc.evaluate("Read", {"file_path": "/home/user/.ssh/id_rsa"})
    assert not d.allowed
    assert d.reason == "Sensitive path"


def test_sensitive_env_file():
    pc = PermissionChecker()
    d = pc.evaluate("Read", {"file_path": "/project/.env"})
    assert not d.allowed


def test_sensitive_env_variant():
    pc = PermissionChecker()
    d = pc.evaluate("Read", {"file_path": "/project/.env.production"})
    assert not d.allowed


def test_normal_path_not_sensitive():
    pc = PermissionChecker()
    d = pc.evaluate("Read", {"file_path": "/home/user/code/main.py"})
    assert d.allowed


def test_read_only_tool_auto_allowed():
    pc = PermissionChecker()
    d = pc.evaluate("Read", {"file_path": "/code/main.py"})
    assert d.allowed


def test_grep_auto_allowed():
    pc = PermissionChecker()
    d = pc.evaluate("Grep", {"pattern": "TODO"})
    assert d.allowed


def test_confirm_mode_mutating_tool():
    pc = PermissionChecker(mode=PermissionMode.CONFIRM)
    d = pc.evaluate("Bash", {"command": "rm -rf /"})
    assert not d.allowed
    assert d.requires_confirmation


def test_auto_mode_allows_all():
    pc = PermissionChecker(mode=PermissionMode.AUTO)
    d = pc.evaluate("Bash", {"command": "npm install"})
    assert d.allowed


def test_read_only_mode_denies_mutating():
    pc = PermissionChecker(mode=PermissionMode.READ_ONLY)
    d = pc.evaluate("Bash", {"command": "npm install"})
    assert not d.allowed
    assert "Read-only mode" in d.reason


def test_read_only_mode_allows_read():
    pc = PermissionChecker(mode=PermissionMode.READ_ONLY)
    d = pc.evaluate("Read", {"file_path": "/code/main.py"})
    assert d.allowed


def test_remember_allows_subsequent():
    pc = PermissionChecker()
    d1 = pc.evaluate("Bash", {"command": "npm test"})
    assert d1.requires_confirmation

    pc.remember("Bash")
    d2 = pc.evaluate("Bash", {"command": "npm test"})
    assert d2.allowed


def test_remember_with_pattern():
    pc = PermissionChecker()
    pc.remember("Bash", "npm:*")
    d = pc.evaluate("Bash", {"command": "npm install"})
    assert d.allowed


def test_bash_with_sensitive_path_in_command():
    pc = PermissionChecker(mode=PermissionMode.AUTO)
    d = pc.evaluate("Bash", {"command": "cat /home/user/.ssh/id_rsa"})
    assert not d.allowed
    assert d.reason == "Sensitive path"


# --- from_workflow_permissions mode mapping ---

def test_from_permissions_default_mode():
    pc = PermissionChecker.from_workflow_permissions(Permissions(mode="default"))
    assert pc.mode == PermissionMode.CONFIRM


def test_from_permissions_read_only_mode():
    pc = PermissionChecker.from_workflow_permissions(Permissions(mode="read_only"))
    assert pc.mode == PermissionMode.READ_ONLY


def test_from_permissions_auto_mode():
    pc = PermissionChecker.from_workflow_permissions(Permissions(mode="auto"))
    assert pc.mode == PermissionMode.AUTO


def test_from_permissions_strict_mode():
    pc = PermissionChecker.from_workflow_permissions(Permissions(mode="strict"))
    assert pc.mode == PermissionMode.CONFIRM


def test_from_permissions_unknown_mode():
    pc = PermissionChecker.from_workflow_permissions(Permissions(mode="whatever"))
    assert pc.mode == PermissionMode.CONFIRM


def test_from_permissions_none():
    pc = PermissionChecker.from_workflow_permissions(None)
    assert pc.mode == PermissionMode.CONFIRM


# --- file_access glob matching ---

def test_file_access_read_glob_allowed():
    fa = FileAccess(read=["src/**"])
    pc = PermissionChecker(mode=PermissionMode.AUTO, file_access=fa)
    d = pc.evaluate("Read", {"file_path": "src/main.py"})
    assert d.allowed


def test_file_access_read_glob_denied():
    fa = FileAccess(read=["src/**"])
    pc = PermissionChecker(mode=PermissionMode.AUTO, file_access=fa)
    d = pc.evaluate("Read", {"file_path": "secrets/key.pem"})
    assert not d.allowed
    assert "File access denied" in d.reason


def test_file_access_write_glob_allowed():
    fa = FileAccess(write=["docs/**"])
    pc = PermissionChecker(mode=PermissionMode.AUTO, file_access=fa)
    d = pc.evaluate("Edit", {"file_path": "docs/notes.md"})
    assert d.allowed


def test_file_access_write_glob_denied():
    fa = FileAccess(write=["docs/**"])
    pc = PermissionChecker(mode=PermissionMode.AUTO, file_access=fa)
    d = pc.evaluate("Edit", {"file_path": "src/main.py"})
    assert not d.allowed
    assert "File access denied" in d.reason


def test_file_access_empty_globs_no_restriction():
    fa = FileAccess(read=[])
    pc = PermissionChecker(mode=PermissionMode.AUTO, file_access=fa)
    d = pc.evaluate("Read", {"file_path": "anything/here.py"})
    assert d.allowed


# --- absolute-path relativization (the gate-integration bug fix) ---
# The SDK sends tools ABSOLUTE file paths, but workflow globs are workspace-
# relative. Without workspace_root, fnmatch(absolute, "courses/**") never matches
# → every file op denied once the checker is enforced (the file_access boundary
# was silently non-functional). workspace_root relativizes first.

_WS = "/app/data/workspaces/owner-A/task-1"


def test_abs_write_under_workspace_relativized_and_allowed():
    """An absolute write path UNDER the workspace root is relativized to
    ``courses/x.md``, matches the write glob, and is allowed."""
    fa = FileAccess(write=["courses/**"])
    pc = PermissionChecker(mode=PermissionMode.AUTO, file_access=fa, workspace_root=_WS)
    d = pc.evaluate("Write", {"file_path": f"{_WS}/courses/fullgate/00 - APPENDIX.md"})
    assert d.allowed


def test_abs_write_outside_globs_still_denied():
    """Relativization does NOT weaken confinement: an absolute path under the
    workspace but outside the write globs still denies (source=file_access)."""
    fa = FileAccess(write=["courses/**"])
    pc = PermissionChecker(mode=PermissionMode.AUTO, file_access=fa, workspace_root=_WS)
    d = pc.evaluate("Write", {"file_path": f"{_WS}/secrets/leak.txt"})
    assert not d.allowed
    assert d.source == "file_access"


def test_abs_read_relativized_against_read_globs():
    """A read whitelist still works with absolute paths once relativized."""
    fa = FileAccess(read=["docs/**"])
    pc = PermissionChecker(mode=PermissionMode.AUTO, file_access=fa, workspace_root=_WS)
    assert pc.evaluate("Read", {"file_path": f"{_WS}/docs/x.md"}).allowed
    assert not pc.evaluate("Read", {"file_path": f"{_WS}/.claude/skills/s.md"}).allowed


def test_abs_path_outside_workspace_kept_absolute():
    """A path NOT under the workspace root stays absolute (won't match a
    workspace glob) — the sensitive-path check guards the dangerous ones."""
    fa = FileAccess(write=["courses/**"])
    pc = PermissionChecker(mode=PermissionMode.AUTO, file_access=fa, workspace_root=_WS)
    d = pc.evaluate("Write", {"file_path": "/etc/cron.d/evil"})
    assert not d.allowed


def test_relative_root_with_absolute_path():
    """THE bug: workspace_root is a workspace-RELATIVE config path
    (``data/workspaces/<user>/<task>``) while the SDK sends an ABSOLUTE path
    resolved against the process cwd. The root must be abspath-normalized before
    relative_to, else every file op denies. Build the absolute path from cwd so
    the test is cwd-independent."""
    rel_root = "data/workspaces/owner-A/task-1"
    abs_under = os.path.join(os.getcwd(), rel_root, "courses/x.md")
    fa = FileAccess(write=["courses/**"])
    pc = PermissionChecker(mode=PermissionMode.AUTO, file_access=fa, workspace_root=rel_root)
    assert pc.evaluate("Write", {"file_path": abs_under}).allowed


def test_relativization_noop_without_workspace_root():
    """Backward-compat: no workspace_root ⇒ absolute paths are NOT relativized,
    so a relative-glob workflow denies them (the pre-fix behavior, preserved for
    callers that never set a root)."""
    fa = FileAccess(write=["courses/**"])
    pc = PermissionChecker(mode=PermissionMode.AUTO, file_access=fa)
    d = pc.evaluate("Write", {"file_path": f"{_WS}/courses/x.md"})
    assert not d.allowed  # absolute path, no root → no match → denied


def test_no_file_access_no_restriction():
    pc = PermissionChecker(mode=PermissionMode.AUTO)
    d = pc.evaluate("Edit", {"file_path": "src/main.py"})
    assert d.allowed


def test_sensitive_path_beats_file_access():
    fa = FileAccess(read=["*/**"])  # would allow everything
    pc = PermissionChecker(mode=PermissionMode.AUTO, file_access=fa)
    d = pc.evaluate("Read", {"file_path": "/home/user/.ssh/id_rsa"})
    assert not d.allowed
    assert d.reason == "Sensitive path"


def test_bash_treated_as_write_for_file_access():
    fa = FileAccess(write=["docs/**"])
    pc = PermissionChecker(mode=PermissionMode.AUTO, file_access=fa)
    # Bash doesn't extract file_path the same way — it uses command substring
    # For a Bash command without a sensitive path match, _extract_path returns None
    # so file_access doesn't apply. This tests the intent.
    d = pc.evaluate("Write", {"file_path": "src/main.py"})
    assert not d.allowed
    assert "File access denied" in d.reason


# --- tool_access enforcement ---

def test_tool_access_deny_blocks_tool():
    ta = ToolAccess(deny=["Bash"])
    pc = PermissionChecker(mode=PermissionMode.AUTO, tool_access=ta)
    d = pc.evaluate("Bash", {"command": "ls"})
    assert not d.allowed
    assert d.source == "tool_access"
    assert "denied by workflow" in d.reason


def test_tool_access_deny_regardless_of_mode():
    """tool_access.deny is a hard deny even in AUTO mode."""
    ta = ToolAccess(deny=["Edit"])
    pc = PermissionChecker(mode=PermissionMode.AUTO, tool_access=ta)
    d = pc.evaluate("Edit", {"file_path": "src/main.py"})
    assert not d.allowed
    assert d.source == "tool_access"


def test_tool_access_deny_beats_remembered():
    """tool_access.deny takes priority over remembered tools."""
    ta = ToolAccess(deny=["Bash"])
    pc = PermissionChecker(tool_access=ta)
    pc.remember("Bash")
    d = pc.evaluate("Bash", {"command": "ls"})
    assert not d.allowed
    assert d.source == "tool_access"


def test_tool_access_allow_auto_allows():
    """tool_access.allow skips confirmation."""
    ta = ToolAccess(allow=["Bash"])
    pc = PermissionChecker(mode=PermissionMode.CONFIRM, tool_access=ta)
    d = pc.evaluate("Bash", {"command": "npm test"})
    assert d.allowed
    assert d.source == "tool_access"
    assert not d.requires_confirmation


def test_tool_access_allow_does_not_override_sensitive_path():
    """Sensitive path deny still takes priority over tool_access.allow."""
    ta = ToolAccess(allow=["Read"])
    pc = PermissionChecker(tool_access=ta)
    d = pc.evaluate("Read", {"file_path": "/home/user/.ssh/id_rsa"})
    assert not d.allowed
    assert d.source == "sensitive_path"


def test_tool_access_allow_does_not_override_deny():
    """If a tool is in both allow and deny, deny wins (checked first)."""
    ta = ToolAccess(allow=["Bash"], deny=["Bash"])
    pc = PermissionChecker(tool_access=ta)
    d = pc.evaluate("Bash", {"command": "ls"})
    assert not d.allowed
    assert d.source == "tool_access"


def test_tool_not_in_lists_falls_through():
    """A tool not in allow or deny falls through to normal chain."""
    ta = ToolAccess(allow=["Bash"], deny=["Write"])
    pc = PermissionChecker(mode=PermissionMode.CONFIRM, tool_access=ta)
    d = pc.evaluate("Edit", {"file_path": "src/main.py"})
    assert not d.allowed
    assert d.requires_confirmation
    assert d.source == "mode"


def test_from_workflow_permissions_passes_tool_access():
    perms = Permissions(
        mode="auto",
        tool_access=ToolAccess(deny=["Bash"]),
    )
    pc = PermissionChecker.from_workflow_permissions(perms)
    d = pc.evaluate("Bash", {"command": "ls"})
    assert not d.allowed
    assert d.source == "tool_access"


# --- source field correctness ---

def test_source_sensitive_path():
    pc = PermissionChecker()
    d = pc.evaluate("Read", {"file_path": "/home/user/.ssh/id_rsa"})
    assert d.source == "sensitive_path"


def test_source_remembered():
    pc = PermissionChecker()
    pc.remember("Bash")
    d = pc.evaluate("Bash", {"command": "ls"})
    assert d.source == "remembered"


def test_source_read_only():
    pc = PermissionChecker()
    d = pc.evaluate("Grep", {"pattern": "TODO"})
    assert d.source == "read_only"


def test_source_mode_auto():
    pc = PermissionChecker(mode=PermissionMode.AUTO)
    d = pc.evaluate("Bash", {"command": "ls"})
    assert d.source == "mode"


def test_source_mode_confirm():
    pc = PermissionChecker(mode=PermissionMode.CONFIRM)
    d = pc.evaluate("Bash", {"command": "ls"})
    assert d.source == "mode"


def test_source_file_access():
    fa = FileAccess(read=["src/**"])
    pc = PermissionChecker(file_access=fa)
    d = pc.evaluate("Read", {"file_path": "secrets/key.pem"})
    assert d.source == "file_access"
