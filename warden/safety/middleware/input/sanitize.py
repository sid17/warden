"""Input sanitization middleware — prompt injection detection."""

from __future__ import annotations

from warden.seams.middleware import RejectResult, SendContext

_BLOCKED_PATTERNS = [
    "ignore instructions",
    "ignore all previous",
    "disregard",
    "forget your instructions",
]


class SanitizeMiddleware:
    """Middleware that rejects messages containing prompt-injection patterns."""

    async def before_send(
        self, content: str, context: SendContext,
    ) -> str | RejectResult:
        lower = content.lower()
        for pattern in _BLOCKED_PATTERNS:
            if pattern in lower:
                return RejectResult(reason="Prompt injection detected")
        return content


_E3_BLOCKED_PATTERNS = _BLOCKED_PATTERNS + [
    "override your",
    "prior directives",
    "new instructions",
    "you are now",
    "act as",
    "pretend you",
    "jailbreak",
    "unrestricted mode",
    "developer mode",
    "ignore previous",
    "bypass",
]

_SECRET_SEEKING_PATTERNS = [
    "password",
    "secret",
    "api_key",
    "api key",
    "private key",
    "credential",
    "access token",
]


class E3ExpandedMiddleware:
    """Expanded middleware for E3 — broader injection + secret-seeking detection."""

    async def before_send(
        self, content: str, context: SendContext,
    ) -> str | RejectResult:
        lower = content.lower()
        for pattern in _E3_BLOCKED_PATTERNS:
            if pattern in lower:
                return RejectResult(reason=f"Blocked: matched pattern '{pattern}'")
        for pattern in _SECRET_SEEKING_PATTERNS:
            if pattern in lower:
                return RejectResult(reason=f"Blocked: secret-seeking pattern '{pattern}'")
        return content
