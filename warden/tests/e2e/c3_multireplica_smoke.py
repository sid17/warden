"""EXT-C3 — the two-container multi-replica Docker bed driver (credential-free).

The culminating integration proof for EXT-C3 (C3a shared-Postgres stores + C3b the
distributed ``(user, task)`` lease + C3c cross-replica cold-resume of a paused run).
Two harness containers share ONE ephemeral Postgres (``WARDEN_POSTGRES_DSN``); this
one driver runs in each, in a different MODE, so ``run.sh`` can orchestrate the three
capabilities across the pair:

(a) VISIBILITY   — a run created on container A is resolvable (identity + reconstructed
    view) from container B, which never held it in memory.
(b) EXCLUSION    — two containers cannot both hold one ``(user, task)`` lease: B's claim
    is refused while A holds it, and succeeds once A releases.
(c) COLD-RESUME  — a run PAUSED for durable HITL on container A is confirmed on B and
    completes there (the tool runs ON B).

No model, no cloud credential: the ChatAPI is a tiny INLINE mock whose durable-defer
handler is built over the SAME shared Postgres defer store the Runner reads (via
``build_defer_store``), so the pause the Runner records is the pause the mock ejects on
and the decision B injects is the one the mock re-reads — deterministic, offline.

Modes (``python -m warden.tests.e2e.c3_multireplica_smoke <mode> [args]``):

    a-seed                 — container A: create a VISIBLE run + a PAUSED HITL run;
                             print ``VISIBLE_RUN=<id>`` and ``PAUSED_RUN=<id>`` then exit
                             (A "dies" — its memory is gone).
    b-verify <vis> <paused>— container B: prove (a) visibility of <vis> and (c) cold-
                             resume of <paused>. GATE: PASS iff both hold.
    lock-hold <user> <task> [hold_s]
                           — hold the distributed lease for ``hold_s`` (default 30) then
                             release; prints ``LEASE_HELD`` once acquired. Backgrounded by
                             run.sh so B can attempt the same key concurrently.
    lock-try <user> <task> [window_s]
                           — try to claim the lease within ``window_s`` (default 4);
                             prints ``CLAIM=REFUSED`` (held elsewhere) or ``CLAIM=OK``.
    exclusion <user> <task>— self-contained (b) proof in ONE process against the shared
                             DB (belt-and-braces, no cross-container timing): hold the
                             lease in a task, assert a second owner is REFUSED, release,
                             assert it then succeeds. GATE: PASS/FAIL printed.

Every mode reads ``WARDEN_POSTGRES_DSN`` from the env. Exit 0 = PASS.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from warden.harness_api.config import (
    GovernanceConfig,
    HarnessApiConfig,
    StateBackendConfig,
)
from warden.harness_api.credentials.keys import KeyRegistry
from warden.harness_api.runner import Runner
from warden.harness_api.schemas import RunSpec, Sink
from warden.harness_api.task_lock import PostgresTaskLock
from warden.schemas.events import (
    CompletionEvent,
    MessageEvent,
    OrchestratorEvent,
    SessionCreatedEvent,
)

# A single managed key so the Runner can resolve auth_env for the mock (never a real
# secret — an env ref to a fake var, exactly as the hermetic C3 tests do).
_KEYS = KeyRegistry.from_config(
    {"keys": {"k1": {"provider": "claude", "secret_env": "S1"}},
     "users": {"u1": {"key_id": "k1", "budget_usd": 100.0}}},
    secrets={"S1": "sk-1"},
)

_TOOL_ID = "toolu_write_1"
_TOOL_INPUT = {"path": "out.txt", "content": "hello"}


def _dsn() -> str:
    dsn = os.environ.get("WARDEN_POSTGRES_DSN")
    if not dsn:
        print("FAIL — WARDEN_POSTGRES_DSN is unset (need the shared Postgres DSN).")
        raise SystemExit(2)
    return dsn


def _pg_cfg() -> HarnessApiConfig:
    """Every shared store on Postgres via the ONE tier switch; governance off (this
    gate proves the state backends + the HITL cold-resume, not the Governor); durable
    HITL handler so a paused run parks on ``requires_action``."""
    cfg = HarnessApiConfig(
        governance=GovernanceConfig(enabled=False),
        state=StateBackendConfig(backend="postgres", dsn=_dsn()),
    )
    cfg.engine.permissions.handler = "durable_http"
    return cfg


def _spec(user: str, task: str) -> RunSpec:
    return RunSpec(
        user_id=user, task_id=task, provider="claude", model="claude-opus-4-8",
        input={"prompt": "write the file"}, sink=Sink(type="sse"),
    )


# --- the inline durable-HITL mock (Postgres-store-aware) ----------------------


class _DurableMockChatAPI:
    """A model-free ChatAPI that consults the injected durable handler for one
    ``Write`` tool. Crucially, its ``set_durable_defer`` builds the handler over the
    SHARED store the Runner selected (``build_defer_store`` → PostgresDeferStore when
    the tier switch is postgres) — NOT a hardcoded FileDeferStore — so the mock's
    eject and the Runner's pause detection see the SAME rows, and a decision recorded
    by B's ``confirm`` is the decision this handler re-reads on the re-drive.

    First pass: no decision recorded → the handler ejects (deny-to-end) → the turn
    ends with no result → the run parks (``requires_action``). Re-drive after an
    approve: the handler injects allow → the tool "runs" (tracker bump) → a result.
    """

    def __init__(self, *, spec: Any, cfg: HarnessApiConfig, tracker: dict) -> None:
        self._spec = spec
        self._cfg = cfg
        self._sid = f"sess-{spec.task_id}"
        self._handler = None
        self._tracker = tracker
        tracker.setdefault("consults", [])

    def set_durable_defer(self, dd) -> None:
        # dd.store_root is ``.../hitl_defer/<run_id>`` — the basename IS the run_id, so
        # build the SAME store the Runner's _maybe_pause_durable / confirm read.
        from warden.seams.defer import DurableDeferHandler
        from warden.seams.defer_store import build_defer_store

        run_id = Path(dd.store_root).name
        store = build_defer_store(self._cfg, run_id, dd.store_root)
        self._handler = DurableDeferHandler(store)

    def set_permission_handler(self, handler) -> None:  # pragma: no cover - claude-only
        self._handler = handler

    async def init(self) -> None:
        return None

    async def send(
        self, content: str, *, session_id: str | None = None,
        workflow: str | None = None,
    ) -> AsyncIterator[OrchestratorEvent]:
        sid = session_id or self._sid
        yield SessionCreatedEvent(session_id=sid, resumed=session_id is not None)
        decision = await self._handler.request_permission(
            "Write", dict(_TOOL_INPUT), "write a file", tool_use_id=_TOOL_ID,
        )
        self._tracker["consults"].append(decision.source)
        if "defer" in decision.source:
            return  # ejected → the run parks (requires_action)
        if decision.allowed:
            self._tracker["ran_tool"] = self._tracker.get("ran_tool", 0) + 1
            yield MessageEvent(kind="tool_use", content={"toolName": "Write"},
                               session_id=sid)
            result = "wrote out.txt"
        else:
            result = f"blocked: {decision.reason}"
        yield MessageEvent(
            kind="status",
            content={"subtype": "result", "result": result,
                     "usage": {"input_tokens": 5, "output_tokens": 5}},
            session_id=sid,
        )
        yield CompletionEvent(session_id=sid)

    async def close(self) -> None:
        return None


def _durable_factory(cfg: HarnessApiConfig, tracker: dict):
    def factory(spec: Any, auth_env: dict | None) -> _DurableMockChatAPI:
        return _DurableMockChatAPI(spec=spec, cfg=cfg, tracker=tracker)

    return factory


def _visible_factory(cfg: HarnessApiConfig):
    """A no-tool mock that just completes — used for the VISIBLE (non-paused) run so
    (a) exercises a normal succeeded run created on A + reconstructed on B."""
    def factory(spec: Any, auth_env: dict | None):
        return _CompleteMockChatAPI(spec=spec)

    return factory


class _CompleteMockChatAPI:
    def __init__(self, *, spec: Any) -> None:
        self._sid = f"sess-{spec.task_id}"

    def set_durable_defer(self, dd) -> None:  # inert: this run makes no tool call
        return None

    async def init(self) -> None:
        return None

    async def send(
        self, content: str, *, session_id: str | None = None,
        workflow: str | None = None,
    ) -> AsyncIterator[OrchestratorEvent]:
        sid = session_id or self._sid
        yield SessionCreatedEvent(session_id=sid, resumed=session_id is not None)
        yield MessageEvent(
            kind="status",
            content={"subtype": "result", "result": "done",
                     "usage": {"input_tokens": 7, "output_tokens": 11}},
            session_id=sid,
        )
        yield CompletionEvent(session_id=sid)

    async def close(self) -> None:
        return None


# --- Mode a-seed: container A seeds a visible run + a paused HITL run ----------


async def _a_seed() -> int:
    cfg = _pg_cfg()

    # (1) A VISIBLE run: runs to completion on the shared Postgres stores.
    vis = Runner(cfg, keys=_KEYS, chat_api_factory=_visible_factory(cfg))
    await vis.init()
    visible_run = vis.submit(_spec("u1", "c3-visible"))
    await vis.task_for(visible_run)
    if vis.get(visible_run).status != "succeeded":
        print(f"FAIL — visible run did not succeed on A: {vis.get(visible_run).status}")
        return 1
    await vis.aclose()

    # (2) A PAUSED HITL run: parks on requires_action, its ask durable in Postgres.
    tracker: dict = {}
    paused = Runner(cfg, keys=_KEYS, chat_api_factory=_durable_factory(cfg, tracker))
    await paused.init()
    paused_run = paused.submit(_spec("u1", "c3-paused"))
    await paused.task_for(paused_run)
    status = paused.get(paused_run).status
    if status != "requires_action":
        print(f"FAIL — paused run is {status!r}, expected requires_action.")
        return 1
    if "ran_tool" in tracker:
        print("FAIL — the tool ran on A; it must only run on B after confirm.")
        return 1
    await paused.aclose()  # A "dies" — nothing left in memory

    print(f"VISIBLE_RUN={visible_run}")
    print(f"PAUSED_RUN={paused_run}")
    print("A_SEED: OK — one visible run + one paused HITL run written to Postgres.")
    return 0


# --- Mode b-verify: container B proves visibility + cold-resume ---------------


async def _b_verify(visible_run: str, paused_run: str) -> int:
    cfg = _pg_cfg()
    ok = True

    # (a) VISIBILITY — a fresh Runner that never held either run resolves identity +
    # reconstructs the view from the SHARED Postgres stores.
    tracker: dict = {}
    b = Runner(cfg, keys=_KEYS, chat_api_factory=_durable_factory(cfg, tracker))
    await b.init()

    assert b.get(visible_run) is None, "B held the visible run in memory (should not)"
    owner = await b.owner_of(visible_run)
    view = await b.get_durable(visible_run)
    if owner == "u1" and view is not None and view.status == "succeeded":
        print(f"  [a] VISIBILITY PASS — B resolved {visible_run}: owner={owner}, "
              f"status={view.status} (never held in memory).")
    else:
        ok = False
        print(f"  [a] VISIBILITY FAIL — owner={owner!r}, "
              f"view={None if view is None else view.status!r}.")

    # (c) COLD-RESUME — B never held the paused run; confirm(approve) must reconstruct
    # it from durable Postgres state, re-drive, and run the tool ON B.
    assert b.get(paused_run) is None, "B held the paused run in memory (should not)"
    result = await b.confirm(paused_run, _TOOL_ID, decision="approve")
    if result is None or result.get("status") != "resumed":
        ok = False
        print(f"  [c] COLD-RESUME FAIL — confirm returned {result!r} "
              "(reconstruct from shared Postgres did not resume).")
    else:
        task = b.task_for(paused_run)
        if task is not None:
            await task  # await B's re-drive
        final = b.get(paused_run)
        final_status = final.status if final is not None else "<none>"
        ran_on_b = tracker.get("ran_tool") == 1
        if final_status == "succeeded" and ran_on_b:
            print(f"  [c] COLD-RESUME PASS — B reconstructed {paused_run}, resumed, "
                  "and the tool ran ON B (cross-replica).")
        else:
            ok = False
            print(f"  [c] COLD-RESUME FAIL — status={final_status!r}, "
                  f"ran_on_b={ran_on_b}.")

    await b.aclose()
    print("GATE: PASS" if ok else "GATE: FAIL")
    return 0 if ok else 1


# --- Modes lock-hold / lock-try: cross-container exclusion primitives ----------


async def _lock_hold(user: str, task: str, hold_s: float) -> int:
    """Hold the distributed lease until ``hold_s`` elapses OR a SIGTERM arrives
    (``docker stop``), then exit the ``hold`` CM so its ``__aexit__`` runs the REAL
    owner-guarded ``_release``. run.sh stops A gracefully after B's first (refused)
    attempt, so the release that frees the key is the genuine lease-release path — not
    a killed row left to expire."""
    import signal

    stop = asyncio.Event()
    loop = asyncio.get_event_loop()
    try:
        loop.add_signal_handler(signal.SIGTERM, stop.set)
        loop.add_signal_handler(signal.SIGINT, stop.set)
    except NotImplementedError:  # pragma: no cover - platform without signal handlers
        pass

    lock = PostgresTaskLock(_dsn())
    async with lock.hold(user, task):
        print("LEASE_HELD", flush=True)
        try:
            await asyncio.wait_for(stop.wait(), timeout=hold_s)
        except asyncio.TimeoutError:
            pass  # held the full duration without a stop signal
    await lock.close()
    print("LEASE_RELEASED", flush=True)
    return 0


async def _lock_try(user: str, task: str, window_s: float) -> int:
    """Try to claim the lease within ``window_s`` WITHOUT blocking forever: poll the
    raw ``_try_claim`` so a live lease held by another owner yields ``CLAIM=REFUSED``
    (the exclusion proof) rather than hanging on ``hold``'s blocking claim."""
    lock = PostgresTaskLock(_dsn())
    await lock._ensure()
    deadline = asyncio.get_event_loop().time() + window_s
    claimed = False
    while asyncio.get_event_loop().time() < deadline:
        if await lock._try_claim(user, task):
            claimed = True
            break
        await asyncio.sleep(0.2)
    if claimed:
        await lock._release(user, task)
    await lock.close()
    print("CLAIM=OK" if claimed else "CLAIM=REFUSED", flush=True)
    return 0 if claimed else 3  # non-zero = refused, so run.sh can read the outcome


