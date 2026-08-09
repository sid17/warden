"""``provider_home_kwargs`` pins every provider's transcript into the task dir.

Durability (S6) depends on each provider writing its transcript INSIDE the task
workspace, so the after-turn snapshot captures it and a wiped task dir can be
restored with memory intact. OpenHarness previously returned ``{}`` here and
wrote to the GLOBAL ``~/.openharness`` — outside the snapshot — so a crash lost
its transcript. This locks all four providers to a per-task home.
"""

from __future__ import annotations

from pathlib import Path

from warden.orchestrator.stream_runtime import provider_home_kwargs


def test_claude_pins_config_dir_into_task() -> None:
    td = Path("/work/task")
    assert provider_home_kwargs("claude", td) == {"claude_config_dir": td / ".claude-home"}
    assert provider_home_kwargs("claude-cli", td) == {"claude_config_dir": td / ".claude-home"}


def test_codex_pins_codex_home_into_task() -> None:
    td = Path("/work/task")
    assert provider_home_kwargs("codex", td) == {"codex_home": td / ".codex"}


def test_openharness_pins_session_home_into_task() -> None:
    """The B-OH-durability fix: openharness transcript lands inside the task dir."""
    td = Path("/work/task")
    kw = provider_home_kwargs("openharness", td)
    assert kw == {"session_home": td / ".openharness"}
    # The pinned home is UNDER the task dir, so a snapshot of the task dir
    # captures the transcript (contract S6).
    assert str(kw["session_home"]).startswith(str(td))


def test_unknown_provider_gets_empty() -> None:
    assert provider_home_kwargs("mystery", Path("/work/task")) == {}
