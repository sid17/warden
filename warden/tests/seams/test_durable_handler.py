"""pre-07b durable — DurableDeferHandler (the OH/Codex re-drive path).

Never holds a future: an unresolved consult records the pending call + returns a
deny-to-end (eject); a resolved consult returns the stored decision (inject).
Backed by FileDeferStore, so the eject and the inject can be in different
processes.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from warden.seams.defer import DurableDeferHandler
from warden.seams.defer_store import FileDeferStore


def _run(coro):
    return asyncio.run(coro)


def test_unresolved_ejects_and_records(tmp_path: Path) -> None:
    store = FileDeferStore(tmp_path / "s")
    h = DurableDeferHandler(store)
    h.session_id = "sess_1"
    d = _run(h.request_permission("Bash", {"command": "ls"}, "why", tool_use_id="toolu_1"))
    assert d.allowed is False          # deny-to-end (eject)
    assert h.last_action == "ejected"
    pending = store.read_pending()
    assert len(pending) == 1 and pending[0].tool_use_id == "toolu_1"
    assert pending[0].session_id == "sess_1"


def test_resolved_allow_injects(tmp_path: Path) -> None:
    store = FileDeferStore(tmp_path / "s")
    # pass 1 ejected + recorded (different handler instance = different "process")
    _run(DurableDeferHandler(store).request_permission(
        "Write", {"path": "out.txt"}, "why", tool_use_id="toolu_2"))
    # approver resolves out-of-band
    store.resolve("toolu_2", allow=True, updated_input={"path": "out.txt"})
    # pass 2: a fresh handler over the same store injects the decision
    h2 = DurableDeferHandler(store)
    d = _run(h2.request_permission("Write", {"path": "out.txt"}, "why", tool_use_id="toolu_2"))
    assert d.allowed is True and h2.last_action == "injected"
    assert d.updated_input == {"path": "out.txt"}


def test_resolved_deny_injects_block(tmp_path: Path) -> None:
    store = FileDeferStore(tmp_path / "s")
    _run(DurableDeferHandler(store).request_permission(
        "Write", {"path": "x"}, "why", tool_use_id="toolu_3"))
    store.resolve("toolu_3", allow=False, reason="no")
    d = _run(DurableDeferHandler(store).request_permission(
        "Write", {"path": "x"}, "why", tool_use_id="toolu_3"))
    assert d.allowed is False and d.reason == "no"


def test_resolve_by_content_when_redriven_id_differs(tmp_path: Path) -> None:
    """Re-drive: pass 2's resumed call has a NEW id; the decision is matched by
    content (tool_name + input), the honest OH/Codex path."""
    store = FileDeferStore(tmp_path / "s")
    _run(DurableDeferHandler(store).request_permission(
        "Write", {"path": "a.txt"}, "why", tool_use_id="toolu_old"))
    store.resolve("toolu_old", allow=True)
    # Fresh id on re-drive, matching content.
    h2 = DurableDeferHandler(store)
    d = _run(h2.request_permission(
        "Write", {"path": "a.txt"}, "why", tool_use_id="toolu_new"))
    assert d.allowed is True and h2.last_action == "injected"
