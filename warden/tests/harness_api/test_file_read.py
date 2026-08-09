"""EXT-A1 — the confined ``GET /runs/{id}/file`` route over a real snapshot.

Drives the actual FastAPI route against a real persistence backend: a workspace is
snapshotted to the archive key for ``(user_a, course)``, a run is registered for it,
and the route serves one confined member. Behind both auth layers (service token +
ownership). Hermetic — local backend on a tmp dir, mock skill (no LLM).
"""

import asyncio
import tempfile
from pathlib import Path

import httpx

from warden.config.build import build_persistence
from warden.config.models import (
    HarnessConfig,
    PersistenceConfig,
    WorkspaceConfig,
)
from warden.harness_api.app import create_app
from warden.harness_api.config import GovernanceConfig, HarnessApiConfig
from warden.harness_api.credentials.keys import KeyRegistry
from warden.harness_api.credentials.service_tokens import (
    ServiceTokenRegistry,
)
from warden.harness_api.event_log import RunEventLog
from warden.harness_api.runner import Runner
from warden.harness_api.schemas import RunSpec, Sink
from warden.persistence.keys import archive_key
from warden.tests.harness_api.mock_skill import Tracker, build_factory

_KEYS = KeyRegistry.from_config(
    {"keys": {"k": {"provider": "claude", "secret_env": "S"}},
     "users": {"user_a": {"key_id": "k"}}},
    secrets={"S": "sk"},
)


def _engine_cfg(tmp: Path) -> HarnessConfig:
    return HarnessConfig(
        persistence=PersistenceConfig(state_root=str(tmp / "store")),
        workspace=WorkspaceConfig(base_dir=str(tmp / "ws")),
    )


async def _snapshot_file(engine: HarnessConfig, user, task, rel, content):
    """Lay a workspace with one file and snapshot it to the (user, task) archive."""
    runtime_cfg, backend = build_persistence(engine.persistence, engine.workspace)
    ws = Path(tempfile.mkdtemp()) / "ws"
    target = ws / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    await backend.backup(ws, archive_key(runtime_cfg, user, task))


def _client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    )


def _hdr(user="user_a"):
    return {"x-service-token": "tok", "x-user-id": user}


def _build(tmp):
    engine = _engine_cfg(tmp)
    cfg = HarnessApiConfig(engine=engine, governance=GovernanceConfig(enabled=False))
    runner = Runner(
        cfg, keys=_KEYS, chat_api_factory=build_factory(Tracker()),
        event_log=RunEventLog(tmp / "run_events.db"),
    )
    app = create_app(runner, token_registry=ServiceTokenRegistry({"svc": "tok"}))
    return engine, runner, app


def test_file_read_serves_confined_member():
    async def _run():
        tmp = Path(tempfile.mkdtemp())
        engine, runner, app = _build(tmp)
        await _snapshot_file(engine, "user_a", "course", "drafts/ch1.md", "# Chapter 1")
        run_id = runner.submit(RunSpec(
            user_id="user_a", task_id="course", input={"prompt": "hi"},
            sink=Sink(type="sse"),
        ))
        await runner.task_for(run_id)
        async with _client(app) as c:
            # owner reads the file → 200 + bytes
            r = await c.get(f"/runs/{run_id}/file?path=drafts/ch1.md", headers=_hdr())
            assert r.status_code == 200
            assert r.content == b"# Chapter 1"
            # traversal → 400
            assert (await c.get(
                f"/runs/{run_id}/file?path=../../etc/passwd", headers=_hdr()
            )).status_code == 400
            # missing member → 404
            assert (await c.get(
                f"/runs/{run_id}/file?path=drafts/nope.md", headers=_hdr()
            )).status_code == 404
            # wrong user → 403 (ownership), never reaches the file
            assert (await c.get(
                f"/runs/{run_id}/file?path=drafts/ch1.md", headers=_hdr("user_b")
            )).status_code == 403

    asyncio.run(_run())


def test_file_read_no_snapshot_is_404():
    async def _run():
        tmp = Path(tempfile.mkdtemp())
        _engine, runner, app = _build(tmp)
        # A run with no snapshot (no completed turn wrote a tarball).
        run_id = runner.submit(RunSpec(
            user_id="user_a", task_id="unsnapshotted", input={"prompt": "hi"},
            sink=Sink(type="sse"),
        ))
        await runner.task_for(run_id)
        async with _client(app) as c:
            r = await c.get(f"/runs/{run_id}/file?path=x.md", headers=_hdr())
            assert r.status_code == 404

    asyncio.run(_run())
