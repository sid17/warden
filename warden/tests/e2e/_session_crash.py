"""Crash-recovery gate for the session-contract bed (docs/08 §C.2, S6).

Where the resume-recall gate proves memory survives a fresh ``SessionManager``
with the **transcript still on disk**, this gate proves the stronger guarantee:
memory survives when the **whole task workspace is destroyed** and rebuilt from
the after-turn snapshot — the real crash / stateless-worker path.

Flow (single container, but the disk that held the transcript is physically
wiped between turns, so recall can ONLY come from the snapshot):

    persistence ACTIVE (task_id set)
    turn 1: plant 'heliotrope'         → orchestrator snapshots the task dir
    rm -rf  <base>/<user>/<task>       → the ONLY surviving copy is the tarball
    turn 2: resume same id (fresh mgr) → ensure_restored rebuilds from the tarball
                                       → model recalls 'heliotrope'

The provider transcript lives INSIDE the task dir (``provider_home_kwargs`` pins
``.claude-home`` / ``.codex`` / ``.openharness`` under it), so it travels with
the snapshot. The session INDEX (SQLite) lives OUTSIDE the task dir — it is the
durable pointer that a real crash keeps, exactly as designed.

Codex note: its SDK couples auth + transcript in ``CODEX_HOME``. Under
persistence that home is pinned to ``<task>/.codex`` (no ``auth.json``). The
credential is re-injected out-of-band by the PRODUCTION restore path
(``prepare_persisted_turn`` → ``reinject_credentials``) from the mounted ambient
``CODEX_HOME``, on turn 1 AND after the wipe+restore — the driver no longer seeds
it. Critically, ``auth.json`` is EXCLUDED from the snapshot (A2), so it never
travels in the tarball; this gate asserts that (safe for a remote S3 backend).
See ADR ``credential-backup-separation``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tarfile
from pathlib import Path

from warden.orchestrator.orchestrator import Orchestrator
from warden.orchestrator.session.db import SessionDB
from warden.orchestrator.session.index import SessionIndex
from warden.orchestrator.session.manager import SessionManager
from warden.persistence.config import PersistenceConfig
from warden.persistence.keys import archive_key
from warden.persistence.local_backend import LocalFileBackend
from warden.workspace.task_workspace import task_dir
from warden.tests.e2e._session_recall import (
    PLANT,
    RECALL,
    SECRET,
    drain_turn,
)

_USER = "default"
_TASK = "crash_task"


def _git_init(path: Path) -> None:
    """Make ``path`` a trusted git repo (bed convention; harmless for all)."""
    try:
        subprocess.run(["git", "init", "-q", str(path)], check=True)
        subprocess.run(["git", "-C", str(path), "config", "user.email",
                        "smoke@example.com"], check=True)
        subprocess.run(["git", "-C", str(path), "config", "user.name",
                        "smoke"], check=True)
    except (OSError, subprocess.CalledProcessError):
        pass  # non-fatal — the SDK path does not hard-require git


#: Credential basenames that must NEVER appear inside a snapshot archive (A2).
_CREDENTIAL_BASENAMES = ("auth.json", ".credentials.json")


def _local_tarball_credential_leak(tar_path: Path) -> list[str]:
    """Return credential member paths inside a LOCAL tarball (empty = clean).

    Strongest, backend-specific check for the local backend: read the raw
    ``.tar.gz`` members directly. For a remote backend use
    ``_restored_credential_leak`` (restore + scan) — the object bytes aren't a
    local file.
    """
    if not tar_path.is_file():
        return []
    leaked: list[str] = []
    with tarfile.open(tar_path, "r:gz") as tar:
        for m in tar.getmembers():
            if Path(m.name).name in _CREDENTIAL_BASENAMES:
                leaked.append(m.name)
    return leaked


async def _restored_credential_leak(backend, key: str, scratch: Path) -> list[str]:
    """Restore ``key`` into ``scratch`` and return any credential files found.

    Backend-agnostic (works for local AND S3): a non-empty result means the
    archive carried a secret — the A2 violation. Complements the byte-level
    local check with a check that holds for a remote object too.
    """
    await backend.restore(key, scratch)
    return [
        str(p.relative_to(scratch))
        for p in scratch.rglob("*")
        if p.is_file() and p.name in _CREDENTIAL_BASENAMES
    ]


def _ambient_codex_source_present() -> bool:
    """True if an out-of-band codex credential source is mounted (for a clear log)."""
    ambient = os.environ.get("CODEX_HOME")
    return bool(ambient and (Path(ambient) / "auth.json").is_file())


def _orchestrator(
    manager: SessionManager,
    run_dir: Path,
    cfg: PersistenceConfig,
    backend: LocalFileBackend,
) -> Orchestrator:
    """A persistence-ACTIVE orchestrator (task_id set → snapshot after each turn)."""
    return Orchestrator(
        session_manager=manager,
        repo_path=run_dir,
        persist_cfg=cfg,
        persist_backend=backend,
        user_id=_USER,
        task_id=_TASK,
    )


def _build_backend(kind: str, state_root: Path, exclude_patterns):
    """Build the snapshot backend for the crash gate.

    ``local`` (default) → on-disk tar backend. ``s3`` → ``S3Boto3Backend`` reading
    bucket/region/creds from env (``AWS_BUCKET_NAME`` + the AWS_* chain, MinIO via
    ``AWS_S3_ENDPOINT``) — the A3 remote-backend proof. The credential exclusion is
    in the shared archive layer, so it holds identically for both.
    """
    if kind == "s3":
        import os as _os

        from warden.persistence.s3_backend import S3Boto3Backend

        bucket = _os.environ.get("AWS_BUCKET_NAME") or _os.environ.get("S3_BUCKET")
        if not bucket:
            raise RuntimeError(
                "CRASH_STORAGE_BACKEND=s3 needs AWS_BUCKET_NAME (bucket) set."
            )
        return S3Boto3Backend(
            bucket=bucket,
            prefix=_os.environ.get("S3_PREFIX", ""),
            exclude_patterns=exclude_patterns,
        )
    return LocalFileBackend(state_root, exclude_patterns)


async def run_crash_gate(
    provider: str,
    *,
    model: str | None = None,
    run_dir: Path | None = None,
    backend_kind: str | None = None,
) -> int:
    """Plant → snapshot → WIPE task dir → restore → resume → recall. 0=PASS/1=FAIL.

    ``backend_kind`` (or ``$CRASH_STORAGE_BACKEND``) selects ``local`` (default) or
    ``s3`` — the A3 remote-backend proof runs the identical flow against S3/MinIO.
    """
    backend_kind = backend_kind or os.environ.get("CRASH_STORAGE_BACKEND", "local")
    run_dir = run_dir or Path("/work/run")
    run_dir.mkdir(parents=True, exist_ok=True)
    persist = run_dir / "persist"
    base_dir = persist / "workspaces"
    state_root = persist / "store"
    base_dir.mkdir(parents=True, exist_ok=True)
    state_root.mkdir(parents=True, exist_ok=True)
    db_path = run_dir / "crash_sessions.db"  # durable index — OUTSIDE the task dir

    cfg = PersistenceConfig(base_dir=base_dir, state_root=state_root)
    backend = _build_backend(backend_kind, state_root, cfg.exclude_patterns)
    td = task_dir(base_dir, _USER, _TASK)
    key = archive_key(cfg, _USER, _TASK)

    print("=" * 66)
    print(f" CRASH-RECOVERY GATE — provider={provider} model={model or 'default'} "
          f"backend={backend_kind}")
    print(" plant → snapshot → WIPE workspace → restore → resume → recall")
    print("=" * 66)

    # Pre-create + git-init the task dir. NOTE: we do NOT seed codex creds here —
    # the production restore path (prepare_persisted_turn → reinject_credentials)
    # re-injects them out-of-band from the mounted ambient CODEX_HOME on every
    # persisted turn (A1). This gate proves that production path, not a test hack.
    td.mkdir(parents=True, exist_ok=True)
    _git_init(td)
    if provider == "codex":
        if _ambient_codex_source_present():
            print(f"  ambient codex credential source present "
                  f"({os.environ.get('CODEX_HOME')}); reinject will seed the pinned "
                  f"home on each turn.")
        else:
            print("  WARNING: no ambient codex credential source (CODEX_HOME); "
                  "reinject will find nothing and the codex turn will fail auth.")

    # --- Turn 1: plant. Persistence snapshots the task dir after the turn. -----
    mgr_a = SessionManager(index=SessionIndex(SessionDB(db_path)))
    await mgr_a.init()
    try:
        orch_a = _orchestrator(mgr_a, run_dir, cfg, backend)
        sid, reply1, err1 = await drain_turn(
            orch_a, provider=provider, model=model, prompt=PLANT, session_id=None,
        )
        if sid:
            await mgr_a.close(sid)
    finally:
        await mgr_a.close_all()
        await mgr_a.close_index()

    print(f"\n  turn1: session_id={sid!r} reply={reply1.strip()[:80]!r} err={err1!r}")
    if not sid:
        print("\n RESULT: FAIL — no session_id captured in turn 1.")
        return 1
    if err1:
        print("\n RESULT: FAIL — turn 1 errored (see above).")
        return 1

    # --- The crash: confirm the snapshot, then physically WIPE the task dir. ---
    if not await backend.exists(key):
        print(f"\n RESULT: FAIL — no snapshot at {key!r}; persistence did not run "
              "(task_id unset?).")
        return 1

    # A2 assertion: the credential must NOT be inside the snapshot archive. This
    # is what makes crash-recovery safe on a REMOTE (S3) backend — the object
    # carries the transcript, never the OAuth token. Local backend: inspect the
    # on-disk tarball bytes directly (strongest). S3: restore into a scratch dir
    # and scan (backend-agnostic — the object bytes aren't a local file).
    if backend_kind == "s3":
        scratch = run_dir / "leakcheck"
        if scratch.exists():
            shutil.rmtree(scratch)
        leaked = await _restored_credential_leak(backend, key, scratch)
        where = f"s3://{key}"
    else:
        tarball = state_root / key
        leaked = _local_tarball_credential_leak(tarball)
        where = tarball.name
    if leaked:
        print(f"\n RESULT: FAIL — credential(s) leaked into the snapshot archive "
              f"{where}: {leaked}. A2 broken — unsafe for a remote backend.")
        return 1
    print(f"  A2 ✓ — no credential in the snapshot archive ({where}).")

    if td.exists():
        shutil.rmtree(td)
    print(f"  snapshot present ({key}); WIPED {td} — exists now: {td.exists()}")
    print("  the only surviving copy of the transcript is now the snapshot tarball.")
    if provider == "codex":
        auth_after_wipe = (td / ".codex" / "auth.json").exists()
        print(f"  codex auth.json present after wipe (should be False): "
              f"{auth_after_wipe}")

    # --- Turn 2: fresh manager + orchestrator → restore from snapshot → recall -
    mgr_b = SessionManager(index=SessionIndex(SessionDB(db_path)))
    await mgr_b.init()
    try:
        orch_b = _orchestrator(mgr_b, run_dir, cfg, backend)
        sid2, reply2, err2 = await drain_turn(
            orch_b, provider=provider, model=model, prompt=RECALL, session_id=sid,
        )
    finally:
        await mgr_b.close_all()
        await mgr_b.close_index()

    restored = td.exists()
    print(f"\n  restored task dir from snapshot: {restored}")
    print(f"  turn2: resumed_id={sid2!r} reply={reply2.strip()[:120]!r} err={err2!r}")

    # A1 assertion (codex): the credential was NOT in the tarball, so after
    # restore it can only be present because the production restore path
    # (reinject_credentials) re-hydrated it out-of-band. Prove it landed.
    auth_reinjected = True
    if provider == "codex":
        auth_reinjected = (td / ".codex" / "auth.json").is_file()
        print(f"  A1 ✓ — codex auth.json re-injected on restore "
              f"(out-of-band, not from tarball): {auth_reinjected}")

    recalled = SECRET.lower() in reply2.lower()
    same_id = sid2 == sid
    print("\n" + "=" * 66)
    if recalled and same_id and restored and auth_reinjected and not err2:
        print(f" RESULT: PASS — after a WIPED workspace, resumed session {sid} "
              f"recalled '{SECRET}'.")
        print(" S6 proven: the snapshot carried the session's memory across a "
              "destroyed+restored workspace.")
        if provider == "codex":
            print(" A1/A2 proven: credential absent from tarball, re-injected "
                  "out-of-band on restore, recall still passed.")
        print("=" * 66)
        return 0

    print(" RESULT: FAIL —")
    if not restored:
        print("   - task dir was NOT restored from the snapshot (ensure_restored).")
    if err2:
        print("   - turn 2 errored (resume/auth after restore failed).")
    if not same_id:
        print(f"   - resumed id {sid2!r} != planted id {sid!r}.")
    if not recalled:
        print(f"   - '{SECRET}' NOT in turn-2 reply → memory did NOT survive the "
              "wipe (transcript not in the snapshot?).")
    if not auth_reinjected:
        print("   - codex auth.json was NOT re-injected on restore "
              "(reinject_credentials found no out-of-band source?).")
    print("=" * 66)
    return 1
