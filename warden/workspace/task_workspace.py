"""Caller-facing glue for task persistence (v16 Phase 2).

Ties the storage core (config, keys, backend) to the concerns a caller (CLI now,
web later) actually needs:

- ``home_env`` — the env vars to inject into a provider subprocess so its session
  home lives *inside* the task folder (one self-contained restore unit). Auth
  tokens are copied from the launching env into that dict; never written to disk.
- ``ensure_restored`` — a **guarded / idempotent** restore: no-op when the local
  copy is current, rebuild from the store on a fresh machine / crash, and leave
  the caller to bootstrap fresh when nothing was ever backed up.
- ``snapshot`` — back the task folder up to the store after a turn.

Design contract: plan §4.2 (guarded restore), §4.4 (session home wiring),
§9.2 / §9.3 (signatures + marker), §9.6 (auth token source).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

from warden.persistence.backend import StorageBackend
from warden.persistence.config import PersistenceConfig
from warden.persistence.keys import archive_key, task_dir
from warden.providers.auth import resolve_auth

# Marker written on restore; its presence means the local copy is current.
_MARKER_SUBDIR = ".workspace"
_MARKER_NAME = "restored.json"

# Per-provider session-home env var + on-disk subfolder of the task dir.
_HOME_VAR_BY_PROVIDER: dict[str, tuple[str, str]] = {
    "claude-cli": ("CLAUDE_CONFIG_DIR", ".claude-home"),
    "claude": ("CLAUDE_CONFIG_DIR", ".claude-home"),
    "codex": ("CODEX_HOME", ".codex"),
    # openharness relocates nothing this pass (its transcript path is hardcoded).
    "openharness": ("", ""),
}


def home_env(
    task_dir: Path, provider: str, auth_env: Mapping[str, str] | None = None
) -> dict:
    """Return the env vars to add to a provider subprocess for this task.

    Pins the provider's session home inside ``task_dir`` (so transcripts + resume
    metadata live in the folder and travel with the restore unit) and resolves the
    auth token to inject.

    Auth source is the ``auth_env`` seam: when a caller supplies a per-run managed
    key (``auth_env={"ANTHROPIC_API_KEY": ...}``), the subprocess gets *that* key
    and only that — the operator's ``os.environ`` credential is not consulted, so
    concurrent runs can each carry a different key. When ``auth_env`` is ``None``,
    creds fall back to ``os.environ`` (unchanged single-key behavior).

    This function does **not** mutate ``os.environ`` and never writes tokens to a
    file — the caller merges this dict onto the subprocess ``env`` only.

    Args:
        task_dir: the task folder (``base_dir/user_id/task_id``).
        provider: one of ``claude-cli``, ``claude``, ``codex``, ``openharness``.
        auth_env: optional explicit credential source (per-run managed key). When
            given, it fully replaces ``os.environ`` as the auth source.

    Returns:
        A plain mapping of env vars to add. ``{}`` for ``openharness`` with no key.
    """
    task_dir = Path(task_dir)
    home_var, subdir = _HOME_VAR_BY_PROVIDER.get(provider, ("", ""))

    env: dict[str, str] = {}
    if home_var:
        env[home_var] = str(task_dir / subdir)

    # Auth resolution lives in one seam (env-based today; container/secret source
    # can plug in later). ``resolve_auth`` reads ``auth_env`` when supplied (the
    # per-run managed-key path), else ``os.environ``. Never touches disk.
    env.update(resolve_auth(provider, auth_env))

    return env


def _marker_path(td: Path) -> Path:
    return td / _MARKER_SUBDIR / _MARKER_NAME


async def ensure_restored(
    cfg: PersistenceConfig,
    backend: StorageBackend,
    user_id: str,
    task_id: str,
) -> Path:
    """Guarded restore of a task folder — idempotent, cheap when current.

    Logic (plan §9.3):
      1. Local dir present **and** marker present -> no-op (already current).
      2. Else if the archive exists in the store -> restore + write the marker.
      3. Else (never backed up) -> return the path **without** creating it, so
         the caller can bootstrap a fresh folder.

    Restore failures (``WorkspaceRestoreError``) propagate — never swallowed.

    Returns:
        The local task folder path.
    """
    td = task_dir(cfg.base_dir, user_id, task_id)
    key = archive_key(cfg, user_id, task_id)

    marker = _marker_path(td)
    if td.exists() and marker.is_file():
        return td

    if await backend.exists(key):
        result = await backend.restore(key, td)  # raises WorkspaceRestoreError
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            json.dumps(
                {
                    "key": key,
                    "restored_at": datetime.now(timezone.utc).isoformat(),
                    "files": result.get("files"),
                    "bytes": result.get("bytes"),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return td

    # Never backed up: leave the folder uncreated; the caller bootstraps fresh.
    return td


async def snapshot(
    cfg: PersistenceConfig,
    backend: StorageBackend,
    user_id: str,
    task_id: str,
) -> dict:
    """Back the task folder up to the store (latest-overwrites). Call after a turn.

    The restore marker is intentionally NOT written here — it is a restore concept,
    and keeping it out of the backup path avoids archiving a stale marker.

    Raises:
        FileNotFoundError: if the task folder does not exist (no silent no-op).

    Returns:
        The backend's backup stats dict (``{key, files, bytes}``).
    """
    td = task_dir(cfg.base_dir, user_id, task_id)
    if not td.exists():
        raise FileNotFoundError(
            f"cannot snapshot: task folder does not exist: {td}"
        )
    key = archive_key(cfg, user_id, task_id)
    return await backend.backup(td, key)
