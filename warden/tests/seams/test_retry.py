"""C4 — retry/backoff seam: policy math, transient classification, with_backoff.

Hermetic; ``sleep`` is injected so no real time passes. Also asserts each
finalized provider DECLARES a ``retry_owner`` so the Governor can dispatch on it.
"""

from __future__ import annotations

import asyncio

import pytest

from warden.seams.retry import (
    DEFAULT_POLICY,
    RetryPolicy,
    classify_transient,
    with_backoff,
)


# --- policy math -----------------------------------------------------------

def test_delays_exponential_and_capped() -> None:
    p = RetryPolicy(max_attempts=5, base_delay_s=1.0, max_delay_s=4.0)
    # before attempts 2..5 → 4 sleeps: 1, 2, 4, (8→capped 4)
    assert p.delays() == [1.0, 2.0, 4.0, 4.0]


def test_single_attempt_has_no_delays() -> None:
    assert RetryPolicy(max_attempts=1).delays() == []


# --- transient classification ---------------------------------------------

@pytest.mark.parametrize(
    "exc",
    [
        TimeoutError("read timed out"),
        ConnectionResetError("connection reset by peer"),
        RuntimeError("503 Service Unavailable"),
        RuntimeError("model is overloaded, try again"),
        RuntimeError("429 rate limit exceeded"),
    ],
)
def test_transient_errors_detected(exc: BaseException) -> None:
    assert classify_transient(exc) is True


@pytest.mark.parametrize(
    "exc",
    [
        ValueError("invalid argument"),
        PermissionError("401 unauthorized"),
        RuntimeError("400 bad request: malformed prompt"),
    ],
)
def test_deterministic_errors_not_retried(exc: BaseException) -> None:
    assert classify_transient(exc) is False


# --- with_backoff ----------------------------------------------------------

def test_retries_transient_then_succeeds() -> None:
    async def _run() -> None:
        slept: list[float] = []

        async def _sleep(d: float) -> None:
            slept.append(d)

        calls = {"n": 0}

        async def _fn() -> str:
            calls["n"] += 1
            if calls["n"] < 3:
                raise TimeoutError("timed out")
            return "ok"

        out = await with_backoff(
            _fn, RetryPolicy(max_attempts=3, base_delay_s=0.1), sleep=_sleep
        )
        assert out == "ok"
        assert calls["n"] == 3
        assert slept == [0.1, 0.2]  # slept before attempts 2 and 3

    asyncio.run(_run())


def test_non_transient_raises_immediately_no_sleep() -> None:
    async def _run() -> None:
        slept: list[float] = []

        async def _sleep(d: float) -> None:
            slept.append(d)

        calls = {"n": 0}

        async def _fn() -> str:
            calls["n"] += 1
            raise ValueError("bad request")

        with pytest.raises(ValueError):
            await with_backoff(_fn, DEFAULT_POLICY, sleep=_sleep)
        assert calls["n"] == 1  # not retried
        assert slept == []

    asyncio.run(_run())


def test_exhausts_attempts_then_reraises_last() -> None:
    async def _run() -> None:
        async def _sleep(_d: float) -> None:
            return None

        calls = {"n": 0}

        async def _fn() -> str:
            calls["n"] += 1
            raise TimeoutError(f"attempt {calls['n']}")

        with pytest.raises(TimeoutError, match="attempt 3"):
            await with_backoff(
                _fn, RetryPolicy(max_attempts=3), sleep=_sleep
            )
        assert calls["n"] == 3

    asyncio.run(_run())


# --- provider declarations -------------------------------------------------

def test_finalized_providers_declare_retry_owner() -> None:
    from warden.providers.claude.session import ClaudeSession
    from warden.providers.codex.sdk_session import CodexSdkSession
    from warden.providers.openharness.session import OpenHarnessSession

    assert ClaudeSession.retry_owner == "sdk"
    assert CodexSdkSession.retry_owner == "sdk"
    # Ollama/LiteLLM has no SDK-owned retry → the harness owns backoff.
    assert OpenHarnessSession.retry_owner == "harness"


def test_c6_max_output_tokens_declared() -> None:
    """C6: harness_driven Ollama exposes its enforced window; native providers None."""
    from warden.providers.claude.session import ClaudeSession
    from warden.providers.codex.sdk_session import CodexSdkSession
    from warden.providers.openharness.session import OpenHarnessSession

    assert ClaudeSession.max_output_tokens is None
    assert CodexSdkSession.max_output_tokens is None
    # The declared capability shares one source with the enforced QueryEngine cap.
    assert OpenHarnessSession.max_output_tokens == OpenHarnessSession._MAX_OUTPUT_TOKENS
    assert isinstance(OpenHarnessSession.max_output_tokens, int)
