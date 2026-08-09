"""N1 tool-invocation seam (§6) — the engine's product boundary.

At scripted ``InvokeToolStep``/``GateStep`` points the mock runner calls a
``ToolInvoker``. The mock's events are streaming-only, never the DB source of truth;
the invoked tools do the DB writeback exactly as in production.

The engine ships ``NoopToolInvoker`` ONLY (canned returns, product-free) — used by
hermetic tests and the ``noop`` mode. A product supplies its own invoker via its
**profile** (e.g. a ``<profile>/invoker.py`` exposing a ``ToolInvoker``);
``MockRunner`` builds it in ``profile`` mode. Keeping the seam a Protocol means the
script runs IDENTICALLY under either invoker (proven by ``test_tool_seam_noop_vs_task2``).
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ToolInvoker(Protocol):
    """The seam the mock runner drives at scripted tool points.

    ``invoke`` runs a named tool with args and returns its (JSON-able) result.
    ``confirm_landscape`` is the gate tool: the product's ``/confirm`` surfaces the
    gate row off this call. Both are async so a real task-2 invoker can do DB I/O.
    """

    async def invoke(
        self, run_id: str, tool_name: str, args: dict[str, Any]
    ) -> dict[str, Any]:
        ...

    async def confirm_landscape(
        self, run_id: str, concepts: list[str]
    ) -> dict[str, Any]:
        ...


class NoopToolInvoker:
    """Canned-return invoker — the only one this phase (task-1 done-condition).

    Records every call (``calls``) so tests can assert a tool fired exactly once
    across a gate pause/resume. No DB, no side effects, no LLM.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def invoke(
        self, run_id: str, tool_name: str, args: dict[str, Any]
    ) -> dict[str, Any]:
        self.calls.append((run_id, tool_name, dict(args)))
        return {"ok": True, "tool": tool_name}

    async def confirm_landscape(
        self, run_id: str, concepts: list[str]
    ) -> dict[str, Any]:
        self.calls.append((run_id, "confirm_landscape", {"concepts": list(concepts)}))
        # Canned: the product surfaces the gate row off this; the mock's
        # permission_request event carries the concepts on the wire.
        return {"ok": True, "surfaced": True, "concepts": list(concepts)}


def build_tool_invoker(mode: str) -> ToolInvoker:
    """Select the invoker by config mode.

    ``noop`` returns the canned invoker. ``profile`` is NOT built here: the active
    profile's invoker needs the runner's run registry (to resolve ``run_id ->
    task_id``, D7) plus the product API config, so ``MockRunner._default_invoker``
    constructs it directly. Reaching this branch means someone bypassed the runner —
    fail loudly (LAW 4)."""
    if mode == "noop":
        return NoopToolInvoker()
    if mode == "profile":
        raise NotImplementedError(
            "the 'profile' invoker must be built by MockRunner (it needs the run "
            "registry + product API config); do not call build_tool_invoker('profile')"
        )
    raise ValueError(f"unknown tool_invoker_mode: {mode!r}")
