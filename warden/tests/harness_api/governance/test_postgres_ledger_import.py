"""M2 3d — the Postgres ledger must be importable WITHOUT asyncpg (lazy import).

The hermetic suite runs against the in-memory ledger; the Postgres ledger's asyncpg
dependency is an OPTIONAL extra. This test guards the lazy-import contract: importing
the module (and referencing the class) must NOT require asyncpg — it is only touched
when the ledger is actually constructed / used. The class is exercised for real on the
Docker bed, not here, so there are no live-DB tests.
"""

from __future__ import annotations

import builtins
import importlib


def test_postgres_ledger_module_imports_without_asyncpg(monkeypatch) -> None:
    """Simulate asyncpg being absent: the module still imports; only construction
    raises the clear install-hint ImportError."""
    real_import = builtins.__import__

    def _blocked_import(name, *args, **kwargs):
        if name == "asyncpg":
            raise ImportError("No module named 'asyncpg'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked_import)

    # Fresh import of the module under the blocked-asyncpg environment.
    mod = importlib.import_module(
        "warden.harness_api.governance.postgres_ledger"
    )
    importlib.reload(mod)
    assert hasattr(mod, "PostgresReservationLedger")

    # Construction / connect touches asyncpg lazily → clear ImportError with hint.
    import pytest

    with pytest.raises(ImportError, match="asyncpg"):
        mod.PostgresReservationLedger(pool=object())


def test_postgres_ledger_reexported() -> None:
    from warden.harness_api.governance import PostgresReservationLedger

    assert PostgresReservationLedger.__name__ == "PostgresReservationLedger"
