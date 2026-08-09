"""B18 — the wall-clock deadline's provider guard.

A deadline can only be *promised* on a provider the harness can actually stop.
The design (``docs/06-resource-governance.md`` §5/§7, 2026-07-17 DECISION) forbids
a hard deadline on OpenHarness at the seam — rejected up front, **no silent
degrade, no auto-promote**.

The discriminator is the provider's ``hard_kill_tier`` capability flag, not
``supports_hard_deadline``:

  * ``"os"``          (codex) — true SIGKILL of a harness-owned PID → honorable
  * ``"cooperative"`` (claude) — SDK ``interrupt()`` at the next checkpoint →
    honorable as a best-effort (cooperative) deadline
  * ``"none"``        (openharness) — the Ollama server keeps generating after a
    disconnect (issue #11889); a deadline **cannot** be honored → reject

Keying on ``supports_hard_deadline`` would wrongly reject claude (it is ``False``
there too, yet claude gets a cooperative deadline). "Can the provider be stopped
at all?" is the right question, and ``hard_kill_tier == 'none'`` answers it.
"""

from __future__ import annotations

# Providers that cannot be stopped once generating ⇒ a deadline is unhonorable.
_UNSTOPPABLE_KILL_TIER = "none"


class DeadlineUnsupportedError(Exception):
    """A deadline was requested against a provider that cannot honor it.

    Carries the typed ``code`` (``deadline_unsupported_on_provider``) the API
    surfaces so the failure is a clear up-front rejection, never a silent degrade.
    """

    code = "deadline_unsupported_on_provider"


def assert_deadline_supported(*, hard_kill_tier: str, has_deadline: bool) -> None:
    """Reject a deadline the target provider cannot enforce.

    No-op unless a deadline is actually requested. Raises
    :class:`DeadlineUnsupportedError` when the provider's ``hard_kill_tier`` is
    ``"none"`` (it keeps generating after a disconnect) — no auto-promote to
    another provider, by design.
    """
    if has_deadline and hard_kill_tier == _UNSTOPPABLE_KILL_TIER:
        raise DeadlineUnsupportedError(
            "a wall-clock deadline cannot be honored on a provider with "
            f"hard_kill_tier={hard_kill_tier!r} (it keeps generating after a "
            "disconnect); reject up front — no auto-promote"
        )


__all__ = ["DeadlineUnsupportedError", "assert_deadline_supported"]
