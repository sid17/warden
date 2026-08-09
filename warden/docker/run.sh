#!/usr/bin/env bash
# Host-side driver for the harness engine image (drive.cli + harness_api). Modes:
#
#   ./docker/run.sh
#       Three-provider isolation smoke (claude SDK, codex SDK, openharness)
#       using the host's REAL credentials — the retired claude-cli/codex-exec
#       adapters SKIP. Now a REAL GATE: exits non-zero if any provider that ran
#       failed.
#
#   ./docker/run.sh --build-check   (T-CONFIG-4)
#       Build the image and assert it imports in-container (harness_api.app).
#       Credential-free; proves the container the whole bed depends on assembles.
#
#   ./docker/run.sh --auth oauth      (T1 — the open question)
#   ./docker/run.sh --auth api-key    (T2 — known-good control)
#       Single-provider (claude SDK) isolation gate. Injects EXACTLY ONE Claude
#       credential and NOTHING else, so a pass can only mean the injected
#       credential authenticated — never an ambient fallback. Exits non-zero on
#       auth failure. The entrypoint additionally strips + asserts inside the
#       container as a defense in depth.
#
# Secrets are NEVER printed and NEVER baked into the image — they are passed at
# `docker run` time via -e / -v only.
set -euo pipefail

# Engine root = parent of this docker/ dir (engine/), which holds orchestrator/
# + harness_api/ + docker/ — so the Dockerfile's COPY paths resolve against it.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENGINE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
IMAGE="orchestrator-cli:latest"

# --- Parse args -----------------------------------------------------------
# --auth oauth|api-key selects the single-credential isolation gate.
# --perm-smoke runs the T4 can_use_tool e2e leg (single Claude credential).
AUTH_MODE=""
PERM_SMOKE=""
CUSTOM_TOOL=""
T3_SMOKE=""
AUTH_TYPED=""
OPENHARNESS_PERM=""
CODEX_AUTH=""
CODEX_PERM=""
CODEX_CUSTOM_TOOL=""
GOVERNANCE_BUDGET=""
GOVERNANCE_BUDGET_CODEX=""
GOVERNANCE_BUDGET_OH=""
CLAUDE_SESSION=""
CODEX_SESSION=""
OPENHARNESS_SESSION=""
CLAUDE_CRASH=""
CODEX_CRASH=""
CODEX_CRASH_S3=""
CODEX_CRASH_2C=""
C3_2C=""
OPENHARNESS_CRASH=""
BUILD_CHECK=""
TELEMETRY_TRACE=""
AUDIT_TRAIL=""
SAFETY_BLOCK=""
M6_HITL=""
M6_GATE=""
M6_HITL_CODEX=""
M6_HITL_OH=""
if [ "${1:-}" = "--auth" ]; then
    AUTH_MODE="${2:-}"
    case "$AUTH_MODE" in
        oauth|api-key) ;;
        *)
            echo "usage: $0 [--auth oauth|api-key] | [--auth-typed] | [--build-check] | [--perm-smoke] | [--custom-tool] | [--t3] | [--codex-auth] | [--codex-perm] | [--codex-custom-tool] | [--audit-trail] | [--safety-block]" >&2
            exit 2
            ;;
    esac
elif [ "${1:-}" = "--perm-smoke" ]; then
    PERM_SMOKE="1"
elif [ "${1:-}" = "--custom-tool" ]; then
    CUSTOM_TOOL="1"
elif [ "${1:-}" = "--t3" ]; then
    T3_SMOKE="1"
elif [ "${1:-}" = "--auth-typed" ]; then
    AUTH_TYPED="1"
elif [ "${1:-}" = "--openharness-perm" ]; then
    OPENHARNESS_PERM="1"
elif [ "${1:-}" = "--codex-auth" ]; then
    CODEX_AUTH="1"
elif [ "${1:-}" = "--codex-perm" ]; then
    CODEX_PERM="1"
elif [ "${1:-}" = "--codex-custom-tool" ]; then
    CODEX_CUSTOM_TOOL="1"
elif [ "${1:-}" = "--governance-budget" ]; then
    GOVERNANCE_BUDGET="1"
elif [ "${1:-}" = "--governance-budget-codex" ]; then
    GOVERNANCE_BUDGET_CODEX="1"
elif [ "${1:-}" = "--governance-budget-oh" ]; then
    GOVERNANCE_BUDGET_OH="1"
elif [ "${1:-}" = "--claude-session" ]; then
    CLAUDE_SESSION="1"
elif [ "${1:-}" = "--codex-session" ]; then
    CODEX_SESSION="1"
elif [ "${1:-}" = "--openharness-session" ]; then
    OPENHARNESS_SESSION="1"
elif [ "${1:-}" = "--claude-crash" ]; then
    CLAUDE_CRASH="1"
elif [ "${1:-}" = "--codex-crash" ]; then
    CODEX_CRASH="1"
elif [ "${1:-}" = "--codex-crash-s3" ]; then
    CODEX_CRASH_S3="1"
elif [ "${1:-}" = "--codex-crash-2c" ]; then
    CODEX_CRASH_2C="1"
elif [ "${1:-}" = "--c3-2c" ]; then
    C3_2C="1"
elif [ "${1:-}" = "--openharness-crash" ]; then
    OPENHARNESS_CRASH="1"
elif [ "${1:-}" = "--build-check" ]; then
    BUILD_CHECK="1"
elif [ "${1:-}" = "--telemetry-trace" ]; then
    TELEMETRY_TRACE="1"
elif [ "${1:-}" = "--audit-trail" ]; then
    AUDIT_TRAIL="1"
elif [ "${1:-}" = "--safety-block" ]; then
    SAFETY_BLOCK="1"
elif [ "${1:-}" = "--m6-hitl" ]; then
    M6_HITL="1"
elif [ "${1:-}" = "--m6-gate" ]; then
    M6_GATE="1"
elif [ "${1:-}" = "--m6-hitl-codex" ]; then
    M6_HITL_CODEX="1"
elif [ "${1:-}" = "--m6-hitl-oh" ]; then
    M6_HITL_OH="1"
fi

# --- Safety-block gate (--safety-block) — HOST-run (M4 acceptance) -----------
# Drives the 37-entry adversarial/benign corpus through the REAL orchestrator
# middleware cascade (recall/FPR bar), the REAL harness_api Runs pipeline (output
# canary/leak cut), and the SAFE-6 path hook + SAFE-4 canary. Host-run (no
# observability stack): free Ollama qwen3:8b judge by default (SAFETY_JUDGE=ollama)
# + OAuth Claude (Keychain) for the SAFE-6 leg. Set SAFETY_JUDGE=haiku to use the
# Haiku llm-judge cascade instead. Optional 2nd arg: all|recall|output|safe6|canary
# (default all).
if [ -n "${SAFETY_BLOCK:-}" ]; then
    REPO_ROOT="$(cd "$ENGINE_ROOT/../.." && pwd)"
    echo "==> Running safety-block gate on host (free Ollama judge + Claude OAuth) ..."
    cd "$REPO_ROOT"
    exec env -u OPENAI_API_KEY -u ANTHROPIC_API_KEY PYTHONPATH=. \
        .venv/bin/python -m warden.tests.e2e.safety_block_smoke "${2:-all}"
