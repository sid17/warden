"""M2 3d — the governance policy (the *caps*) + the worst-case reservation price.

The Governor's numbers live here, not in the engine (GOV-1). A :class:`GovernancePolicy`
is the immutable bundle of caps that apply to one run: a cost cap (USD), a wall-clock
deadline (seconds), and a max-turns bound. Any field may be ``None`` (unset/uncapped).

Caps are layered — an operator tier default, an optional per-task override, and an
optional per-run override. :func:`resolve_policy` folds them with **run > task > tier**
precedence (the later, more specific non-``None`` field wins). A field left ``None`` in
every layer stays ``None`` (uncapped for that dimension).

:func:`worst_case_usd` is the *reservation price*: the most a single run could cost
before it is ever launched. Reserving this worst case UP FRONT (M2 3d, ledger) is what
closed the N10 allow-first bug of the retired ``SpendTracker.over_budget`` gate,
which only checked *accumulated* spend after the fact — a single oversized run slips
through. Here we bound the run before it runs.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from warden.harness_api.governance.pricing import _DEFAULT_MODEL, _rates_for

_PER_MTOK = 1_000_000.0


@dataclass(frozen=True)
class GovernancePolicy:
    """The caps that apply to one run. ``None`` on any field means uncapped there."""

    cost_cap_usd: float | None = None
    deadline_s: float | None = None
    max_turns: int | None = None


def resolve_policy(
    tier: GovernancePolicy | None,
    task: GovernancePolicy | None,
    run: GovernancePolicy | None,
) -> GovernancePolicy:
    """Fold the three cap layers with **run > task > tier** field precedence.

    Each argument is a full :class:`GovernancePolicy` or ``None`` (a whole layer
    absent). Per field, the most specific non-``None`` value wins; a field that is
    ``None`` in every present layer stays ``None`` (uncapped).
    """
    layers = [layer for layer in (tier, task, run) if layer is not None]

    def _pick(attr: str):
        value = None
        for layer in layers:  # later layers (task, run) override earlier (tier)
            layer_value = getattr(layer, attr)
            if layer_value is not None:
                value = layer_value
        return value

    return GovernancePolicy(
        cost_cap_usd=_pick("cost_cap_usd"),
        deadline_s=_pick("deadline_s"),
        max_turns=_pick("max_turns"),
    )


def _model_is_priced(
    model: str | None, table: Mapping[str, tuple[float, float]]
) -> bool:
    """True iff ``model`` matches a real pricing row by prefix.

    ``_rates_for`` falls back to the default-model row for any unknown model, so a
    local/Ollama model would otherwise be priced at a bogus Opus rate. We need an
    explicit "is this model actually in the table?" check so an unpriced provider
    yields a ``None`` reservation (time/turn-only) rather than a fake dollar hold.

    A ``None`` model IS priced — it resolves to the default model the run will use.
    """
    name = model if model is not None else _DEFAULT_MODEL
    return any(name.startswith(prefix) for prefix in table)


def worst_case_usd(
    *,
    input_tokens_est: int,
    requested_max_out: int | None,
    model_max_out: int | None,
    hard_cap_out: int | None,
    model: str | None,
    table: Mapping[str, tuple[float, float]],
) -> float | None:
    """Worst-case USD a single run could cost, to reserve BEFORE the call (N10 fix).

    ``out_tokens`` is the *tightest* known output bound — ``min`` of whichever of
    ``requested_max_out`` / ``model_max_out`` / ``hard_cap_out`` are set. We fold in
    the estimated input tokens too (LiteLLM's cost includes input; an output-only
    reservation under-reserves and lets a large-context run slip the cap).

    Returns ``None`` when:
      * no output bound is known (all three ``*_out`` are ``None``) — the caller
        cannot price a worst case and falls back to the time/turn bound; or
      * the model is not in the pricing table (a local/Ollama model) — pricing it
        at the default-model fallback would reserve bogus dollars, so we defer to
        the time/turn bound instead.
    """
    out_bounds = [b for b in (requested_max_out, model_max_out, hard_cap_out) if b is not None]
    if not out_bounds:
        return None
    if not _model_is_priced(model, table):
        return None
    out_tokens = min(out_bounds)
    in_rate, out_rate = _rates_for(model, table)
    return input_tokens_est * in_rate / _PER_MTOK + out_tokens * out_rate / _PER_MTOK


__all__ = ["GovernancePolicy", "resolve_policy", "worst_case_usd"]
