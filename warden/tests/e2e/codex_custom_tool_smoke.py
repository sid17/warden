"""Codex SDK — ungated custom-tool via in-proc MCP (real turn).

Proves the REAL CodexSdkSession delivers a custom tool to headless codex through
an in-process streamable-HTTP MCP server and that the model actually CALLS the
in-proc handler:

  - build a CodexSdkSession with allow_ungated_custom_tools=True and ONE custom
    tool whose handler WRITES A MARKER FILE,
  - run a real turn that forces the tool,
  - assert the marker exists (handler ran via MCP → ungated delivery works).

This is the ungated path: custom tools ride the MCP elicitation approval, which
the adapter auto-accepts ({action:accept}) BECAUSE opted-in. exec/patch gating is
untouched (still fail-closed) — not exercised here.

    python -m warden.tests.e2e.codex_custom_tool_smoke

Exit 0 = PASS (marker written by the in-proc handler through codex). Non-zero =
FAIL (codex never reached / called the in-proc tool).
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from warden.providers.codex.sdk_session import CodexSdkSession
from warden.seams.custom_tools import CustomTool

MARKER = Path("/tmp/codex_custom_tool_smoke/marker.txt")


async def main() -> int:
    MARKER.parent.mkdir(parents=True, exist_ok=True)
    if MARKER.exists():
        MARKER.unlink()

    calls = {"n": 0}

    def write_marker(text: str) -> str:
        calls["n"] += 1
        MARKER.write_text(f"MARK:{text}", encoding="utf-8")
        print(f"[custom-tool][handler] write_marker(text={text!r})", flush=True)
        return f"wrote marker with text={text}"

    tool = CustomTool(
        name="write_marker",
        description="Write a marker file with the given text. Call this exactly once.",
        input_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        handler=write_marker,
    )

    codex_home = os.environ.get("CODEX_HOME")
    api_key = os.environ.get("OPENAI_API_KEY")

    # A fail-closed can_use_tool: proves exec/patch stays gated even here. Custom
    # tools do NOT flow through this (ungated MCP path).
    async def deny_all(tool_name, tool_input, context):
        class _Deny:
            behavior = "deny"
            message = "denied"
        print(f"[custom-tool][exec-callback] {tool_name} → deny", flush=True)
        return _Deny()

    session = CodexSdkSession(
        repo_path=MARKER.parent,
        can_use_tool=deny_all,
        custom_tools=[tool],
        allow_ungated_custom_tools=True,
        codex_home=Path(codex_home) if codex_home else None,
        auth_env={"OPENAI_API_KEY": api_key} if api_key else None,
    )

    prompt = (
        "You have an MCP tool named 'write_marker' from server 'harness_custom'. "
        "Call the write_marker tool with text='hello-from-e2e' RIGHT NOW. "
        "Do not run any shell commands — just call the write_marker tool once."
    )

    print("=" * 66)
    print(" Codex SDK — ungated custom-tool via in-proc MCP (real turn)")
    print("=" * 66)
    try:
        await session.start()
        print(f"[custom-tool] mcp url = {session._mcp_url}", flush=True)
        async for ev in session.send(prompt):
            kind = ev.get("kind") if isinstance(ev, dict) else None
            if kind == "text":
                t = ev.get("text", "").strip()
                if t:
                    print(f"[custom-tool][text] {t[:160]}", flush=True)
            elif kind == "error":
                print(f"[custom-tool][ERROR] {ev.get('text')}", flush=True)
    finally:
        await session.close()

    written = MARKER.exists()
    print("\n--- results ---")
    print(f"  handler fired : {calls['n']} time(s)")
    print(f"  marker written: {written}  (want True)")
    if written:
        print(f"  marker content: {MARKER.read_text()}")

    print("\n" + "=" * 66)
    if written and calls["n"] > 0:
        print(" CODEX CUSTOM-TOOL RESULT: PASS — codex called the in-proc MCP")
        print(" custom-tool handler (ungated delivery works).")
        print("=" * 66)
        return 0
    print(" CODEX CUSTOM-TOOL RESULT: FAIL — marker not written (codex did not")
    print(" reach/call the in-proc tool). Check mcp_servers config injection.")
    print("=" * 66)
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