fi

# --- Audit-trail gate (--audit-trail) — HOST-run (M5 acceptance) ------------
# Fires an audited turn per provider (config-gated AuditConfig), validates the
# per-agent JSONL trails, runs the aggregate+derive_manifest derivation, and
# asserts the governance stop is recorded (AUD-3). Host-run (no observability
# stack needed): OAuth Claude (Keychain) + OAuth Codex (~/.codex) + free Ollama
# qwen3:8b. Optional 2nd arg: claude|openharness|codex|all (default all).
if [ -n "${AUDIT_TRAIL:-}" ]; then
    REPO_ROOT="$(cd "$ENGINE_ROOT/../.." && pwd)"
    echo "==> Running audit-trail gate on host (claude OAuth + codex OAuth + free Ollama) ..."
    cd "$REPO_ROOT"
    exec env -u OPENAI_API_KEY -u ANTHROPIC_API_KEY PYTHONPATH=. \
        .venv/bin/python -m warden.tests.e2e.audit_trail_smoke "${2:-all}"
fi

# --- Telemetry-trace gate (--telemetry-trace) — HOST-run (M3 acceptance) ----
# Unlike the container gates, this asserts enriched telemetry LANDS on the running
# observability stack (Langfuse/Tempo), so it runs on the HOST via the repo venv
# and queries localhost backends. Needs: the stack up (infra/docker-compose.
# {langfuse,observability}.yml), Claude OAuth (Keychain), free Ollama qwen3:8b.
# Optional 2nd arg selects the provider(s): claude|openharness|both (default both).
if [ -n "$TELEMETRY_TRACE" ]; then
    REPO_ROOT="$(cd "$ENGINE_ROOT/../.." && pwd)"
    LF="${LANGFUSE_HOST:-http://localhost:3456}"
    TEMPO="${TEMPO_URL:-http://localhost:3200}"
    echo "==> [1/2] Checking observability stack (Langfuse $LF, Tempo $TEMPO) ..."
    curl -sf -o /dev/null "$LF/api/public/health" \
        || { echo "Langfuse not up — bring up infra/docker-compose.langfuse.yml" >&2; exit 1; }
    curl -sf -o /dev/null "$TEMPO/ready" \
        || { echo "Tempo not up — bring up infra/docker-compose.observability.yml" >&2; exit 1; }
    echo "==> [2/2] Running telemetry-trace gate on host (claude OAuth + free Ollama) ..."
    cd "$REPO_ROOT"
    exec env -u OPENAI_API_KEY -u ANTHROPIC_API_KEY PYTHONPATH=. \
        .venv/bin/python -m warden.tests.e2e.telemetry_trace_smoke "${2:-both}"
fi

# Codex credential resolver — echoes the docker `-e`/`-v` args (one credential):
#   OAuth  → mount ~/.codex/auth.json :ro into /codexhome + set CODEX_HOME.
#   api-key→ inject OPENAI_API_KEY (from env/.env).
# Prints the args to stdout; caller splices them into `docker run`.
_codex_cred_args() {
    if [ -n "${OPENAI_API_KEY:-}" ]; then
        printf -- '-e OPENAI_API_KEY=%s' "$OPENAI_API_KEY"
        return 0
    fi
    local host_auth="${CODEX_AUTH_JSON:-$HOME/.codex/auth.json}"
    if [ -f "$host_auth" ]; then
        printf -- '-v %s:/codexhome/auth.json:ro -e CODEX_HOME=/codexhome' "$host_auth"
        return 0
    fi
    return 1
}

echo "==> Engine root:  $ENGINE_ROOT"
echo "==> Image tag:    $IMAGE"
[ -n "$AUTH_MODE" ] && echo "==> Auth mode:    $AUTH_MODE (single-credential isolation gate)"

# --- 1. Build -------------------------------------------------------------
echo
echo "==> [1/4] Building image (docker build -f engine/docker/Dockerfile) ..."
docker build -f "$ENGINE_ROOT/docker/Dockerfile" -t "$IMAGE" "$ENGINE_ROOT"

# --- Docker build-test gate (--build-check, T-CONFIG-4) -------------------
# The whole 3-layer bed depends on this image assembling. The build above
# already runs a COPY-layout + import sanity check inside the Dockerfile
# (RUN python -c "import ..."), so a successful `docker build` IS the build
# test. This flag makes that a first-class, named gate and adds an explicit
# post-build in-image import of the API app — a repeatable check that lives
# OUTSIDE the pytest suite (it needs Docker). Credential-free.
if [ -n "$BUILD_CHECK" ]; then
    echo
    echo "==> [2/2] In-image import check (warden.harness_api.app) ..."
    if docker run --rm "$IMAGE" python -c \
        "import warden.harness_api.app; import warden.drive.cli; print('import OK')"; then
        echo "=================================================================="
        echo " BUILD-CHECK GATE: PASS — image builds and imports cleanly."
        echo "=================================================================="
        exit 0
    fi
    echo "=================================================================="
    echo " BUILD-CHECK GATE: FAIL — image import failed (see output above)." >&2
    echo "=================================================================="
    exit 1
fi

# --- Single-credential isolation gate (--auth) ----------------------------
# Inject EXACTLY ONE credential; pass NOTHING else. The entrypoint strips any
# residue and asserts emptiness before the turn, then runs the claude SDK path.
if [ -n "$AUTH_MODE" ]; then
    echo
    if [ "$AUTH_MODE" = "oauth" ]; then
        echo "==> [2/2] Extracting Claude OAuth token from Keychain ..."
        OAUTH_TOKEN=""
        if RAW="$(security find-generic-password -s "Claude Code-credentials" -w 2>/dev/null)"; then
            OAUTH_TOKEN="$(printf '%s' "$RAW" | python3 -c \
                'import json,sys;
