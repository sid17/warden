"""EXT-C3 — the Postgres defer store must import WITHOUT psycopg (lazy import) and
the ``state.backend`` tier switch must route ``build_defer_store`` to it.

Mirrors ``test_postgres_run_registry.py``: the hermetic suite never needs a DB driver,
so importing the module (and referencing the class) must NOT require psycopg — it is
only touched at construction. Real SQL is exercised in
``test_postgres_defer_store_live.py`` (opt-in) and on the Docker bed, not here.

The routing test needs no DB because ``PostgresDeferStore`` connects LAZILY (first
verb): construction only imports the driver + records the DSN/run_id, so asserting the
type is DB-free.
"""

from __future__ import annotations

import builtins
import importlib

import pytest

from warden.harness_api.config import HarnessApiConfig, StateBackendConfig
from warden.seams.defer_store import (
    FileDeferStore,
    build_defer_store,
)


def test_postgres_defer_store_module_imports_without_psycopg(monkeypatch) -> None:
    """Simulate psycopg being absent: the module still imports; only construction
    raises the clear install-hint ImportError."""
    real_import = builtins.__import__

    def _blocked_import(name, *args, **kwargs):
        if name == "psycopg":
            raise ImportError("No module named 'psycopg'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked_import)

    mod = importlib.import_module(
        "warden.seams.postgres_defer_store"
    )
    importlib.reload(mod)
    assert hasattr(mod, "PostgresDeferStore")

    # Construction touches psycopg lazily → clear ImportError with the install hint.
    with pytest.raises(ImportError, match="psycopg"):
        mod.PostgresDeferStore(dsn="postgresql://x/y", run_id="r1")


# --- the tier switch routes build_defer_store ---------------------------------


def test_local_backend_builds_file_store(tmp_path):
    """Default (state.backend='local') → the process-local FileDeferStore."""
    cfg = HarnessApiConfig()  # state defaults to local
    store = build_defer_store(cfg, run_id="r1", local_root=tmp_path)
    assert isinstance(store, FileDeferStore)


def test_postgres_state_backend_builds_postgres_store(tmp_path):
    """state.backend='postgres' routes to PostgresDeferStore (DSN-carried, unconnected
    until first verb — so this assertion needs no DB)."""
    from warden.seams.postgres_defer_store import PostgresDeferStore

    cfg = HarnessApiConfig(
        state=StateBackendConfig(backend="postgres", dsn="postgresql://x/y")
    )
    store = build_defer_store(cfg, run_id="r1", local_root=tmp_path)
    assert isinstance(store, PostgresDeferStore)
    # Lazy-connect: no live connection yet (first verb connects).
    assert store._conn is None  # noqa: SLF001 (whitebox: prove deferred-connect shape)
    assert store._run_id == "r1"  # noqa: SLF001


def test_file_store_satisfies_protocol_still(tmp_path):
    """Guard: the file store the switch returns is a full DurableDeferStore (record →
    resolve → get_decision consume-once), unchanged by this task."""
    store = build_defer_store(HarnessApiConfig(), run_id="r1", local_root=tmp_path)
    store.record_pending("t1", "Bash", {"cmd": "ls"}, "s1")
    assert store.resolve("t1", allow=True, reason="ok") is True
    d = store.get_decision("t1", "Bash", {"cmd": "ls"})
    assert d is not None and d.allow is True
    # consume-once
    assert store.get_decision("t1", "Bash", {"cmd": "ls"}) is None
