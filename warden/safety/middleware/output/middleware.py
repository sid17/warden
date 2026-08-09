"""Seam-conforming output middlewares — guard the model response direction.

Two flavours, both subclassing PassThroughMiddleware (so ``before_send`` stays a
pass-through — they only touch output):

  - StreamingLeakFilterMiddleware — INCREMENTAL. Feeds each chunk into a rolling
    StreamingOutputFilter; on a leak it CUTS the stream mid-flight (RejectResult).
    Holds back the trailing ``buffer_size`` chars until ``flush()`` at turn end.
  - RedactOutputMiddleware — BUFFERED/stateless. Per-chunk, TRANSFORMS sensitive
    content in place (redaction) rather than cutting.

The drain loop (wired in rung 3b-2) drives the streaming filter's per-turn
lifecycle: ``reset()`` at turn start, ``after_receive(chunk, ctx)`` per chunk,
``flush(ctx)`` at end-of-turn.
"""

from __future__ import annotations

from warden.seams.middleware import (
    PassThroughMiddleware,
    RejectResult,
    SendContext,
)

from .filters import StreamingOutputFilter, check_output_for_leaks
from .sanitize import sanitize_output


class StreamingLeakFilterMiddleware(PassThroughMiddleware):
    """Incremental leak filter — cuts the stream mid-flight on a leak.

    Wraps a rolling StreamingOutputFilter. Each ``after_receive`` pushes the
    chunk into the buffer and emits the safe-to-yield prefix (which may be ``""``
    while the buffer is still filling — the trailing ``buffer_size`` chars are
    held back until ``flush``). A detected leak returns RejectResult, cutting the
    stream. The same instance is reused across turns, so ``reset`` MUST be called
    at turn start to clear cross-turn buffer state.
    """

    def __init__(self, buffer_size: int = 200) -> None:
        self._buffer_size = buffer_size
        self._filter = StreamingOutputFilter(buffer_size=buffer_size)

    def reset(self) -> None:
        """Start a FRESH rolling filter — clears held-back cross-turn tail state."""
        self._filter = StreamingOutputFilter(buffer_size=self._buffer_size)

    async def after_receive(
        self, content: str, context: SendContext,
    ) -> str | RejectResult:
        """Push a chunk; emit the safe prefix, or cut mid-flight on a leak."""
        # NOTE (rung 3e): a per-chunk canary check will be added here — plant a
        # canary token upstream and reject the moment it surfaces in output.
        yield_text, is_filtered = self._filter.push(content)
        if is_filtered:
            return RejectResult(reason=yield_text or "output leak detected")
        return yield_text or ""

    async def flush(self, context: SendContext) -> str | RejectResult:
        """Emit the held-back tail at stream end, or reject if it leaks."""
        tail, is_filtered = self._filter.flush()
        if is_filtered:
            return RejectResult(reason=tail or "output leak detected")
        return tail


class RedactOutputMiddleware(PassThroughMiddleware):
    """Per-chunk stateless redaction — transforms sensitive output in place.

    Unlike the streaming filter, this never cuts: it swaps sensitive content for
    a placeholder (``sanitize_output``) or, on a leak-pattern hit, a
    ``[FILTERED: reason]`` marker. No per-turn state, so ``reset``/``flush`` are
    unneeded.
    """

    async def after_receive(
        self, content: str, context: SendContext,
    ) -> str:
        """Redact sensitive content in the chunk; pass benign chunks unchanged."""
        replacement = sanitize_output(content)
        if replacement is not None:
            return replacement
        leak = check_output_for_leaks(content)
        if leak is not None:
            return f"[FILTERED: {leak}]"
        return content


class CanaryOutputMiddleware(PassThroughMiddleware):
    """SAFE-4 canary backstop — cut the stream the moment a planted token surfaces.

    A synthetic canary token is planted verbatim in the system prompt (see
    ``plant_canary``); if the model echoes the prompt back, the token appears in
    output and this middleware CUTS. It is a DETECTOR, not a redactor: benign text
    passes through byte-identical, only a hit rejects.

    ROLLING check: a canary split across chunk boundaries is still caught. A tail
    of the last ``len(token)-1`` chars is retained; each ``after_receive`` searches
    ``tail + content``. ``reset`` clears the tail (per-turn lifecycle, like
    StreamingLeakFilterMiddleware).
    """

    def __init__(self, token: str) -> None:
        self._token = token
        self._tail = ""

    def reset(self) -> None:
        """Clear the cross-chunk tail buffer (per-turn lifecycle hook)."""
        self._tail = ""

    async def after_receive(
        self, content: str, context: SendContext,
    ) -> str | RejectResult:
        """Cut on a canary hit (spanning the chunk boundary); else pass through."""
        window = self._tail + content
        if self._token in window:
            return RejectResult(
                reason="canary leak: verbatim system-prompt token in output"
            )
        # Retain just enough tail to catch a token split across the next boundary.
        keep = len(self._token) - 1
        self._tail = window[-keep:] if keep > 0 else ""
        return content
