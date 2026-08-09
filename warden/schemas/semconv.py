"""GenAI semantic-convention attribute names + builders (ONE convention).

OTel GenAI-semconv dot-notation attribute names, shared by every enriched
telemetry path (the wire ``Event``, both Langfuse tracers) so usage/model/tool
attributes speak one vocabulary — the SAME dot-notation the audit JSONL emits
(``schemas/audit.py`` ``to_jsonl_dict``), not a forked second convention.
"""
from __future__ import annotations

from typing import Any

from warden.schemas.usage import Usage

# Canonical OTel GenAI-semconv attribute names (dot notation).
REQUEST_MODEL = "gen_ai.request.model"
USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens"
USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
OPERATION_NAME = "gen_ai.operation.name"
SYSTEM = "gen_ai.system"
TOOL_NAME = "gen_ai.tool.name"


def usage_attrs(
    usage: Usage | dict | None,
    model: str | None = None,
    system: str | None = None,
) -> dict[str, Any]:
    """gen_ai.* attributes for an LLM-call / usage-bearing event.

    Accepts a :class:`Usage`, a normalized ``{input, output, ...}`` dict, or a
    raw provider dict (``input_tokens``/``output_tokens``) — reads whichever
    token keys are present.
    """
    if isinstance(usage, Usage):
        inp, out = usage.input, usage.output
    else:
        u = usage or {}
        inp = u.get("input", u.get("input_tokens", 0))
        out = u.get("output", u.get("output_tokens", 0))
    attrs: dict[str, Any] = {
        OPERATION_NAME: "chat",
        USAGE_INPUT_TOKENS: inp,
        USAGE_OUTPUT_TOKENS: out,
    }
    if model:
        attrs[REQUEST_MODEL] = model
    if system:
        attrs[SYSTEM] = system
    return attrs


def tool_attrs(tool_name: str | None, system: str | None = None) -> dict[str, Any]:
    """gen_ai.* attributes for a tool_use / tool_result event."""
    attrs: dict[str, Any] = {OPERATION_NAME: "execute_tool"}
    if tool_name:
        attrs[TOOL_NAME] = tool_name
    if system:
        attrs[SYSTEM] = system
    return attrs


__all__ = [
    "REQUEST_MODEL", "USAGE_INPUT_TOKENS", "USAGE_OUTPUT_TOKENS",
    "OPERATION_NAME", "SYSTEM", "TOOL_NAME", "usage_attrs", "tool_attrs",
]
