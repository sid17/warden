"""Custom tool implementations for experiment presets."""

from __future__ import annotations

from pathlib import Path

from warden.seams.custom_tools import CustomTool


def save_note_handler(content: str, title: str | None = None) -> str:
    """Append a note to .notes/cli-notes.md."""
    notes_dir = Path(".notes")
    notes_dir.mkdir(exist_ok=True)
    notes_file = notes_dir / "cli-notes.md"
    with open(notes_file, "a") as f:
        if title:
            f.write(f"## {title}\n\n")
        f.write(f"{content}\n\n")
    return "Note saved."


SAVE_NOTE_TOOL = CustomTool(
    name="save-note",
    description="Save a note to the notes file",
    input_schema={
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "The note content"},
            "title": {"type": "string", "description": "Optional note title"},
        },
        "required": ["content"],
    },
    handler=save_note_handler,
)


_BLOCKED_READ_PATTERNS = [
    ".claude/",
    ".env",
    "secret",
    "credential",
    "private_key",
    "/.ssh/",
    "/.aws/",
]


def safe_read_handler(file_path: str) -> str:
    """Read a file, blocking sensitive paths."""
    for pattern in _BLOCKED_READ_PATTERNS:
        if pattern in file_path.lower():
            return "This file is not available in this workflow."
    try:
        with open(file_path) as f:
            return f.read()
    except (FileNotFoundError, PermissionError) as e:
        return f"Cannot read file: {e}"


SAFE_READ_TOOL = CustomTool(
    name="safe-read",
    description="Read a file from the workspace",
    input_schema={
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Path to the file to read",
            },
        },
        "required": ["file_path"],
    },
    handler=safe_read_handler,
)
