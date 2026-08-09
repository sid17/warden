"""Wiring tests for v16 Phase 2 persistence (Step 4).

Proves the guarded restore/backup + per-provider session-home wiring threads
correctly through SessionManager → Orchestrator → ChatAPI, WITHOUT spawning a
real provider subprocess. A fake ``create_session`` is monkeypatched into the
session manager module so the whole flow is deterministic.

Async style matches the repo (no pytest-asyncio): ``asyncio.run`` inside sync
test functions (see test_api.py / test_cli.py).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

from warden import ChatAPI, HarnessConfig
from warden.persistence import (
    LocalFileBackend,
    PersistenceConfig,
    archive_key,
    task_dir,
)
import warden.orchestrator.session.manager as manager_mod


def _persist_config(
    *,
    provider: str = "claude",
    user_id: str = "default",
    task_id: str | None = None,
    base_dir: Any = "data/workspaces",
    state_root: Any = "data/store",
) -> HarnessConfig:
    """A HarnessConfig with the persistence/workspace knobs the wiring tests set."""
    config = HarnessConfig()
    config.provider.provider = provider
    config.workspace.user_id = user_id
    config.workspace.task_id = task_id
    config.workspace.base_dir = str(base_dir)
    config.persistence.state_root = str(state_root)
    return config


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _FakeSession:
    """Minimal provider session — no subprocess, deterministic messages."""

    def __init__(self) -> None:
        self.session_id: str = "fake-persist-session"
        self.jsonl_path: str | None = None
        self._started = False

    async def start(self) -> None:
        self._started = True

    async def send(self, prompt: str) -> AsyncGenerator[dict, None]:
        # Yield a couple of dict messages then return (turn complete).
        yield {"type": "text", "text": "hello"}
        yield {"type": "text", "text": " world"}

    async def stop(self) -> None:
        pass

    async def close(self) -> None:
        pass


def _install_fake_create_session(monkeypatch_target: dict) -> Any:
    """Return a fake ``create_session`` that records its kwargs.

    The recorder dict is mutated in place so the test can assert on the
    ``provider_kwargs`` (e.g. ``claude_config_dir``) the manager forwarded.
    """

    def _fake_create_session(provider: str, **kwargs: Any) -> _FakeSession:
        monkeypatch_target["provider"] = provider
        monkeypatch_target["kwargs"] = kwargs
        return _FakeSession()

    return _fake_create_session


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


async def _drain(api: ChatAPI, prompt: str) -> list[Any]:
    events: list[Any] = []
    async for event in api.send(prompt):
        events.append(event)
    return events


# ---------------------------------------------------------------------------
# Test 1: init() runs ensure_restored and derives repo_path to the task dir
# ---------------------------------------------------------------------------

def test_init_derives_repo_path_to_task_dir(tmp_path: Path) -> None:
    async def _test() -> None:
        base_dir = tmp_path / "workspaces"
        state_root = tmp_path / "store"
        api = ChatAPI(
            _persist_config(
                user_id="alice", task_id="task_1",
                base_dir=base_dir, state_root=state_root,
            ),
            repo_path=".",
        )
        await api.init()

        expected = task_dir(base_dir, "alice", "task_1")
        assert api._repo_path == expected
        # ensure_restored + mkdir means the folder exists (fresh task).
        assert expected.exists()
        # Orchestrator sees persistence as active.
        assert api._orchestrator._persist_active is True

        await api.close()

    _run(_test())


# ---------------------------------------------------------------------------
# Test 2: after a send completes, snapshot backs up to the store
# ---------------------------------------------------------------------------

def test_send_snapshots_to_store(tmp_path: Path, monkeypatch: Any) -> None:
    async def _test() -> None:
        base_dir = tmp_path / "workspaces"
        state_root = tmp_path / "store"

        recorder: dict = {}
        monkeypatch.setattr(
            manager_mod, "create_session", _install_fake_create_session(recorder)
        )

        api = ChatAPI(
            _persist_config(
                provider="claude-cli", user_id="bob", task_id="task_2",
                base_dir=base_dir, state_root=state_root,
            ),
            repo_path=".",
        )
        await api.init()

        await _drain(api, "count please")

        # A real LocalFileBackend on the same state_root should now see the key.
        cfg = PersistenceConfig(base_dir=base_dir, state_root=state_root)
        backend = LocalFileBackend(cfg.state_root, cfg.exclude_patterns)
        key = archive_key(cfg, "bob", "task_2")
        assert await backend.exists(key), f"snapshot did not write archive {key}"

        await api.close()

    _run(_test())


# ---------------------------------------------------------------------------
# Test 3: provider_kwargs for claude-cli carries claude_config_dir=<task>/.claude-home
# ---------------------------------------------------------------------------

def test_provider_kwargs_claude_config_dir(tmp_path: Path, monkeypatch: Any) -> None:
    async def _test() -> None:
        base_dir = tmp_path / "workspaces"
        state_root = tmp_path / "store"

        recorder: dict = {}
        monkeypatch.setattr(
            manager_mod, "create_session", _install_fake_create_session(recorder)
        )

        api = ChatAPI(
            _persist_config(
                provider="claude-cli", user_id="carol", task_id="task_3",
                base_dir=base_dir, state_root=state_root,
            ),
            repo_path=".",
        )
        await api.init()

        await _drain(api, "hi")

        assert recorder["provider"] == "claude-cli"
        expected_home = task_dir(base_dir, "carol", "task_3") / ".claude-home"
        assert recorder["kwargs"].get("claude_config_dir") == expected_home

        await api.close()

    _run(_test())


# ---------------------------------------------------------------------------
# Test 4: no task_id → persistence off, no snapshot written
# ---------------------------------------------------------------------------

def test_no_task_id_persistence_off(tmp_path: Path, monkeypatch: Any) -> None:
    async def _test() -> None:
        state_root = tmp_path / "store"

        recorder: dict = {}
        monkeypatch.setattr(
            manager_mod, "create_session", _install_fake_create_session(recorder)
        )

        api = ChatAPI(
            _persist_config(
                provider="claude-cli",
                base_dir=tmp_path / "workspaces", state_root=state_root,
            ),
            repo_path=".",
        )
        await api.init()

        # persistence inactive
        assert api._orchestrator._persist_active is False

        await _drain(api, "hello")

        # No claude_config_dir threaded (persistence off → no provider home kwargs).
        assert "claude_config_dir" not in recorder["kwargs"]
        # Nothing written to the store (either the root does not exist or is empty).
        if state_root.exists():
            assert list(state_root.rglob("*.tar.gz")) == []

        await api.close()

    _run(_test())
