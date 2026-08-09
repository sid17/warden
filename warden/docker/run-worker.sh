#!/usr/bin/env bash
# Host-side driver for the STATELESS ONE-JOB WORKER.
#
# Runs a single orchestrator turn in a container whose only durable state is
# external: task snapshots in S3, and the session index on a host-mounted
# volume (/state). The image carries NO task state — delete and rebuild it and
# a fresh container recovers everything from (user_id, task_id, session_id) +
# S3 + the shared session index. This is the direct proof of invariant I1.
#
# Secrets are NEVER printed and NEVER baked into the image — passed at run time
# via -e / -v only.
#
# Usage:
#   AWS_ACCESS_KEY=... AWS_SECRET_ACCESS_KEY=... \
#   AWS_BUCKET_NAME=... AWS_BUCKET_LOCATION=... \
#   ./docker/run-worker.sh <user_id> <task_id> "<prompt>" [session_id]
#
# Env:
#   AWS_ACCESS_KEY / AWS_SECRET_ACCESS_KEY / AWS_BUCKET_NAME / AWS_BUCKET_LOCATION
#     — required; the S3 store for task snapshots.
#   AWS_S3_ENDPOINT — optional; point at MinIO for local testing.
#   S3_PREFIX       — optional key prefix (default: none).
#   PROVIDER        — provider (default: claude; the claude SDK adapter).
#   STATE_DIR       — host dir for the shared session index (default: ./data/worker-state).
#   IMAGE           — image tag (default: orchestrator-cli:latest).
#   CLAUDE_CODE_OAUTH_TOKEN — if unset, read from the macOS Keychain.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# ENGINE_ROOT = engine/ (docker build context; holds orchestrator/ + harness_api/).
# REPO_ROOT   = the project repo root (one level up from engine/); host data/ lives here.
ENGINE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
IMAGE="${IMAGE:-orchestrator-cli:latest}"
PROVIDER="${PROVIDER:-claude}"
STATE_DIR="${STATE_DIR:-$REPO_ROOT/data/worker-state}"

USER_ID="${1:?usage: run-worker.sh <user_id> <task_id> <prompt> [session_id]}"
TASK_ID="${2:?missing task_id}"
PROMPT="${3:?missing prompt}"
SESSION_ID="${4:-}"

: "${AWS_ACCESS_KEY:?set AWS_ACCESS_KEY}"
: "${AWS_SECRET_ACCESS_KEY:?set AWS_SECRET_ACCESS_KEY}"
: "${AWS_BUCKET_NAME:?set AWS_BUCKET_NAME}"
: "${AWS_BUCKET_LOCATION:?set AWS_BUCKET_LOCATION}"

# --- Claude OAuth token (from env or Keychain), never printed ---------------
if [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
    if RAW="$(security find-generic-password -s "Claude Code-credentials" -w 2>/dev/null)"; then
        CLAUDE_CODE_OAUTH_TOKEN="$(printf '%s' "$RAW" | python3 -c \
            'import json,sys; print(json.load(sys.stdin).get("claudeAiOauth",{}).get("accessToken","") or "")' 2>/dev/null || true)"
    fi
fi
[ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] || echo "WARNING: no CLAUDE_CODE_OAUTH_TOKEN — claude providers will fail auth."
export CLAUDE_CODE_OAUTH_TOKEN

mkdir -p "$STATE_DIR"

# --- Build (cheap when cached) ---------------------------------------------
docker build -q -f "$ENGINE_ROOT/docker/Dockerfile" -t "$IMAGE" "$ENGINE_ROOT" >/dev/null

# --- One-job run ------------------------------------------------------------
# entrypoint.sh execs any command it's given (passthrough), so we invoke the
# CLI worker directly. Its exit code is a real gate.
RESUME_ARGS=()
[ -n "$SESSION_ID" ] && RESUME_ARGS+=( --resume "$SESSION_ID" )
PREFIX_ARGS=()
[ -n "${S3_PREFIX:-}" ] && PREFIX_ARGS+=( --s3-prefix "$S3_PREFIX" )
ENDPOINT_ARGS=()
[ -n "${AWS_S3_ENDPOINT:-}" ] && ENDPOINT_ARGS+=( -e AWS_S3_ENDPOINT )

# Note: ${arr[@]+"${arr[@]}"} guards empty-array expansion under `set -u` on
# macOS's bash 3.2 (a bare "${arr[@]}" errors there when the array is empty).
exec docker run --rm \
    -e CLAUDE_CODE_OAUTH_TOKEN \
    -e AWS_ACCESS_KEY -e AWS_SECRET_ACCESS_KEY \
    -e AWS_BUCKET_NAME -e AWS_BUCKET_LOCATION \
    ${ENDPOINT_ARGS[@]+"${ENDPOINT_ARGS[@]}"} \
    -v "$STATE_DIR:/state" \
    "$IMAGE" \
    python -m warden.drive.cli --single "$PROMPT" \
        --provider "$PROVIDER" --user-id "$USER_ID" --task-id "$TASK_ID" \
        --storage-backend s3 --s3-bucket "$AWS_BUCKET_NAME" \
        ${PREFIX_ARGS[@]+"${PREFIX_ARGS[@]}"} \
        --session-db /state/sessions.db \
        ${RESUME_ARGS[@]+"${RESUME_ARGS[@]}"}
