"""EXT-C1 — durable run identity: the ``RunRegistry`` + event-log reconstruction.

Proves: UUID ids never collide across a restart; ``JsonlRunRegistry`` round-trips
identity append-only; ``GET /runs/{id}`` survives a process restart (identity from
the registry, state derived from the event log); and status derivation covers the
terminal + pause cases. Hermetic against a ``TemporaryDirectory`` (like the auth
store + ledger tests), following the ``asyncio.run(_run())`` idiom.
"""

import asyncio
import tempfile
from pathlib import Path

from warden.harness_api.config import (
    GovernanceConfig,
    HarnessApiConfig,
)
from warden.harness_api.credentials.keys import KeyRegistry
from warden.harness_api.event_log import RunEventLog
from warden.harness_api.run_registry import (
    InMemoryRunRegistry,
    JsonlRunRegistry,
    RunIdentity,
)
from warden.harness_api.runner import Runner
from warden.harness_api.schemas import Event, RunSpec, Sink
from warden.tests.harness_api.mock_skill import Tracker, build_factory

_KEYS = KeyRegistry.from_config(
    {"keys": {"k": {"provider": "claude", "secret_env": "S"}},
     "users": {"u1": {"key_id": "k"}}},
    secrets={"S": "sk"},
)


def _spec(user="u1", task="course", **kw):
    return RunSpec(user_id=user, task_id=task, input={"prompt": "hi"},
                   sink=Sink(type="sse"), **kw)


# --- JsonlRunRegistry round-trip ------------------------------------------


def test_jsonl_registry_round_trip_survives_restart():
    async def _run():
        path = Path(tempfile.mkdtemp()) / "runs.jsonl"
        reg = JsonlRunRegistry(path)
        await reg.put(RunIdentity("abc", "u1", "course", "2026-07-25T00:00:00Z"))
        # A fresh instance over the same file (a "restart") replays the record.
        fresh = JsonlRunRegistry(path)
        await fresh.load()
        got = await fresh.get("abc")
        assert got is not None
        assert (got.user_id, got.task_id) == ("u1", "course")

    asyncio.run(_run())


def test_jsonl_registry_put_is_append_only_idempotent():
    async def _run():
        path = Path(tempfile.mkdtemp()) / "runs.jsonl"
        reg = JsonlRunRegistry(path)
        ident = RunIdentity("abc", "u1", "course", "t0")
        await reg.put(ident)
        await reg.put(ident)  # re-put same id → no-op (records are immutable)
        lines = [ln for ln in path.read_text().splitlines() if ln.strip()]
        assert len(lines) == 1

    asyncio.run(_run())


# --- UUID ids never collide across restart --------------------------------


def test_uuid_ids_distinct_and_no_cross_restart_collision():
    async def _run():
        tmp = Path(tempfile.mkdtemp())
        reg_path = tmp / "runs.jsonl"

        def _make_runner():
            return Runner(
                HarnessApiConfig(governance=GovernanceConfig(enabled=False)),
                keys=_KEYS,
                chat_api_factory=build_factory(Tracker()),
                event_log=RunEventLog(tmp / "run_events.db"),
                run_registry=JsonlRunRegistry(reg_path),
            )

        r1 = _make_runner()
        await r1.init()
        id_a = r1.submit(_spec(task="a"))
        id_b = r1.submit(_spec(task="b"))
        await asyncio.gather(r1.task_for(id_a), r1.task_for(id_b))
        assert id_a != id_b  # two submits → distinct UUIDs

        # A "restart": a fresh Runner over the same durable stores.
        r2 = _make_runner()
        await r2.init()
        id_c = r2.submit(_spec(task="c"))
        await r2.task_for(id_c)
        assert id_c not in {id_a, id_b}  # fresh id can't collide with pre-restart

    asyncio.run(_run())


# --- GET /runs/{id} survives restart --------------------------------------