d=json.load(sys.stdin);
print(d.get("claudeAiOauth",{}).get("accessToken","") or "")' 2>/dev/null || true)"
        fi
        if [ -z "$OAUTH_TOKEN" ]; then
            echo "    ERROR: no Claude OAuth token in Keychain. Log in on the host (claude" >&2
            echo "    setup-token / claude login) before running the OAuth leg." >&2
            exit 4
        fi
        echo "    Claude OAuth token extracted (value hidden). Injecting ONLY \$CLAUDE_CODE_OAUTH_TOKEN."
        # ONLY the OAuth token crosses into the container. No -v ~/.claude mount,
        # no ANTHROPIC_API_KEY passthrough — clean isolation by construction.
        exec docker run --rm \
            -e CLAUDE_CODE_OAUTH_TOKEN="$OAUTH_TOKEN" \
            "$IMAGE" --auth oauth
    else
        echo "==> [2/2] Reading ANTHROPIC_API_KEY from environment ..."
        if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
            echo "    ERROR: ANTHROPIC_API_KEY is not set in your shell/.env. Export it" >&2
            echo "    (e.g. from .env) before running the api-key leg." >&2
            exit 4
        fi
        echo "    ANTHROPIC_API_KEY present (value hidden). Injecting ONLY \$ANTHROPIC_API_KEY."
        # ONLY the API key crosses in. OAuth token is NOT passed.
        exec docker run --rm \
            -e ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
            "$IMAGE" --auth api-key
    fi
fi

# --- T4 permission-bridge smoke (--perm-smoke) ----------------------------
# Inject EXACTLY ONE Claude credential (OAuth from Keychain if available, else
# ANTHROPIC_API_KEY from env). The entrypoint strips residue + runs the T4 leg.
if [ -n "$PERM_SMOKE" ]; then
    echo
    echo "==> [2/2] Preparing single Claude credential for T4 perm-smoke ..."
    OAUTH_TOKEN=""
    if RAW="$(security find-generic-password -s "Claude Code-credentials" -w 2>/dev/null)"; then
        OAUTH_TOKEN="$(printf '%s' "$RAW" | python3 -c \
            'import json,sys;
d=json.load(sys.stdin);
print(d.get("claudeAiOauth",{}).get("accessToken","") or "")' 2>/dev/null || true)"
    fi
    if [ -n "$OAUTH_TOKEN" ]; then
        echo "    Using Claude OAuth token (value hidden). Injecting ONLY \$CLAUDE_CODE_OAUTH_TOKEN."
        exec docker run --rm \
            -e CLAUDE_CODE_OAUTH_TOKEN="$OAUTH_TOKEN" \
            "$IMAGE" --perm-smoke
    elif [ -n "${ANTHROPIC_API_KEY:-}" ]; then
        echo "    No OAuth token; using ANTHROPIC_API_KEY (value hidden). Injecting ONLY \$ANTHROPIC_API_KEY."
        exec docker run --rm \
            -e ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
            "$IMAGE" --perm-smoke
    else
        echo "    ERROR: no Claude OAuth token in Keychain and ANTHROPIC_API_KEY unset." >&2
        echo "    Log in on the host (claude setup-token) or export ANTHROPIC_API_KEY." >&2
        exit 4
    fi
fi

# --- Custom-tool consumption smoke (--custom-tool) ------------------------
# Inject EXACTLY ONE Claude credential (OAuth from Keychain if available, else
# ANTHROPIC_API_KEY from env), same isolation as --perm-smoke. The entrypoint
# strips residue + runs the TOOL-1/2 custom-tool leg (one real Claude turn).
if [ -n "$CUSTOM_TOOL" ]; then
    echo
    echo "==> [2/2] Preparing single Claude credential for --custom-tool ..."
    OAUTH_TOKEN=""
    if RAW="$(security find-generic-password -s "Claude Code-credentials" -w 2>/dev/null)"; then
        OAUTH_TOKEN="$(printf '%s' "$RAW" | python3 -c \
            'import json,sys;
d=json.load(sys.stdin);
print(d.get("claudeAiOauth",{}).get("accessToken","") or "")' 2>/dev/null || true)"
    fi
    if [ -n "$OAUTH_TOKEN" ]; then
        echo "    Using Claude OAuth token (value hidden). Injecting ONLY \$CLAUDE_CODE_OAUTH_TOKEN."
        exec docker run --rm \
            -e CLAUDE_CODE_OAUTH_TOKEN="$OAUTH_TOKEN" \
            "$IMAGE" --custom-tool
    elif [ -n "${ANTHROPIC_API_KEY:-}" ]; then
        echo "    No OAuth token; using ANTHROPIC_API_KEY (value hidden). Injecting ONLY \$ANTHROPIC_API_KEY."
        exec docker run --rm \
            -e ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
            "$IMAGE" --custom-tool
    else
        echo "    ERROR: no Claude OAuth token in Keychain and ANTHROPIC_API_KEY unset." >&2
        exit 4
    fi
fi

# --- Per-run auth isolation smoke (--t3) ----------------------------------
# Credential-free structural gate (bogus keys, no real turn); still routed
# through the container for a repeatable, isolated run.
if [ -n "$T3_SMOKE" ]; then
    echo
    echo "==> [2/2] Running T3 per-run auth isolation smoke (no credential) ..."
    exec docker run --rm "$IMAGE" --t3
fi

# --- Typed auth resolve→inject gate (--auth-typed) ------------------------
# pre-03 M0: the unified auth path (typed AuthMethod, JSONL store, policy gate,
# AUTH-4 multi-cred, no os.environ bleed). Credential-free — the resolve→inject
# proof holds secrets by REFERENCE to fake env-var names, so it runs green in the
# real image with no cloud cred. Routed through the container to prove it in the bed.
if [ -n "$AUTH_TYPED" ]; then
    echo
    echo "==> [2/2] Running typed auth resolve→inject gate (no credential) ..."
    exec docker run --rm "$IMAGE" --auth-typed
fi

# --- Codex SDK auth smoke (--codex-auth) ----------------------------------
# One codex credential: OPENAI_API_KEY if set, else the mounted ~/.codex/auth.json
# (OAuth). The entrypoint strips residue + runs the hello turn.
if [ -n "$CODEX_AUTH" ]; then
    echo
    echo "==> [2/2] Preparing single codex credential for --codex-auth ..."
    if ! CRED_ARGS="$(_codex_cred_args)"; then
        echo "    ERROR: no codex credential. Export OPENAI_API_KEY or ensure ~/.codex/auth.json exists." >&2
        exit 1
    fi
    echo "    Injecting single codex credential (value/path hidden by docker)."
    # shellcheck disable=SC2086
    exec docker run --rm $CRED_ARGS "$IMAGE" --codex-auth
fi

# --- Codex SDK permission fail-closed smoke (--codex-perm) — LOAD-BEARING --
if [ -n "$CODEX_PERM" ]; then
    echo
    echo "==> [2/2] Preparing single codex credential for --codex-perm (T4) ..."
    if ! CRED_ARGS="$(_codex_cred_args)"; then
        echo "    ERROR: no codex credential. Export OPENAI_API_KEY or ensure ~/.codex/auth.json exists." >&2
        exit 1
    fi
    echo "    Injecting single codex credential (value/path hidden by docker)."
    # shellcheck disable=SC2086
    exec docker run --rm $CRED_ARGS "$IMAGE" --codex-perm
