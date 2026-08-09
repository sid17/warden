"""Tests for the pricing table + cost function.

The spend GATE moved to the Governor's reservation ledger (see
``tests/harness_api/governance/test_governor.py``); the N10 allow-first
``SpendTracker`` is retired. What remains here is the price *table* + *function*.
"""

from warden.harness_api.governance.pricing import (
    build_pricing,
    cost_usd,
)


def test_cost_opus_input_output():
    # 1M input @ $5 + 1M output @ $25 = $30
    usage = {"input_tokens": 1_000_000, "output_tokens": 1_000_000}
    assert cost_usd("claude-opus-4-8", usage) == 30.0


def test_cost_sonnet_cheaper_than_opus():
    usage = {"input_tokens": 1_000_000, "output_tokens": 0}
    assert cost_usd("claude-sonnet-5", usage) < cost_usd("claude-opus-4-8", usage)


def test_cost_cache_read_is_cheap():
    read = cost_usd("claude-opus-4-8", {"cache_read_input_tokens": 1_000_000})
    inp = cost_usd("claude-opus-4-8", {"input_tokens": 1_000_000})
    # cache read is ~0.1x input
    assert abs(read - inp * 0.1) < 1e-9


def test_cost_cache_write_premium():
    write = cost_usd("claude-opus-4-8", {"cache_creation_input_tokens": 1_000_000})
    inp = cost_usd("claude-opus-4-8", {"input_tokens": 1_000_000})
    assert abs(write - inp * 1.25) < 1e-9


def test_unknown_model_prices_at_default_not_zero():
    usage = {"input_tokens": 1_000_000}
    assert cost_usd("some-unknown-model", usage) > 0


def test_none_model_uses_default():
    usage = {"input_tokens": 1_000_000}
    assert cost_usd(None, usage) == cost_usd("claude-opus-4-8", usage)


def test_longest_prefix_wins():
    # haiku-4-5 explicit row ($1) vs generic claude-haiku ($1) — both same here,
    # but verify opus-4-8 resolves to its own row, not a shorter generic.
    assert cost_usd("claude-opus-4-8", {"output_tokens": 1_000_000}) == 25.0


def test_build_pricing_override():
    """``build_pricing`` overlays a PRICING_JSON override onto the default table."""
    import json

    table = build_pricing(json.dumps({"claude-opus-4-8": [1.0, 2.0]}))
    cost = cost_usd("claude-opus-4-8", {"output_tokens": 1_000_000}, table)
    assert cost == 2.0


def test_build_pricing_none_is_default():
    """No override → the default table (a known row is priced at its list rate)."""
    table = build_pricing(None)
    assert cost_usd("claude-opus-4-8", {"output_tokens": 1_000_000}, table) == 25.0


def test_build_pricing_malformed_json_raises():
    """Malformed PRICING_JSON is a hard error (never silently price at stale rates)."""
    import pytest

    with pytest.raises(ValueError):
        build_pricing("{not valid json")
