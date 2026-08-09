"""C4 — the retry / backoff seam (the highest-value provider-contract gap).

Two halves, deliberately decoupled:

1. **Declaration** — each provider declares ``retry_owner`` (a capability flag):
   who is responsible for retrying a transient transport error.
     * ``"sdk"``    — the underlying SDK already retries (Claude SDK, Codex SDK);
                      the harness must NOT also retry or it double-fires.
     * ``"harness"``— the SDK/transport does not retry (e.g. a bare Ollama HTTP
                      call); the harness owns backoff.
     * ``"none"``   — no retry anywhere (a deliberate fail-fast provider).

2. **Mechanism** — a small, reusable, SDK-free backoff helper the harness (and a
   future Governor, Phase 7) applies ONLY when ``retry_owner == "harness"``.
   Kept a pure seam here so the Governor designs its ``retry`` verdict against a
   declared fact rather than an ``isinstance`` guess, and so retries never stack
   on top of SDK-owned retries.

No wiring into any provider ``send()`` yet — that is the Governor's decision. This
module is the contract + the tool.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

# Re-exported alias (the capability-flag literal lives with the other flags).
from warden.schemas.providers import RetryOwner

T = TypeVar("T")

# Substrings of exception-type names / messages that mark a *transient* transport
# error worth retrying (vs a deterministic 4xx/auth error, which must not retry).
_TRANSIENT_TYPE_MARKERS = (
    "timeout",
    "connectionerror",
    "connectionreseterror",
    "connecterror",
    "readtimeout",
    "remotedisconnected",
    "serviceunavailable",
    "apiconnectionerror",
    "internalservererror",
)
_TRANSIENT_MSG_MARKERS = (
    "timed out",
    "connection reset",
    "connection aborted",
    "temporarily unavailable",
    "503",
    "502",
    "504",
    "429",
    "rate limit",
    "overloaded",
)


@dataclass(frozen=True)
class RetryPolicy:
    """Harness-applied exponential backoff parameters.

    ``delays()`` yields the per-attempt sleep BEFORE each retry (so
    ``max_attempts=3`` → up to 2 sleeps between 3 tries). Capped, deterministic
    (no jitter) so it is trivially testable; a caller that wants jitter can wrap.
    """

    max_attempts: int = 3
    base_delay_s: float = 0.5
    max_delay_s: float = 8.0

    def delays(self) -> list[float]:
        """The sleep before attempts 2..max_attempts (exponential, capped)."""
        out: list[float] = []
        for i in range(self.max_attempts - 1):
            out.append(min(self.base_delay_s * (2 ** i), self.max_delay_s))
        return out


DEFAULT_POLICY = RetryPolicy()


def classify_transient(exc: BaseException) -> bool:
    """True if ``exc`` looks like a *transient* transport error (retry-worthy).

    Heuristic by exception type-name + message (works across httpx / anthropic /
    openai / aiohttp without importing any of them). Deterministic 4xx (auth,
    bad-request) errors return False — retrying them just burns attempts.
    """
    name = type(exc).__name__.lower()
    if any(m in name for m in _TRANSIENT_TYPE_MARKERS):
        return True
    msg = str(exc).lower()
    return any(m in msg for m in _TRANSIENT_MSG_MARKERS)


async def with_backoff(
    fn: Callable[[], Awaitable[T]],
    policy: RetryPolicy = DEFAULT_POLICY,
    *,
    is_transient: Callable[[BaseException], bool] = classify_transient,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    """Call ``fn`` with exponential backoff on *transient* errors.

    Retries only when ``is_transient(exc)`` is True; a non-transient error (or the
    final attempt) re-raises immediately (LAW 4 — never swallowed). ``sleep`` is
    injectable so tests run instantly. Apply ONLY for ``retry_owner == "harness"``
    providers — SDK-owned retries must not be double-wrapped.
    """
    delays = policy.delays()
    last: BaseException | None = None
    for attempt in range(policy.max_attempts):
        try:
            return await fn()
        except BaseException as exc:  # noqa: BLE001 — re-raised below if not transient
            last = exc
            is_last = attempt == policy.max_attempts - 1
            if is_last or not is_transient(exc):
                raise
            await sleep(delays[attempt])
    # Unreachable (the loop either returns or raises), but keeps type-checkers calm.
    assert last is not None
    raise last


__all__ = [
    "RetryOwner",
    "RetryPolicy",
    "DEFAULT_POLICY",
    "classify_transient",
    "with_backoff",
]
