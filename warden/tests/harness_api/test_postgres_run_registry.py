"""EXT-C3 — the Postgres run registry must import WITHOUT asyncpg (lazy import) and
the ``state.backend`` tier switch must route ``build_run_registry`` to it.

Mirrors ``governance/test_postgres_ledger_import.py``: the hermetic suite never needs
a DB driver, so importing the module (and referencing the class) must NOT require
asyncpg — it is only touched at construction. The class is exercised for real against
a live Postgres in ``test_postgres_run_registry_live.py`` (opt-in) and on the Docker
bed, not here.
"""

from __future__ import annotations

import builtins
import importlib

import pytest

from warden.harness_api.config import HarnessApiConfig, StateBackendConfig
from warden.harness_api.run_registry import (
    InMemoryRunRegistry,
    JsonlRunRegistry,
    build_run_registry,
)


def test_postgres_run_registry_module_imports_without_asyncpg(monkeypatch) -> None:
    """Simulate asyncpg being absent: the module still imports; only construction
    raises the clear install-hint ImportError."""
    real_import = builtins.__import__

    def _blocked_import(name, *args, **kwargs):
        if name == "asyncpg":
            raise ImportError("No module named 'asyncpg'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked_import)

    mod = importlib.import_module(
        "warden.harness_api.postgres_run_registry"
    )
    importlib.reload(mod)
    assert hasattr(mod, "PostgresRunRegistry")

    # Construction touches asyncpg lazily → clear ImportError with the install hint.
    with pytest.raises(ImportError, match="asyncpg"):
        mod.PostgresRunRegistry(dsn="postgres://x")


# --- the tier switch routes build_run_registry --------------------------------


def test_local_backend_defaults_to_process_local():
    """Default (state.backend='local') never builds the Postgres backend."""
    cfg = HarnessApiConfig()  # state defaults to local
    assert isinstance(build_run_registry(cfg), InMemoryRunRegistry)


def test_local_jsonl_backend_unaffected_by_state_switch():
    cfg = HarnessApiConfig()
    cfg.run_registry.store_backend = "jsonl"
    assert isinstance(build_run_registry(cfg), JsonlRunRegistry)


def test_postgres_state_backend_builds_postgres_registry():
    """state.backend='postgres' routes to PostgresRunRegistry (DSN-deferred, unconnected
    until load()) — regardless of the local run_registry.store_backend value."""
    from warden.harness_api.postgres_run_registry import PostgresRunRegistry

    cfg = HarnessApiConfig(
        state=StateBackendConfig(backend="postgres", dsn="postgresql://x/y")
    )
    reg = build_run_registry(cfg)
    assert isinstance(reg, PostgresRunRegistry)
    # DSN-deferred: no pool yet (load() connects at startup).
    assert reg._pool is None  # noqa: SLF001 (whitebox: prove the deferred-connect shape)