def test_get_durable_survives_restart():
    async def _run():
        tmp = Path(tempfile.mkdtemp())
        reg_path = tmp / "runs.jsonl"
        db_path = tmp / "run_events.db"

        r1 = Runner(
            HarnessApiConfig(governance=GovernanceConfig(enabled=False)),
            keys=_KEYS,
            chat_api_factory=build_factory(
                Tracker(), usage={"input_tokens": 10, "output_tokens": 20}
            ),
            event_log=RunEventLog(db_path),
            run_registry=JsonlRunRegistry(reg_path),
        )
        await r1.init()
        run_id = r1.submit(_spec())
        await r1.task_for(run_id)
        live = r1.get(run_id)
        assert live.status == "succeeded"
        await r1.aclose()

        # Restart: fresh Runner, in-memory registry empty, same durable stores.
        r2 = Runner(
            HarnessApiConfig(governance=GovernanceConfig(enabled=False)),
            keys=_KEYS,
            chat_api_factory=build_factory(Tracker()),
            event_log=RunEventLog(db_path),
            run_registry=JsonlRunRegistry(reg_path),
        )
        await r2.init()
        assert r2.get(run_id) is None  # in-memory registry lost it
        # Identity resolves from the durable registry...
        assert await r2.owner_of(run_id) == "u1"
        # ...and the full view is reconstructed from the event log.
        view = await r2.get_durable(run_id)
        assert view is not None
        assert view.run_id == run_id
        assert view.status == "succeeded"
        assert view.usage.get("input") == 10
        assert view.usage.get("output") == 20
        assert view.session_id == live.session_id
        await r2.aclose()

    asyncio.run(_run())


def test_get_durable_unknown_run_is_none():
    async def _run():
        tmp = Path(tempfile.mkdtemp())
        r = Runner(
            HarnessApiConfig(governance=GovernanceConfig(enabled=False)),
            keys=_KEYS,
            chat_api_factory=build_factory(Tracker()),
            event_log=RunEventLog(tmp / "run_events.db"),
            run_registry=JsonlRunRegistry(tmp / "runs.jsonl"),
        )
        await r.init()
        assert await r.get_durable("nope") is None

    asyncio.run(_run())


# --- status derivation (the fiddly part) ----------------------------------


def _append_all(log: RunEventLog, run_id: str, specs: list[tuple[str, dict]]):
    async def _go():
        await log.init()
        for i, (etype, data) in enumerate(specs, start=1):
            await log.append(Event(run_id=run_id, seq=i, type=etype,
                                   session_id="sess", data=data, at="t"))
    asyncio.run(_go())


def test_status_derivation_all_cases():
    tmp = Path(tempfile.mkdtemp())

    async def _view(run_id, log):
        return await log.reconstruct_view(run_id)

    # succeeded
    log = RunEventLog(tmp / "s.db")
    _append_all(log, "r", [("session", {}), ("token", {"text": "x"}),
                           ("result", {"usage": {"input": 1}, "cost_usd": 0.5})])
    v = asyncio.run(_view("r", log))
    assert v.status == "succeeded" and v.cost_usd == 0.5 and v.usage == {"input": 1}

    # error
    log = RunEventLog(tmp / "e.db")
    _append_all(log, "r", [("session", {}), ("error", {"reason": "boom"})])
    v = asyncio.run(_view("r", log))
    assert v.status == "error" and v.error == "boom"

    # stopped
    log = RunEventLog(tmp / "st.db")
    _append_all(log, "r", [("session", {}), ("stopped", {"reason": "budget"})])
    assert asyncio.run(_view("r", log)).status == "stopped"

    # requires_action: trailing permission_request, no resolved
    log = RunEventLog(tmp / "ra.db")
    _append_all(log, "r", [("session", {}), ("permission_request", {"tool_use_id": "t"})])
    assert asyncio.run(_view("r", log)).status == "requires_action"

    # resolved then done → not requires_action
    log = RunEventLog(tmp / "res.db")
    _append_all(log, "r", [("session", {}), ("permission_request", {}),
                           ("permission_resolved", {}), ("result", {})])
    assert asyncio.run(_view("r", log)).status == "succeeded"

    # unterminated → running
    log = RunEventLog(tmp / "run.db")
    _append_all(log, "r", [("session", {}), ("token", {"text": "x"})])
    assert asyncio.run(_view("r", log)).status == "running"


def test_in_memory_registry_is_ephemeral():
    async def _run():
        reg = InMemoryRunRegistry()
        await reg.put(RunIdentity("a", "u1", "t", "t0"))
        assert await reg.get("a") is not None
        await reg.load()  # no-op; nothing durable
        assert await reg.get("a") is not None  # still in this instance

    asyncio.run(_run())
