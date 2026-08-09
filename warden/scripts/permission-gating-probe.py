#!/usr/bin/env python3
"""Permission-gating probe — checks whether the `can_use_tool` seam is actually
consulted for REGULAR vs CUSTOM tools, per provider.

As of pre-07 · M9 (2026-07-22) this is a REGRESSION GUARD for parity, not a
demonstration of a gap. Expected after-state: Claude regular+custom = GATED,
OpenHarness regular+custom = GATED, Codex regular = GATED / custom = NOT GATED
(ungated by design behind `codex_allow_ungated_custom_tools`; see §3d finding
in docs/guides/permissions/tool-permission-gating.md).

For the chosen provider we install a DENY-ALL permission handler (every
`can_use_tool` consult => DENY) and run two probes:

  A) regular tool: ask the model to write regular_out.txt with its native write
     tool. If gated, the file is ABSENT.
  B) custom tool:  register `ping` (its handler writes custom_ran.marker) and ask
     the model to call it.
       - gate CONSULTED  -> marker ABSENT   (blocked)         => GATED
       - gate BYPASSED   -> marker PRESENT   (ran anyway)      => NOT GATED

The authoritative signal is `handler consulted` (was the gate asked at all) plus
the side-effect (file / marker present). The `tool_use` event stream is only
reliably populated by Claude, so do NOT read gating off it for codex/openharness.

Usage (from repo root):
    PYTHONPATH=. uv run --no-sync python warden/scripts/permission-gating-probe.py \
        --provider openharness --model qwen3:8b --base /tmp/permproof
    env -u ANTHROPIC_API_KEY PYTHONPATH=. uv run --no-sync python \
        warden/scripts/permission-gating-probe.py --provider claude --base /tmp/permproof
    env -u OPENAI_API_KEY   PYTHONPATH=. uv run --no-sync python \
        warden/scripts/permission-gating-probe.py --provider codex  --base /tmp/permproof

Cost discipline: Claude+Codex OAuth, OpenHarness free Ollama qwen3:8b. Never the
API-key lane (prefix with `env -u ANTHROPIC_API_KEY` / `env -u OPENAI_API_KEY`).
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from warden import (
    ChatAPI,
    CustomTool,
    ErrorEvent,
    MessageEvent,
    ToolAccessNotificationEvent,
)
from warden.config import get_harness_config
from warden.seams.permissions import PermissionDecision


class DenyAllHandler:
    """PermissionHandler that denies every request — the gate, when consulted."""

    def __init__(self) -> None:
        self.consulted: list[str] = []

    async def request_permission(self, tool_name, tool_input, reason, tool_use_id=None):
        self.consulted.append(tool_name)
        return PermissionDecision(allowed=False, source="probe", reason="denied by probe")

    async def ask_user_question(self, questions):
        return {"result": {}}


def make_ping_tool(marker: Path) -> CustomTool:
    def ping_handler(**kwargs) -> str:
        marker.write_text("custom tool handler executed")
        return "pong"

    return CustomTool(
        name="ping",
        description="A ping tool. When asked to ping, call this tool.",
        input_schema={
            "type": "object",
            "properties": {"reason": {"type": "string", "description": "why"}},
            "required": [],
        },
        handler=ping_handler,
    )


async def run_probe(provider, model, run_dir, prompt, custom_tools):
    handler = DenyAllHandler()
    config = get_harness_config()
    config.provider.provider = provider
    config.provider.model = model
    # Leave allow/deny lists EMPTY so regular tools fall through to can_use_tool.
    # (Listing a tool explicitly would let the Claude SDK shadow it too.)
    config.permissions.allowed_tools = None
    config.permissions.denied_tools = None
    config.permissions.handler_instance = handler
    config.custom_tools.tools = custom_tools or []
    # Codex custom tools only fire when ungated opt-in is on — needed to even
    # reach the "is it gated?" question for probe B.
    config.provider.codex_allow_ungated_custom_tools = True

    api = ChatAPI(config, repo_path=str(run_dir), workflow=None)
    await api.init()
    denied, tool_uses, errors = [], [], []
    try:
        async for event in api.send(prompt):
            if isinstance(event, ToolAccessNotificationEvent):
                if event.action == "denied":
                    denied.append(event.tool_name)
            elif isinstance(event, MessageEvent) and event.kind == "tool_use":
                tool_uses.append(event.content.get("name", "?"))
            elif isinstance(event, ErrorEvent):
                errors.append(event.text)
    finally:
        await api.close()
    return denied, tool_uses, errors, handler


def _verdict(gate_consulted: bool, side_effect_happened: bool) -> str:
    if not gate_consulted and not side_effect_happened:
        return "INCONCLUSIVE (model never attempted the tool)"
    if side_effect_happened:
        return "NOT GATED (tool ran despite deny)"
    return "GATED (blocked)"


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", required=True,
                    choices=["claude", "openharness", "codex"])
    ap.add_argument("--model", default=None)
    ap.add_argument("--base", required=True, help="base scratchpad dir")
    args = ap.parse_args()

    base = Path(args.base) / args.provider
    base.mkdir(parents=True, exist_ok=True)
    print(f"\n{'='*70}\nPROVIDER: {args.provider}  model={args.model or '(default)'}\n{'='*70}")

    # -------- Probe A: regular tool --------
    a_dir = base / "regular"; a_dir.mkdir(exist_ok=True)
    reg_file = a_dir / "regular_out.txt"
    reg_file.unlink(missing_ok=True)
    a_prompt = ("Create a file named regular_out.txt containing exactly the word "
                "hello. Use your file-writing tool. Do it now, no explanation.")
    _, a_uses, a_err, a_h = await run_probe(args.provider, args.model, a_dir, a_prompt, None)
    a_written = reg_file.exists()
    a_verdict = _verdict(bool(a_h.consulted), a_written)
    print(f"\n[A regular tool]  handler consulted={a_h.consulted}")
    print(f"                  regular_out.txt written? {a_written}  tool_use={a_uses}  err={a_err[:1]}")
    print(f"                  => {a_verdict}")

    # -------- Probe B: custom tool --------
    b_dir = base / "custom"; b_dir.mkdir(exist_ok=True)
    marker = b_dir / "custom_ran.marker"
    marker.unlink(missing_ok=True)
    ping = make_ping_tool(marker)
    b_prompt = ("Call the ping tool now. You must invoke the ping tool. "
                "Do not answer in text, just call ping.")
    _, b_uses, b_err, b_h = await run_probe(args.provider, args.model, b_dir, b_prompt, [ping])
    b_ran = marker.exists()
    b_verdict = _verdict(bool(b_h.consulted), b_ran)
    print(f"\n[B custom tool]   handler consulted={b_h.consulted}")
    print(f"                  custom handler ran (marker)? {b_ran}  tool_use={b_uses}  err={b_err[:1]}")
    print(f"                  => {b_verdict}")

    print(f"\nSUMMARY {args.provider}:  regular={a_verdict.split()[0]}  custom={b_verdict.split()[0]}")


if __name__ == "__main__":
    asyncio.run(main())