fi

# --- Codex SDK ungated custom-tool via in-proc MCP (--codex-custom-tool) ---
if [ -n "$CODEX_CUSTOM_TOOL" ]; then
    echo
    echo "==> [2/2] Preparing single codex credential for --codex-custom-tool ..."
    if ! CRED_ARGS="$(_codex_cred_args)"; then
        echo "    ERROR: no codex credential. Export OPENAI_API_KEY or ensure ~/.codex/auth.json exists." >&2
        exit 1
    fi
    echo "    Injecting single codex credential (value/path hidden by docker)."
    # shellcheck disable=SC2086
    exec docker run --rm $CRED_ARGS "$IMAGE" --codex-custom-tool
fi

# --- OpenHarness perm + custom-tool smoke (--openharness-perm) ------------
# Free Ollama lane: no cloud credential. Reaches the host's Ollama via
# host.docker.internal (qwen3:8b for reliable tool-calling).
if [ -n "$OPENHARNESS_PERM" ]; then
    echo
    echo "==> [2/2] Running OpenHarness perm+custom-tool smoke on host Ollama ..."
    echo "    openharness -> http://host.docker.internal:11434 (model qwen3:8b)"
    exec docker run --rm \
        -e OPENHARNESS_BASE_URL="http://host.docker.internal:11434" \
        -e OPENHARNESS_MODEL="qwen3:8b" \
        --add-host=host.docker.internal:host-gateway \
        "$IMAGE" --openharness-perm
fi

# --- Session resume-recall gates (docs/08 S1–S4) --------------------------
# Each plants a fact → closes → resumes the SAME id in a fresh manager → asks
# the model to recall it. Single credential per provider (same isolation as the
# perm gates). Claude/Codex use OAuth; OpenHarness uses the free host Ollama.

# Governance budget gate (Claude OAuth from Keychain, else ANTHROPIC_API_KEY).
# OAuth-first — never inject the billed API key when a token exists.
if [ -n "$GOVERNANCE_BUDGET" ]; then
    echo
    echo "==> [2/2] Preparing single Claude credential for --governance-budget ..."
    OAUTH_TOKEN=""
    if RAW="$(security find-generic-password -s "Claude Code-credentials" -w 2>/dev/null)"; then
        OAUTH_TOKEN="$(printf '%s' "$RAW" | python3 -c \
            'import json,sys;
d=json.load(sys.stdin);
print(d.get("claudeAiOauth",{}).get("accessToken","") or "")' 2>/dev/null || true)"
    fi
    if [ -n "$OAUTH_TOKEN" ]; then
        echo "    Using Claude OAuth token (value hidden). Injecting ONLY \$CLAUDE_CODE_OAUTH_TOKEN."
        exec docker run --rm \
            -e CLAUDE_CODE_OAUTH_TOKEN="$OAUTH_TOKEN" \
            "$IMAGE" --governance-budget
    elif [ -n "${ANTHROPIC_API_KEY:-}" ]; then
        echo "    No OAuth token; using ANTHROPIC_API_KEY (value hidden). Injecting ONLY \$ANTHROPIC_API_KEY."
        exec docker run --rm \
            -e ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
            "$IMAGE" --governance-budget
    else
        echo "    ERROR: no Claude OAuth token in Keychain and ANTHROPIC_API_KEY unset." >&2
        echo "    Log in on the host (claude setup-token) or export ANTHROPIC_API_KEY." >&2
        exit 4
    fi
fi

# Governance budget gate — codex (single OAuth credential; OPENAI_API_KEY unset
# forces the auth.json OAuth lane, never the billed key). codex is UNPRICED by the
# default table → the gate proves reject + the unpriced (time/turn-governed) lane.
if [ -n "$GOVERNANCE_BUDGET_CODEX" ]; then
    echo
    echo "==> [2/2] Preparing single codex credential for --governance-budget-codex ..."
    if ! CRED_ARGS="$(_codex_cred_args)"; then
        echo "    ERROR: no codex credential. Ensure ~/.codex/auth.json exists (OAuth)." >&2
        exit 1
    fi
    echo "    Injecting single codex credential (value/path hidden by docker)."
    # shellcheck disable=SC2086
    exec docker run --rm $CRED_ARGS "$IMAGE" --governance-budget-codex
fi

# Governance budget gate — openharness (free host Ollama, no cloud credential).
if [ -n "$GOVERNANCE_BUDGET_OH" ]; then
    echo
    echo "==> [2/2] Running governance budget gate on host Ollama (openharness) ..."
    echo "    openharness -> http://host.docker.internal:11434 (model qwen3:8b)"
    exec docker run --rm \
        -e OPENHARNESS_BASE_URL="http://host.docker.internal:11434" \
        -e OPENHARNESS_MODEL="qwen3:8b" \
        --add-host=host.docker.internal:host-gateway \
        "$IMAGE" --governance-budget-oh
fi

# M6 durable-HITL gate — claude (HARD gate; OAuth from Keychain, else ANTHROPIC_API_KEY).
if [ -n "$M6_HITL" ]; then
    echo
    echo "==> [2/2] Preparing single Claude credential for --m6-hitl ..."
    OAUTH_TOKEN=""
    if RAW="$(security find-generic-password -s "Claude Code-credentials" -w 2>/dev/null)"; then
        OAUTH_TOKEN="$(printf '%s' "$RAW" | python3 -c \
            'import json,sys;
d=json.load(sys.stdin);
print(d.get("claudeAiOauth",{}).get("accessToken","") or "")' 2>/dev/null || true)"
    fi
    # Forward the optional matrix knobs (unset ⇒ entrypoint defaults apply).
    M6_FWD=(-e M6_CASES -e M6_TOOLS -e M6_TIMEOUT_S)
    if [ -n "$OAUTH_TOKEN" ]; then
        echo "    Using Claude OAuth token (value hidden). Injecting ONLY \$CLAUDE_CODE_OAUTH_TOKEN."
        exec docker run --rm -e CLAUDE_CODE_OAUTH_TOKEN="$OAUTH_TOKEN" "${M6_FWD[@]}" "$IMAGE" --m6-hitl
    elif [ -n "${ANTHROPIC_API_KEY:-}" ]; then
        echo "    No OAuth token; using ANTHROPIC_API_KEY (value hidden). Injecting ONLY \$ANTHROPIC_API_KEY."
        exec docker run --rm -e ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" "${M6_FWD[@]}" "$IMAGE" --m6-hitl
    else
        echo "    ERROR: no Claude OAuth token in Keychain and ANTHROPIC_API_KEY unset." >&2
        echo "    Log in on the host (claude setup-token) or export ANTHROPIC_API_KEY." >&2
        exit 4
    fi
