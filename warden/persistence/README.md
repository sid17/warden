# persistence/ — L3: the durability mechanism

> **Layer / role:** L3 cross-cutting — restore/snapshot the workspace folder; a peer of `safety/` and `observability/`.
> **Implements:** [`01-conceptual-model`](../docs/01-conceptual-model.md) §5 L3, §8 (isolation — `task_dir`/keying), §13.
> **Inbound:** `workspace/task_workspace.py` (the L0 caller), and through it the orchestrator (L2) and drive paths.
> **Outbound:** storage only (local filesystem, S3/boto3) — no dependency back on `workspace/` or the engine.

The **storage engine** for a workspace: how the task folder's bytes get durably saved
and restored, keyed by `(user_id, task_id)`. This is the *mechanism* (backends, keying,
archiving); *what* a workspace is lives in `workspace/` (L0). The split keeps an S3
backend out of the L0 "what a workspace is" folder.

## What's here

| File | What it is |
|---|---|
| `backend.py` | `StorageBackend` contract + `WorkspaceRestoreError`. |
| `local_backend.py` · `s3_backend.py` | Concrete stores (local "mock-S3" root; boto3 S3). |
| `factory.py` | `get_backend("local"|"s3", …)` — the one selection point. |
| `config.py` | `PersistenceConfig` — `base_dir` (per-task folders) + `state_root` (archive root). |
| `keys.py` | `workspace_key` / `task_dir` / `archive_key` — one derivation for local root and S3 prefix. |
| `archive.py` | Pure build/extract of a task-folder archive (caller owns placement/upload). |

## How it connects

- `restore-at-init / snapshot-after-turn` ([§6](../docs/01-conceptual-model.md#s6)): the
  orchestrator restores the folder before a turn and snapshots after — a snapshot runs
  even if the turn errored (latest-overwrites; never lose partial work).
- Keying by `(user_id, task_id)` is the **addressing** half of isolation
  ([§8](../docs/01-conceptual-model.md#s8), Axis-1) — pure mechanism, so it lives in the
  engine (the account/billing half is `harness_api/`).
