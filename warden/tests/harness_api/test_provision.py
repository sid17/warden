"""EXT-W1 (E3) — seed upload → ref → provision, over the real Runs API.

Round-trips a dummy seed bundle: ``POST /seeds`` → an opaque immutable ``seed_ref``;
``POST /provision`` → the workspace holds the skills + ``.workflows/``; the same ref
provisions a second task (reuse). Also proves the scaffold rides the snapshot
(persist-everything) and the ``x-user-id`` provision guard. Hermetic — local backend
on a tmp dir, no S3.
"""

import asyncio
import io
import tarfile
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
from warden.harness_api.credentials.service_tokens import (
    ServiceTokenRegistry,
)
from warden.harness_api.event_log import RunEventLog
from warden.harness_api.runner import Runner
from warden.harness_api.schemas import RunSpec, Sink
from warden.persistence.keys import task_dir
from warden.workspace.task_workspace import ensure_restored, snapshot
from warden.tests.harness_api.mock_skill import Tracker, build_factory


def _make_bundle() -> bytes:
    """A dummy seed .tar.gz: one skill + one workflow manifest."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        def _add(name: str, content: str):
            data = content.encode()
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))

        _add("skills/dummy/SKILL.md",
             "---\nname: dummy\ndescription: a dummy skill\n---\nbody\n")
        _add("workflows/dummy.yaml",
             "name: dummy\ndescription: d\npermissions:\n  mode: auto\n")
    return buf.getvalue()


def _cfg(tmp: Path) -> HarnessApiConfig:
    engine = HarnessConfig(
        persistence=PersistenceConfig(state_root=str(tmp / "store")),
        workspace=WorkspaceConfig(base_dir=str(tmp / "ws")),
    )
    return HarnessApiConfig(engine=engine, governance=GovernanceConfig(enabled=False))


def _build(tmp: Path, *, token_registry=None):
    cfg = _cfg(tmp)
    runner = Runner(cfg, event_log=RunEventLog(tmp / "run_events.db"))
    app = create_app(runner, token_registry=token_registry)
    return cfg, runner, app


def _client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    )


def test_seed_upload_provision_roundtrip_and_reuse():
    async def _run():
        tmp = Path(tempfile.mkdtemp())
        cfg, runner, app = _build(tmp)
        bundle = _make_bundle()
        async with _client(app) as c:
            # Upload once → an opaque seed_ref.
            up = await c.post("/seeds", content=bundle)
            assert up.status_code == 201
            seed_ref = up.json()["seed_ref"]
            assert seed_ref.startswith("seed_")

            # Provision task A.
            r = await c.post("/provision", json={
                "user_id": "u1", "task_id": "courseA", "seed_ref": seed_ref,
            })
            assert r.status_code == 200
            # A legacy (dir-scan) seed carries no generic copy-list / mkdir-list, so
            # those ack fields are empty; the workflow/skill fields are unchanged.
            assert r.json() == {
                "workflows": ["dummy"], "skills": ["dummy"],
                "copied": [], "mkdirs": [],
            }

            base = Path(cfg.engine.workspace.base_dir)
            tdA = task_dir(base, "u1", "courseA")
            assert (tdA / ".claude" / "skills" / "dummy" / "SKILL.md").is_file()
            assert (tdA / ".workflows" / "dummy.yaml").is_file()

            # Reuse the SAME ref for a second task.
            r2 = await c.post("/provision", json={
                "user_id": "u1", "task_id": "courseB", "seed_ref": seed_ref,
            })
            assert r2.status_code == 200
            tdB = task_dir(base, "u1", "courseB")
            assert (tdB / ".workflows" / "dummy.yaml").is_file()

            # Re-upload identical bytes → the same (immutable, dedup) ref.
            up2 = await c.post("/seeds", content=bundle)
            assert up2.json()["seed_ref"] == seed_ref

    asyncio.run(_run())


def test_provision_unknown_seed_ref_is_404():
    async def _run():
        tmp = Path(tempfile.mkdtemp())
        _cfg_, _runner, app = _build(tmp)
        async with _client(app) as c:
            r = await c.post("/provision", json={
                "user_id": "u1", "task_id": "t", "seed_ref": "seed_nope",
            })
            assert r.status_code == 404

    asyncio.run(_run())


def test_provision_reprovision_preserves_produced_work():
    async def _run():
        tmp = Path(tempfile.mkdtemp())
        cfg, runner, app = _build(tmp)
        async with _client(app) as c:
            seed_ref = (await c.post("/seeds", content=_make_bundle())).json()["seed_ref"]
            await c.post("/provision", json={
                "user_id": "u1", "task_id": "c", "seed_ref": seed_ref})
            td = task_dir(Path(cfg.engine.workspace.base_dir), "u1", "c")
            # Simulate produced work: edit the landed manifest + add a draft.
            (td / ".workflows" / "dummy.yaml").write_text("EDITED\n")
            (td / "draft.md").write_text("chapter\n")
            # Re-provision → idempotent, never clobbers.
            await c.post("/provision", json={
                "user_id": "u1", "task_id": "c", "seed_ref": seed_ref})
            assert (td / ".workflows" / "dummy.yaml").read_text() == "EDITED\n"
            assert (td / "draft.md").read_text() == "chapter\n"

    asyncio.run(_run())


def test_provision_x_user_id_guard_when_configured():
    async def _run():
        tmp = Path(tempfile.mkdtemp())
        _cfg_, _runner, app = _build(
            tmp, token_registry=ServiceTokenRegistry({"svc": "tok"})
        )
        async with _client(app) as c:
            hdr = {"x-service-token": "tok"}
            seed_ref = (await c.post("/seeds", content=_make_bundle(),
                                     headers=hdr)).json()["seed_ref"]
            # Asserting a different user than the provision target → 403.
            bad = await c.post("/provision",
                               json={"user_id": "u1", "task_id": "t",
                                     "seed_ref": seed_ref},
                               headers={**hdr, "x-user-id": "other"})
            assert bad.status_code == 403
            # Matching user → allowed.
            ok = await c.post("/provision",
                              json={"user_id": "u1", "task_id": "t",
                                    "seed_ref": seed_ref},
                              headers={**hdr, "x-user-id": "u1"})
            assert ok.status_code == 200

    asyncio.run(_run())


# --- E3: a run naming input.workflow must be provisioned first ----------------


def _mock_runner(tmp: Path) -> Runner:
    return Runner(
        _cfg(tmp),
        chat_api_factory=build_factory(Tracker()),
        event_log=RunEventLog(tmp / "run_events.db"),
    )


def _run_spec(task: str, *, workflow: str | None = None) -> RunSpec:
    inp = {"prompt": "hi"}
    if workflow is not None:
        inp["workflow"] = workflow
    return RunSpec(user_id="u1", task_id=task, input=inp, sink=Sink(type="sse"))


def test_unprovisioned_run_errors_loudly():
    """A run naming input.workflow on a never-provisioned workspace terminates
    ``error`` (workspace not provisioned) — it never runs ungoverned."""
    async def _run():
        tmp = Path(tempfile.mkdtemp())
        runner = _mock_runner(tmp)
        await runner.init()
        run_id = runner.submit(_run_spec("neverprov", workflow="gate"))
        await runner.task_for(run_id)
        view = runner.get(run_id)
        assert view.status == "error"
        assert "not provisioned" in (view.error or "")

    asyncio.run(_run())


def test_run_without_workflow_is_unaffected():
    """A run that names NO workflow is not gated by the provisioning check."""
    async def _run():
        tmp = Path(tempfile.mkdtemp())
        runner = _mock_runner(tmp)
        await runner.init()
        run_id = runner.submit(_run_spec("noworkflow"))  # no input.workflow
        await runner.task_for(run_id)
        assert runner.get(run_id).status == "succeeded"

    asyncio.run(_run())


def test_provisioned_run_proceeds():
    """After provisioning, a run naming that workflow proceeds normally."""
    async def _run():
        tmp = Path(tempfile.mkdtemp())
        runner = _mock_runner(tmp)
        await runner.init()
        # Provision the workspace with the dummy seed (carries workflows/dummy.yaml).
        seed_ref = await runner.store_seed(_make_bundle())
        await runner.provision("u1", "prov", seed_ref)
        run_id = runner.submit(_run_spec("prov", workflow="dummy"))
        await runner.task_for(run_id)
        assert runner.get(run_id).status == "succeeded"

    asyncio.run(_run())


def test_scaffold_rides_snapshot_and_restores():
    """Persist-everything (decision 2): the scaffold (skills + .workflows/) rides the
    per-turn snapshot and is replayed verbatim on restore — the permission surface is
    preserved for a resumed session."""
    async def _run():
        tmp = Path(tempfile.mkdtemp())
        cfg, runner, app = _build(tmp)
        async with _client(app) as c:
            seed_ref = (await c.post("/seeds", content=_make_bundle())).json()["seed_ref"]
            await c.post("/provision", json={
                "user_id": "u1", "task_id": "c", "seed_ref": seed_ref})

        runtime_cfg, backend = build_persistence(
            cfg.engine.persistence, cfg.engine.workspace
        )
        # Snapshot the provisioned box, wipe it, restore.
        await snapshot(runtime_cfg, backend, "u1", "c")
        td = task_dir(Path(cfg.engine.workspace.base_dir), "u1", "c")
        import shutil
        shutil.rmtree(td)
        restored = await ensure_restored(runtime_cfg, backend, "u1", "c")
        # The scaffold came back verbatim — the permission surface is preserved.
        assert (restored / ".claude" / "skills" / "dummy" / "SKILL.md").is_file()
        assert (restored / ".workflows" / "dummy.yaml").is_file()

    asyncio.run(_run())
