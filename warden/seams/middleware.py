"""Middleware pipeline — intercept messages before they reach the provider."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class SendContext:
    """Context available to middleware."""

    workflow: str | None
    session_id: str | None
    provider: str
    model: str | None


@dataclass
class RejectResult:
    """Middleware rejected the message."""

    reason: str


class Middleware(Protocol):
    """Intercept messages in both directions around the provider.

    Middleware runs in list order. Each sees the output of the previous one.
    ``before_send`` guards the INPUT (prompt) direction; ``after_receive`` guards
    the OUTPUT (model response) direction. Either method returns the (possibly
    modified) string to continue, or RejectResult to abort the pipeline.

    A middleware may implement only the direction it cares about: ``after_receive``
    carries a pass-through default, so an input-only middleware leaves output
    untouched (and vice-versa — see PassThroughMiddleware).
    """

    async def before_send(
        self, content: str, context: SendContext,
    ) -> str | RejectResult:
        """Return (possibly modified) content to proceed, or RejectResult to block."""
        ...

    async def after_receive(
        self, content: str, context: SendContext,
    ) -> str | RejectResult:
        """Guard the model response: return (possibly redacted) content to proceed,
        or RejectResult to cut it. Defaults to pass-through so a middleware that only
        cares about the input direction inherits an untouched output.
        """
        return content


class PassThroughMiddleware:
    """A no-op middleware transparent in both directions.

    Subclass it and override only the direction you care about; the other stays
    a pass-through.
    """

    async def before_send(
        self, content: str, context: SendContext,
    ) -> str | RejectResult:
        """Return content unchanged."""
        return content

    async def after_receive(
        self, content: str, context: SendContext,
    ) -> str | RejectResult:
        """Return content unchanged."""
        return content