fi

# M6 landscape-gate — claude (HARD; OAuth from Keychain, else ANTHROPIC_API_KEY).
if [ -n "$M6_GATE" ]; then
    echo
    echo "==> [2/2] Preparing single Claude credential for --m6-gate ..."
    OAUTH_TOKEN=""
    if RAW="$(security find-generic-password -s "Claude Code-credentials" -w 2>/dev/null)"; then
        OAUTH_TOKEN="$(printf '%s' "$RAW" | python3 -c \
            'import json,sys;
d=json.load(sys.stdin);
print(d.get("claudeAiOauth",{}).get("accessToken","") or "")' 2>/dev/null || true)"
    fi
    M6_FWD=(-e M6_CASES -e M6_TIMEOUT_S)
    if [ -n "$OAUTH_TOKEN" ]; then
        echo "    Using Claude OAuth token (value hidden). Injecting ONLY \$CLAUDE_CODE_OAUTH_TOKEN."
        exec docker run --rm -e CLAUDE_CODE_OAUTH_TOKEN="$OAUTH_TOKEN" "${M6_FWD[@]}" "$IMAGE" --m6-gate
    elif [ -n "${ANTHROPIC_API_KEY:-}" ]; then
        echo "    No OAuth token; using ANTHROPIC_API_KEY (value hidden). Injecting ONLY \$ANTHROPIC_API_KEY."
        exec docker run --rm -e ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" "${M6_FWD[@]}" "$IMAGE" --m6-gate
    else
        echo "    ERROR: no Claude OAuth token in Keychain and ANTHROPIC_API_KEY unset." >&2
        echo "    Log in on the host (claude setup-token) or export ANTHROPIC_API_KEY." >&2
        exit 4
    fi
fi

# M6 durable-HITL gate — codex (single OAuth credential; OPENAI_API_KEY unset forces
# the auth.json OAuth lane). Report-mode (mechanics gate); custom is N/A (ungated).
if [ -n "$M6_HITL_CODEX" ]; then
    echo
    echo "==> [2/2] Preparing single codex credential for --m6-hitl-codex ..."
    if ! CRED_ARGS="$(_codex_cred_args)"; then
        echo "    ERROR: no codex credential. Ensure ~/.codex/auth.json exists (OAuth)." >&2
        exit 1
    fi
    echo "    Injecting single codex credential (value/path hidden by docker)."
    # shellcheck disable=SC2086
    exec docker run --rm $CRED_ARGS -e M6_CASES -e M6_TOOLS -e M6_TIMEOUT_S "$IMAGE" --m6-hitl-codex
fi

# M6 durable-HITL gate — openharness (free host Ollama, no cloud credential).
if [ -n "$M6_HITL_OH" ]; then
    echo
    echo "==> [2/2] Running M6 durable-HITL gate on host Ollama (openharness) ..."
    echo "    openharness -> http://host.docker.internal:11434 (model qwen3:8b)"
    exec docker run --rm \
        -e OPENHARNESS_BASE_URL="http://host.docker.internal:11434" \
        -e OPENHARNESS_MODEL="qwen3:8b" \
        -e M6_CASES -e M6_TOOLS -e M6_TIMEOUT_S \
        --add-host=host.docker.internal:host-gateway \
        "$IMAGE" --m6-hitl-oh
fi

# Claude resume-recall (OAuth from Keychain, else ANTHROPIC_API_KEY).
if [ -n "$CLAUDE_SESSION" ]; then
    echo
    echo "==> [2/2] Preparing single Claude credential for --claude-session ..."
    OAUTH_TOKEN=""
    if RAW="$(security find-generic-password -s "Claude Code-credentials" -w 2>/dev/null)"; then
        OAUTH_TOKEN="$(printf '%s' "$RAW" | python3 -c \
            'import json,sys;
d=json.load(sys.stdin);
print(d.get("claudeAiOauth",{}).get("accessToken","") or "")' 2>/dev/null || true)"
    fi
    if [ -n "$OAUTH_TOKEN" ]; then
        echo "    Using Claude OAuth token (value hidden). Injecting ONLY \$CLAUDE_CODE_OAUTH_TOKEN."
        exec docker run --rm \
            -e CLAUDE_CODE_OAUTH_TOKEN="$OAUTH_TOKEN" \
            "$IMAGE" --claude-session
    elif [ -n "${ANTHROPIC_API_KEY:-}" ]; then
        echo "    No OAuth token; using ANTHROPIC_API_KEY (value hidden). Injecting ONLY \$ANTHROPIC_API_KEY."
        exec docker run --rm \
            -e ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
            "$IMAGE" --claude-session
    else
        echo "    ERROR: no Claude OAuth token in Keychain and ANTHROPIC_API_KEY unset." >&2
        echo "    Log in on the host (claude setup-token) or export ANTHROPIC_API_KEY." >&2
        exit 4
    fi
fi

# Codex resume-recall (single codex credential; run with OPENAI_API_KEY unset to
# force the OAuth auth.json lane and avoid the billed API-key lane).
if [ -n "$CODEX_SESSION" ]; then
    echo
    echo "==> [2/2] Preparing single codex credential for --codex-session ..."
    if ! CRED_ARGS="$(_codex_cred_args)"; then
        echo "    ERROR: no codex credential. Ensure ~/.codex/auth.json exists (OAuth)." >&2
        exit 1
    fi
    echo "    Injecting single codex credential (value/path hidden by docker)."
    # shellcheck disable=SC2086
    exec docker run --rm $CRED_ARGS "$IMAGE" --codex-session
fi

# OpenHarness resume-recall (free Ollama lane, no cloud credential).
if [ -n "$OPENHARNESS_SESSION" ]; then
    echo
    echo "==> [2/2] Running OpenHarness resume-recall gate on host Ollama ..."
    echo "    openharness -> http://host.docker.internal:11434 (model qwen3:8b)"
    exec docker run --rm \
        -e OPENHARNESS_BASE_URL="http://host.docker.internal:11434" \
        -e OPENHARNESS_MODEL="qwen3:8b" \
        --add-host=host.docker.internal:host-gateway \
        "$IMAGE" --openharness-session
fi

# --- Crash-recovery gates (docs/08 S6) ------------------------------------
# Same credential wiring as the session gates; the driver additionally drives a
# persistence-active snapshot → WIPE task dir → restore → resume → recall.

if [ -n "$CLAUDE_CRASH" ]; then
    echo
    echo "==> [2/2] Preparing single Claude credential for --claude-crash ..."
    OAUTH_TOKEN=""
    if RAW="$(security find-generic-password -s "Claude Code-credentials" -w 2>/dev/null)"; then
        OAUTH_TOKEN="$(printf '%s' "$RAW" | python3 -c \
            'import json,sys;
