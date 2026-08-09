"""M2 3b — the ``deadline_unsupported_on_provider`` guard (B18).

A wall-clock deadline can only be *promised* on a provider the harness can
actually stop. The discriminator is the provider's ``hard_kill_tier``:

  * ``"os"``          (codex) — true SIGKILL of a harness-owned PID → OK
  * ``"cooperative"`` (claude) — SDK ``interrupt()`` at the next checkpoint → OK
  * ``"none"``        (openharness) — the server keeps generating after a
    disconnect; a deadline cannot be honored → **reject up front**, no silent
    degrade, no auto-promote.

This is *not* keyed on ``supports_hard_deadline`` (which is ``False`` for claude
too, yet claude gets a cooperative deadline) — the right question is "can the
provider be stopped at all," which ``hard_kill_tier == 'none'`` answers.
"""

from __future__ import annotations

import pytest

from warden.harness_api.governance.deadline import (
    DeadlineUnsupportedError,
    assert_deadline_supported,
)


def test_openharness_deadline_rejected() -> None:
    with pytest.raises(DeadlineUnsupportedError) as ei:
        assert_deadline_supported(hard_kill_tier="none", has_deadline=True)
    assert ei.value.code == "deadline_unsupported_on_provider"


def test_claude_cooperative_deadline_allowed() -> None:
    # No raise — cooperative cancel can honor a (best-effort) deadline.
    assert_deadline_supported(hard_kill_tier="cooperative", has_deadline=True)


def test_codex_hard_deadline_allowed() -> None:
    assert_deadline_supported(hard_kill_tier="os", has_deadline=True)


def test_no_deadline_never_rejected() -> None:
    # No deadline requested → the guard is a no-op even on an unstoppable provider.
    assert_deadline_supported(hard_kill_tier="none", has_deadline=False)
