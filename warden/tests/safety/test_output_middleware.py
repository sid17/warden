"""Tests for output-guarding middleware (M4 safety, rung 3b-1).

Covers the two seam-conforming output middlewares:
  - StreamingLeakFilterMiddleware — incremental mid-flight cut + held-back tail
  - RedactOutputMiddleware — per-chunk stateless redaction
"""

import asyncio
from typing import Any

from warden.safety.middleware.output.middleware import (
    RedactOutputMiddleware,
    StreamingLeakFilterMiddleware,
)
from warden.seams.middleware import (
    RejectResult,
    SendContext,
)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


_CTX = SendContext(
    workflow=None,
    session_id="s",
    provider="claude",
    model=None,
)


# ---------------------------------------------------------------------------
# StreamingLeakFilterMiddleware — held-back tail, nothing lost
# ---------------------------------------------------------------------------

def test_streaming_benign_nothing_lost():
    async def _test():
        mw = StreamingLeakFilterMiddleware(buffer_size=50)
        mw.reset()
        # Sum of chunks well past buffer_size so the buffer fills and emits.
        chunks = ["hello world " * 10, "more benign text " * 5, "tail bit"]
        full_input = "".join(chunks)

        emitted = ""
        for chunk in chunks:
            result = await mw.after_receive(chunk, _CTX)
            assert not isinstance(result, RejectResult)
            emitted += result

        tail = await mw.flush(_CTX)
        assert not isinstance(tail, RejectResult)
        emitted += tail

        # Incremental + flush must reconstruct the exact benign input.
        assert emitted == full_input

    _run(_test())


# ---------------------------------------------------------------------------
# StreamingLeakFilterMiddleware — mid-flight cut on a split leak
# ---------------------------------------------------------------------------

def test_streaming_leak_cuts_mid_flight():
    async def _test():
        mw = StreamingLeakFilterMiddleware(buffer_size=50)
        mw.reset()
        # Leak split across chunks; the absolute /Users/ path is a detected leak.
        # Pad first so the buffer crosses the threshold once the leak lands.
        chunks = [
            "x" * 40,
            "some path ",
            "/Users/",
            "alice/secret",
        ]

        got_reject = False
        for chunk in chunks:
            result = await mw.after_receive(chunk, _CTX)
            if isinstance(result, RejectResult):
                got_reject = True
                assert result.reason
                break

        assert got_reject, "expected a mid-flight RejectResult on the leak"

    _run(_test())


# ---------------------------------------------------------------------------
# StreamingLeakFilterMiddleware — reset() clears cross-turn state
# ---------------------------------------------------------------------------

def test_streaming_reset_clears_state():
    async def _test():
        mw = StreamingLeakFilterMiddleware(buffer_size=50)
        mw.reset()
        # Poison the buffer with a leak.
        await mw.after_receive("y" * 40 + " /Users/bob/ leaked", _CTX)

        # Fresh turn: reset then feed benign input — must pass clean.
        mw.reset()
        benign = "clean benign text " * 10
        result = await mw.after_receive(benign, _CTX)
        assert not isinstance(result, RejectResult)
        tail = await mw.flush(_CTX)
        assert not isinstance(tail, RejectResult)
        assert (result + tail) == benign

    _run(_test())


# ---------------------------------------------------------------------------
# RedactOutputMiddleware — per-chunk stateless redaction
# ---------------------------------------------------------------------------

def test_redact_replaces_sensitive_chunk():
    async def _test():
        mw = RedactOutputMiddleware()
        result = await mw.after_receive("see /Users/alice/ for the file", _CTX)
        assert result == "[Content not available in this workflow]"

    _run(_test())


def test_redact_passes_benign_chunk():
    async def _test():
        mw = RedactOutputMiddleware()
        result = await mw.after_receive("just a benign response", _CTX)
        assert result == "just a benign response"

    _run(_test())


def test_redact_leak_becomes_marker():
    async def _test():
        mw = RedactOutputMiddleware()
        # 3+ skill names trigger check_output_for_leaks but not sanitize_output.
        text = "use kickoff, grilling and spec skills to proceed"
        result = await mw.after_receive(text, _CTX)
        assert result.startswith("[FILTERED:")
        assert not isinstance(result, RejectResult)

    _run(_test())


# ---------------------------------------------------------------------------
# Both are valid Middleware — inherited before_send is pass-through
# ---------------------------------------------------------------------------

def test_both_are_valid_middleware_passthrough_input():
    async def _test():
        for mw in (StreamingLeakFilterMiddleware(), RedactOutputMiddleware()):
            # Conform structurally to the Middleware seam (both directions present).
            assert hasattr(mw, "before_send")
            assert hasattr(mw, "after_receive")
            # Inherited before_send is a pass-through (output-only middlewares).
            assert await mw.before_send("prompt in", _CTX) == "prompt in"

    _run(_test())
