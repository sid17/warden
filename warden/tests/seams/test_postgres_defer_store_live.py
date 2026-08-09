"""EXT-C3 — the Postgres defer store against a LIVE Postgres (opt-in).

Skipped unless ``WARDEN_TEST_POSTGRES_DSN`` is set — the default hermetic suite stays
DB-free (like the run registry). Run it locally against a throwaway DB to prove the real
SQL + the durability/isolation invariants:

    WARDEN_TEST_POSTGRES_DSN=postgresql://warden:warden@localhost:5432/warden_test \\
      uv run --no-sync python -m pytest \\
      warden/tests/seams/test_postgres_defer_store_live.py -q

Each test uses a unique ``run_id`` per store (uuid4) and DELETEs its own rows, so it is
safe to point at a shared DB without polluting it. Covers: record idempotency, resolve →
get_decision by exact id, get_decision by content_key (re-drive), consume-once, run
isolation (different run_ids don't cross by content_key), and cross-process sharing
(same run_id, two stores).
"""

from __future__ import annotations

import os
import uuid

import pytest

from warden.seams.postgres_defer_store import PostgresDeferStore

_DSN = os.environ.get("WARDEN_TEST_POSTGRES_DSN")

pytestmark = pytest.mark.skipif(
    not _DSN, reason="set WARDEN_TEST_POSTGRES_DSN to run the live Postgres tests"
)


def _store(run_id: str) -> PostgresDeferStore:
    return PostgresDeferStore(dsn=_DSN, run_id=run_id)


def _cleanup(store: PostgresDeferStore, run_id: str) -> None:
    store._connection().execute(  # noqa: SLF001 (test teardown)
        "DELETE FROM hitl_defers WHERE run_id = %s", (run_id,)
    )
    store.close()


def test_live_record_pending_idempotent():
    """A re-record of the same tool_use_id does not overwrite a resolved record."""
    run_id = f"c3-{uuid.uuid4()}"
    store = _store(run_id)
    try:
        rec = store.record_pending("t1", "Bash", {"cmd": "ls"}, "s1")
        assert rec.status == "pending"
        assert store.resolve("t1", allow=True, reason="ok") is True
        # Re-record: must NOT clobber the resolved decision back to pending.
        again = store.record_pending("t1", "Bash", {"cmd": "ls"}, "s1")
        assert again.status == "resolved"
        assert again.allow is True
    finally:
        _cleanup(store, run_id)


def test_live_resolve_then_get_by_exact_id():
    run_id = f"c3-{uuid.uuid4()}"
    store = _store(run_id)
    try:
        store.record_pending("t1", "Bash", {"cmd": "rm -rf x"}, "s1")
        assert store.resolve(
            "t1", allow=True, updated_input={"cmd": "rm x"}, reason="scoped"
        ) is True
        d = store.get_decision("t1", "Bash", {"cmd": "rm -rf x"})
        assert d is not None
        assert d.allow is True
        assert d.updated_input == {"cmd": "rm x"}
        assert d.reason == "scoped"
    finally:
        _cleanup(store, run_id)


def test_live_get_by_content_key_redrive():
    """Re-drive path: the resumed call has a NEW tool_use_id but the same
    (tool_name, input) → resolved decision is found by content_key."""
    run_id = f"c3-{uuid.uuid4()}"
    store = _store(run_id)
    try:
        store.record_pending("orig-id", "Bash", {"cmd": "ls -la"}, "s1")
        assert store.resolve("orig-id", allow=True, reason="ok") is True
        # A re-driven call mints a fresh id; look up by content (id unknown/None).
        d = store.get_decision(None, "Bash", {"cmd": "ls -la"})
        assert d is not None and d.allow is True
        # And a mismatched-id lookup still falls back to content_key.
        store.record_pending("orig2", "Grep", {"q": "x"}, "s1")
        store.resolve("orig2", allow=False, reason="no")
        d2 = store.get_decision("brand-new-id", "Grep", {"q": "x"})
        assert d2 is not None and d2.allow is False
    finally:
        _cleanup(store, run_id)


