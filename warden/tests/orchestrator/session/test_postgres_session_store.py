"""EXT-C3 — the Postgres session store must import WITHOUT asyncpg (lazy import) and
the ``state.backend`` tier switch must route ``build_session_store`` to it.

Mirrors ``harness_api/test_postgres_run_registry.py``: the hermetic suite never needs
a DB driver, so importing the module (and referencing the class) must NOT require
asyncpg — it is only touched at construction. The class is exercised for real against
a live Postgres in ``test_postgres_session_store_live.py`` (opt-in), not here.

Also pins backward-compat: ``SessionIndex()`` / ``SessionManager()`` with no args MUST
still default to the local sqlite ``SessionDB`` (many callers/tests rely on it).
"""

from __future__ import annotations

import builtins
import importlib

import pytest

from warden.harness_api.config import HarnessApiConfig, StateBackendConfig
from warden.orchestrator.session.db import SessionDB, build_session_store
from warden.orchestrator.session.index import SessionIndex
from warden.orchestrator.session.manager import SessionManager


def test_postgres_session_store_module_imports_without_asyncpg(monkeypatch) -> None:
    """Simulate asyncpg being absent: the module still imports; only construction
    raises the clear install-hint ImportError."""
    real_import = builtins.__import__

    def _blocked_import(name, *args, **kwargs):
        if name == "asyncpg":
            raise ImportError("No module named 'asyncpg'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked_import)

    mod = importlib.import_module(
        "warden.orchestrator.session.postgres_db"
    )
    importlib.reload(mod)
    assert hasattr(mod, "PostgresSessionStore")

    # Construction touches asyncpg lazily → clear ImportError with the install hint.
    with pytest.raises(ImportError, match="asyncpg"):
        mod.PostgresSessionStore(dsn="postgres://x")


# --- the tier switch routes build_session_store -------------------------------


def test_local_backend_defaults_to_session_db():
    """Default (state.backend='local') never builds the Postgres backend."""
    cfg = HarnessApiConfig()  # state defaults to local
    assert isinstance(build_session_store(cfg), SessionDB)


def test_postgres_state_backend_builds_postgres_store():
    """state.backend='postgres' routes to PostgresSessionStore (DSN-deferred,
    unconnected until init())."""
    from warden.orchestrator.session.postgres_db import PostgresSessionStore

    cfg = HarnessApiConfig(
        state=StateBackendConfig(backend="postgres", dsn="postgresql://x/y")
    )
    store = build_session_store(cfg)
    assert isinstance(store, PostgresSessionStore)
    # DSN-deferred: no pool yet (init() connects at startup).
    assert store._pool is None  # noqa: SLF001 (whitebox: prove the deferred shape)


# --- backward compatibility: no-arg construction stays local ------------------


def test_session_index_default_is_session_db():
    """``SessionIndex()`` with no args MUST still wrap the local sqlite SessionDB."""
    idx = SessionIndex()
    assert isinstance(idx._db, SessionDB)  # noqa: SLF001


def test_session_manager_default_is_local_index():
    """``SessionManager()`` with no args MUST still wrap a local SessionDB index."""
    mgr = SessionManager()
    assert isinstance(mgr._index, SessionIndex)  # noqa: SLF001
    assert isinstance(mgr._index._db, SessionDB)  # noqa: SLF001


def test_session_index_from_config_local_is_session_db():
    """``from_config`` with the default (local) cfg still wraps a SessionDB."""
    idx = SessionIndex.from_config(HarnessApiConfig())
    assert isinstance(idx._db, SessionDB)  # noqa: SLF001


def test_session_index_from_config_postgres_is_postgres_store():
    """``from_config`` with state.backend='postgres' wraps the Postgres store."""
    from warden.orchestrator.session.postgres_db import PostgresSessionStore

    cfg = HarnessApiConfig(
        state=StateBackendConfig(backend="postgres", dsn="postgresql://x/y")
    )
    idx = SessionIndex.from_config(cfg)
    assert isinstance(idx._db, PostgresSessionStore)  # noqa: SLF001


def test_session_manager_from_config_postgres_is_postgres_store():
    """``SessionManager.from_config`` threads the tier switch through to the store."""
    from warden.orchestrator.session.postgres_db import PostgresSessionStore

    cfg = HarnessApiConfig(
        state=StateBackendConfig(backend="postgres", dsn="postgresql://x/y")
    )
    mgr = SessionManager.from_config(cfg)
    assert isinstance(mgr._index._db, PostgresSessionStore)  # noqa: SLF001
