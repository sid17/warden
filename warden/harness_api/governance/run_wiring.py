"""Runner-side glue for the Governor (M2 3e/3f) — kept out of ``runner.py``.

The Runner's governed path needs to turn a :class:`~warden.harness_api.schemas.RunSpec`
into a per-run :class:`~warden.harness_api.governance.governor.RunGovernor`:
fold the run's caps into a :class:`GovernancePolicy`, convert the ISO deadline to
seconds, and call ``GovernorService.resolve``. Extracted here so ``runner.py`` stays
well under the 500-line law (this is the analogue of ``orchestrator/governor_surface.py``).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from warden.harness_api.governance.governor import GovernorService
from warden.harness_api.governance.policy import GovernancePolicy
from warden.providers import provider_max_output_tokens

if TYPE_CHECKING:
    from warden.harness_api.governance.governor import RunGovernor
    from warden.harness_api.schemas import RunSpec

# LiteLLM-style absolute output ceiling: the reservation's worst case always folds
# this in, so even a priced model with no tighter bound has a bounded worst case
# (a run can never reserve — or spend — as if it had unlimited output).
HARD_CAP_OUT = 16384


def deadline_seconds(deadline: str | None) -> float | None:
    """Convert an ISO-8601 UTC wall-clock deadline to seconds-from-now.

    ``None`` deadline ⇒ ``None`` (uncapped). A trailing ``Z`` is normalized to
    ``+00:00`` for :func:`datetime.fromisoformat`. A deadline already in the past
    clamps to ``0.0`` (the governor breaches immediately) rather than going negative.
    """
    if deadline is None:
        return None
    parsed = datetime.fromisoformat(deadline.replace("Z", "+00:00"))
    delta = (parsed - datetime.now(timezone.utc)).total_seconds()
    return max(0.0, delta)


async def resolve_run_governor(
    service: GovernorService, spec: "RunSpec",
) -> "RunGovernor":
    """Build the per-run RunGovernor from the shared GovernorService (governed path).

    Folds the run's caps (``budget_usd`` → cost cap, ``deadline`` → seconds,
    ``max_turns``) into a run-level :class:`GovernancePolicy` and asks the service to
    resolve credential + policy + balance + worst-case in one step.
    """
    run_policy = GovernancePolicy(
        cost_cap_usd=spec.budget_usd,
        deadline_s=deadline_seconds(spec.deadline),
        max_turns=spec.max_turns,
    )
    return await service.resolve(
        user_id=spec.user_id,
        task_id=spec.task_id,
        provider=spec.provider,
        model=spec.model,
        requested_max_out=None,
        model_max_out=provider_max_output_tokens(spec.provider),
        hard_cap_out=HARD_CAP_OUT,
        run_policy=run_policy,
    )


__all__ = ["HARD_CAP_OUT", "deadline_seconds", "resolve_run_governor"]
