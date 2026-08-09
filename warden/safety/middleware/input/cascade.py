"""Cascade input middleware — ordered cheapest→heaviest classifier stages.

Runs an ORDERED list of classifier STAGES, cheapest first, short-circuiting per
an explicit policy (M4 doc 06 §4c). The invariant: **the heavy detector runs
only on the cases the cheap ones can't resolve — never on every task.** Both a
confident block and a confident authoritative allow stop the cascade early.

Per stage, in order, on ``result = await stage.classifier.classify(content)``:

- **block-stop:** ``label == "unsafe" and score >= block_threshold`` → reject
  immediately; later stages are NOT invoked. Any stage may block.
- **allow-stop:** ``label == "safe" and score >= allow_threshold`` AND
  ``stage.allow_authority`` → accept immediately (return content), do NOT
  escalate.
- **escalate:** anything else — an ``uncertain`` verdict, a non-authoritative
  ``safe``, or a below-threshold score — falls through to the next stage.

The ``allow_authority`` flag encodes trust: a naive regex stage's "safe" only
means "no known bad pattern matched" (absence of a pattern is not proof of
safety), so it must NOT allow-stop — it escalates, and a trained/judge stage
with authority makes the accept call. This is precisely what keeps the heavy
detector off the tasks the cheap stages already resolved.

When every stage is exhausted with no decision, ``default_allow`` decides:
allow (default — keeps FPR low, don't over-block an all-uncertain run) or
reject with a "cascade exhausted" reason.
"""

from __future__ import annotations

from dataclasses import dataclass

from warden.safety.middleware.classifiers import Classifier
from warden.seams.middleware import RejectResult, SendContext


@dataclass
class CascadeStage:
    """One rung of the cascade: a classifier plus its allow authority.

    ``allow_authority`` says whether this stage's confident "safe" may STOP the
    cascade (accept). Cheap heuristics leave it False so their misleading "safe"
    escalates; trained/judge stages set it True.
    """

    classifier: Classifier
    allow_authority: bool = False


class CascadeMiddleware:
    """Ordered classifier cascade: block-stop / allow-stop / uncertain-escalate."""

    def __init__(
        self,
        stages: list[CascadeStage],
        block_threshold: float = 0.5,
        allow_threshold: float = 0.8,
        default_allow: bool = True,
    ) -> None:
        self._stages = stages
        self._block_threshold = block_threshold
        self._allow_threshold = allow_threshold
        self._default_allow = default_allow

    async def before_send(
        self, content: str, context: SendContext,
    ) -> str | RejectResult:
        for stage in self._stages:
            result = await stage.classifier.classify(content)

            # block-stop: any stage may confidently reject.
            if result.label == "unsafe" and result.score >= self._block_threshold:
                return RejectResult(
                    reason=(
                        f"Cascade blocked at '{result.classifier}': "
                        f"unsafe (score {result.score:.2f})"
                    ),
                )

            # allow-stop: only an authoritative confident "safe" accepts early.
            if (
                result.label == "safe"
                and result.score >= self._allow_threshold
                and stage.allow_authority
            ):
                return content

            # escalate: uncertain, non-authoritative safe, or below threshold.

        if self._default_allow:
            return content
        return RejectResult(reason="cascade exhausted, no confident allow")
