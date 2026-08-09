"""Example profile scripts — a product-agnostic reference the mock engine can play.

A ``Script`` is an ordered ``list[Step]`` the ``MockRunner`` plays to produce a
deterministic ``Event`` stream — no LLM, no subprocess. This demonstrates the ``Step``
vocabulary and the engine invariants a profile must uphold WITHOUT naming any product:

- **first event is always ``session``**;
- **seq** is per-run monotonic (assigned by the runner, not the step);
- **exactly one terminal** (``result`` / ``error`` / ``stopped``).

``SCRIPTS`` maps a workflow name to its script; the runner picks
``SCRIPTS[input.workflow]`` and falls back to ``"default"``.
"""

from __future__ import annotations

from warden.harness_api_mock.steps import (
    EmitStep,
    InvokeToolStep,
    Script,
)

# session → (tool: emit_step) → checkpoint → result. The InvokeToolStep is invisible
# on the wire (it drives the profile's ToolInvoker); the example uses the engine's
# canned NoopToolInvoker, so it records the call and returns without side effects.
_DEFAULT: Script = [
    EmitStep("session", lambda ctx: {"resumed": ctx["resumed"]}),
    InvokeToolStep("emit_step", args_fn=lambda ctx: {"step": "work", "status": "running"}),
    EmitStep("checkpoint", lambda ctx: {"step": "work"}),
    EmitStep(
        "result",
        lambda ctx: {
            "result": "ok",
            "usage": {"input": 100, "output": 200, "cached": 0},
            "cost_usd": 0.001,
            "model": "mock-harness",
        },
    ),
]

SCRIPTS = {"default": _DEFAULT}

__all__ = ["SCRIPTS"]
