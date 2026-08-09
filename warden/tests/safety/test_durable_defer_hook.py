"""pre-07b durable — Claude native-defer PreToolUse hook.

Unresolved → permissionDecision:"defer" (+ records the pending call); resolved →
allow/deny injected (the exact-id resume path). Fired with a fake hook_input, no
live SDK. Also checks ClaudeSession installs the durable hook and SKIPS the warm
custom gate in durable mode.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from warden.config.models import DurableDeferConfig
from warden.safety.permissions.durable_defer_hook import build_durable_defer_hook
from warden.seams.custom_tools import CustomTool
from warden.seams.defer_store import FileDeferStore


def _cb(store):
    return build_durable_defer_hook(store)["PreToolUse"][0]


def _fire(store, hook_input, tuid="toolu_x"):
    return asyncio.run(_cb(store).hooks[0](hook_input, tuid, {"signal": None}))


def _pd(result):
    return result.get("hookSpecificOutput", {}).get("permissionDecision")


def test_unresolved_defers_and_records(tmp_path: Path) -> None:
    store = FileDeferStore(tmp_path / "s")
    result = _fire(
        store,
        {"tool_name": "Bash", "tool_input": {"command": "ls"},
         "session_id": "sess_1"},
        tuid="toolu_1",
    )
    assert _pd(result) == "defer"
    pending = store.read_pending()
    assert len(pending) == 1
    assert pending[0].tool_use_id == "toolu_1"
    assert pending[0].session_id == "sess_1"


def test_on_defer_called_when_a_tool_defers(tmp_path: Path) -> None:
    """The on_defer signal fires (tool_name, tuid) exactly when a NEW pending defer
    is recorded — the session uses it to interrupt the turn (the SDK's defer doesn't
    stop the agent loop)."""
    store = FileDeferStore(tmp_path / "s")
    seen: list[tuple] = []
    hook = build_durable_defer_hook(store, on_defer=lambda n, t: seen.append((n, t)))
    cb = hook["PreToolUse"][0].hooks[0]
    result = asyncio.run(
        cb({"tool_name": "confirm_landscape", "tool_input": {}}, "toolu_g", {"signal": None})
    )
    assert _pd(result) == "defer"
    assert seen == [("confirm_landscape", "toolu_g")]


def test_on_defer_not_called_on_resolved_inject(tmp_path: Path) -> None:
    """A resolved (allow/deny) call injects without re-deferring, so on_defer must
    NOT fire on the resume re-fire — else the session would interrupt a resume."""
    store = FileDeferStore(tmp_path / "s")
    seen: list[tuple] = []
    hook = build_durable_defer_hook(store, on_defer=lambda n, t: seen.append((n, t)))
    cb = hook["PreToolUse"][0].hooks[0]
    # first pass defers (fires on_defer), then resolve, then re-fire injects (no defer)
    asyncio.run(cb({"tool_name": "confirm_landscape", "tool_input": {}}, "toolu_g", {"signal": None}))
    store.resolve("toolu_g", allow=True)
    seen.clear()
    result = asyncio.run(
        cb({"tool_name": "confirm_landscape", "tool_input": {}}, "toolu_g", {"signal": None})
    )
    assert _pd(result) == "allow"
    assert seen == []  # resolved inject must not re-signal an interrupt


def test_resolved_allow_injects_with_updated_input(tmp_path: Path) -> None:
    store = FileDeferStore(tmp_path / "s")
    _fire(store, {"tool_name": "Write", "tool_input": {"path": "o.txt"}}, tuid="toolu_2")
    store.resolve("toolu_2", allow=True, updated_input={"path": "o.txt"})
    result = _fire(store, {"tool_name": "Write", "tool_input": {"path": "o.txt"}}, tuid="toolu_2")
    assert _pd(result) == "allow"
    # camelCase per the SDK TypedDict.
    assert result["hookSpecificOutput"]["updatedInput"] == {"path": "o.txt"}


def test_resolved_deny_injects_block(tmp_path: Path) -> None:
    store = FileDeferStore(tmp_path / "s")
    _fire(store, {"tool_name": "Write", "tool_input": {"path": "x"}}, tuid="toolu_3")
    store.resolve("toolu_3", allow=False, reason="no")
    result = _fire(store, {"tool_name": "Write", "tool_input": {"path": "x"}}, tuid="toolu_3")
    assert _pd(result) == "deny"


def test_hook_fails_closed_on_error(tmp_path: Path) -> None:
    class _Boom:
        def get_decision(self, *a, **k):
            raise RuntimeError("boom")

    result = _fire(_Boom(), {"tool_name": "Bash", "tool_input": {}})
    assert _pd(result) == "deny"
    assert "fail-closed" in result["hookSpecificOutput"]["permissionDecisionReason"]


# --- EXT-G1: checker-aware defer (only the confirm-listed tool pauses) --------


def _gate_check():
    """A (tool_name, tool_input) -> PermissionDecision from a real checker with the
    landscape-gate manifest: mode: auto + confirm:[Write]."""
    from warden.safety.permissions.checker import PermissionChecker
    from warden.workspace.workflow.permissions import Permissions, ToolAccess

    checker = PermissionChecker.from_workflow_permissions(
        Permissions(mode="auto", tool_access=ToolAccess(confirm=["Write"]))
    )
    return lambda name, inp: checker.evaluate(name, inp or {})


def _fire_checked(store, hook_input, check, tuid="toolu_x"):
    cb = build_durable_defer_hook(store, permission_check=check)["PreToolUse"][0]
    return asyncio.run(cb.hooks[0](hook_input, tuid, {"signal": None}))


def test_checker_aware_auto_allowed_tool_does_not_defer(tmp_path: Path) -> None:
    """A non-confirm tool (Bash under mode:auto) must NOT defer — it proceeds via
    can_use_tool (empty hook output), and nothing is recorded pending."""
    store = FileDeferStore(tmp_path / "s")
    result = _fire_checked(
        store, {"tool_name": "Bash", "tool_input": {"command": "ls"}},
        _gate_check(), tuid="toolu_bash",
    )
    assert result == {}  # no permissionDecision → normal flow → runs
    assert store.read_pending() == []  # not deferred


def test_checker_aware_read_only_tool_does_not_defer(tmp_path: Path) -> None:
    store = FileDeferStore(tmp_path / "s")
    result = _fire_checked(
        store, {"tool_name": "Read", "tool_input": {"file_path": "x"}},
        _gate_check(), tuid="toolu_read",
    )
    assert result == {}
    assert store.read_pending() == []


def test_checker_aware_confirm_listed_tool_defers(tmp_path: Path) -> None:
    """The confirm-listed tool (Write) DOES defer + records pending."""
    store = FileDeferStore(tmp_path / "s")
    result = _fire_checked(
        store, {"tool_name": "Write", "tool_input": {"file_path": "o.txt"}},
        _gate_check(), tuid="toolu_write",
    )
    assert _pd(result) == "defer"
    assert len(store.read_pending()) == 1


def test_checker_aware_denied_tool_denies_not_defers(tmp_path: Path) -> None:
    """A hard-denied tool (a sensitive path) denies immediately (a hard deny aborts,
    it must not pause) and records nothing."""
    store = FileDeferStore(tmp_path / "s")
    result = _fire_checked(
        store, {"tool_name": "Read", "tool_input": {"file_path": "/home/u/.ssh/id_rsa"}},
        _gate_check(), tuid="toolu_secret",
    )
    assert _pd(result) == "deny"
    assert store.read_pending() == []


def test_no_checker_defers_every_tool_legacy(tmp_path: Path) -> None:
    """Without a checker (permission_check=None), the legacy M6 behavior holds:
    every tool defers."""
    store = FileDeferStore(tmp_path / "s")
    result = _fire(store, {"tool_name": "Bash", "tool_input": {}}, tuid="toolu_legacy")
    assert _pd(result) == "defer"


# --- Fix #1/#2/#3: checker-first (no disk for auto-allow) + timeout + cancel safety ---


def test_checker_aware_auto_allow_never_reads_store() -> None:
    """PERF (#1): an auto-allowed tool must return WITHOUT touching the durable store.
    The per-call store read (on a slow bind-mounted volume) is what tripped the SDK hook
    timeout under the research-phase tool flood. A store that raises on ANY access proves
    the auto-allow path never reaches it — the checker verdict alone decides."""

    class _ExplodingStore:
        def get_decision(self, *a, **k):
            raise AssertionError("auto-allowed tool must not READ the defer store")

        def record_pending(self, *a, **k):
            raise AssertionError("auto-allowed tool must not WRITE the defer store")

    result = _fire_checked(
        _ExplodingStore(), {"tool_name": "Bash", "tool_input": {"command": "ls"}},
        _gate_check(), tuid="toolu_auto",
    )
    assert result == {}  # auto-allow, zero disk


def test_checker_aware_resolved_confirm_tool_injects(tmp_path: Path) -> None:
    """The REAL resume path (checker wired): the confirm-listed tool defers, the approver
    resolves, and the re-fire reads the store + injects allow — the reorder still reaches
    the store for a requires_confirmation tool (it just skips it for everything else)."""
    store = FileDeferStore(tmp_path / "s")
    check = _gate_check()
    r1 = _fire_checked(
        store, {"tool_name": "Write", "tool_input": {"file_path": "o"}}, check, tuid="toolu_r",
    )
    assert _pd(r1) == "defer"
    store.resolve("toolu_r", allow=True)
    r2 = _fire_checked(
        store, {"tool_name": "Write", "tool_input": {"file_path": "o"}}, check, tuid="toolu_r",
    )
    assert _pd(r2) == "allow"


def test_hook_cancelled_fails_closed() -> None:
    """#3: a hook-timeout CancelledError (a BaseException) must fail CLOSED (deny), not
    escape and surface as "Error in hook callback" (which crashes the permission stream)."""

    class _CancelStore:
        def get_decision(self, *a, **k):
            raise asyncio.CancelledError()

    result = _fire(_CancelStore(), {"tool_name": "Bash", "tool_input": {}})
    assert _pd(result) == "deny"
    assert "fail-closed" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_hook_timeout_is_generous(tmp_path: Path) -> None:
    """#2: the hook timeout was raised off the tight 5s that tripped under load."""
    matcher = build_durable_defer_hook(FileDeferStore(tmp_path / "s"))["PreToolUse"][0]
    assert matcher.timeout >= 30.0


# --- ClaudeSession install behavior in durable mode --------------------------


class _Opts:
    def __init__(self, hooks=None):
        self.hooks = hooks


def _ping():
    return CustomTool(name="ping", description="x",
                      input_schema={"type": "object", "properties": {}}, handler=lambda **_: "")


def test_session_installs_durable_hook_and_skips_warm_gate(tmp_path: Path) -> None:
    from warden.providers.claude.session import ClaudeSession

    async def _cut(*a):  # a warm seam that must NOT be gated in durable mode
        raise AssertionError("warm gate should be skipped in durable mode")

    sess = ClaudeSession(
        repo_path=Path("."),
        can_use_tool=_cut,
        custom_tools=[_ping()],
        durable_defer=DurableDeferConfig(enabled=True, store_root=str(tmp_path / "store")),
    )
    opts = _Opts(hooks=None)
    sess.install_hooks(opts)
    matchers = (opts.hooks or {}).get("PreToolUse", [])
    # durable hook installed (matcher None); warm custom gate (^mcp__...) skipped.
    assert len(matchers) == 1
    assert matchers[0].matcher is None
