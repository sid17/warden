"""Tests for permission hot-swap through the Orchestrator."""

from warden.safety.permissions.checker import (
    PermissionChecker,
)
from warden.workspace.workflow.permissions import Permissions


def test_from_workflow_permissions_resets_to_permissive_default():
    """from_workflow_permissions(None) resets checker to permissive default."""
    checker = PermissionChecker.from_workflow_permissions(
        Permissions(mode="read_only"),
    )
    d = checker.evaluate("Edit", {"file_path": "src/main.py"})
    assert not d.allowed

    # Reset to None → default CONFIRM mode
    checker = PermissionChecker.from_workflow_permissions(None)
    d = checker.evaluate("Read", {"file_path": "/tmp/x"})
    assert d.allowed


def test_from_workflow_permissions_applies_new_deny():
    """Swapping to read_only permissions denies write tools."""
    checker = PermissionChecker()
    assert checker.evaluate("Read", {}).allowed is True

    checker = PermissionChecker.from_workflow_permissions(
        Permissions(mode="read_only"),
    )
    d = checker.evaluate("Write", {"file_path": "/tmp/x", "content": ""})
    assert not d.allowed
    assert "Read-only" in d.reason


def test_evaluate_uses_new_checker_after_swap():
    """After creating a new checker, evaluate() uses its settings."""
    checker = PermissionChecker()
    d1 = checker.evaluate("Write", {"file_path": "/tmp/x", "content": ""})
    assert d1.requires_confirmation is True

    checker = PermissionChecker.from_workflow_permissions(
        Permissions(mode="auto"),
    )
    d2 = checker.evaluate("Write", {"file_path": "/tmp/x", "content": ""})
    assert d2.allowed is True

    checker = PermissionChecker.from_workflow_permissions(
        Permissions(mode="default"),
    )
    d3 = checker.evaluate("Write", {"file_path": "/tmp/x", "content": ""})
    assert d3.requires_confirmation is True
