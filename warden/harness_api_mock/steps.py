"""Step primitives — the engine's script vocabulary (task-14).

A ``Script`` is an ordered ``list[Step]`` the ``MockRunner`` plays to produce a
deterministic ``Event`` stream (no LLM, no subprocess). The **step types** are
product-agnostic engine machinery; the concrete **script instances** are supplied by
the active profile (e.g. a ``<profile>/scripts.py``).

Invariants a profile's scripts must uphold (mirrors the real runner):
- **first event is always ``session``** (``harness_api.schemas`` Event docstring);
- **seq** is per-run monotonic (assigned by the runner, not the step);
- **exactly one terminal** (``result``/``error``/``stopped``).

Step variants (each a frozen dataclass; ``*_fn`` builders take the run context so a
step can read ``spec.input`` / ``run_id`` / ``manifest``):
- ``EmitStep(type, data_fn)`` — build ``data`` and emit one ``Event``.
- ``SleepStep(seconds)`` — sleep ``seconds * config.step_delay_s`` (streaming realism).
- ``InvokeToolStep(tool, args_fn)`` — call the ``ToolInvoker`` seam (N1 writeback).
- ``GateStep(tool_name, concepts_fn, reason)`` — the durable-HITL pause (§6).

``InvokeToolStep`` is invisible on the wire (the tool does DB writeback out-of-band),
so adding tool calls keeps the event stream — and the shared contract suite —
byte-identical (task-10 A1/D8).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

# A step's builder reads a small context dict: {"spec": RunSpec, "run_id": str,
# "session_id": str, "resumed": bool, "manifest": list[{path,title,order}]}. Kept as
# ``dict`` to avoid a circular import with runner.
StepCtx = dict[str, Any]
DataFn = Callable[[StepCtx], dict[str, Any]]


@dataclass(frozen=True)
class EmitStep:
    """Emit one typed event; ``data_fn`` builds its ``data`` from the run context."""

    type: str
    data_fn: DataFn = field(default=lambda ctx: {})


@dataclass(frozen=True)
class SleepStep:
    """Sleep ``seconds * config.step_delay_s`` — streaming realism, 0 in tests."""

    seconds: float = 1.0


@dataclass(frozen=True)
class InvokeToolStep:
    """Call the ``ToolInvoker`` seam (N1: fires the profile's DB writeback tool)."""

    tool: str
    args_fn: DataFn = field(default=lambda ctx: {})


@dataclass(frozen=True)
class GateStep:
    """The durable-HITL pause (§6): emit ``permission_request``, wait for a
    ``tool_confirmation`` resume (or SLA auto-deny)."""

    tool_name: str = "confirm_landscape"
    concepts_fn: Callable[[StepCtx], list[str]] = field(
        default=lambda ctx: ["intro", "core", "advanced"]
    )
    reason: str = "Confirm before proceeding."  # profiles override with their own text


Step = EmitStep | SleepStep | InvokeToolStep | GateStep
Script = list[Step]


__all__ = [
    "StepCtx",
    "DataFn",
    "EmitStep",
    "SleepStep",
    "InvokeToolStep",
    "GateStep",
    "Step",
    "Script",
]
