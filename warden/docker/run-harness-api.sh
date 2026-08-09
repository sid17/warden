#!/usr/bin/env bash
# Host-side driver for the LONG-LIVED HARNESS RUNS-API SERVER.
#
# Unlike run-worker.sh (one turn, then exit), this runs the stateless execution
# engine as a persistent HTTP service: products POST /runs and receive a typed,
# run_id-keyed event stream via webhook or SSE. The container holds NO durable
# job state (the run registry is in-memory) — task snapshots live in S3/local and
# the session index on the host-mounted /state volume, so a restart recovers
# workspaces + resumable sessions.
#
# Secrets are NEVER printed and NEVER baked into the image — passed at run time
# via -e / -v only. Managed per-user keys are supplied via MANAGED_KEYS_JSON (or
# a mounted MANAGED_KEYS_FILE); the actual key secrets are separate env vars the
# config points at by name.
#
# Usage:
#   ./docker/run-harness-api.sh                 # local backend, port 8080
#   PORT=9000 WARDEN_STORAGE_BACKEND=s3 \
#     AWS_ACCESS_KEY=... AWS_SECRET_ACCESS_KEY=... AWS_BUCKET_NAME=... \
#     ./docker/run-harness-api.sh
#
# Env:
#   PORT                    — host port to publish (default: 8080).
#   WARDEN_CONCURRENCY     — max concurrent runs (Semaphore N; default: 8).
#   WARDEN_STORAGE_BACKEND — local | s3 (default: local).
#   WARDEN_BASE_DIR / WARDEN_STATE_ROOT — in-container workspace/store paths.
#   WARDEN_SESSION_DB      — shared session index path (default: /state/sessions.db).
#   MANAGED_KEYS_JSON / MANAGED_KEYS_FILE — managed-key registry (see credentials/keys.py).
#   PRICING_JSON            — optional per-model pricing override for the spend cap.
#   AWS_*                   — required when WARDEN_STORAGE_BACKEND=s3.
#   STATE_DIR               — host dir for the session index (default: ./data/harness-state).
#   IMAGE                   — image tag (default: orchestrator-cli:latest).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# ENGINE_ROOT = engine/ (docker build context; holds orchestrator/ + harness_api/).
# REPO_ROOT   = the project repo root (one level up from engine/); host data/ lives here.
ENGINE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
IMAGE="${IMAGE:-orchestrator-cli:latest}"
PORT="${PORT:-9100}"
STATE_DIR="${STATE_DIR:-$REPO_ROOT/data/harness-state}"

mkdir -p "$STATE_DIR"

# --- Claude OAuth auto-discovery (macOS Keychain) --------------------------
# The provider authenticates via subscription OAuth ($0 marginal — doc 09 §2/§3.1),
# NEVER a billed API key. If CLAUDE_CODE_OAUTH_TOKEN is already in the shell we keep
# it; otherwise we read it from the Keychain the same way run.sh does: read item
# "Claude Code-credentials", parse the JSON, take claudeAiOauth.accessToken. The
# value is never printed and only forwarded via -e at run time (never baked in).
if [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
    if RAW="$(security find-generic-password -s "Claude Code-credentials" -w 2>/dev/null)"; then
        CLAUDE_CODE_OAUTH_TOKEN="$(printf '%s' "$RAW" | python3 -c \
            'import json,sys;
d=json.load(sys.stdin);
print(d.get("claudeAiOauth",{}).get("accessToken","") or "")' 2>/dev/null || true)"
    fi
    if [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
        echo "==> Claude OAuth token extracted from Keychain (value hidden)."
        export CLAUDE_CODE_OAUTH_TOKEN
    else
        echo "==> WARNING: no CLAUDE_CODE_OAUTH_TOKEN in shell or Keychain — runs that" >&2
        echo "    drive Claude will fail auth. Run 'claude setup-token' on the host." >&2
    fi
fi

# --- Build (cheap when cached) ---------------------------------------------
docker build -q -f "$ENGINE_ROOT/docker/Dockerfile" -t "$IMAGE" "$ENGINE_ROOT" >/dev/null

# --- Optional passthrough env (only forwarded when set) --------------------
PASS_ENV=(
    WARDEN_CONCURRENCY WARDEN_STORAGE_BACKEND WARDEN_BASE_DIR WARDEN_STATE_ROOT
    MANAGED_KEYS_JSON PRICING_JSON
    AWS_ACCESS_KEY AWS_SECRET_ACCESS_KEY AWS_BUCKET_NAME AWS_BUCKET_LOCATION
    AWS_S3_ENDPOINT S3_PREFIX
    ANTHROPIC_API_KEY CLAUDE_CODE_OAUTH_TOKEN OPENAI_API_KEY
)
ENV_ARGS=()
for var in "${PASS_ENV[@]}"; do
    [ -n "${!var:-}" ] && ENV_ARGS+=( -e "$var" )
done

# The session index defaults onto the mounted /state volume.
ENV_ARGS+=( -e "WARDEN_SESSION_DB=${WARDEN_SESSION_DB:-/state/sessions.db}" )

# --- Long-lived server ------------------------------------------------------
# entrypoint.sh execs any command it's given (passthrough), so we invoke uvicorn.
exec docker run --rm \
    -p "$PORT:8080" \
    ${ENV_ARGS[@]+"${ENV_ARGS[@]}"} \
    -v "$STATE_DIR:/state" \
    "$IMAGE" \
    python -m uvicorn warden.harness_api.app:app --host 0.0.0.0 --port 8080
