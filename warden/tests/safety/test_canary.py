"""SAFE-4 (rung 3e-3) — canary backstop unit tests.

Covers ``plant_canary`` (token embedding) and ``CanaryOutputMiddleware`` (per-chunk
rolling detection that cuts on a verbatim system-prompt token, passing benign text
through unchanged).
"""

import asyncio
from typing import Any

from warden.safety.middleware.input.canary import (
    DEFAULT_CANARY,
    plant_canary,
)
from warden.safety.middleware.output.middleware import (
    CanaryOutputMiddleware,
)
from warden.seams.middleware import RejectResult, SendContext


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


_CTX = SendContext(workflow=None, session_id="s", provider="claude", model=None)


# ---------------------------------------------------------------------------
# plant_canary
# ---------------------------------------------------------------------------

def test_plant_canary_embeds_token():
    out = plant_canary("You are a helpful assistant.", "TOK_abc123")
    assert "TOK_abc123" in out
    assert out.startswith("You are a helpful assistant.")


def test_plant_canary_none_base_yields_token():
    out = plant_canary(None, DEFAULT_CANARY)
    assert DEFAULT_CANARY in out


# ---------------------------------------------------------------------------
# CanaryOutputMiddleware
# ---------------------------------------------------------------------------

def test_canary_chunk_with_token_rejects():
    mw = CanaryOutputMiddleware("TOK_abc123")
    result = _run(mw.after_receive("leaking prompt: TOK_abc123 here", _CTX))
    assert isinstance(result, RejectResult)
    assert "canary leak" in result.reason


def test_canary_benign_chunk_passes_unchanged():
    mw = CanaryOutputMiddleware("TOK_abc123")
    benign = "the quick brown fox jumps over the lazy dog"
    result = _run(mw.after_receive(benign, _CTX))
    assert result == benign


def test_canary_split_across_chunks_is_caught():
    mw = CanaryOutputMiddleware("TOK_abc123")
    # First half of the token arrives — benign so far, passes through.
    first = _run(mw.after_receive("prefix text TOK_ab", _CTX))
    assert first == "prefix text TOK_ab"
    # Second chunk completes the token across the boundary → CUT.
    second = _run(mw.after_receive("c123 and the rest", _CTX))
    assert isinstance(second, RejectResult)


def test_canary_reset_clears_tail():
    mw = CanaryOutputMiddleware("TOK_abc123")
    # Prime the tail with the token's leading half.
    _run(mw.after_receive("prefix text TOK_ab", _CTX))
    mw.reset()
    # After reset the dangling tail is gone: completing chars alone can't match.
    result = _run(mw.after_receive("c123 and the rest", _CTX))
    assert result == "c123 and the rest"


def test_canary_before_send_is_passthrough():
    mw = CanaryOutputMiddleware("TOK_abc123")
    payload = "an input prompt mentioning TOK_abc123 freely"
    assert _run(mw.before_send(payload, _CTX)) == payload
