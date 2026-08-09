"""Egress adapters — where a run's typed events are delivered.

One seam (:class:`EgressAdapter`), two adapters chosen per run by the ``sink``:

- :class:`WebhookEgress` — POST each event to the product backend (push; the
  product persists on receive, so it is naturally durable). Used for checkpoints.
- :class:`SseEgress` — buffer events for a ``GET /runs/{id}/events`` hold-open
  (the product's backend relays + taps to its frontend). Used for token streams.

Every event already carries a per-run ``seq`` (assigned by the runner), so a
durable Redis stream + ``Last-Event-ID`` reconnect can slot in later unchanged.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

import httpx

from warden.harness_api.schemas import Event

logger = logging.getLogger(__name__)

# Terminal event types close an SSE stream.
_TERMINAL = {"result", "error"}


class EgressAdapter:
    """Delivery seam: ``emit`` one event, ``aclose`` when the run ends."""

    async def emit(self, event: Event) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    async def aclose(self) -> None:  # pragma: no cover - interface
        pass


class WebhookEgress(EgressAdapter):
    """POST each event as JSON to the product's URL.

    A delivery failure is retried a few times then **logged and counted**
    (``delivery_failures``) — never silently swallowed (LAW 4) — but it does not
    abort the run: the run's work is done regardless of whether the product's
    receiver was reachable, and the product owns durable redelivery.
    """

    def __init__(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        *,
        client: httpx.AsyncClient | None = None,
        max_attempts: int = 3,
    ) -> None:
        if not url:
            raise ValueError("webhook sink requires a url")
        self._url = url
        self._headers = headers or {}
        self._client = client or httpx.AsyncClient(timeout=10.0)
        self._owns_client = client is None
        self._max_attempts = max_attempts
        self.delivery_failures = 0

    async def emit(self, event: Event) -> None:
        payload = event.model_dump()
        last_exc: Exception | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                resp = await self._client.post(
                    self._url, json=payload, headers=self._headers
                )
                if resp.status_code >= 400:
                    raise httpx.HTTPStatusError(
                        f"webhook returned {resp.status_code}",
                        request=resp.request,
                        response=resp,
                    )
                return
            except Exception as exc:  # noqa: BLE001 - retried + surfaced below
                last_exc = exc
                if attempt < self._max_attempts:
                    await asyncio.sleep(0.2 * attempt)
        self.delivery_failures += 1
        logger.error(
            "webhook delivery failed for run=%s seq=%s after %d attempts: %s",
            event.run_id, event.seq, self._max_attempts, last_exc,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


class SseEgress(EgressAdapter):
    """Buffer events for a ``GET /runs/{id}/events`` hold-open.

    ``emit`` enqueues; :meth:`stream` yields buffered + live events in order and
    ends after the terminal (``result``/``error``) event. The buffer means a
    late-joining consumer still sees the whole stream from the first event.
    """

    def __init__(self) -> None:
        self._queue: asyncio.Queue[Event | None] = asyncio.Queue()
        self._closed = False

    async def emit(self, event: Event) -> None:
        await self._queue.put(event)
        if event.type in _TERMINAL:
            self._closed = True
            await self._queue.put(None)  # sentinel: end of stream

    async def stream(self) -> AsyncIterator[Event]:
        """Yield events until the terminal event closes the stream."""
        while True:
            event = await self._queue.get()
            if event is None:
                break
            yield event
            if event.type in _TERMINAL:
                break

    async def aclose(self) -> None:
        if not self._closed:
            await self._queue.put(None)
