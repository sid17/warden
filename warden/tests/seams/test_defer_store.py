"""pre-07b durable — FileDeferStore: the file-backed pending-approval store
that survives process death (the durable-mode persistence).

Two store instances over the same root stand in for two processes: pass 1
records + ejects, the approver resolves, pass 2 (a fresh instance) reads the
decision. No shared memory — only the on-disk store.
"""

from __future__ import annotations

from pathlib import Path

from warden.seams.defer_store import (
    DurableDeferStore,
    FileDeferStore,
    content_key,
)


def test_record_then_read_across_instances(tmp_path: Path) -> None:
    """Durability: a record written by one instance is visible to another over
    the same root (the cross-process contract)."""
    s1 = FileDeferStore(tmp_path / "store")
    s1.record_pending("toolu_1", "Bash", {"command": "ls"}, session_id="sess_a")

    s2 = FileDeferStore(tmp_path / "store")  # "fresh process"
    pending = s2.read_pending()
    assert len(pending) == 1
    assert pending[0].tool_use_id == "toolu_1"
    assert pending[0].session_id == "sess_a"
    assert pending[0].status == "pending"


def test_resolve_then_get_decision_exact_id(tmp_path: Path) -> None:
    s1 = FileDeferStore(tmp_path / "store")
    s1.record_pending("toolu_2", "Write", {"path": "out.txt"}, session_id="s")
    # Out-of-band approver (could be a third process) resolves it.
    approver = FileDeferStore(tmp_path / "store")
    assert approver.resolve("toolu_2", allow=True, updated_input={"path": "out.txt"})

    resumer = FileDeferStore(tmp_path / "store")
    d = resumer.get_decision("toolu_2", "Write", {"path": "out.txt"})
    assert d is not None and d.allow is True
    assert d.updated_input == {"path": "out.txt"}


def test_get_decision_by_content_when_id_differs(tmp_path: Path) -> None:
    """Re-drive path: the resumed call has a NEW id, so the decision is found by
    content key, not the old id."""
    s = FileDeferStore(tmp_path / "store")
    s.record_pending("toolu_old", "Write", {"path": "a.txt"}, session_id="s")
    s.resolve("toolu_old", allow=False, reason="nope")

    # Resume with a DIFFERENT id but matching content.
    d = s.get_decision("toolu_fresh", "Write", {"path": "a.txt"})
    assert d is not None and d.allow is False
    assert d.reason == "nope"


def test_get_decision_is_idempotent_consume_once(tmp_path: Path) -> None:
    """A resolved decision is returned once then marked consumed — a duplicate
    resume / replayed node is a no-op (idempotency)."""
    s = FileDeferStore(tmp_path / "store")
    s.record_pending("toolu_3", "Bash", {"command": "x"}, session_id="s")
    s.resolve("toolu_3", allow=True)

    first = s.get_decision("toolu_3", "Bash", {"command": "x"})
    assert first is not None and first.allow is True
    second = s.get_decision("toolu_3", "Bash", {"command": "x"})
    assert second is None  # already consumed


def test_unresolved_pending_returns_no_decision(tmp_path: Path) -> None:
    s = FileDeferStore(tmp_path / "store")
    s.record_pending("toolu_4", "Bash", {"command": "x"}, session_id="s")
    assert s.get_decision("toolu_4", "Bash", {"command": "x"}) is None  # still pending


def test_resolve_unknown_id_returns_false(tmp_path: Path) -> None:
    s = FileDeferStore(tmp_path / "store")
    assert s.resolve("nope", allow=True) is False


def test_record_pending_does_not_clobber_resolved(tmp_path: Path) -> None:
    """A re-fired defer (pass-2 records again before reading) must not wipe an
    approver's resolution."""
    s = FileDeferStore(tmp_path / "store")
    s.record_pending("toolu_5", "Bash", {"command": "x"}, session_id="s")
    s.resolve("toolu_5", allow=True)
    s.record_pending("toolu_5", "Bash", {"command": "x"}, session_id="s")  # re-record
    d = s.get_decision("toolu_5", "Bash", {"command": "x"})
    assert d is not None and d.allow is True  # resolution survived


def test_content_key_stable_and_order_independent() -> None:
    assert content_key("W", {"a": 1, "b": 2}) == content_key("W", {"b": 2, "a": 1})
    assert content_key("W", {"a": 1}) != content_key("W", {"a": 2})


def test_file_store_satisfies_the_protocol(tmp_path: Path) -> None:
    """T9 (M6 §3.0): FileDeferStore is a structural DurableDeferStore, so the
    durable_http handler — which binds to the Protocol, not the impl — accepts it
    and will accept the future run_events/Postgres impl unchanged."""
    store = FileDeferStore(tmp_path / "store")
    assert isinstance(store, DurableDeferStore)  # runtime_checkable structural match
    # And the Protocol is not accidentally satisfied by an unrelated object.
    assert not isinstance(object(), DurableDeferStore)
