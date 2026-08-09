"""C5 — one normalized ``Usage`` struct, decoupled from ``cost_visibility``.

Every provider already surfaces a per-turn ``usage`` dict on its terminal
``status``/``result`` event, but with different keys (Claude ``input_tokens`` +
``cache_read_input_tokens`` + ``total_cost_usd``; Codex ``ThreadTokenUsage`` →
``input_tokens``/``cached_input_tokens``; Ollama ``prompt_eval_count`` /
``eval_count``). Consumers should not branch on provider to read token counts —
``normalize_usage`` collapses any of those shapes into one struct.

Decoupled from the ``cost_visibility`` capability flag: that flag says *when* a
provider reveals usage (mid-turn / coarse / terminal); this struct is *what* the
numbers are once revealed.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

# Accepted aliases per field (first match wins). Kept generous so a new provider's
# native keys normalize without a code change.
_INPUT_KEYS = ("input_tokens", "prompt_tokens", "prompt_eval_count", "input")
_OUTPUT_KEYS = ("output_tokens", "completion_tokens", "eval_count", "output")
_CACHED_KEYS = (
    "cache_read_input_tokens", "cached_input_tokens", "cache_read", "cached",
)
# A2 — cache-WRITE tokens are reported separately from cache-read but are also real
# input the model processed. Summed INTO ``cached`` (not first-match-wins) so no input
# tokens are silently dropped from the total the UI shows.
_CACHE_CREATE_KEYS = ("cache_creation_input_tokens", "cache_creation")
_COST_KEYS = ("cost_usd", "total_cost_usd", "totalCostUsd")


@dataclass(frozen=True)
class Usage:
    """Provider-agnostic token/cost accounting for one turn (or a run total)."""

    input: int = 0
    output: int = 0
    cached: int = 0
    cost_usd: float = 0.0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            input=self.input + other.input,
            output=self.output + other.output,
            cached=self.cached + other.cached,
            cost_usd=self.cost_usd + other.cost_usd,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "input": self.input, "output": self.output,
            "cached": self.cached, "cost_usd": self.cost_usd,
        }


def _pick_int(src: Any, keys: tuple[str, ...]) -> int:
    for k in keys:
        v = src.get(k) if isinstance(src, Mapping) else getattr(src, k, None)
        if v is not None:
            try:
                return int(v)
            except (TypeError, ValueError):
                continue
    return 0


def _pick_float(src: Any, keys: tuple[str, ...]) -> float:
    for k in keys:
        v = src.get(k) if isinstance(src, Mapping) else getattr(src, k, None)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return 0.0


def normalize_usage(raw: Any, *, cost_usd: float | None = None) -> Usage:
    """Collapse any provider's usage dict/object into a :class:`Usage`.

    ``raw`` is a mapping or an object with token attributes (``None`` → zeros).
    ``cost_usd`` overrides any cost found in ``raw`` (e.g. the runner passes the
    price it computed from its own table). Best-effort and total: an unknown shape
    yields zeros rather than raising, so a usage read never breaks a turn.
    """
    if raw is None:
        raw = {}
    cost = cost_usd if cost_usd is not None else _pick_float(raw, _COST_KEYS)
    return Usage(
        input=_pick_int(raw, _INPUT_KEYS),
        output=_pick_int(raw, _OUTPUT_KEYS),
        # cache-read (first-match among aliases) + cache-write, so both cache classes
        # count toward the input the model actually processed (A2).
        cached=_pick_int(raw, _CACHED_KEYS) + _pick_int(raw, _CACHE_CREATE_KEYS),
        cost_usd=cost,
    )


__all__ = ["Usage", "normalize_usage"]
