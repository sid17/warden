"""Tests for CustomTool dataclass."""

from warden.seams.custom_tools import CustomTool


def test_custom_tool_construction():
    def handler(chapter: str) -> str:
        return f"Chapter {chapter}"

    tool = CustomTool(
        name="read-chapter",
        description="Read course chapter content",
        input_schema={
            "type": "object",
            "properties": {"chapter": {"type": "string"}},
            "required": ["chapter"],
        },
        handler=handler,
    )
    assert tool.name == "read-chapter"
    assert tool.description == "Read course chapter content"
    assert tool.input_schema["type"] == "object"
    assert tool.handler("1") == "Chapter 1"
