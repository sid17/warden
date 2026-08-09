"""Governor-seam surface for the orchestrator turn loop (M2 / B17).

The thin binding between the engine's turn loop and the optional Governor seam
(``seams/governor.py``). Kept out of ``orchestrator.py`` so the loop stays lean
(the 500-line law) and the seam logic lives in one place — the analogue of
``permission_surface.py`` for permissions.

GOV-2: with no Governor wired, every checkpoint is a no-op (``None`` ⇒ continue).
GOV-1: only :class:`Usage` (tokens) + ``elapsed_s`` (seconds) reach the seam.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from warden.schemas.events import MessageEvent, StoppedEvent
from warden.schemas.usage import Usage, normalize_usage
from warden.seams.governor import Checkpoint, Governor, Stop


async def checkpoint_stop(
    governor: Governor | None,
    checkpoint: Checkpoint,
    usage: Usage,
    elapsed_s: float,
    session_id: str | None,
) -> StoppedEvent | None:
    """Evaluate the Governor at ``checkpoint``; return a :class:`StoppedEvent`
    when the verdict is ``stop``, else ``None`` (keep running).

    ``None`` covers both "no Governor wired" (GOV-2) and a ``continue`` verdict —
    the loop just checks for a truthy return, then emits the event and halts. The
    verdict-to-event mapping lives here so the turn loop stays a one-liner.
    """
    if governor is None:
        return None
    verdict = await governor.check(checkpoint, usage, elapsed_s)
    if not isinstance(verdict, Stop):
        return None
    return StoppedEvent(reason=verdict.reason, session_id=session_id or "")


def usage_from_status(content: dict) -> Usage | None:
    """Extract normalized :class:`Usage` from a provider ``status``/``result``
    message's content, or ``None`` when the message carries no usage.

    Providers surface a per-turn ``usage`` dict on their terminal status message
    (Claude ``ResultMessage`` → ``{"input_tokens", "output_tokens", ...}``);
    :func:`normalize_usage` collapses any provider's shape into one struct so the
    turn-boundary check never branches on provider.
    """
    raw = content.get("usage")
    if not raw:
        return None
    return normalize_usage(raw)


async def process_provider_message(
    sdk_msg: Any,
    *,
    message_handler: Any,
    tag_sid: str,
    queue: Any,
    governor: Governor | None,
    run_started: float,
    sid: str | None,
) -> StoppedEvent | None:
    """Transform one provider message, forward its chat events, and run the
    per-message Governor checkpoints. Returns a :class:`StoppedEvent` to halt the
    turn loop, else ``None``.

    Two checkpoints land here (design §7): ``mid_stream`` on a Claude
    ``usage_delta`` frame (the intra-turn tripwire — the frame is an internal
    cost signal, consumed here, never forwarded as a chat message) and
    ``turn_boundary`` once the terminal ``status`` message's usage is known.
    """
    turn_usage: Usage | None = None
    for ws_msg in message_handler(sdk_msg, tag_sid):
        kind = ws_msg.pop("kind", "text")
        ws_msg.pop("id", None)
        ws_msg.pop("timestamp", None)
        msg_sid = ws_msg.pop("sessionId", tag_sid)
        if kind == "usage_delta":
            mid = await checkpoint_stop(
                governor, "mid_stream", usage_from_status(ws_msg) or Usage(),
                time.monotonic() - run_started, sid,
            )
            if mid is not None:
                return mid
            continue  # internal cost signal — not a chat message
        if kind == "status":
            turn_usage = usage_from_status(ws_msg) or turn_usage
        await queue.put(MessageEvent(
            kind=kind, content=ws_msg, session_id=msg_sid,
        ))

    if turn_usage is not None:
        return await checkpoint_stop(
            governor, "turn_boundary", turn_usage,
            time.monotonic() - run_started, sid,
        )
    return None


class ClockWatchdog:
    """The wall-clock time bound (B18) — the ``clock_tick`` checkpoint site.

    While a turn streams, ticks every ``interval_s`` and runs the Governor's
    ``clock_tick`` checkpoint (time only). On a stop verdict it records the
    :class:`StoppedEvent` in :attr:`pending` and cooperatively interrupts the
    session (``session.stop()``); the turn loop then emits ``pending`` in place of
    a completion. The engine never learns the deadline (GOV-1) — it only ticks and
    obeys; the Governor holds the cutoff.

    Periodic ticking (not an exact sleep-to-deadline) is deliberate: the engine
    cannot compute the cutoff without seeing the number. A future Governor may
    return a next-check hint to widen the interval.
    """

    def __init__(
        self,
        governor: Governor | None,
        session: Any,
        started_monotonic: float,
        interval_s: float,
    ) -> None:
        self._governor = governor
        self._session = session
        self._started = started_monotonic
        self._interval = interval_s
        self.pending: StoppedEvent | None = None
        self._task: asyncio.Task | None = None

    def start(self) -> "ClockWatchdog":
        """Arm the watchdog (no-op when no Governor is wired — GOV-2)."""
        if self._governor is not None:
            self._task = asyncio.create_task(self._run())
        return self

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            sid = getattr(self._session, "session_id", None)
            ev = await checkpoint_stop(
                self._governor, "clock_tick", Usage(),
                time.monotonic() - self._started, sid,
            )
            if ev is not None:
                self.pending = ev
                await self._session.stop()
                return

    async def aclose(self) -> None:
        """Cancel the timer (idempotent) — call in the turn loop's finally."""
        if self._task is not None and not self._task.done():
            self._task.cancel()


__all__ = [
    "checkpoint_stop",
    "usage_from_status",
    "process_provider_message",
    "ClockWatchdog",
]
