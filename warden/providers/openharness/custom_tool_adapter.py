"""Adapt a harness ``CustomTool`` to an OpenHarness ``BaseTool`` (G1/B2).

OpenHarness tools implement ``openharness.tools.base.BaseTool``: a ``name`` /
``description`` / ``input_model`` (a pydantic model) plus an async
``execute(arguments, context) -> ToolResult``. A harness ``CustomTool`` carries a
JSON-Schema ``input_schema`` and a sync-or-async ``handler(**kwargs) -> Any``.

This module bridges the two: the JSON-Schema becomes a permissive pydantic model
(the schema is advertised to the model verbatim via ``to_api_schema`` override,
while validation stays lenient so arbitrary kwargs pass through to the handler),
and ``execute`` invokes the handler (awaiting if needed) and normalizes the
result to a ``ToolResult`` string. Delivery is an in-proc registry entry
(TOOL-2, ``custom_tool_delivery = in_proc_list``).
"""

from __future__ import annotations

import inspect
from typing import Any

from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult
from pydantic import BaseModel, ConfigDict

from warden.seams.custom_tools import CustomTool


class _PassthroughInput(BaseModel):
    """Lenient input model — accepts any kwargs so the handler owns validation.

    The real contract the model sees is ``CustomTool.input_schema`` (surfaced via
    the adapter's ``to_api_schema`` override); this model only needs to let the
    validated tool_input through to the handler as a plain dict.
    """

    model_config = ConfigDict(extra="allow")


class CustomToolAdapter(BaseTool):
    """Wrap one ``CustomTool`` as an OpenHarness ``BaseTool``."""

    input_model = _PassthroughInput

    def __init__(self, custom_tool: CustomTool) -> None:
        self._ct = custom_tool
        self.name = custom_tool.name
        self.description = custom_tool.description

    def to_api_schema(self) -> dict[str, Any]:
        """Advertise the CustomTool's own JSON-Schema to the model."""
        return {
            "name": self._ct.name,
            "description": self._ct.description,
            "input_schema": self._ct.input_schema,
        }

    def is_read_only(self, arguments: BaseModel) -> bool:
        # Custom tools are treated as mutating so DEFAULT-mode permission gating
        # (and the orchestrator PRE_TOOL_USE hook) always evaluates them.
        del arguments
        return False

    async def execute(
        self, arguments: BaseModel, context: ToolExecutionContext
    ) -> ToolResult:
        del context
        kwargs = arguments.model_dump()
        try:
            res = self._ct.handler(**kwargs)
            if inspect.isawaitable(res):
                res = await res
        except Exception as exc:  # noqa: BLE001 — surface as a tool error, not a crash
            return ToolResult(output=f"custom tool {self._ct.name} failed: {exc}", is_error=True)
        return ToolResult(output=str(res))


def register_custom_tools(registry: Any, custom_tools: list[CustomTool]) -> None:
    """Register each ``CustomTool`` into an OpenHarness ``ToolRegistry``."""
    for ct in custom_tools:
        registry.register(CustomToolAdapter(ct))
