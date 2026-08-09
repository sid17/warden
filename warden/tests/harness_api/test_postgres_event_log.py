"""EXT-C3 — the Postgres event log must import WITHOUT asyncpg (lazy import) and the
``state.backend`` tier switch must route ``build_event_log`` to it.

Mirrors ``test_postgres_run_registry.py``: the hermetic suite never needs a DB driver,
so importing the module (and referencing the class) must NOT require asyncpg — it is
only touched at construction. The class is exercised for real against a live Postgres
in ``test_postgres_event_log_live.py`` (opt-in) and on the Docker bed, not here.
"""

from __future__ import annotations

import builtins
import importlib

import pytest

from warden.harness_api.config import HarnessApiConfig, StateBackendConfig
from warden.harness_api.event_log import RunEventLog, build_event_log


def test_postgres_event_log_module_imports_without_asyncpg(monkeypatch) -> None:
    """Simulate asyncpg being absent: the module still imports; only construction
    raises the clear install-hint ImportError."""
    real_import = builtins.__import__

    def _blocked_import(name, *args, **kwargs):
        if name == "asyncpg":
            raise ImportError("No module named 'asyncpg'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked_import)

    mod = importlib.import_module(
        "warden.harness_api.postgres_event_log"
    )
    importlib.reload(mod)
    assert hasattr(mod, "PostgresRunEventLog")

    # Construction touches asyncpg lazily → clear ImportError with the install hint.
    with pytest.raises(ImportError, match="asyncpg"):
        mod.PostgresRunEventLog(dsn="postgres://x")


# --- the tier switch routes build_event_log -----------------------------------


def test_local_backend_defaults_to_sqlite_event_log():
    """Default (state.backend='local') never builds the Postgres backend."""
    cfg = HarnessApiConfig()  # state defaults to local
    assert isinstance(build_event_log(cfg), RunEventLog)


def test_postgres_state_backend_builds_postgres_event_log():
    """state.backend='postgres' routes to PostgresRunEventLog (DSN-deferred, unconnected
    until init())."""
    from warden.harness_api.postgres_event_log import PostgresRunEventLog

    cfg = HarnessApiConfig(
        state=StateBackendConfig(backend="postgres", dsn="postgresql://x/y")
    )
    log = build_event_log(cfg)
    assert isinstance(log, PostgresRunEventLog)
    # DSN-deferred: no pool yet (init() connects at startup).
    assert log._pool is None  # noqa: SLF001 (whitebox: prove the deferred-connect shape)
