"""B17 — the Governor seam (the harness's fourth seam).

Resource governance reaches the engine through a callback, exactly as permissions
do (``seams/permissions.py``). The engine invokes ``governor.check()`` at a fixed
set of **checkpoints** and obeys the verdict without understanding it: the numbers
(dollars, users, tiers) live in the Governor — an Axis-2 component in
``harness_api/`` — never in the engine (GOV-1).

Two facts cross the seam INTO the Governor: normalized :class:`Usage` (token
counts) and ``elapsed_s`` (wall-clock seconds). Both are mechanism facts, not
policy — the engine already reads tokens for spend and owns the clock; it just
never learns what a *dollar* is. The Governor maps those to its own cap.

The seam is OPTIONAL (GOV-2): no Governor wired ⇒ ``check()`` is never called ⇒
the harness runs ungoverned, exactly as before. See design
``docs/06-resource-governance.md`` §3 (the seam table) and §7 (the checkpoint
sites); the concrete Governor + reservation ledger land in ``harness_api/governance/``
(M2 3d).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from warden.schemas.usage import Usage

# The checkpoint sites the engine exposes (design §7). Not every provider can feed
# every site — the Governor's verdict is obeyed at whichever ones a run reaches:
#   pre_flight    before the provider is called (reservation reject site)
#   tool_gate     inside the permission seam (usage-so-far + elapsed)
#   turn_boundary as the turn's events drain / on completion
#   clock_tick    a wall-clock timer armed at run start (time only)
#   mid_stream    per usage delta INSIDE a turn — only where the stream reports it
Checkpoint = Literal[
    "pre_flight", "tool_gate", "turn_boundary", "clock_tick", "mid_stream",
]


@dataclass(frozen=True)
class Continue:
    """Verdict: keep running. The engine proceeds as if ungoverned."""


@dataclass(frozen=True)
class Stop:
    """Verdict: halt the run. ``reason`` is a typed, engine-opaque token
    (e.g. ``"budget"`` / ``"deadline"`` / ``"max_turns"``) that the engine emits
    on a :class:`~warden.schemas.events.StoppedEvent` and never
    interprets. M5 records it as the AUD-3 terminal event."""

    reason: str


Verdict = Continue | Stop

#: Singleton ``continue`` verdict (verdicts are immutable value objects).
CONTINUE: Continue = Continue()


@runtime_checkable
class Governor(Protocol):
    """What the engine sees of the Governor: one async checkpoint callback.

    Returns :data:`CONTINUE` to proceed or :class:`Stop` to halt. Kept async so a
    real Governor may consult its reservation ledger (a Postgres row-lock, 3d)
    without the seam signature changing.
    """

    async def check(
        self, checkpoint: Checkpoint, usage: Usage, elapsed_s: float,
    ) -> Verdict: ...


__all__ = [
    "Checkpoint",
    "Continue",
    "Stop",
    "Verdict",
    "CONTINUE",
    "Governor",
]
