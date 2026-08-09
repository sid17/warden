"""EXT-C3b — the (user, task) lock: import-guard + tier-switch routing (hermetic).

The distributed Postgres lock is exercised against a live DB in
``test_task_lock_live.py`` (opt-in) and on the Docker bed; here we only prove the
lazy-import contract + that the ``state.backend`` switch routes ``build_task_lock``,
and that the in-process lock preserves today's per-key serialization semantics.
"""

from __future__ import annotations

import asyncio
import builtins
import importlib

import pytest

from warden.harness_api.config import HarnessApiConfig, StateBackendConfig


def test_task_lock_module_imports_without_asyncpg(monkeypatch) -> None:
    real_import = builtins.__import__

    def _blocked_import(name, *args, **kwargs):
        if name == "asyncpg":
            raise ImportError("No module named 'asyncpg'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked_import)
    mod = importlib.import_module("warden.harness_api.task_lock")
    importlib.reload(mod)
    try:
        assert hasattr(mod, "PostgresTaskLock")
        with pytest.raises(ImportError, match="asyncpg"):
            mod.PostgresTaskLock("postgresql://x/y")
    finally:
        # Restore un-blocked classes so sibling tests' imports match the module dict
        # (reload rebinds the module's classes in place; leave it clean).
        monkeypatch.undo()
        importlib.reload(mod)


def test_local_backend_builds_in_process_lock():
    # Import fresh from the (possibly-reloaded) module so class identity matches.
    from warden.harness_api.task_lock import (
        InProcessTaskLock,
        build_task_lock,
    )

    assert isinstance(build_task_lock(HarnessApiConfig()), InProcessTaskLock)


def test_postgres_backend_builds_postgres_lock():
    from warden.harness_api.task_lock import (
        PostgresTaskLock,
        build_task_lock,
    )

    cfg = HarnessApiConfig(
        state=StateBackendConfig(backend="postgres", dsn="postgresql://x/y")
    )
    lock = build_task_lock(cfg)
    assert isinstance(lock, PostgresTaskLock)
    assert lock._pool is None  # noqa: SLF001 (DSN-deferred; connects on first hold)


def test_in_process_lock_serializes_same_key():
    """Two holders of the SAME (user,task) never overlap; the 2nd waits for the 1st."""
    from warden.harness_api.task_lock import InProcessTaskLock

    async def _run():
        lock = InProcessTaskLock()
        order: list[str] = []

        async def worker(tag: str):
            async with lock.hold("u", "t"):
                order.append(f"enter-{tag}")
                await asyncio.sleep(0.02)
                order.append(f"exit-{tag}")

        await asyncio.gather(worker("a"), worker("b"))
        # whichever entered first must fully exit before the other enters (no interleave)
        assert order[0].startswith("enter") and order[1].startswith("exit")
        assert order[2].startswith("enter") and order[3].startswith("exit")

    asyncio.run(_run())


def test_in_process_lock_different_keys_do_not_block():
    """Different (user,task) keys run concurrently (independent locks)."""
    from warden.harness_api.task_lock import InProcessTaskLock

    async def _run():
        lock = InProcessTaskLock()
        both_in = asyncio.Event()
        seen = {"n": 0}

        async def worker(task_id: str):
            async with lock.hold("u", task_id):
                seen["n"] += 1
                if seen["n"] == 2:
                    both_in.set()
                await asyncio.wait_for(both_in.wait(), timeout=1.0)

        # if the two keys blocked each other, both_in would never set → TimeoutError
        await asyncio.gather(worker("t1"), worker("t2"))
        assert seen["n"] == 2

    asyncio.run(_run())
