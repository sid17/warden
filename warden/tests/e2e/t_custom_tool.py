"""TOOL-1/2 — Claude SDK custom-tool consumption e2e.

The open question this answers, end-to-end, inside the Docker bed: does the
Claude SDK actually REACH a harness-registered custom tool and execute its
handler for a real turn, or is registration a silent no-op?

A trivial ``SAVE_NOTE_TOOL`` is registered whose handler writes a marker file.
We run ONE real Claude turn with a prompt that forces the tool, then assert the
side-effect file exists with the expected content. Because the custom tool is
delivered as an in-proc SDK-MCP server (``mcp__harness_custom__save_note``) and
its FQMN is added to ``allowed_tools``, a written marker proves the SDK invoked
the handler (TOOL-1 consume, TOOL-2 in_proc_list delivery).

Run inside the Docker bed with a real Claude credential:
    python -m warden.tests.e2e.t_custom_tool

Exit 0 = PASS (custom tool handler executed). Non-zero = FAIL / no-op.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from warden.providers.claude.session import ClaudeSession
from warden.seams.custom_tools import CustomTool

RUN_DIR = Path("/tmp/t_custom_tool")
MARKER = RUN_DIR / "save_note_marker.txt"
MARKER_TEXT = "custom-tool-fired"


def _save_note(text: str = "") -> str:
    """Handler for SAVE_NOTE_TOOL — writes the note text to the marker file."""
    MARKER.parent.mkdir(parents=True, exist_ok=True)
    MARKER.write_text(f"{MARKER_TEXT}:{text}")
    return f"saved: {text}"


SAVE_NOTE_TOOL = CustomTool(
    name="save_note",
    description="Save a short note to the user's notebook. Always use this to save notes.",
    input_schema={
        "type": "object",
        "properties": {"text": {"type": "string", "description": "the note text"}},
        "required": ["text"],
    },
    handler=_save_note,
)


async def main() -> int:
    print("=" * 66)
    print(" TOOL-1/2 — Claude SDK custom-tool consumption")
    print("=" * 66)

    RUN_DIR.mkdir(parents=True, exist_ok=True)
    if MARKER.exists():
        MARKER.unlink()

    sess = ClaudeSession(repo_path=RUN_DIR, custom_tools=[SAVE_NOTE_TOOL])

    # Structural precondition: the tool was consumed (stored), not dropped.
    consumed = SAVE_NOTE_TOOL in sess._custom_tools
    print(f"  custom tool consumed (stored): {consumed}")
    if not consumed:
        print(" TOOL RESULT: FAIL — custom tool was NOT consumed (silent drop).")
        return 1

    await sess.start()
    prompt = (
        "Use the save_note tool to save a note with the text 'hello from t_custom_tool'. "
        "Call the tool now — do not just describe it."
    )
    try:
        async for msg in sess.send(prompt):
            _ = msg  # drain the stream; the side effect is the assertion
    finally:
        await sess.close()

    fired = MARKER.exists()
    content_ok = fired and MARKER.read_text().startswith(MARKER_TEXT)
    print(f"  marker file written : {fired}  (want True)")
    if fired:
        print(f"  marker content      : {MARKER.read_text()[:120]!r}")

    print()
    print("=" * 66)
    if fired and content_ok:
        print(" TOOL RESULT: PASS — the SDK invoked the custom-tool handler")
        print(" (marker written). TOOL-1 consume + TOOL-2 in_proc_list delivery.")
        print("=" * 66)
        return 0

    print(" TOOL RESULT: FAIL")
    if not fired:
        print("   - handler NEVER ran → SDK did not reach the custom tool"
              " (silent no-op or the model declined to call it; retune prompt).")
    elif not content_ok:
        print("   - marker written but content unexpected.")
    print("=" * 66)
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
