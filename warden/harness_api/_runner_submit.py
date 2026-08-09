"""Submit / seed / provision methods for :class:`Runner`.

Composed into ``Runner`` via the MRO; assumes ``Runner.__init__`` state
(``self._runs``, ``self._cfg``, ``self._build_egress``, ``self._run`` etc.).
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

from warden.harness_api._run_state import _RunState
from warden.harness_api.schemas import RunSpec


class _SubmitMixin:
    def submit(self, spec: RunSpec) -> str:
        """Register a run, spawn its background task, return the ``run_id``.

        EXT-C1: ``run_id`` is a UUID (dash-free hex) — globally unique + collision-free
        across restarts (the old ``run_{counter}`` reset to ``run_000001`` on restart).
        The durable identity record is persisted at the top of ``_run`` (async).
        """
        run_id = uuid.uuid4().hex
        state = _RunState(run_id=run_id, user_id=spec.user_id, task_id=spec.task_id)
        # M6: stash spec + egress so a durable HITL resume can re-drive this run.
        state.spec = spec
        self._runs[run_id] = state
        egress = self._build_egress(run_id, spec.sink)
        state.egress = egress
        state.task = asyncio.create_task(self._run(run_id, spec, egress))
        return run_id

    async def store_seed(self, bundle: bytes) -> str:
        """EXT-W1 — store a product seed bundle and return its opaque ``seed_ref``.

        Reuses the persistence backend (local/s3 per config); the ref is
        content-addressed (immutable + dedupe). Not run-scoped — seeds are
        product-global content (gated only by the service token)."""
        from warden.config.build import build_persistence
        from warden.harness_api.seeds import SeedStore

        _cfg, backend = build_persistence(
            self._cfg.engine.persistence, self._cfg.engine.workspace
        )
        return await SeedStore(backend).put(bundle)

    async def provision(
        self, user_id: str, task_id: str, seed_ref: str
    ) -> dict:
        """EXT-W1 — lay a seed's skills + agents + ``.workflows/`` into a workspace.

        Fetches the bundle by ``seed_ref``, extracts it, and bootstraps the
        ``(user, task)`` task dir (idempotent — bootstrap skips already-populated
        entries, so re-provision never clobbers produced work). Returns the ack
        ``{workflows[], skills[]}``. The scaffold rides the per-turn snapshot
        (persist-everything), so a resumed session keeps its permission surface.

        Raises:
            FileNotFoundError: unknown ``seed_ref`` (→ route 404).
            RuntimeError: bootstrap failed to place an entry (→ route 500; no lockfile
                is written, so the box is never left partial).
        """
        from warden.config.build import build_persistence
        from warden.harness_api.seeds import SeedStore
        from warden.persistence.archive import extract_tar_gz
        from warden.persistence.keys import task_dir
        from warden.workspace.bootstrap import bootstrap
        from warden.workspace.provision import resolve_seed

        runtime_cfg, backend = build_persistence(
            self._cfg.engine.persistence, self._cfg.engine.workspace
        )
        bundle = await SeedStore(backend).get(seed_ref)
        if bundle is None:
            raise FileNotFoundError(f"unknown seed_ref {seed_ref}")
        td = task_dir(runtime_cfg.base_dir, user_id, task_id)

        def _lay_down() -> dict:
            import tempfile

            with tempfile.TemporaryDirectory() as d:
                bundle_path = Path(d) / "seed.tar.gz"
                bundle_path.write_bytes(bundle)
                extract_dir = Path(d) / "extracted"
                extract_tar_gz(bundle_path, extract_dir, seed_ref)
                payload = resolve_seed(extract_dir)
                return bootstrap(
                    td, skills=payload["skills"], agents=payload["agents"],
                    workflows=payload["workflows"],
                    copy_dirs=payload["copy_dirs"], mkdirs=payload["mkdirs"],
                    source_ref=seed_ref,
                )

        result = await asyncio.to_thread(_lay_down)
        return {
            "workflows": result["workflows"],
            "skills": result["skills"],
            "copied": result["copied"],
            "mkdirs": result["mkdirs"],
        }

    def _assert_provisioned(self, spec: RunSpec) -> None:
        """E3 — fail loudly if a run names ``input.workflow`` but the workspace was
        never provisioned with that manifest.

        Without this, a run whose ``input.workflow`` resolves to nothing runs
        **ungoverned** (no permission surface — no landscape gate, no deny rules),
        silently. Enforce correct ordering: a run that requests a workflow must find
        it on disk (``POST /provision`` ran first, or it rode a restored snapshot).
        A run with NO ``input.workflow`` is unaffected. A present-but-broken manifest
        raises ``WorkflowLoadError`` from ``load_workflow`` (also fail-closed).
        """
        from warden.persistence.keys import task_dir
        from warden.workspace.workflow.loader import load_workflow

        wf_name = spec.input.get("workflow")
        if not wf_name:
            return
        td = task_dir(
            self._cfg.engine.workspace.base_dir, spec.user_id, spec.task_id
        )
        if load_workflow(td, wf_name) is None:
            raise RuntimeError(
                f"workspace not provisioned: no .workflows/{wf_name}.yaml for "
                f"user={spec.user_id!r} task={spec.task_id!r} — call POST /provision "
                f"before a run that names input.workflow"
            )
