"""Workflow definition schemas — maps to *.yaml / GET /api/workflows."""

from __future__ import annotations

from pydantic import BaseModel

from warden.workspace.workflow.middleware import WorkflowMiddleware
from warden.workspace.workflow.permissions import FileAccess, Permissions, ToolAccess


class Workflow(BaseModel):
    """A workflow definition parsed from .workflows/*.yaml — the harness's permission manifest.

    SAFE-5: the optional ``middleware`` block lets a workflow's safety policy
    (which input/output middlewares run) travel WITH the manifest, symmetric to
    ``permissions``. ``config/build.py::apply_workflow_middleware`` merges it into
    the effective ``MiddlewareConfig`` at drive time.
    """

    name: str
    description: str
    permissions: Permissions | None = None
    middleware: WorkflowMiddleware | None = None
    # EXT-P1/A2 (E4): a ``{tool_name → event_type}`` map. The harness re-tags a
    # named custom-tool call into the typed egress event, ``data`` opaque, and never
    # learns the tool names (product policy). Values are validated at load against
    # the fixed milestone set (checkpoint/completion/milestone) — see ``loader.py``.
    event_tool_map: dict[str, str] = {}


__all__ = [
    "FileAccess",
    "Permissions",
    "ToolAccess",
    "Workflow",
    "WorkflowMiddleware",
]
