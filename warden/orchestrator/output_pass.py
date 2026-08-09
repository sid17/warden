"""The per-turn output-middleware pass over the orchestrator's drain loop.

Both drive paths (``ChatAPI.send`` and the Runs API) funnel every event through
the ONE drain loop in ``orchestrator.send_message`` — so wiring the output pass
here makes both inherit it (SAFE-1). This module owns all of that logic so the
orchestrator's loop stays a one-liner (``orchestrator.py`` is at its 500-line
budget).

The pass runs the model-response text through each output middleware's
``after_receive`` (redact / streaming-leak filter). A ``RejectResult`` CUTS the
stream mid-flight (an ``ErrorEvent`` is emitted and the provider task cancelled);
otherwise the possibly-redacted text replaces the event's text. Streaming filters
hold back a trailing buffer, so ``after_receive`` may legitimately return ``""``
(emit nothing) until ``flush`` at end-of-turn releases the tail.

NO-OP INVARIANT (load-bearing): with an EMPTY middleware list this is a
transparent pass-through — every event yielded unchanged, no flush events, no
behavior change. The default config (``enable_output_middleware=False`` → empty
list) therefore alters nothing, and the existing suite stays green.

``reset``/``flush`` are OPTIONAL lifecycle hooks — NOT on the seam Protocol (kept
at two methods). This module getattr-guards them so a middleware may implement
only ``after_receive``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from warden.schemas.events import (
    ErrorEvent,
    MessageEvent,
    OrchestratorEvent,
)
from warden.seams.middleware import RejectResult, SendContext

# Text-bearing MessageEvent kinds the output pass filters. Everything else
# (tool_use, status, thinking, tool_result, …) passes through untouched.
_TEXT_KINDS = ("text", "stream_delta")

# Sentinel returned by OutputPass.process / .flush to signal a mid-flight cut.
_CUT = object()


class OutputPass:
    """Per-turn driver for the output middleware pipeline.

    Constructed once per turn from the output middleware list. ``reset`` clears
    per-turn buffer state, ``process`` filters each event, ``flush`` releases any
    held-back tail at end-of-turn. Empty list ⇒ every method is a no-op.
    """

    def __init__(self, middleware: list) -> None:
        self._middleware = middleware or []

    @property
    def enabled(self) -> bool:
        """True when there is at least one output middleware to run."""
        return bool(self._middleware)

    def reset(self) -> None:
        """Start a fresh per-turn state on every middleware that has ``reset``.

        ``reset`` is an OPTIONAL lifecycle hook (not on the seam Protocol), so it
        is getattr-guarded — a stateless middleware (e.g. redaction) needs none.
        """
        for mw in self._middleware:
            reset = getattr(mw, "reset", None)
            if callable(reset):
                reset()

    async def process(
        self, event: OrchestratorEvent, ctx: SendContext,
    ) -> tuple[Any, OrchestratorEvent | None]:
        """Run one event through the output pass.

        Returns ``(marker, event_or_None)``:
          - ``(_CUT, error_event)`` — a middleware rejected: cut the stream and
            emit ``error_event``.
          - ``(None, event)`` — emit ``event`` (the original, or a copy with the
            replaced text).
          - ``(None, None)`` — emit nothing (streaming buffer still filling).

        Non-text events (and empty-text text events) pass through unchanged.
        """
        if not self._middleware or not isinstance(event, MessageEvent):
            return None, event
        if event.kind not in _TEXT_KINDS:
            return None, event
        text = event.content.get("text")
        if not text:
            return None, event

        for mw in self._middleware:
            result = await mw.after_receive(text, ctx)
            if isinstance(result, RejectResult):
                return _CUT, ErrorEvent(
                    text=f"[output blocked] {result.reason}",
                    session_id=event.session_id,
                )
            text = result

        if text == "":
            # Streaming filter is still buffering — emit nothing this chunk.
            return None, None

        # Copy-on-write: the content dict may be shared; never mutate in place.
        new_content = {**event.content, "text": text}
        return None, MessageEvent(
            kind=event.kind, content=new_content, session_id=event.session_id,
        )

    async def flush(
        self, ctx: SendContext,
    ) -> tuple[Any, list[OrchestratorEvent]]:
        """Release each middleware's held-back tail at end-of-turn.

        Returns ``(marker, events)``: ``(_CUT, [error_event])`` if a flush
        rejected, else ``(None, events)`` — one MessageEvent per non-empty tail
        (usually zero or one). ``flush`` is an OPTIONAL hook, getattr-guarded.
        """
        out: list[OrchestratorEvent] = []
        if not self._middleware:
            return None, out
        for mw in self._middleware:
            flush = getattr(mw, "flush", None)
            if not callable(flush):
                continue
            tail = await flush(ctx)
            if isinstance(tail, RejectResult):
                return _CUT, [ErrorEvent(
                    text=f"[output blocked] {tail.reason}",
                    session_id=ctx.session_id or "",
                )]
            if tail:
                out.append(MessageEvent(
                    kind="text",
                    content={"text": tail},
                    session_id=ctx.session_id or "",
                ))
        return None, out


async def drain_with_output_pass(
    queue: Any,
    middleware: list,
    orch: Any,
) -> AsyncIterator[OrchestratorEvent]:
    """Drain the orchestrator's event queue, applying the output pass per event.

    Replaces the orchestrator's bare ``while True: queue.get()`` loop. With an
    EMPTY middleware list this is byte-identical to that loop (no reset, no flush,
    every event yielded unchanged — the NO-OP INVARIANT).

    ``orch`` is the live :class:`Orchestrator`; the per-turn ``SendContext`` and
    the cut callback (which cancels its provider stream task) are derived from it
    here so the orchestrator's own loop stays a one-liner. On a mid-flight cut the
    ``[output blocked]`` ErrorEvent is yielded, the stream task is cancelled, and
    draining stops.
    """
    pass_ = OutputPass(middleware)

    # Fast path: no output middleware ⇒ the exact original drain loop. Skip all
    # context/callback setup so the no-op path is provably behavior-preserving.
    if not pass_.enabled:
        while True:
            event = await queue.get()
            if event is None:
                break
            yield event
        return

    ctx = SendContext(
        workflow=orch._workflow_name,
        session_id=orch._current_session_id,
        provider=orch._current_provider,
        model=orch._current_model,
    )

    def _on_cut() -> None:
        task = orch._stream_task
        if task is not None and not task.done():
            task.cancel()

    pass_.reset()
    while True:
        event = await queue.get()
        if event is None:
            break
        marker, out = await pass_.process(event, ctx)
        if marker is _CUT:
            yield out
            _on_cut()
            return
        if out is not None:
            yield out

    # End-of-turn: release any held-back tail (streaming filter).
    marker, events = await pass_.flush(ctx)
    for ev in events:
        yield ev
    if marker is _CUT:
        _on_cut()
