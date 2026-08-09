"""C5 — normalize_usage collapses each provider's usage shape into one struct."""

from __future__ import annotations

from warden.schemas.usage import Usage, normalize_usage


def test_claude_shape() -> None:
    raw = {
        "input_tokens": 100,
        "output_tokens": 40,
        "cache_read_input_tokens": 12,
    }
    u = normalize_usage(raw, cost_usd=0.0031)
    assert u == Usage(input=100, output=40, cached=12, cost_usd=0.0031)


def test_claude_cache_creation_folded_into_cached() -> None:
    """A2 — Claude reports cache-WRITE tokens (``cache_creation_input_tokens``) separately
    from cache-read. Both are real input the model processed; dropping creation made the
    displayed input tokens look absurdly low. Fold both cache classes into ``cached`` so
    the total input (input + cached) reflects everything the model actually saw."""
    raw = {
        "input_tokens": 30,
        "output_tokens": 10570,
        "cache_read_input_tokens": 8000,
        "cache_creation_input_tokens": 4450,
    }
    u = normalize_usage(raw)
    assert u.input == 30
    assert u.output == 10570
    assert u.cached == 8000 + 4450  # both cache classes counted, not just read


def test_codex_shape() -> None:
    raw = {"input_tokens": 200, "output_tokens": 80, "cached_input_tokens": 5}
    assert normalize_usage(raw) == Usage(input=200, output=80, cached=5)


def test_ollama_shape() -> None:
    raw = {"prompt_eval_count": 55, "eval_count": 33}
    assert normalize_usage(raw) == Usage(input=55, output=33, cached=0)


def test_object_with_attributes() -> None:
    class _TU:
        input_tokens = 7
        output_tokens = 9
        cached_input_tokens = 1

    assert normalize_usage(_TU()) == Usage(input=7, output=9, cached=1)


def test_none_and_unknown_shape_yield_zeros() -> None:
    assert normalize_usage(None) == Usage()
    assert normalize_usage({"weird": 1}) == Usage()


def test_cost_override_wins_over_embedded() -> None:
    raw = {"input_tokens": 1, "total_cost_usd": 9.99}
    assert normalize_usage(raw).cost_usd == 9.99  # embedded used when no override
    assert normalize_usage(raw, cost_usd=0.5).cost_usd == 0.5  # override wins


def test_usage_add_and_as_dict() -> None:
    total = Usage(1, 2, 3, 0.5) + Usage(10, 20, 30, 1.5)
    assert total == Usage(11, 22, 33, 2.0)
    assert total.as_dict() == {"input": 11, "output": 22, "cached": 33, "cost_usd": 2.0}