d=json.load(sys.stdin);
print(d.get("claudeAiOauth",{}).get("accessToken","") or "")' 2>/dev/null || true)"
    fi
    if [ -n "$OAUTH_TOKEN" ]; then
        echo "    Using Claude OAuth token (value hidden). Injecting ONLY \$CLAUDE_CODE_OAUTH_TOKEN."
        exec docker run --rm \
            -e CLAUDE_CODE_OAUTH_TOKEN="$OAUTH_TOKEN" \
            "$IMAGE" --claude-crash
    elif [ -n "${ANTHROPIC_API_KEY:-}" ]; then
        echo "    No OAuth token; using ANTHROPIC_API_KEY (value hidden). Injecting ONLY \$ANTHROPIC_API_KEY."
        exec docker run --rm \
            -e ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
            "$IMAGE" --claude-crash
    else
        echo "    ERROR: no Claude OAuth token in Keychain and ANTHROPIC_API_KEY unset." >&2
        exit 4
    fi
fi

if [ -n "$CODEX_CRASH" ]; then
    echo
    echo "==> [2/2] Preparing single codex credential for --codex-crash ..."
    if ! CRED_ARGS="$(_codex_cred_args)"; then
        echo "    ERROR: no codex credential. Ensure ~/.codex/auth.json exists (OAuth)." >&2
        exit 1
    fi
    echo "    Injecting single codex credential (value/path hidden by docker)."
    # shellcheck disable=SC2086
    exec docker run --rm $CRED_ARGS "$IMAGE" --codex-crash
fi

# --- A3: Codex crash-recovery on a REMOTE (S3/MinIO) backend --------------
# Same flow as --codex-crash but snapshots to S3. Pass the codex credential AND
# the S3 config (bucket + AWS_* creds; AWS_S3_ENDPOINT for MinIO). Proves the
# remote object carries the transcript but NOT the OAuth token (A2+A3).
#   AWS_BUCKET_NAME=my-bucket AWS_ACCESS_KEY_ID=… AWS_SECRET_ACCESS_KEY=… \
#   [AWS_S3_ENDPOINT=http://host.docker.internal:9000] ./docker/run.sh --codex-crash-s3
if [ -n "$CODEX_CRASH_S3" ]; then
    echo
    echo "==> [2/2] Preparing codex + S3 credentials for --codex-crash-s3 ..."
    if ! CRED_ARGS="$(_codex_cred_args)"; then
        echo "    ERROR: no codex credential. Ensure ~/.codex/auth.json exists (OAuth)." >&2
        exit 1
    fi
    if [ -z "${AWS_BUCKET_NAME:-}" ]; then
        echo "    ERROR: AWS_BUCKET_NAME unset — needed for the S3 backend." >&2
        exit 1
    fi
    S3_ARGS=( -e "AWS_BUCKET_NAME=${AWS_BUCKET_NAME}" )
    [ -n "${AWS_ACCESS_KEY_ID:-}" ]     && S3_ARGS+=( -e "AWS_ACCESS_KEY_ID=${AWS_ACCESS_KEY_ID}" )
    [ -n "${AWS_SECRET_ACCESS_KEY:-}" ] && S3_ARGS+=( -e "AWS_SECRET_ACCESS_KEY=${AWS_SECRET_ACCESS_KEY}" )
    [ -n "${AWS_REGION:-}" ]            && S3_ARGS+=( -e "AWS_REGION=${AWS_REGION}" )
    [ -n "${AWS_S3_ENDPOINT:-}" ]       && S3_ARGS+=( -e "AWS_S3_ENDPOINT=${AWS_S3_ENDPOINT}" )
    [ -n "${S3_PREFIX:-}" ]             && S3_ARGS+=( -e "S3_PREFIX=${S3_PREFIX}" )
    echo "    Injecting codex credential + S3 config (bucket=${AWS_BUCKET_NAME}, "
    echo "    endpoint=${AWS_S3_ENDPOINT:-<aws>}; secrets hidden)."
    # shellcheck disable=SC2086
    exec docker run --rm $CRED_ARGS "${S3_ARGS[@]}" \
        --add-host=host.docker.internal:host-gateway \
        "$IMAGE" --codex-crash-s3
fi

# --- A4: Codex TWO-CONTAINER (cross-host) crash-recovery ------------------
# Two separate `docker run`s share a snapshot-backend volume AND a session-db
# volume; each container has a FRESH task FS by construction. Container 1 plants
# + snapshots; container 2 (nothing on its local disk) --resume's and recalls.
# This is the real stateless-worker proof (docs/08 Part F.3 point 5).
if [ -n "$CODEX_CRASH_2C" ]; then
    echo
    echo "==> [2/2] Two-container codex crash-recovery (cross-host proof) ..."
    if ! CRED_ARGS="$(_codex_cred_args)"; then
        echo "    ERROR: no codex credential. Ensure ~/.codex/auth.json exists (OAuth)." >&2
        exit 1
    fi
    # Shared host volumes: the snapshot store + the durable session index. Each
    # container mounts them at the same in-container paths; their task work dirs
    # are NOT shared (fresh FS per container = a genuine cross-host wipe).
    SHARED="$(mktemp -d)"
    mkdir -p "$SHARED/store" "$SHARED/state"
    echo "    shared backend+index volume: $SHARED"
    VOLS=( -v "$SHARED/store:/backend" -v "$SHARED/state:/state" )
    ENVS=( -e "WARDEN_STATE_ROOT=/backend" -e "WARDEN_BASE_DIR=/work/ws" \
           -e "WARDEN_SESSION_DB=/state/sessions.db" )
    UT=( --user-id default --task-id twoc_task --storage-backend local \
         --session-db /state/sessions.db )

    echo "    [container 1] plant → snapshot to the shared backend ..."
    # shellcheck disable=SC2086
    SID="$(docker run --rm $CRED_ARGS "${VOLS[@]}" "${ENVS[@]}" "$IMAGE" \
        python -m warden.drive.cli --single \
        'Remember this exactly for later: my favorite color is heliotrope. Just acknowledge with the single word: ok.' \
        --provider codex "${UT[@]}" 2>/dev/null | sed -n 's/.*\[session: \([^]]*\)\].*/\1/p' | head -1)"
    if [ -z "$SID" ]; then
        echo "    FAIL — container 1 produced no session id." >&2
        rm -rf "$SHARED"; exit 1
    fi
    echo "    [container 1] planted session: $SID"

    echo "    [container 2] FRESH FS → --resume $SID → recall ..."
    # shellcheck disable=SC2086
    REPLY2="$(docker run --rm $CRED_ARGS "${VOLS[@]}" "${ENVS[@]}" "$IMAGE" \
        python -m warden.drive.cli --single \
        'What is my favorite color? Answer with only the single color word.' \
        --provider codex "${UT[@]}" --resume "$SID" 2>/dev/null)"
    echo "    [container 2] reply: $(printf '%s' "$REPLY2" | tr '\n' ' ' | cut -c1-160)"
    rm -rf "$SHARED"
    echo "=================================================================="
    if printf '%s' "$REPLY2" | grep -qi 'heliotrope'; then
        echo " CODEX 2-CONTAINER CRASH GATE: PASS — a brand-new container recalled the"
        echo " planted fact from only the shared snapshot + index (stateless-worker proof)."
        echo "=================================================================="
        exit 0
    fi
    echo " CODEX 2-CONTAINER CRASH GATE: FAIL — recall did not survive the cross-host resume."
    echo "=================================================================="
    exit 1
