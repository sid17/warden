"""pre-07b · 3b/3c — checkpoint-and-inject defer mechanic (DEFER-1..4).

These exercise :class:`DeferRegistry` directly — no live SDK — driving a consult
in one task while a controller injects the decision by ``tool_use_id`` in
another. This is the deterministic, exact-id, nudge-free core the six-case probe
and M6 build on. Repo convention: ``asyncio.run`` (no pytest-asyncio).
"""

from __future__ import annotations

import asyncio

from warden.seams.defer import DeferRegistry, _content_key


def _run(coro):
    return asyncio.run(coro)


async def _consult(reg: DeferRegistry, tool, inp, tuid):
    return await reg.request_permission(tool, inp, "why", tool_use_id=tuid)


def test_capture_pause_then_inject_allow() -> None:
    """DEFER-1 capture + DEFER-2 pause + DEFER-3 inject (allow, exact id)."""

    async def scenario():
        reg = DeferRegistry()
        task = asyncio.create_task(_consult(reg, "Bash", {"command": "ls"}, "toolu_1"))
        await asyncio.sleep(0)  # let the consult park

        # DEFER-1: the pending call is captured with a non-null id.
        assert reg.pending_ids() == ["toolu_1"]
        pc = reg.get_pending("toolu_1")
        assert pc.tool_use_id == "toolu_1" and pc.tool_name == "Bash"
        # DEFER-2: it is PAUSED — the consult has not returned.
        assert not task.done()

        # DEFER-3: inject allow into the exact held call.
        assert reg.resolve("toolu_1", allow=True, updated_input={"command": "ls -la"})
        decision = await task
        assert decision.allowed is True
        assert decision.updated_input == {"command": "ls -la"}
        assert reg.pending_ids() == []  # drained

    _run(scenario())


def test_inject_deny() -> None:
    async def scenario():
        reg = DeferRegistry()
        task = asyncio.create_task(_consult(reg, "Bash", {"command": "rm -rf /"}, "toolu_2"))
        await asyncio.sleep(0)
        assert reg.resolve("toolu_2", allow=False, reason="nope")
        decision = await task
        assert decision.allowed is False
        assert decision.reason == "nope"

    _run(scenario())


def test_multi_approval_two_ids_resolved_independently() -> None:
    """DEFER-4 — two concurrent consults, two ids, resolved independently. The
    case a nudge could never handle."""

    async def scenario():
        reg = DeferRegistry()
        t1 = asyncio.create_task(_consult(reg, "Write", {"path": "a.txt"}, "toolu_a"))
        t2 = asyncio.create_task(_consult(reg, "Write", {"path": "b.txt"}, "toolu_b"))
        await asyncio.sleep(0)
        assert set(reg.pending_ids()) == {"toolu_a", "toolu_b"}

        # Allow a, deny b — independently.
        reg.resolve("toolu_a", allow=True)
        reg.resolve("toolu_b", allow=False)
        d1, d2 = await asyncio.gather(t1, t2)
        assert d1.allowed is True
        assert d2.allowed is False

    _run(scenario())


def test_resolve_unknown_id_returns_false() -> None:
    async def scenario():
        reg = DeferRegistry()
        assert reg.resolve("nope", allow=True) is False

    _run(scenario())


def test_no_id_falls_back_to_content_key() -> None:
    """A provider that carries no id still parks holdably under a content key."""

    async def scenario():
        reg = DeferRegistry()
        task = asyncio.create_task(_consult(reg, "Bash", {"command": "ls"}, None))
        await asyncio.sleep(0)
        key = _content_key("Bash", {"command": "ls"})
        assert reg.pending_ids() == [key]
        reg.resolve(key, allow=True)
        assert (await task).allowed is True

    _run(scenario())


def test_preseed_short_circuits_redrive_by_content() -> None:
    """Cross-process / Codex re-drive: pre-seed a decision; the re-reached call
    (fresh id) auto-resolves by content without a second hold."""

    async def scenario():
        reg = DeferRegistry()
        reg.preseed(
            "Write", {"path": "out.txt"}, allow=True, updated_input={"path": "out.txt"},
        )
        # The re-driven consult carries a DIFFERENT id but matching content.
        decision = await _consult(reg, "Write", {"path": "out.txt"}, "toolu_fresh_id")
        assert decision.allowed is True
        assert decision.updated_input == {"path": "out.txt"}
        assert reg.pending_ids() == []  # never parked — resolved immediately

    _run(scenario())


def test_on_pending_callback_fires_for_persistence() -> None:
    async def scenario():
        seen = []
        reg = DeferRegistry(on_pending=lambda pc: seen.append(pc.tool_use_id))
        task = asyncio.create_task(_consult(reg, "Bash", {"command": "ls"}, "toolu_p"))
        await asyncio.sleep(0)
        assert seen == ["toolu_p"]
        reg.resolve("toolu_p", allow=True)
        await task

    _run(scenario())
