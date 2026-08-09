# workspace/ — L0: what the agent operates on

> **Layer / role:** L0 — the workspace: the task folder plus its capabilities.
> **Implements:** [`01-conceptual-model`](../docs/01-conceptual-model.md) §4 (workspace & workflow), §5 L0, §13.
> **Inbound:** the orchestrator (L2) restores/uses the workspace each turn; `drive`/`harness_api` supply `base_dir` + `(user_id, task_id)`.
> **Outbound:** `persistence/` (the storage backend that snapshots/restores the folder). One-way: `workspace → persistence`, never the reverse.

A **workspace** is *what the agent operates on* — the task folder at
`base_dir/{user_id}/{task_id}/`, scaffolded with the skills/agents it may use and
governed by a **workflow** permission manifest. This folder holds the L0 *definition*
of that: how a workspace is described, scaffolded, and addressed. How its bytes are
durably stored is `persistence/` (L3).

## What's here

| Path | What it is |
|---|---|
| `workflow/loader.py` | Parse `.workflows/*.yaml` → `Workflow`; mtime-cached; **fail-closed** deny-baseline. |
| `workflow/__init__.py`, `workflow/permissions.py` | The `Workflow` permission-manifest model (`name`, `description`, `permissions`) + `FileAccess`/`ToolAccess`/`Permissions`. |
| `bootstrap.py` | Scaffold `.claude/{skills,agents}` from an **explicit allowlist**; records `bootstrap.lock.json` for reproducibility. |
| `task_workspace.py` | The task-folder abstraction the caller uses — `home_env` / `ensure_restored` / `snapshot`, wiring `persistence` config+keys+backend to a turn. |

## How it connects

- The **workflow** is chosen at session init and fixes the permission surface for the
  session's life ([§6](../docs/01-conceptual-model.md#s6)); the loader's deny-baseline
  (intersection of every workflow's denies) is computed once at session creation
  ([§7a](../docs/01-conceptual-model.md#s7a)).
- `task_workspace` calls `persistence/` to `ensure_restored` before a turn and
  `snapshot` after it — the L0/L3 seam is this one module.
