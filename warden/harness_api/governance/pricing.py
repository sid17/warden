"""The pricing table + cost function (harness-owned).

This is a pure, stateless pricing module: the model-id → $/Mtok table and the
:func:`cost_usd` function that prices one turn's usage. The **spend GATE** moved to
the Governor's reservation ledger (M2 3d); the old N10 allow-first ``SpendTracker``
(which only checked accumulated spend after the fact) is retired (3g.2b). What
remains is the price *table* and *function*, imported by both the Governor (for the
worst-case reservation) and the Runner (for stateless per-turn pricing).

Pricing is USD per 1M tokens, keyed by a model-id prefix (so ``claude-opus-4-8``
and any dated variant resolve to the same row). Values are the published
list prices as of 2026-07; they are **overridable** via ``PRICING_JSON`` (same
shape as :data:`DEFAULT_PRICING`) so a deploy can correct them without a code
change — a spend cap must never silently price at a stale rate.

Cache tokens are priced relative to input: a cache *read* is ~0.1x input, a cache
*write* (5-minute TTL) is ~1.25x input — matching Anthropic's cache economics.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping

# USD per 1M tokens: model-id prefix -> (input, output). Longest-prefix wins.
DEFAULT_PRICING: dict[str, tuple[float, float]] = {
    "claude-fable-5": (10.0, 50.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-opus": (5.0, 25.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-haiku": (1.0, 5.0),
}

# Model used to price a run when the RunSpec pins no explicit model (the
# claude-cli default is an Opus-tier model).
_DEFAULT_MODEL = "claude-opus-4-8"

# Cache-token multipliers relative to the input rate.
_CACHE_READ_MULT = 0.1
_CACHE_WRITE_MULT = 1.25

_PER_MTOK = 1_000_000.0


def _load_pricing(env: Mapping[str, str] | None = None) -> dict[str, tuple[float, float]]:
    """Return the pricing table, applying ``PRICING_JSON`` overrides if present."""
    src = os.environ if env is None else env
    return build_pricing(src.get("PRICING_JSON"))


def build_pricing(pricing_json: str | None) -> dict[str, tuple[float, float]]:
    """Build the pricing table from an optional ``PRICING_JSON`` override string.

    The typed entry point used by the Axis-2 config (``SpendConfig.pricing_json``)
    so the ``PRICING_JSON`` env read is routed through config, not scattered.
    ``None`` → the default table; malformed JSON is a hard error (a spend cap must
    never silently price at a stale/zero rate).
    """
    if not pricing_json:
        return dict(DEFAULT_PRICING)
    try:
        overrides = json.loads(pricing_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid PRICING_JSON: {exc}") from exc
    table = dict(DEFAULT_PRICING)
    for prefix, pair in overrides.items():
        table[prefix] = (float(pair[0]), float(pair[1]))
    return table


def _rates_for(model: str | None, table: Mapping[str, tuple[float, float]]) -> tuple[float, float]:
    """Resolve (input, output) $/Mtok for a model id by longest-prefix match."""
    name = model or _DEFAULT_MODEL
    best: tuple[float, float] | None = None
    best_len = -1
    for prefix, rates in table.items():
        if name.startswith(prefix) and len(prefix) > best_len:
            best, best_len = rates, len(prefix)
    if best is None:
        # Unknown model: fall back to the default-model row rather than pricing
        # at zero (a zero price would silently defeat the cap).
        return _rates_for(_DEFAULT_MODEL, table)
    return best


def cost_usd(
    model: str | None,
    usage: Mapping[str, int],
    table: Mapping[str, tuple[float, float]] | None = None,
) -> float:
    """Price one turn's ``usage`` dict (as emitted by the providers) in USD.

    ``usage`` keys: ``input_tokens``, ``output_tokens``,
    ``cache_read_input_tokens``, ``cache_creation_input_tokens`` (any missing → 0).
    """
    tbl = table if table is not None else DEFAULT_PRICING
    in_rate, out_rate = _rates_for(model, tbl)
    inp = usage.get("input_tokens", 0)
    out = usage.get("output_tokens", 0)
    cache_read = usage.get("cache_read_input_tokens", 0)
    cache_write = usage.get("cache_creation_input_tokens", 0)
    dollars = (
        inp * in_rate
        + out * out_rate
        + cache_read * in_rate * _CACHE_READ_MULT
        + cache_write * in_rate * _CACHE_WRITE_MULT
    )
    return dollars / _PER_MTOK
