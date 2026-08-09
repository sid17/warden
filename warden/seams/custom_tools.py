"""CustomTool — user-defined tool registered with the provider."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class CustomTool:
    """A custom tool registered with the provider.

    The handler is called with keyword arguments matching the input_schema.
    It should return a string result.
    """

    name: str
    description: str
    input_schema: dict
    handler: Callable[..., Any]
