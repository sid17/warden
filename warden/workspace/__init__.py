"""Workspace (L0) — what the agent operates on.

A workspace is the task folder (``base_dir/{user_id}/{task_id}/``) plus its
capabilities: the **workflow** permission manifest (``.workflows/*.yaml``), the
**skills/agents** scaffolding (bootstrap), and the task-folder abstraction that
restores/snapshots it via the persistence backend. See ``01-conceptual-model``
§4 and L0 in §5.

Depends on ``warden.persistence`` (the storage mechanism); persistence
never depends back on this package.
"""

from warden.workspace.bootstrap import bootstrap, verify_bootstrap
from warden.workspace.credentials import reinject_credentials
from warden.workspace.task_workspace import (
    ensure_restored,
    home_env,
    snapshot,
)
from warden.workspace.workflow import (
    FileAccess,
    Permissions,
    ToolAccess,
    Workflow,
)

__all__ = [
    "bootstrap",
    "verify_bootstrap",
    "home_env",
    "ensure_restored",
    "reinject_credentials",
    "snapshot",
    "Workflow",
    "FileAccess",
    "Permissions",
    "ToolAccess",
]
