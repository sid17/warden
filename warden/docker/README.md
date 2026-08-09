# Containerized Orchestrator CLI — four-provider isolation smoke

Runs the orchestrator CLI inside a Docker container and smoke-tests **all four
providers** — `claude` (SDK), `claude-cli`, `codex`, and `openharness` — using
the **host's real credentials**, injected at run time. Demonstrates that the
orchestrator runs cleanly in an isolated container with no host Python env.

## What's here

| File | Role |
|------|------|
| `Dockerfile` | Image: `python:3.14-slim` + Node 20 + `claude`/`codex` CLIs + pinned Python deps + `orchestrator/`. |
| `requirements.txt` | Pinned Python deps for the CLI import chain. |
| `entrypoint.sh` | With **no args**: the four-provider demo smoke (always exits 0). With **args**: execs them (passthrough) — this is how the stateless worker runs the CLI. |
| `run.sh` | Host driver: builds the image, wires in real creds, `docker run`s the smoke. |
| `run-worker.sh` | Host driver for the **stateless one-job worker**: runs a single turn with S3 snapshots + a host-mounted session index. Exit code is a real gate. |
| `.dockerignore` | Keeps the build context small (only `orchestrator/` + `docker/`). |

## Prerequisites

- **Docker** running (tested on Docker Desktop 28.1, Apple Silicon / arm64).
- **Host Ollama** running with the `qwen3:1.7b` model pulled (for openharness):
  `ollama serve` + `ollama pull qwen3:1.7b`.
- **Logged-in `claude` on the host** — a Claude Code OAuth token in the macOS
  Keychain (item `"Claude Code-credentials"`). `run.sh` reads it automatically.
- **Logged-in `codex` on the host** — `~/.codex/auth.json` present (ChatGPT
  OAuth). `run.sh` bind-mounts it into the container.

## Run it

```bash
./docker/run.sh
```

This builds `orchestrator-cli:latest` and runs the smoke. Read the **SUMMARY**
table at the bottom for per-provider PASS/FAIL.

## Stateless worker mode (S3 restore — invariant I1)

The container is a **stateless, restartable worker**: its only durable state is
external — task snapshots in **S3** and the session index on a **host-mounted
volume** (`/state`). Delete and rebuild the image and a fresh container recovers
the full workspace (files, skills, session memory) from just
`(user_id, task_id, session_id)` + S3 + the shared index.

```bash
# 1. First turn — plants state, snapshots to S3, prints a [session: <sid>]
AWS_ACCESS_KEY=... AWS_SECRET_ACCESS_KEY=... \
AWS_BUCKET_NAME=my-bucket AWS_BUCKET_LOCATION=ap-south-1 \
./docker/run-worker.sh myuser task1 "Remember the secret number 4242."

# 2. (Optionally: docker rmi the image and rebuild — no state is lost.)

# 3. Resume — a FRESH container restores the workspace from S3 and continues
./docker/run-worker.sh myuser task1 "What was the secret number?" <sid>
```

The store is selected by a single flag (`--storage-backend s3`); bootstrap, the
CLI flow, and the providers are unchanged. Point `AWS_S3_ENDPOINT` at MinIO to
test locally without real AWS. The session index is shared via `--session-db
/state/sessions.db` on the mounted volume — **required** for a fresh container
to resume a session it did not create (without the row, resume silently falls
back to a fresh session).

## How auth flows, per provider

Auth resolution lives in `providers/auth.py`. See also
`docs/reference/provider-auth-and-home-isolation.md`.

- **claude / claude-cli** — `run.sh` extracts the Claude OAuth access token from
  the host Keychain and passes it as `-e CLAUDE_CODE_OAUTH_TOKEN`. The token is
  never printed and never baked into the image.
- **codex** — uses ChatGPT OAuth from `~/.codex/auth.json`, bind-mounted
  read-only into `CODEX_HOME=/codexhome`. Note: `is_authed("codex")` only checks
  the `OPENAI_API_KEY` env var, so it reports `False` here even though the codex
  CLI authenticates fine via the mounted `auth.json`.
- **openharness** — no cloud credential. Runs against the **host's Ollama** via
  `-e OPENHARNESS_BASE_URL=http://host.docker.internal:11434` +
  `--add-host=host.docker.internal:host-gateway`.

## Known caveats

- **Codex needs a trusted git dir.** `CodexSession` runs `codex exec -C <cwd>`
  without `--skip-git-repo-check`, so the entrypoint `git init`s its run dir
  before invoking codex. (No orchestrator code change.)
- **Codex OAuth refresh under a read-only mount.** `auth.json` is mounted `:ro`,
  so if the ChatGPT access token were stale enough to need a refresh, codex
  couldn't persist the refreshed token and would fail. Fix: refresh on the host
  (`codex login` or any codex command) before running, or mount `:rw`.
- **openharness needs host Ollama.** If Ollama isn't running (or `qwen3:1.7b`
  isn't pulled), openharness FAILs with an Ollama-unreachable error.
- **openharness CLI display gap.** `openharness` emits `stream_delta` events the
  CLI's `--single` printer suppresses, so `--single` shows blank even on
  success. The entrypoint verifies openharness with a small Python `ChatAPI`
  probe that inspects the streamed `MessageEvent` text instead.
- **Shutdown tracebacks.** The `aiosqlite` `RuntimeError: Event loop is closed`
  at teardown is now fixed — `ChatAPI.close()` closes the session index inside
  the event loop (`SessionManager.close_index`). A benign `No module named
  'opentelemetry'` (optional telemetry) from openharness may still appear; it is
  after the reply is captured and does not affect results.