# --- Mode exclusion: a self-contained (b) proof in one process ----------------


async def _exclusion(user: str, task: str) -> int:
    """Belt-and-braces (b): one process, the shared DB. Owner A holds the lease; a
    SECOND owner B is REFUSED while A holds; once A releases, B succeeds. Proven at the
    lease primitive so there is no cross-container timing to make it flaky."""
    ok = True
    a = PostgresTaskLock(_dsn(), owner_id="ownerA")
    b = PostgresTaskLock(_dsn(), owner_id="ownerB")
    await a._ensure()
    await b._ensure()

    got_a = await a._try_claim(user, task)
    refused_b = not await b._try_claim(user, task)  # B must NOT get it while A holds
    if got_a and refused_b:
        print("  [b] EXCLUSION PASS(1/2) — A holds the lease; B's concurrent claim REFUSED.")
    else:
        ok = False
        print(f"  [b] EXCLUSION FAIL — got_a={got_a}, refused_b={refused_b}.")

    await a._release(user, task)  # A releases
    got_b = await b._try_claim(user, task)  # now B can claim the freed key
    if got_b:
        print("  [b] EXCLUSION PASS(2/2) — after A releases, B claims the freed lease.")
    else:
        ok = False
        print("  [b] EXCLUSION FAIL — B could not claim after A released.")
    await b._release(user, task)

    await a.close()
    await b.close()
    print("GATE: PASS" if ok else "GATE: FAIL")
    return 0 if ok else 1


# --- dispatch -----------------------------------------------------------------


async def _main(argv: list[str]) -> int:
    if not argv:
        print("usage: c3_multireplica_smoke <a-seed|b-verify|lock-hold|lock-try|exclusion> [args]")
        return 2
    mode = argv[0]
    if mode == "a-seed":
        return await _a_seed()
    if mode == "b-verify":
        if len(argv) < 3:
            print("usage: b-verify <visible_run> <paused_run>")
            return 2
        return await _b_verify(argv[1], argv[2])
    if mode == "lock-hold":
        if len(argv) < 3:
            print("usage: lock-hold <user> <task> [hold_s]")
            return 2
        return await _lock_hold(argv[1], argv[2], float(argv[3]) if len(argv) > 3 else 30.0)
    if mode == "lock-try":
        if len(argv) < 3:
            print("usage: lock-try <user> <task> [window_s]")
            return 2
        return await _lock_try(argv[1], argv[2], float(argv[3]) if len(argv) > 3 else 4.0)
    if mode == "exclusion":
        if len(argv) < 3:
            print("usage: exclusion <user> <task>")
            return 2
        return await _exclusion(argv[1], argv[2])
    print(f"unknown mode: {mode!r}")
    return 2


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(sys.argv[1:])))