fi

# --- EXT-C3: TWO-CONTAINER MULTI-REPLICA on a SHARED EPHEMERAL POSTGRES --------
# The culminating integration proof for EXT-C3 (C3a shared stores + C3b distributed
# (user,task) lease + C3c cross-replica cold-resume). Stand up a THROWAWAY Postgres
# + a dedicated docker network, then run TWO harness containers on it sharing the one
# DSN (WARDEN_STATE_BACKEND=postgres). Proves (a) a run created on A is resolvable on
# B, (b) two containers can't both hold one (user,task) lease, (c) a run PAUSED on A is
# cold-resumed + completed on B. ZERO credentials (model-free inline mock). The
# ephemeral PG + network are torn down on exit (trap), pass OR fail — the host dev DB
# is never touched.
if [ -n "$C3_2C" ]; then
    echo
    echo "==> [2/2] EXT-C3 two-container multi-replica gate (ephemeral Postgres) ..."

    NET="c3net-$$"
    PG="c3pg-$$"
    HOLD="c3hold-$$"
    PG_IMAGE="${C3_PG_IMAGE:-paradedb/paradedb:latest}"
    DSN="postgresql://phoenix:phoenix_dev@${PG}:5432/harness_c3"

    # Tear down the ephemeral PG + hold container + network on ANY exit (pass/fail).
    _c3_teardown() {
        docker rm -f "$HOLD" >/dev/null 2>&1 || true
        docker rm -f "$PG"   >/dev/null 2>&1 || true
        docker network rm "$NET" >/dev/null 2>&1 || true
    }
    trap _c3_teardown EXIT

    echo "    network=$NET  pg=$PG  image=$PG_IMAGE"
    docker network create "$NET" >/dev/null

    echo "    starting throwaway Postgres (harness_c3; NOT the dev DB) ..."
    docker run -d --name "$PG" --network "$NET" \
        -e POSTGRES_USER=phoenix -e POSTGRES_PASSWORD=phoenix_dev \
        -e POSTGRES_DB=harness_c3 "$PG_IMAGE" >/dev/null

    RUNENV=( -e "WARDEN_STATE_BACKEND=postgres" -e "WARDEN_POSTGRES_DSN=${DSN}" )
    DRIVER="warden.tests.e2e.c3_multireplica_smoke"

    # Wait for a REAL NETWORK connection, not `pg_isready`. paradedb runs its init and
    # RESTARTS the server to load extensions: during that window `pg_isready` (the local
    # socket, checked via `docker exec`) reports ready while NETWORK connections to
    # <pg>:5432 are still REFUSED. The first gate container (the (b) lock-hold) would
    # otherwise start in that window, get ConnectionRefused on its single connect, and
    # die → "container A never acquired the lease". Probe with the SAME image + DSN the
    # gate containers use (asyncpg over the network), so we only proceed once the DB
    # truly accepts network connections (past the init-restart).
    echo "    waiting for Postgres to accept NETWORK connections (past init-restart) ..."
    PG_UP=""
    for _ in $(seq 1 60); do
        if docker run --rm --network "$NET" "${RUNENV[@]}" "$IMAGE" \
            python -c "import asyncio, asyncpg; asyncio.run(asyncpg.connect('${DSN}'))" \
            >/dev/null 2>&1; then
            PG_UP="1"; break
        fi
        sleep 1
    done
    if [ -z "$PG_UP" ]; then
        echo " C3 2-CONTAINER GATE: FAIL — ephemeral Postgres never accepted a network connection." >&2
        exit 1
    fi
    echo "    Postgres ready (network-verified)."

    RC=0

    # --- (b) MUTUAL EXCLUSION — cross-container, concurrent -------------------
    # Container A holds the (u1,c3-lock) lease in the background; while it holds, B's
    # claim must be REFUSED; after A releases, B's claim must succeed.
    echo
    echo "    (b) exclusion: container A holds the lease; B attempts concurrently ..."
    docker run -d --name "$HOLD" --network "$NET" "${RUNENV[@]}" "$IMAGE" \
        python -m "$DRIVER" lock-hold u1 c3-lock 120 >/dev/null
    # Wait until A has actually acquired the lease (prints LEASE_HELD).
    HELD=""
    for _ in $(seq 1 30); do
        if docker logs "$HOLD" 2>&1 | grep -q "LEASE_HELD"; then HELD="1"; break; fi
        sleep 1
    done
    if [ -z "$HELD" ]; then
        echo "      FAIL — container A never acquired the lease." >&2
        RC=1
    else
        # B tries to claim while A holds → expect CLAIM=REFUSED (driver exit 3). The
        # `|| true` is load-bearing under `set -e`: REFUSED is the EXPECTED outcome, so
        # its non-zero exit must not abort the gate — we read CLAIM= from the text.
        B_OUT="$(docker run --rm --network "$NET" "${RUNENV[@]}" "$IMAGE" \
            python -m "$DRIVER" lock-try u1 c3-lock 4 2>&1 || true)"
        echo "      [B while A holds] $(printf '%s' "$B_OUT" | grep -E 'CLAIM=' | head -1)"
        if printf '%s' "$B_OUT" | grep -q "CLAIM=REFUSED"; then
            echo "      (b) PASS(1/2) — B's claim REFUSED while A holds the lease."
        else
            echo "      (b) FAIL — B was NOT refused while A held the lease." >&2
            RC=1
        fi
        # Gracefully STOP A (SIGTERM → the driver exits the hold CM → the REAL
        # owner-guarded _release runs, freeing the row), wait for LEASE_RELEASED, then
        # B must be able to claim the freed key. `docker stop` (not `rm -f`/SIGKILL) so
        # the lease is genuinely RELEASED, not left to expire.
        docker stop -t 15 "$HOLD" >/dev/null 2>&1 || true
        for _ in $(seq 1 20); do
            docker logs "$HOLD" 2>&1 | grep -q "LEASE_RELEASED" && break
            sleep 1
        done
        docker rm -f "$HOLD" >/dev/null 2>&1 || true
        B_OUT2="$(docker run --rm --network "$NET" "${RUNENV[@]}" "$IMAGE" \
            python -m "$DRIVER" lock-try u1 c3-lock 8 2>&1 || true)"
        echo "      [B after A gone] $(printf '%s' "$B_OUT2" | grep -E 'CLAIM=' | head -1)"
        if printf '%s' "$B_OUT2" | grep -q "CLAIM=OK"; then
            echo "      (b) PASS(2/2) — B claims the freed lease once A releases."
        else
            echo "      (b) FAIL — B could not claim after A released." >&2
            RC=1
        fi
    fi

    # --- (a) VISIBILITY + (c) COLD-RESUME — A seeds, B verifies ---------------
    echo
    echo "    (a)+(c): container A seeds a visible run + a paused HITL run ..."
    A_OUT="$(docker run --rm --network "$NET" "${RUNENV[@]}" "$IMAGE" \
        python -m "$DRIVER" a-seed 2>&1 || true)"
    echo "$A_OUT" | grep -E "A_SEED|FAIL" | sed 's/^/      /'
    VIS="$(printf '%s' "$A_OUT" | sed -n 's/^VISIBLE_RUN=\(.*\)$/\1/p' | head -1)"
    PAUSED="$(printf '%s' "$A_OUT" | sed -n 's/^PAUSED_RUN=\(.*\)$/\1/p' | head -1)"
    if [ -z "$VIS" ] || [ -z "$PAUSED" ]; then
        echo "      FAIL — container A did not seed both runs (see output above)." >&2
        echo "$A_OUT" | tail -n 20 | sed 's/^/      /'
        RC=1
    else
        echo "      seeded VISIBLE_RUN=$VIS  PAUSED_RUN=$PAUSED"
        echo "    container B (fresh, never held them) verifies visibility + cold-resume ..."
        B_OUT3="$(docker run --rm --network "$NET" "${RUNENV[@]}" "$IMAGE" \
            python -m "$DRIVER" b-verify "$VIS" "$PAUSED" 2>&1 || true)"
        echo "$B_OUT3" | grep -E "\[a\]|\[c\]|GATE:" | sed 's/^/      /'
        if ! printf '%s' "$B_OUT3" | grep -q "GATE: PASS"; then
            echo "      FAIL — B's verify did not reach GATE: PASS." >&2
            echo "$B_OUT3" | tail -n 25 | sed 's/^/      /'
            RC=1
        fi
    fi

    echo
    echo "=================================================================="
    if [ "$RC" -eq 0 ]; then
        echo " C3 2-CONTAINER GATE: PASS — shared-PG visibility (a) + distributed lease"
        echo " exclusion (b) + cross-replica cold-resume (c), two real containers."
        echo " GATE: PASS"
    else
        echo " C3 2-CONTAINER GATE: FAIL — see the per-check lines above."
        echo " GATE: FAIL"
    fi
    echo "=================================================================="
    exit "$RC"