def test_live_consume_once():
    """The idempotency guarantee: a resolved decision is returned once (consume=True
    marks it consumed); a second get_decision returns None."""
    run_id = f"c3-{uuid.uuid4()}"
    store = _store(run_id)
    try:
        store.record_pending("t1", "Bash", {"cmd": "ls"}, "s1")
        store.resolve("t1", allow=True, reason="ok")
        first = store.get_decision("t1", "Bash", {"cmd": "ls"})
        assert first is not None and first.allow is True
        second = store.get_decision("t1", "Bash", {"cmd": "ls"})
        assert second is None
        # By content_key too: already consumed ⇒ None.
        assert store.get_decision(None, "Bash", {"cmd": "ls"}) is None
    finally:
        _cleanup(store, run_id)


def test_live_get_decision_consume_false_does_not_mark():
    """consume=False returns the decision without consuming it (peek)."""
    run_id = f"c3-{uuid.uuid4()}"
    store = _store(run_id)
    try:
        store.record_pending("t1", "Bash", {"cmd": "ls"}, "s1")
        store.resolve("t1", allow=True, reason="ok")
        peek = store.get_decision("t1", "Bash", {"cmd": "ls"}, consume=False)
        assert peek is not None
        # Still resolvable (not consumed).
        got = store.get_decision("t1", "Bash", {"cmd": "ls"})
        assert got is not None and got.allow is True
    finally:
        _cleanup(store, run_id)


def test_live_resolve_unknown_id_returns_false():
    run_id = f"c3-{uuid.uuid4()}"
    store = _store(run_id)
    try:
        assert store.resolve("nope", allow=True) is False
    finally:
        _cleanup(store, run_id)


def test_live_read_pending():
    run_id = f"c3-{uuid.uuid4()}"
    store = _store(run_id)
    try:
        store.record_pending("t1", "Bash", {"cmd": "a"}, "s1")
        store.record_pending("t2", "Bash", {"cmd": "b"}, "s1")
        store.resolve("t2", allow=True)  # resolved ⇒ no longer pending
        pending = store.read_pending()
        ids = {p.tool_use_id for p in pending}
        assert ids == {"t1"}
    finally:
        _cleanup(store, run_id)


def test_live_run_isolation_by_content_key():
    """Two stores with DIFFERENT run_ids must NOT see each other's records by
    content_key — the per-run scoping the file store gets from its run-scoped dir."""
    run_a = f"c3-{uuid.uuid4()}"
    run_b = f"c3-{uuid.uuid4()}"
    store_a = _store(run_a)
    store_b = _store(run_b)
    try:
        # Run A records + resolves a call with a given (tool_name, input).
        store_a.record_pending("shared-id", "Bash", {"cmd": "danger"}, "s1")
        store_a.resolve("shared-id", allow=True, reason="A-approved")
        # Run B has its OWN pending call with the SAME content but must not pick up
        # A's decision — neither by content_key nor by the (colliding) id.
        assert store_b.get_decision(None, "Bash", {"cmd": "danger"}) is None
        assert store_b.get_decision("shared-id", "Bash", {"cmd": "danger"}) is None
        # And A's read_pending never shows B's rows and vice-versa.
        store_b.record_pending("b-only", "Grep", {"q": "z"}, "s1")
        assert {p.tool_use_id for p in store_a.read_pending()} == set()
        assert {p.tool_use_id for p in store_b.read_pending()} == {"b-only"}
    finally:
        _cleanup(store_a, run_a)
        _cleanup(store_b, run_b)


def test_live_cross_process_sharing_same_run():
    """The multi-replica invariant: process 1 records+resolves; a SEPARATE process
    (second store, fresh connection, SAME run_id) resolves it — shared live state, not
    a per-process cache."""
    run_id = f"c3-{uuid.uuid4()}"
    parker = _store(run_id)   # "replica A": parks + is resolved
    resumer = _store(run_id)  # "replica B": a distinct process, same run
    try:
        parker.record_pending("t1", "Bash", {"cmd": "ls"}, "s1")
        parker.resolve("t1", allow=True, updated_input={"cmd": "ls -1"}, reason="ok")
        # Resumer never saw the record locally — reads it live from Postgres.
        d = resumer.get_decision("t1", "Bash", {"cmd": "ls"})
        assert d is not None
        assert d.allow is True
        assert d.updated_input == {"cmd": "ls -1"}
        # Consume-once holds ACROSS processes: parker now gets None.
        assert parker.get_decision("t1", "Bash", {"cmd": "ls"}) is None
    finally:
        _cleanup(parker, run_id)
        resumer.close()
