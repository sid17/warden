"""Output sanitization — pattern-based sensitive content replacement."""

from __future__ import annotations

import re

_SANITIZE_PATTERNS = [
    r'\.claude/',
    r'SKILL\.md',
    r'/Users/\w+/',
    r'---\s*\nname:',
    r'when_to_use:',
    r'domain:\s+\w+',
]


def sanitize_output(text: str) -> str | None:
    """Check if text contains sensitive tool output. Returns replacement if sensitive, None if safe."""
    for pattern in _SANITIZE_PATTERNS:
        if re.search(pattern, text):
            return "[Content not available in this workflow]"
    return None
