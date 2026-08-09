"""Workflow middleware schema — safety policy that TRAVELS WITH THE MANIFEST.

SAFE-5: a workflow's *.yaml may declare a ``middleware:`` block so its safety
policy (which input/output middlewares run, and whether the pipelines are on)
travels with the workflow manifest, exactly as ``permissions:`` does. This is the
declarative surface only — names + toggles. It mirrors ``MiddlewareConfig``'s
declarative fields but carries NO Python instances (a YAML can't hold objects);
``config/build.py::apply_workflow_middleware`` merges it into the effective
``MiddlewareConfig`` before the pipeline is built.
"""

from __future__ import annotations

from pydantic import BaseModel

from warden.config.models import (
    InputMiddlewareName,
    OutputMiddlewareName,
)


class WorkflowMiddleware(BaseModel):
    """A workflow's declared middleware policy (declarative names + toggles).

    ``input``/``output`` EXTEND the base config's name lists; ``enable_input``/
    ``enable_output`` OVERRIDE the base master switches when set (non-None).
    ``cascade_members`` optionally re-orders the production cascade battery.
    """

    input: list[InputMiddlewareName] = []
    output: list[OutputMiddlewareName] = []
    enable_input: bool | None = None
    enable_output: bool | None = None
    cascade_members: list[str] | None = None
