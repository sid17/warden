"""Tests for CascadeMiddleware — ordered cheapest→heaviest classifier stages.

Uses STUB classifiers (no torch/network/Ollama). A stub returns a fixed
ClassifyResult and records whether it was called, so short-circuiting is
directly observable.
"""

import asyncio
from typing import Any

from warden.safety.middleware.classifiers import ClassifyResult
from warden.safety.middleware.input.cascade import (
    CascadeMiddleware,
    CascadeStage,
)
from warden.seams.middleware import RejectResult, SendContext


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


_CTX = SendContext(
    workflow="study",
    session_id="abc",
    provider="claude",
    model="sonnet",
)


class _StubClassifier:
    """Fixed-verdict classifier that records whether it was invoked."""

    def __init__(self, label: str, score: float, name: str = "stub") -> None:
        self.name = name
        self._label = label
        self._score = score
        self.called = False

    async def classify(self, text: str) -> ClassifyResult:
        self.called = True
        return ClassifyResult(
            label=self._label,
            score=self._score,
            latency_ms=0.0,
            classifier=self.name,
        )


# ---------------------------------------------------------------------------
# block-stop short-circuits: any stage may block; later stages never run
# ---------------------------------------------------------------------------

def test_block_stop_short_circuits():
    async def _test():
        stage1 = _StubClassifier("unsafe", 0.9, name="cheap")
        stage2 = _StubClassifier("safe", 1.0, name="heavy")
        mw = CascadeMiddleware([
            CascadeStage(stage1, allow_authority=False),
            CascadeStage(stage2, allow_authority=True),
        ])
        result = await mw.before_send("do bad things", _CTX)
        assert isinstance(result, RejectResult)
        assert stage1.called is True
        assert stage2.called is False  # heavy stage NOT invoked

    _run(_test())


# ---------------------------------------------------------------------------
# allow-stop short-circuits on an authoritative confident "safe"
# ---------------------------------------------------------------------------

def test_allow_stop_short_circuits_on_authority():
    async def _test():
        stage1 = _StubClassifier("safe", 0.95, name="authority")
        stage2 = _StubClassifier("unsafe", 1.0, name="heavy")
        mw = CascadeMiddleware([
            CascadeStage(stage1, allow_authority=True),
            CascadeStage(stage2, allow_authority=False),
        ])
        result = await mw.before_send("hello there", _CTX)
        assert result == "hello there"  # content returned unchanged
        assert stage1.called is True
        assert stage2.called is False  # heavy stage NOT invoked

    _run(_test())


# ---------------------------------------------------------------------------
# cheap "safe" does NOT allow-stop — the misleading safe must escalate
# ---------------------------------------------------------------------------

def test_cheap_safe_does_not_allow_stop_escalates():
    async def _test():
        # A naive regex-style stage: confident "safe" but no allow authority.
        stage1 = _StubClassifier("safe", 1.0, name="regex")
        stage2 = _StubClassifier("unsafe", 0.9, name="judge")
        mw = CascadeMiddleware([
            CascadeStage(stage1, allow_authority=False),
            CascadeStage(stage2, allow_authority=False),
        ])
        result = await mw.before_send("sneaky payload", _CTX)
        assert stage1.called is True
        assert stage2.called is True  # escalated past the cheap "safe"
        assert isinstance(result, RejectResult)  # heavy stage BLOCKS

    _run(_test())


# ---------------------------------------------------------------------------
# uncertain escalates to the final judge
# ---------------------------------------------------------------------------

def test_uncertain_escalates_to_final_judge():
    async def _test():
        stage1 = _StubClassifier("uncertain", 0.3, name="cheap")
        stage2 = _StubClassifier("safe", 0.9, name="judge")
        mw = CascadeMiddleware([
            CascadeStage(stage1, allow_authority=False),
            CascadeStage(stage2, allow_authority=True),
        ])
        result = await mw.before_send("ambiguous", _CTX)
        assert result == "ambiguous"
        assert stage1.called is True
        assert stage2.called is True

    _run(_test())


# ---------------------------------------------------------------------------
# threshold gating on the block side
# ---------------------------------------------------------------------------

def test_block_threshold_below_does_not_block():
    async def _test():
        stage1 = _StubClassifier("unsafe", 0.4, name="cheap")
        stage2 = _StubClassifier("safe", 0.9, name="judge")
        mw = CascadeMiddleware(
            [
                CascadeStage(stage1, allow_authority=False),
                CascadeStage(stage2, allow_authority=True),
            ],
            block_threshold=0.5,
        )
        result = await mw.before_send("borderline", _CTX)
        # 0.4 < 0.5 → stage1 does NOT block, escalates; stage2 allow-stops.
        assert result == "borderline"
        assert stage1.called is True
        assert stage2.called is True

    _run(_test())


def test_block_threshold_at_or_above_blocks():
    async def _test():
        stage1 = _StubClassifier("unsafe", 0.6, name="cheap")
        stage2 = _StubClassifier("safe", 1.0, name="judge")
        mw = CascadeMiddleware(
            [
                CascadeStage(stage1, allow_authority=False),
                CascadeStage(stage2, allow_authority=True),
            ],
            block_threshold=0.5,
        )
        result = await mw.before_send("bad", _CTX)
        assert isinstance(result, RejectResult)
        assert stage1.called is True
        assert stage2.called is False

    _run(_test())


# ---------------------------------------------------------------------------
# exhaustion default: all stages uncertain
# ---------------------------------------------------------------------------

def test_exhaustion_default_allow():
    async def _test():
        stage1 = _StubClassifier("uncertain", 0.3, name="a")
        stage2 = _StubClassifier("uncertain", 0.3, name="b")
        mw = CascadeMiddleware(
            [
                CascadeStage(stage1, allow_authority=True),
                CascadeStage(stage2, allow_authority=True),
            ],
            default_allow=True,
        )
        result = await mw.before_send("who knows", _CTX)
        assert result == "who knows"
        assert stage1.called is True
        assert stage2.called is True

    _run(_test())


def test_exhaustion_default_deny():
    async def _test():
        stage1 = _StubClassifier("uncertain", 0.3, name="a")
        stage2 = _StubClassifier("uncertain", 0.3, name="b")
        mw = CascadeMiddleware(
            [
                CascadeStage(stage1, allow_authority=True),
                CascadeStage(stage2, allow_authority=True),
            ],
            default_allow=False,
        )
        result = await mw.before_send("who knows", _CTX)
        assert isinstance(result, RejectResult)
        assert stage1.called is True
        assert stage2.called is True

    _run(_test())


# ---------------------------------------------------------------------------
# is a valid before_send middleware
# ---------------------------------------------------------------------------

def test_is_valid_before_send_middleware():
    async def _test():
        stage1 = _StubClassifier("safe", 0.9, name="authority")
        mw = CascadeMiddleware([CascadeStage(stage1, allow_authority=True)])
        assert hasattr(mw, "before_send")
        result = await mw.before_send("hi", _CTX)
        assert result == "hi"

    _run(_test())