fi

if [ -n "$OPENHARNESS_CRASH" ]; then
    echo
    echo "==> [2/2] Running OpenHarness crash-recovery gate on host Ollama ..."
    echo "    openharness -> http://host.docker.internal:11434 (model qwen3:8b)"
    exec docker run --rm \
        -e OPENHARNESS_BASE_URL="http://host.docker.internal:11434" \
        -e OPENHARNESS_MODEL="qwen3:8b" \
        --add-host=host.docker.internal:host-gateway \
        "$IMAGE" --openharness-crash
fi

# ==========================================================================
# Default: four-provider isolation smoke (unchanged wiring; now a real gate).
# ==========================================================================

# --- 2. Claude OAuth token from macOS Keychain ----------------------------
# Mirrors the test fixture: read item "Claude Code-credentials", parse JSON,
# take claudeAiOauth.accessToken. Never echo the value.
echo
echo "==> [2/4] Extracting Claude OAuth token from Keychain ..."
CLAUDE_CODE_OAUTH_TOKEN=""
if RAW="$(security find-generic-password -s "Claude Code-credentials" -w 2>/dev/null)"; then
    CLAUDE_CODE_OAUTH_TOKEN="$(printf '%s' "$RAW" | python3 -c \
        'import json,sys;
d=json.load(sys.stdin);
print(d.get("claudeAiOauth",{}).get("accessToken","") or "")' 2>/dev/null || true)"
fi
if [ -n "$CLAUDE_CODE_OAUTH_TOKEN" ]; then
    echo "    Claude OAuth token extracted (value hidden)."
else
    echo "    WARNING: no Claude OAuth token found — claude/claude-cli will SKIP/FAIL auth."
fi
export CLAUDE_CODE_OAUTH_TOKEN

# --- 3. Codex home (ChatGPT OAuth via auth.json) --------------------------
# Bind-mount the host ~/.codex creds into a writable CODEX_HOME in-container.
# Read-only mounts can't persist an OAuth refresh; if the access token is stale
# codex may fail to refresh — that's a valid isolation finding, reported as-is.
echo
echo "==> [3/4] Preparing Codex credentials mount ..."
CODEX_ARGS=()
HOST_CODEX="$HOME/.codex"
if [ -f "$HOST_CODEX/auth.json" ]; then
    CODEX_ARGS+=( -e CODEX_HOME=/codexhome )
    CODEX_ARGS+=( -v "$HOST_CODEX/auth.json:/codexhome/auth.json:ro" )
    if [ -f "$HOST_CODEX/config.toml" ]; then
        CODEX_ARGS+=( -v "$HOST_CODEX/config.toml:/codexhome/config.toml:ro" )
    fi
    echo "    Mounting $HOST_CODEX/auth.json -> /codexhome/auth.json (ro)."
else
    echo "    WARNING: no $HOST_CODEX/auth.json — codex will FAIL auth in-container."
fi

# --- 4. Run ---------------------------------------------------------------
# openharness: reach the host's Ollama via host.docker.internal.
echo
echo "==> [4/4] Running four-provider smoke in container ..."
echo "    openharness -> http://host.docker.internal:11434 (model qwen3:1.7b)"
echo

docker run --rm \
    -e CLAUDE_CODE_OAUTH_TOKEN \
    -e OPENHARNESS_BASE_URL="http://host.docker.internal:11434" \
    -e OPENHARNESS_MODEL="qwen3:1.7b" \
    --add-host=host.docker.internal:host-gateway \
    "${CODEX_ARGS[@]}" \
    "$IMAGE"

echo
echo "==> Done. See the SUMMARY table above; the exit code is now a real gate."
