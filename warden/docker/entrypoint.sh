#!/usr/bin/env bash
# Container entrypoint. Three modes:
#   1. Passthrough / one-job worker   — any command given -> exec it (real gate).
#   2. Single-provider isolation smoke — `--auth oauth|api-key` -> REAL GATE:
#      assert clean isolation, inject EXACTLY ONE Claude credential, run the SDK
#      path, exit NON-ZERO on auth failure. This is the T1/T2 provider gate.
#   3. Four-provider demo smoke        — NO args -> runs all four; exit code now
#      reflects pass/fail of the tested providers (the SUMMARY table stays).
set -u

PROMPT="Reply with exactly the word: hello"
TIMEOUT_S=120

# --- Credential strip precedence (highest -> lowest) -----------------------
# From smoke-runbook.md §Notes. When we target exactly one Claude credential we
# must unset EVERY other, so the operator's ambient key cannot shadow the one we
# injected (mirrors the harness's own strip-first behavior, capability AUTH-2).
# Order matters only for documentation; we unset the whole set except the keeper.
CLAUDE_CRED_VARS=(
    CLAUDE_CODE_USE_BEDROCK
    CLAUDE_CODE_USE_VERTEX
    CLAUDE_CODE_USE_FOUNDRY
    ANTHROPIC_AUTH_TOKEN
    ANTHROPIC_API_KEY
    CLAUDE_CODE_OAUTH_TOKEN
)
# OpenAI creds must also be absent from a clean Claude-only container.
OTHER_CRED_VARS=( OPENAI_API_KEY )

# assert_clean_except <KEEP_VAR>
# Fail LOUDLY (exit 3) if ANY Anthropic/OpenAI credential is present that is NOT
# the single var we intend to keep. This is what makes a pass meaningful: a green
# turn can only have used the credential we injected, never an ambient fallback.
# Never prints a secret value — only variable NAMES.
assert_clean_except() {
    local keep="$1"
    local stray=()
    local v
    for v in "${CLAUDE_CRED_VARS[@]}" "${OTHER_CRED_VARS[@]}"; do
        [ "$v" = "$keep" ] && continue
        if [ -n "${!v:-}" ]; then
            stray+=("$v")
        fi
    done
    if [ "${#stray[@]}" -gt 0 ]; then
        echo "!! ISOLATION VIOLATION: stray credential(s) present that were not injected: ${stray[*]}" >&2
        echo "!! A pass here would be a false green (ambient-credential fallback). Aborting." >&2
        return 1
    fi
    return 0
}

# strip_all_except <KEEP_VAR>
# Unset every Claude/OpenAI credential EXCEPT the keeper, so exactly one remains.
strip_all_except() {
    local keep="$1"
    local v
    for v in "${CLAUDE_CRED_VARS[@]}" "${OTHER_CRED_VARS[@]}"; do
        [ "$v" = "$keep" ] && continue
        unset "$v"
    done
}

# --- Mode 2: single-provider (claude SDK) isolation smoke — a REAL GATE ------
# `--auth oauth`   -> keep ONLY CLAUDE_CODE_OAUTH_TOKEN, strip all others.
# `--auth api-key` -> keep ONLY ANTHROPIC_API_KEY, strip all others.
# Runs the SDK path (`--provider claude`), NOT bare mode. Exit non-zero on auth
# failure / incomplete turn.
if [ "${1:-}" = "--auth" ]; then
    AUTH_MODE="${2:-}"
    case "$AUTH_MODE" in
        oauth)   KEEP="CLAUDE_CODE_OAUTH_TOKEN" ;;
        api-key) KEEP="ANTHROPIC_API_KEY" ;;
        *)
            echo "usage: entrypoint.sh --auth oauth|api-key" >&2
            exit 2
            ;;
    esac

    echo "=================================================================="
    echo " Claude SDK isolation smoke — auth=$AUTH_MODE (keep only: $KEEP)"
    echo " $(python --version 2>&1) | node $(node --version) | $(uname -m)"
    echo "=================================================================="

    # (a) Strip every credential except the one we target. Even if run.sh already
    #     injected only one, this defends against an ambient leak in the image.
    strip_all_except "$KEEP"

    # (b) Assert the container now holds NOTHING but the keeper. If a stray cred
    #     survived (e.g. baked into the image), fail loudly — do not run a turn.
    if ! assert_clean_except "$KEEP"; then
        exit 3
    fi

    # (c) The keeper itself must be present, else there is nothing to test with.
    if [ -z "${!KEEP:-}" ]; then
        echo "!! No credential injected: \$$KEEP is empty. Nothing to authenticate with." >&2
        echo "   (inject it at docker run time, e.g. -e $KEEP=... — never baked into the image)" >&2
        exit 4
    fi
    echo "   Isolation OK: only \$$KEEP is present (value hidden)."

    # (d) Working dir: a trusted git repo (harmless for the SDK; consistent bed).
    RUN_DIR="/work/run"
    mkdir -p "$RUN_DIR"
    cd "$RUN_DIR" || { echo "cannot cd to $RUN_DIR" >&2; exit 5; }
    git init -q .
    git config user.email "smoke@example.com"
    git config user.name "smoke"

    echo
    echo " [claude/SDK] python -m warden.drive.cli --single --provider claude"
    OUT="$(timeout "$TIMEOUT_S" python -m warden.drive.cli \
        --provider claude --single "$PROMPT" 2>&1)"
    RC=$?
    echo "$OUT" | tail -n 25

    echo
    echo "=================================================================="
    if [ "$RC" -eq 124 ]; then
        echo " RESULT: FAIL — claude SDK timed out after ${TIMEOUT_S}s (auth=$AUTH_MODE)"
        echo "=================================================================="
        exit 1
    fi
    # Match only GENUINE auth-failure markers. NOTE: bare "OAuth"/"authentication"
    # were previously here and matched benign SDK log lines (captured via 2>&1) →
    # a successful turn (hello + session) got a spurious FAIL. The positive "hello"
    # check below is the real success signal; keep this list specific.
    if echo "$OUT" | grep -qiE "\[ERROR\]|not authenticated|authentication failed|invalid api key|unauthorized|401|403|no known auth|oauth token (expired|invalid|missing)"; then
        echo " RESULT: FAIL — claude SDK did not authenticate (auth=$AUTH_MODE)"
        echo "  $(echo "$OUT" | grep -iE '\[ERROR\]|not authenticated|authentication failed|unauthorized|invalid api key|401|403|oauth token' | head -n1 | cut -c1-140)"
        echo "=================================================================="
        exit 1
    fi
    if echo "$OUT" | grep -qi "hello"; then
        echo " RESULT: PASS — claude SDK authenticated + completed a turn (auth=$AUTH_MODE)"
        echo "=================================================================="
        exit 0
    fi
    echo " RESULT: FAIL — no 'hello' in output; turn did not complete (rc=$RC, auth=$AUTH_MODE)"
    echo "=================================================================="
    exit 1
fi

# --- Mode 2b: T4 permission-bridge smoke — a REAL GATE ---------------------
# `--perm-smoke` runs the Claude-SDK can_use_tool e2e leg (deny + allow control)
# with EXACTLY ONE Claude credential present (same isolation as --auth). This is
# the load-bearing D7 leg: does the SDK actually invoke can_use_tool and block a
# denied tool? Exit 0 iff the driver proves callback-fired + side-effect-absent.
if [ "${1:-}" = "--perm-smoke" ]; then
    # Keep whichever single Claude credential was injected. Prefer OAuth, else
    # ANTHROPIC_API_KEY. Strip everything else so a pass is meaningful.
    if [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
        KEEP="CLAUDE_CODE_OAUTH_TOKEN"
    elif [ -n "${ANTHROPIC_API_KEY:-}" ]; then
        KEEP="ANTHROPIC_API_KEY"
    else
        echo "!! No Claude credential injected for --perm-smoke." >&2
        echo "   Inject -e CLAUDE_CODE_OAUTH_TOKEN=... or -e ANTHROPIC_API_KEY=..." >&2
        exit 4
    fi

    echo "=================================================================="
    echo " T4 permission-bridge smoke (Claude SDK) — keep only: $KEEP"
    echo " $(python --version 2>&1) | node $(node --version) | $(uname -m)"
    echo "=================================================================="

    strip_all_except "$KEEP"
    if ! assert_clean_except "$KEEP"; then
        exit 3
    fi
    echo "   Isolation OK: only \$$KEEP is present (value hidden)."

    RUN_DIR="/work/run"
    mkdir -p "$RUN_DIR"
    cd "$RUN_DIR" || { echo "cannot cd to $RUN_DIR" >&2; exit 5; }
    git init -q .
    git config user.email "smoke@example.com"
    git config user.name "smoke"

    timeout "$TIMEOUT_S" python -m warden.tests.e2e.t4_perm_smoke
    RC=$?
    echo
    echo "=================================================================="
    if [ "$RC" -eq 0 ]; then
        echo " T4 GATE: PASS — SDK enforces via can_use_tool (see driver output)."
    elif [ "$RC" -eq 124 ]; then
        echo " T4 GATE: FAIL — driver timed out after ${TIMEOUT_S}s."
    else
        echo " T4 GATE: FAIL — driver exit $RC (see driver output above)."
    fi
    echo "=================================================================="
    exit "$RC"
fi

# --- Mode 2b-session: Claude resume-recall gate (docs/08 S1–S4) — REAL GATE --
# `--claude-session` plants a fact in turn 1, closes the session, resumes the
# SAME id in a fresh manager, and asserts the model recalls the fact in turn 2.
# EXACTLY ONE Claude credential present (same isolation as --perm-smoke). A pass
# proves the harness resumed the session AND the SDK reloaded prior-turn memory.
if [ "${1:-}" = "--claude-session" ]; then
    if [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
        KEEP="CLAUDE_CODE_OAUTH_TOKEN"
    elif [ -n "${ANTHROPIC_API_KEY:-}" ]; then
        KEEP="ANTHROPIC_API_KEY"
    else
        echo "!! No Claude credential injected for --claude-session." >&2
        echo "   Inject -e CLAUDE_CODE_OAUTH_TOKEN=... or -e ANTHROPIC_API_KEY=..." >&2
        exit 4
    fi

    echo "=================================================================="
    echo " Claude resume-recall gate — keep only: $KEEP"
    echo " $(python --version 2>&1) | node $(node --version) | $(uname -m)"
    echo "=================================================================="

    strip_all_except "$KEEP"
    if ! assert_clean_except "$KEEP"; then
        exit 3
    fi
    echo "   Isolation OK: only \$$KEEP is present (value hidden)."

    RUN_DIR="/work/run"
    mkdir -p "$RUN_DIR"
    cd "$RUN_DIR" || { echo "cannot cd to $RUN_DIR" >&2; exit 5; }
    git init -q .
    git config user.email "smoke@example.com"
    git config user.name "smoke"

    # Two turns (plant + recall) — allow more than a single-turn budget.
    SESS_TIMEOUT_S="${SESS_TIMEOUT_S:-240}"
    timeout "$SESS_TIMEOUT_S" python -m warden.tests.e2e.claude_session_smoke
    RC=$?
    echo
    echo "=================================================================="
    if [ "$RC" -eq 0 ]; then
        echo " CLAUDE SESSION GATE: PASS — resumed session recalled the planted fact."
    elif [ "$RC" -eq 124 ]; then
        echo " CLAUDE SESSION GATE: FAIL — driver timed out after ${SESS_TIMEOUT_S}s."
    else
        echo " CLAUDE SESSION GATE: FAIL — driver exit $RC (see output above)."
    fi
    echo "=================================================================="
    exit "$RC"
fi

# --- Governance budget gate (M2 3g / T8) — REAL GATE -----------------------
# `--governance-budget` drives the real Orchestrator + a per-run Governor (JSONL
# balance ledger) on Claude (the PRICED, mid-turn-visible lane): T8a zero-budget
# reject, T8b funded-run cost-debit, T8c low-cap mid-turn stop. Same single Claude
# credential isolation as --claude-session. Uses OAuth — never the billed key lane.
if [ "${1:-}" = "--governance-budget" ]; then
    if [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
        KEEP="CLAUDE_CODE_OAUTH_TOKEN"
    elif [ -n "${ANTHROPIC_API_KEY:-}" ]; then
        KEEP="ANTHROPIC_API_KEY"
    else
        echo "!! No Claude credential injected for --governance-budget." >&2
        exit 4
    fi

    echo "=================================================================="
    echo " Governance budget gate (T8) — keep only: $KEEP"
    echo " $(python --version 2>&1) | $(uname -m)"
    echo "=================================================================="

    strip_all_except "$KEEP"
    if ! assert_clean_except "$KEEP"; then
        exit 3
    fi
    echo "   Isolation OK: only \$$KEEP is present (value hidden)."

    RUN_DIR="/work/run"
    mkdir -p "$RUN_DIR"
    cd "$RUN_DIR" || { echo "cannot cd to $RUN_DIR" >&2; exit 5; }
    git init -q .
    git config user.email "smoke@example.com"
    git config user.name "smoke"

    # Three sub-gates, two of them real turns (T8b + T8c) — allow headroom.
    GOV_TIMEOUT_S="${GOV_TIMEOUT_S:-240}"
    timeout "$GOV_TIMEOUT_S" python -m warden.tests.e2e.governance_budget_smoke claude
    RC=$?
    echo
    echo "=================================================================="
    if [ "$RC" -eq 0 ]; then
        echo " GOVERNANCE BUDGET GATE: PASS — reject / meter+debit / cap-stop proven."
    elif [ "$RC" -eq 124 ]; then
        echo " GOVERNANCE BUDGET GATE: FAIL — driver timed out after ${GOV_TIMEOUT_S}s."
    else
        echo " GOVERNANCE BUDGET GATE: FAIL — driver exit $RC (see output above)."
    fi
    echo "=================================================================="
    exit "$RC"
fi

# --- Governance budget gate — codex + openharness (unpriced lanes) ----------
# Same scenario as --governance-budget, different provider. codex/openharness are
# UNPRICED by the default table, so the gate proves: T8a reject (provider-agnostic,
# pre-flight) + the unpriced lane (run admits, balance stays flat — governed by
# time/turns, not dollars). This is the documented per-provider behavior difference.
if [ "${1:-}" = "--governance-budget-codex" ]; then
    if [ -z "${OPENAI_API_KEY:-}" ] && [ ! -f "${CODEX_HOME:-/codexhome}/auth.json" ]; then
        echo "!! No codex credential for --governance-budget-codex." >&2
        exit 4
    fi
    strip_all_except "OPENAI_API_KEY"  # keep only codex-relevant; auth.json is a mount
    echo "=================================================================="
    echo " Governance budget gate (T8) — provider=codex (unpriced lane)"
    echo "=================================================================="
    RUN_DIR="/work/run"; mkdir -p "$RUN_DIR"; cd "$RUN_DIR" || exit 5
    git init -q .; git config user.email "smoke@example.com"; git config user.name "smoke"
    GOV_TIMEOUT_S="${GOV_TIMEOUT_S:-240}"
    timeout "$GOV_TIMEOUT_S" python -m warden.tests.e2e.governance_budget_smoke codex
    RC=$?
    echo "=================================================================="
    [ "$RC" -eq 0 ] && echo " GOVERNANCE BUDGET GATE (codex): PASS" \
        || echo " GOVERNANCE BUDGET GATE (codex): FAIL — exit $RC"
    echo "=================================================================="
    exit "$RC"
fi

if [ "${1:-}" = "--governance-budget-oh" ]; then
    echo "=================================================================="
    echo " Governance budget gate (T8) — provider=openharness (unpriced lane)"
    echo "=================================================================="
    RUN_DIR="/work/run"; mkdir -p "$RUN_DIR"; cd "$RUN_DIR" || exit 5
    git init -q .; git config user.email "smoke@example.com"; git config user.name "smoke"
    GOV_TIMEOUT_S="${GOV_TIMEOUT_S:-240}"
    timeout "$GOV_TIMEOUT_S" python -m warden.tests.e2e.governance_budget_smoke openharness
    RC=$?
    echo "=================================================================="
    [ "$RC" -eq 0 ] && echo " GOVERNANCE BUDGET GATE (openharness): PASS" \
        || echo " GOVERNANCE BUDGET GATE (openharness): FAIL — exit $RC"
    echo "=================================================================="
    exit "$RC"
fi

# --- M6 durable-HITL gate (docs/07-durable-hitl §7) — REAL GATE --------------
# Drives the real Runner through the Runs-API pause→confirm→resume cycle
# (requires_action → permission_request on run_events → tool_confirmation → resume)
# on built-in + custom tools. Claude is the HARD gate (strict: exact-id native
# defer, allow/deny outcomes asserted, multi-tool convergent); OH/Codex are
# FAIL-CLOSED (07b) — durable_http is Claude-only, so their gate asserts the run is
# REJECTED (needs no live model — rejected pre-flight). $M6_CASES defaults to "allow,deny".
if [ "${1:-}" = "--m6-hitl" ]; then
    if [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
        KEEP="CLAUDE_CODE_OAUTH_TOKEN"
    elif [ -n "${ANTHROPIC_API_KEY:-}" ]; then
        KEEP="ANTHROPIC_API_KEY"
    else
        echo "!! No Claude credential injected for --m6-hitl." >&2
        exit 4
    fi
    echo "=================================================================="
    echo " M6 durable-HITL gate — provider=claude (HARD) — keep only: $KEEP"
    echo "=================================================================="
    strip_all_except "$KEEP"
    if ! assert_clean_except "$KEEP"; then exit 3; fi
    RUN_DIR="/work/run"; mkdir -p "$RUN_DIR"
    export M6_GATE_RUN_DIR="$RUN_DIR"
    M6_TIMEOUT_S="${M6_TIMEOUT_S:-420}"
    timeout "$M6_TIMEOUT_S" python -m warden.tests.e2e.m6_hitl_smoke \
        claude "${M6_TOOLS:-builtin,custom}" "${M6_CASES:-allow,deny}"
    RC=$?
    echo "=================================================================="
    [ "$RC" -eq 0 ] && echo " M6 DURABLE-HITL GATE (claude): PASS" \
        || echo " M6 DURABLE-HITL GATE (claude): FAIL — exit $RC"
    echo "=================================================================="
    exit "$RC"
fi

if [ "${1:-}" = "--m6-gate" ]; then
    # EXT-G1/G2 landscape gate — claude only (durable_http is Claude-only, 07b).
    if [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
        KEEP="CLAUDE_CODE_OAUTH_TOKEN"
    elif [ -n "${ANTHROPIC_API_KEY:-}" ]; then
        KEEP="ANTHROPIC_API_KEY"
    else
        echo "!! No Claude credential injected for --m6-gate." >&2
        exit 4
    fi
    echo "=================================================================="
    echo " M6 landscape-gate — provider=claude (mode:auto + confirm:[Write])"
    echo "=================================================================="
    strip_all_except "$KEEP"
    if ! assert_clean_except "$KEEP"; then exit 3; fi
    RUN_DIR="/work/run"; mkdir -p "$RUN_DIR"
    export M6_GATE_RUN_DIR="$RUN_DIR"
    M6_TIMEOUT_S="${M6_TIMEOUT_S:-420}"
    timeout "$M6_TIMEOUT_S" python -m warden.tests.e2e.m6_gate_smoke \
        "${M6_CASES:-allow,deny,edit,revise}"
    RC=$?
    echo "=================================================================="
    [ "$RC" -eq 0 ] && echo " M6 LANDSCAPE-GATE (claude): PASS" \
        || echo " M6 LANDSCAPE-GATE (claude): FAIL — exit $RC"
    echo "=================================================================="
    exit "$RC"
fi

if [ "${1:-}" = "--m6-hitl-codex" ]; then
    # 07b: codex durable_http is fail-closed (rejected pre-flight) — no provider is
    # driven, so no credential is required. Strip keys anyway for a clean env.
    strip_all_except "OPENAI_API_KEY"
    echo "=================================================================="
    echo " M6 durable-HITL gate — provider=codex (FAIL-CLOSED; custom=N/A)"
    echo "=================================================================="
    RUN_DIR="/work/run"; mkdir -p "$RUN_DIR"
    export M6_GATE_RUN_DIR="$RUN_DIR"
    M6_TIMEOUT_S="${M6_TIMEOUT_S:-420}"
    timeout "$M6_TIMEOUT_S" python -m warden.tests.e2e.m6_hitl_smoke \
        codex "${M6_TOOLS:-builtin}" "${M6_CASES:-allow,deny}"
    RC=$?
    echo "=================================================================="
    [ "$RC" -eq 0 ] && echo " M6 DURABLE-HITL GATE (codex): PASS (fail-closed)" \
        || echo " M6 DURABLE-HITL GATE (codex): FAIL — exit $RC"
    echo "=================================================================="
    exit "$RC"
fi

if [ "${1:-}" = "--m6-hitl-oh" ]; then
    # 07b: openharness durable_http is fail-closed (rejected pre-flight) — no Ollama
    # is contacted; the gate asserts the run is rejected.
    echo "=================================================================="
    echo " M6 durable-HITL gate — provider=openharness (FAIL-CLOSED)"
    echo "=================================================================="
    RUN_DIR="/work/run"; mkdir -p "$RUN_DIR"
    export M6_GATE_RUN_DIR="$RUN_DIR"
    M6_TIMEOUT_S="${M6_TIMEOUT_S:-420}"
    timeout "$M6_TIMEOUT_S" python -m warden.tests.e2e.m6_hitl_smoke \
        openharness "${M6_TOOLS:-builtin,custom}" "${M6_CASES:-allow,deny}"
    RC=$?
    echo "=================================================================="
    [ "$RC" -eq 0 ] && echo " M6 DURABLE-HITL GATE (openharness): PASS (fail-closed)" \
        || echo " M6 DURABLE-HITL GATE (openharness): FAIL — exit $RC"
    echo "=================================================================="
    exit "$RC"
fi

# pre-03 M0 — the unified auth path (typed resolve→inject, policy gate, AUTH-4, no
# bleed). Needs NO credential: the resolve→inject proof holds secrets by reference to
# fake env-var names, so it runs green in the real image with no cloud cred (proving
# the module works in the bed). A live-provider turn on the resolved cred is optional.
if [ "${1:-}" = "--auth-typed" ]; then
    echo "=================================================================="
    echo " Auth gate — typed (user,provider)→AuthMethod→inject (pre-03 M0)"
    echo "=================================================================="
    AUTH_TIMEOUT_S="${AUTH_TIMEOUT_S:-60}"
    timeout "$AUTH_TIMEOUT_S" python -m warden.tests.e2e.auth_typed_smoke
    RC=$?
    echo "=================================================================="
    [ "$RC" -eq 0 ] && echo " AUTH-TYPED GATE: PASS — resolve→inject, policy, AUTH-4, no bleed." \
        || echo " AUTH-TYPED GATE: FAIL — exit $RC (see output above)."
    echo "=================================================================="
    exit "$RC"
fi

# --- Mode 2b-crash: Claude crash-recovery gate (docs/08 S6) — REAL GATE ------
# `--claude-crash` runs a persistence-active plant → snapshot → WIPE task dir →
# restore → resume → recall. Proves memory survives a DESTROYED workspace, not
# just a fresh manager. Same single-credential isolation as --claude-session.
if [ "${1:-}" = "--claude-crash" ]; then
    if [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
        KEEP="CLAUDE_CODE_OAUTH_TOKEN"
    elif [ -n "${ANTHROPIC_API_KEY:-}" ]; then
        KEEP="ANTHROPIC_API_KEY"
    else
        echo "!! No Claude credential injected for --claude-crash." >&2
        exit 4
    fi

    echo "=================================================================="
    echo " Claude crash-recovery gate — keep only: $KEEP"
    echo " $(python --version 2>&1) | node $(node --version) | $(uname -m)"
    echo "=================================================================="

    strip_all_except "$KEEP"
    if ! assert_clean_except "$KEEP"; then
        exit 3
    fi
    echo "   Isolation OK: only \$$KEEP is present (value hidden)."

    RUN_DIR="/work/run"
    mkdir -p "$RUN_DIR"
    cd "$RUN_DIR" || { echo "cannot cd to $RUN_DIR" >&2; exit 5; }
    git init -q .
    git config user.email "smoke@example.com"
    git config user.name "smoke"

    SESS_TIMEOUT_S="${SESS_TIMEOUT_S:-240}"
    timeout "$SESS_TIMEOUT_S" python -m warden.tests.e2e.claude_crash_smoke
    RC=$?
    echo
    echo "=================================================================="
    if [ "$RC" -eq 0 ]; then
        echo " CLAUDE CRASH GATE: PASS — recall survived a wiped+restored workspace."
    elif [ "$RC" -eq 124 ]; then
        echo " CLAUDE CRASH GATE: FAIL — driver timed out after ${SESS_TIMEOUT_S}s."
    else
        echo " CLAUDE CRASH GATE: FAIL — driver exit $RC (see output above)."
    fi
    echo "=================================================================="
    exit "$RC"
fi

# --- Mode 2c: Custom-tool consumption smoke — a REAL GATE -------------------
# `--custom-tool` runs the Claude-SDK custom-tool e2e leg (TOOL-1/2) with
# EXACTLY ONE Claude credential present (same isolation as --auth/--perm-smoke).
# Registers an in-proc SDK-MCP tool, forces the model to call it in one real
# turn, and asserts the handler's side effect (marker file) happened. Exit 0 iff
# the SDK actually reached + executed the harness-registered custom tool.
if [ "${1:-}" = "--custom-tool" ]; then
    if [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
        KEEP="CLAUDE_CODE_OAUTH_TOKEN"
    elif [ -n "${ANTHROPIC_API_KEY:-}" ]; then
        KEEP="ANTHROPIC_API_KEY"
    else
        echo "!! No Claude credential injected for --custom-tool." >&2
        echo "   Inject -e CLAUDE_CODE_OAUTH_TOKEN=... or -e ANTHROPIC_API_KEY=..." >&2
        exit 4
    fi

    echo "=================================================================="
    echo " Custom-tool consumption smoke (Claude SDK) — keep only: $KEEP"
    echo " $(python --version 2>&1) | node $(node --version) | $(uname -m)"
    echo "=================================================================="

    strip_all_except "$KEEP"
    if ! assert_clean_except "$KEEP"; then
        exit 3
    fi
    echo "   Isolation OK: only \$$KEEP is present (value hidden)."

    timeout "$TIMEOUT_S" python -m warden.tests.e2e.t_custom_tool
    RC=$?
    echo
    echo "=================================================================="
    if [ "$RC" -eq 0 ]; then
        echo " CUSTOM-TOOL GATE: PASS — SDK executed the harness custom tool."
    elif [ "$RC" -eq 124 ]; then
        echo " CUSTOM-TOOL GATE: FAIL — driver timed out after ${TIMEOUT_S}s."
    else
        echo " CUSTOM-TOOL GATE: FAIL — driver exit $RC (see output above)."
    fi
    echo "=================================================================="
    exit "$RC"
fi

# --- Mode 2e: Codex SDK auth smoke (C1/T1/T2) — a REAL GATE -----------------
# `--codex-auth` runs a codex hello turn with EXACTLY ONE codex credential:
#   OAuth  → a mounted CODEX_HOME/auth.json (OPENAI_API_KEY stripped).
#   api-key→ OPENAI_API_KEY only.
# A completed hello turn proves the injected credential authed.
if [ "${1:-}" = "--codex-auth" ]; then
    if [ -n "${OPENAI_API_KEY:-}" ]; then
        KEEP="OPENAI_API_KEY"; MODE="api-key"
    elif [ -n "${CODEX_HOME:-}" ] && [ -f "${CODEX_HOME}/auth.json" ]; then
        KEEP="OPENAI_API_KEY"; MODE="oauth"   # nothing to keep in cred vars; auth.json is a file
    else
        echo "!! No codex credential injected for --codex-auth." >&2
        echo "   Inject -e OPENAI_API_KEY=... OR mount CODEX_HOME/auth.json + set CODEX_HOME." >&2
        exit 4
    fi

    echo "=================================================================="
    echo " Codex SDK auth smoke — mode=$MODE (keep only: $KEEP; CODEX_HOME=${CODEX_HOME:-unset})"
    echo " $(python --version 2>&1) | node $(node --version) | $(uname -m)"
    echo "=================================================================="

    strip_all_except "$KEEP"
    if ! assert_clean_except "$KEEP"; then
        exit 3
    fi
    echo "   Isolation OK: only \$$KEEP (+ CODEX_HOME/auth.json in OAuth mode) present."

    timeout "$TIMEOUT_S" python -m warden.tests.e2e.codex_auth_smoke
    RC=$?
    echo
    echo "=================================================================="
    if [ "$RC" -eq 0 ]; then
        echo " CODEX AUTH GATE: PASS — hello turn completed, credential authed."
    elif [ "$RC" -eq 124 ]; then
        echo " CODEX AUTH GATE: FAIL — driver timed out after ${TIMEOUT_S}s."
    else
        echo " CODEX AUTH GATE: FAIL — driver exit $RC (see output above)."
    fi
    echo "=================================================================="
    exit "$RC"
fi

# --- Mode 2f: Codex SDK permission fail-closed smoke (C3/T4) — LOAD-BEARING --
# `--codex-perm` denies an exec via the approval handler and asserts the handler
# FIRED + the side effect is ABSENT, then an allow control writes. Proves the
# codex approval bridge is load-bearing (untrusted policy, not auto_review).
if [ "${1:-}" = "--codex-perm" ]; then
    if [ -n "${OPENAI_API_KEY:-}" ]; then
        KEEP="OPENAI_API_KEY"; MODE="api-key"
    elif [ -n "${CODEX_HOME:-}" ] && [ -f "${CODEX_HOME}/auth.json" ]; then
        KEEP="OPENAI_API_KEY"; MODE="oauth"
    else
        echo "!! No codex credential injected for --codex-perm." >&2
        echo "   Inject -e OPENAI_API_KEY=... OR mount CODEX_HOME/auth.json + set CODEX_HOME." >&2
        exit 4
    fi

    echo "=================================================================="
    echo " Codex T4 permission smoke — mode=$MODE (keep only: $KEEP)"
    echo " $(python --version 2>&1) | node $(node --version) | $(uname -m)"
    echo "=================================================================="

    strip_all_except "$KEEP"
    if ! assert_clean_except "$KEEP"; then
        exit 3
    fi
    echo "   Isolation OK: only \$$KEEP (+ CODEX_HOME/auth.json in OAuth mode) present."

    RUN_DIR="/work/run"
    mkdir -p "$RUN_DIR"
    cd "$RUN_DIR" || { echo "cannot cd to $RUN_DIR" >&2; exit 5; }
    git init -q .
    git config user.email "smoke@example.com"
    git config user.name "smoke"

    timeout "$TIMEOUT_S" python -m warden.tests.e2e.codex_perm_smoke
    RC=$?
    echo
    echo "=================================================================="
    if [ "$RC" -eq 0 ]; then
        echo " CODEX T4 GATE: PASS — approval bridge fires AND blocks a denied exec."
    elif [ "$RC" -eq 124 ]; then
        echo " CODEX T4 GATE: FAIL — driver timed out after ${TIMEOUT_S}s."
    else
        echo " CODEX T4 GATE: FAIL — driver exit $RC (see output above)."
    fi
    echo "=================================================================="
    exit "$RC"
fi

# --- Mode 2f-session: Codex resume-recall gate (docs/08 S1–S4) — REAL GATE --
# `--codex-session` plants a fact, closes, resumes the SAME thread id in a fresh
# manager, and asserts recall. Driving through the Orchestrator also exercises
# the codex message-handler (bug 4b). Single codex credential (OAuth auth.json
# preferred; OPENAI_API_KEY unset avoids the billed API-key lane).
if [ "${1:-}" = "--codex-session" ]; then
    if [ -n "${OPENAI_API_KEY:-}" ]; then
        KEEP="OPENAI_API_KEY"; MODE="api-key"
    elif [ -n "${CODEX_HOME:-}" ] && [ -f "${CODEX_HOME}/auth.json" ]; then
        KEEP="OPENAI_API_KEY"; MODE="oauth"
    else
        echo "!! No codex credential injected for --codex-session." >&2
        echo "   Inject -e OPENAI_API_KEY=... OR mount CODEX_HOME/auth.json + set CODEX_HOME." >&2
        exit 4
    fi

    echo "=================================================================="
    echo " Codex resume-recall gate — mode=$MODE (keep only: $KEEP; CODEX_HOME=${CODEX_HOME:-unset})"
    echo " $(python --version 2>&1) | node $(node --version) | $(uname -m)"
    echo "=================================================================="

    strip_all_except "$KEEP"
    if ! assert_clean_except "$KEEP"; then
        exit 3
    fi
    echo "   Isolation OK: only \$$KEEP (+ CODEX_HOME/auth.json in OAuth mode) present."

    RUN_DIR="/work/run"
    mkdir -p "$RUN_DIR"
    cd "$RUN_DIR" || { echo "cannot cd to $RUN_DIR" >&2; exit 5; }
    git init -q .
    git config user.email "smoke@example.com"
    git config user.name "smoke"

    SESS_TIMEOUT_S="${SESS_TIMEOUT_S:-240}"
    timeout "$SESS_TIMEOUT_S" python -m warden.tests.e2e.codex_session_smoke
    RC=$?
    echo
    echo "=================================================================="
    if [ "$RC" -eq 0 ]; then
        echo " CODEX SESSION GATE: PASS — resumed thread recalled the planted fact."
    elif [ "$RC" -eq 124 ]; then
        echo " CODEX SESSION GATE: FAIL — driver timed out after ${SESS_TIMEOUT_S}s."
    else
        echo " CODEX SESSION GATE: FAIL — driver exit $RC (see output above)."
    fi
    echo "=================================================================="
    exit "$RC"
fi

# --- Mode 2f-crash: Codex crash-recovery gate (docs/08 S6) — REAL GATE -------
# `--codex-crash` runs persistence-active plant → snapshot → WIPE → restore →
# resume → recall. The driver seeds the OAuth auth.json into the pinned
# <task>/.codex so the persisted turn authenticates. Single codex credential.
if [ "${1:-}" = "--codex-crash" ]; then
    if [ -n "${OPENAI_API_KEY:-}" ]; then
        KEEP="OPENAI_API_KEY"; MODE="api-key"
    elif [ -n "${CODEX_HOME:-}" ] && [ -f "${CODEX_HOME}/auth.json" ]; then
        KEEP="OPENAI_API_KEY"; MODE="oauth"
    else
        echo "!! No codex credential injected for --codex-crash." >&2
        exit 4
    fi

    echo "=================================================================="
    echo " Codex crash-recovery gate — mode=$MODE (keep only: $KEEP; CODEX_HOME=${CODEX_HOME:-unset})"
    echo " $(python --version 2>&1) | node $(node --version) | $(uname -m)"
    echo "=================================================================="

    strip_all_except "$KEEP"
    if ! assert_clean_except "$KEEP"; then
        exit 3
    fi
    echo "   Isolation OK: only \$$KEEP (+ CODEX_HOME/auth.json in OAuth mode) present."

    RUN_DIR="/work/run"
    mkdir -p "$RUN_DIR"
    cd "$RUN_DIR" || { echo "cannot cd to $RUN_DIR" >&2; exit 5; }
    git init -q .
    git config user.email "smoke@example.com"
    git config user.name "smoke"

    SESS_TIMEOUT_S="${SESS_TIMEOUT_S:-240}"
    timeout "$SESS_TIMEOUT_S" python -m warden.tests.e2e.codex_crash_smoke
    RC=$?
    echo
    echo "=================================================================="
    if [ "$RC" -eq 0 ]; then
        echo " CODEX CRASH GATE: PASS — recall survived a wiped+restored workspace."
    elif [ "$RC" -eq 124 ]; then
        echo " CODEX CRASH GATE: FAIL — driver timed out after ${SESS_TIMEOUT_S}s."
    else
        echo " CODEX CRASH GATE: FAIL — driver exit $RC (see output above)."
    fi
    echo "=================================================================="
    exit "$RC"
fi

# --- Mode 2f-crash-s3: Codex crash-recovery on a REMOTE (S3) backend (A3) -----
# Identical flow to --codex-crash, but the snapshot backend is S3/MinIO
# (CRASH_STORAGE_BACKEND=s3; the driver reads AWS_BUCKET_NAME + the AWS_* chain,
# AWS_S3_ENDPOINT for MinIO). This is the A3 proof: the remote object carries the
# transcript, and the A2 assertion (no credential in the archive) is re-checked by
# restoring the object and scanning it. Credential exclusion lives in the shared
# archive layer, so it holds for S3 exactly as for local.
if [ "${1:-}" = "--codex-crash-s3" ]; then
    if [ -n "${OPENAI_API_KEY:-}" ]; then
        KEEP="OPENAI_API_KEY"; MODE="api-key"
    elif [ -n "${CODEX_HOME:-}" ] && [ -f "${CODEX_HOME}/auth.json" ]; then
        KEEP="OPENAI_API_KEY"; MODE="oauth"
    else
        echo "!! No codex credential injected for --codex-crash-s3." >&2
        exit 4
    fi
    if [ -z "${AWS_BUCKET_NAME:-}" ]; then
        echo "!! --codex-crash-s3 needs AWS_BUCKET_NAME (S3/MinIO bucket)." >&2
        exit 4
    fi

    echo "=================================================================="
    echo " Codex crash-recovery gate (REMOTE S3) — mode=$MODE bucket=$AWS_BUCKET_NAME"
    echo " endpoint=${AWS_S3_ENDPOINT:-<aws>} | $(python --version 2>&1) | $(uname -m)"
    echo "=================================================================="

    # NOTE: S3 mode keeps the AWS_* creds alongside the codex credential — the
    # backend needs them. So we do NOT strip to a single credential here.
    RUN_DIR="/work/run"
    mkdir -p "$RUN_DIR"
    cd "$RUN_DIR" || { echo "cannot cd to $RUN_DIR" >&2; exit 5; }
    git init -q .
    git config user.email "smoke@example.com"
    git config user.name "smoke"

    SESS_TIMEOUT_S="${SESS_TIMEOUT_S:-240}"
    CRASH_STORAGE_BACKEND=s3 timeout "$SESS_TIMEOUT_S" \
        python -m warden.tests.e2e.codex_crash_smoke
    RC=$?
    echo
    echo "=================================================================="
    if [ "$RC" -eq 0 ]; then
        echo " CODEX S3 CRASH GATE: PASS — recall survived a wipe on a REMOTE backend"
        echo " (and no credential in the S3 object)."
    elif [ "$RC" -eq 124 ]; then
        echo " CODEX S3 CRASH GATE: FAIL — driver timed out after ${SESS_TIMEOUT_S}s."
    else
        echo " CODEX S3 CRASH GATE: FAIL — driver exit $RC (see output above)."
    fi
    echo "=================================================================="
    exit "$RC"
fi

# `--codex-custom-tool` delivers a marker-writing custom tool to codex via the
# in-proc streamable-HTTP MCP server (allow_ungated_custom_tools=True) and asserts
# the model CALLED the in-proc handler (marker written). Ungated delivery path;
# exec/patch gating is untouched (still fail-closed).
if [ "${1:-}" = "--codex-custom-tool" ]; then
    if [ -n "${OPENAI_API_KEY:-}" ]; then
        KEEP="OPENAI_API_KEY"; MODE="api-key"
    elif [ -n "${CODEX_HOME:-}" ] && [ -f "${CODEX_HOME}/auth.json" ]; then
        KEEP="OPENAI_API_KEY"; MODE="oauth"
    else
        echo "!! No codex credential injected for --codex-custom-tool." >&2
        echo "   Inject -e OPENAI_API_KEY=... OR mount CODEX_HOME/auth.json + set CODEX_HOME." >&2
        exit 4
    fi

    echo "=================================================================="
    echo " Codex ungated custom-tool (in-proc MCP) smoke — mode=$MODE (keep only: $KEEP)"
    echo " $(python --version 2>&1) | node $(node --version) | $(uname -m)"
    echo "=================================================================="

    strip_all_except "$KEEP"
    if ! assert_clean_except "$KEEP"; then
        exit 3
    fi
    echo "   Isolation OK: only \$$KEEP (+ CODEX_HOME/auth.json in OAuth mode) present."

    RUN_DIR="/work/run"
    mkdir -p "$RUN_DIR"
    cd "$RUN_DIR" || { echo "cannot cd to $RUN_DIR" >&2; exit 5; }
    git init -q .
    git config user.email "smoke@example.com"
    git config user.name "smoke"

    timeout "$TIMEOUT_S" python -m warden.tests.e2e.codex_custom_tool_smoke
    RC=$?
    echo
    echo "=================================================================="
    if [ "$RC" -eq 0 ]; then
        echo " CODEX CUSTOM-TOOL GATE: PASS — codex called the in-proc MCP handler."
    elif [ "$RC" -eq 124 ]; then
        echo " CODEX CUSTOM-TOOL GATE: FAIL — driver timed out after ${TIMEOUT_S}s."
    else
        echo " CODEX CUSTOM-TOOL GATE: FAIL — driver exit $RC (see output above)."
    fi
    echo "=================================================================="
    exit "$RC"
fi

# --- Mode 2d: Per-run auth isolation smoke (T3) — a REAL GATE ---------------
# `--t3` runs the credential-free structural no-bleed + AUTH-FIX regression leg.
# It uses deliberately-bogus injected keys and inspects the resolved options.env
# (no real turn, no spend), so it needs no credential — but we still route it
# through the bed for a repeatable, containerized gate.
if [ "${1:-}" = "--t3" ]; then
    echo "=================================================================="
    echo " Per-run auth isolation smoke (T3, structural — no credential)"
    echo " $(python --version 2>&1) | $(uname -m)"
    echo "=================================================================="
    timeout "$TIMEOUT_S" python -m warden.tests.e2e.t3_isolation
    RC=$?
    echo
    echo "=================================================================="
    if [ "$RC" -eq 0 ]; then
        echo " T3 GATE: PASS — per-run keys isolated + AUTH-FIX holds."
    else
        echo " T3 GATE: FAIL — driver exit $RC (see output above)."
    fi
    echo "=================================================================="
    exit "$RC"
fi

# --- Mode 2e: OpenHarness perm + custom-tool smoke (free Ollama lane) -------
# `--openharness-perm` runs the B15 arg/path-level permission + custom-tool
# consumption leg against the host's Ollama (no cloud credential — permissions
# are in-process, so the free lane is faithful). Reaches Ollama via
# OPENHARNESS_BASE_URL (host.docker.internal from run.sh).
if [ "${1:-}" = "--openharness-perm" ]; then
    echo "=================================================================="
    echo " OpenHarness perm (B15 arg-level) + custom-tool smoke — free Ollama"
    echo " base=${OPENHARNESS_BASE_URL:-unset} model=${OPENHARNESS_MODEL:-default}"
    echo " $(python --version 2>&1) | $(uname -m)"
    echo "=================================================================="
    # OpenHarness runs a LOCAL model across 4 legs (deny/arg-deny/allow/custom),
    # each up to max_turns=8 — far slower than a single cloud turn, and a denied
    # tool makes the model retry until its turn cap. Give it a much larger budget
    # than the default single-turn TIMEOUT_S.
    OH_TIMEOUT_S="${OH_TIMEOUT_S:-600}"
    timeout "$OH_TIMEOUT_S" python -m warden.tests.e2e.openharness_perm_smoke
    RC=$?
    echo
    echo "=================================================================="
    if [ "$RC" -eq 0 ]; then
        echo " OPENHARNESS-PERM GATE: PASS — arg-level deny + custom tool work."
    elif [ "$RC" -eq 124 ]; then
        echo " OPENHARNESS-PERM GATE: FAIL — driver timed out after ${OH_TIMEOUT_S}s."
    else
        echo " OPENHARNESS-PERM GATE: FAIL — driver exit $RC (see output above)."
    fi
    echo "=================================================================="
    exit "$RC"
fi

# --- Mode 2e-session: OpenHarness resume-recall gate (docs/08 S1–S4) --------
# `--openharness-session` plants a fact, closes, resumes the SAME id in a fresh
# manager, and asserts recall against the host's free Ollama. This is the gate
# that forces the cold-resume fix (bug B-OH): until start() seeds the engine
# from the transcript on resume, turn 2 runs on an EMPTY conversation and FAILs.
if [ "${1:-}" = "--openharness-session" ]; then
    echo "=================================================================="
    echo " OpenHarness resume-recall gate — free Ollama"
    echo " base=${OPENHARNESS_BASE_URL:-unset} model=${OPENHARNESS_MODEL:-default}"
    echo " $(python --version 2>&1) | $(uname -m)"
    echo "=================================================================="

    RUN_DIR="/work/run"
    mkdir -p "$RUN_DIR"
    cd "$RUN_DIR" || { echo "cannot cd to $RUN_DIR" >&2; exit 5; }
    git init -q .
    git config user.email "smoke@example.com"
    git config user.name "smoke"

    # Two turns of a LOCAL model + transcript seeding — generous budget.
    OH_SESS_TIMEOUT_S="${OH_SESS_TIMEOUT_S:-600}"
    timeout "$OH_SESS_TIMEOUT_S" python -m warden.tests.e2e.openharness_session_smoke
    RC=$?
    echo
    echo "=================================================================="
    if [ "$RC" -eq 0 ]; then
        echo " OPENHARNESS SESSION GATE: PASS — resumed session recalled the fact."
    elif [ "$RC" -eq 124 ]; then
        echo " OPENHARNESS SESSION GATE: FAIL — driver timed out after ${OH_SESS_TIMEOUT_S}s."
    else
        echo " OPENHARNESS SESSION GATE: FAIL — driver exit $RC (see output above)."
    fi
    echo "=================================================================="
    exit "$RC"
fi

# --- Mode 2e-crash: OpenHarness crash-recovery gate (docs/08 S6) ------------
# `--openharness-crash` runs persistence-active plant → snapshot → WIPE →
# restore → resume → recall against the host's free Ollama. Exercises the
# openharness home-pinning fix (transcript must live at <task>/.openharness so
# it travels with the snapshot).
if [ "${1:-}" = "--openharness-crash" ]; then
    echo "=================================================================="
    echo " OpenHarness crash-recovery gate — free Ollama"
    echo " base=${OPENHARNESS_BASE_URL:-unset} model=${OPENHARNESS_MODEL:-default}"
    echo " $(python --version 2>&1) | $(uname -m)"
    echo "=================================================================="

    RUN_DIR="/work/run"
    mkdir -p "$RUN_DIR"
    cd "$RUN_DIR" || { echo "cannot cd to $RUN_DIR" >&2; exit 5; }
    git init -q .
    git config user.email "smoke@example.com"
    git config user.name "smoke"

    OH_SESS_TIMEOUT_S="${OH_SESS_TIMEOUT_S:-600}"
    timeout "$OH_SESS_TIMEOUT_S" python -m warden.tests.e2e.openharness_crash_smoke
    RC=$?
    echo
    echo "=================================================================="
    if [ "$RC" -eq 0 ]; then
        echo " OPENHARNESS CRASH GATE: PASS — recall survived a wiped+restored workspace."
    elif [ "$RC" -eq 124 ]; then
        echo " OPENHARNESS CRASH GATE: FAIL — driver timed out after ${OH_SESS_TIMEOUT_S}s."
    else
        echo " OPENHARNESS CRASH GATE: FAIL — driver exit $RC (see output above)."
    fi
    echo "=================================================================="
    exit "$RC"
fi

# --- Mode 1: Passthrough / one-job worker mode -----------------------------
# If any command is given, exec it instead of the demo smoke. This is how the
# container runs as a stateless one-job worker, e.g.:
#   docker run ... IMAGE python -m warden.drive.cli --single "<prompt>" \
#     --provider claude --user-id U --task-id T \
#     --storage-backend s3 --s3-bucket "$AWS_BUCKET_NAME" \
#     --session-db /state/sessions.db [--resume <sid>]
# The exit code is the CLI's (a real gate), like the isolation smoke above.
# With NO args, fall through to the four-provider demo smoke.
if [ "$#" -gt 0 ]; then
    exec "$@"
fi

# Provider results, filled in as we go (PASS / FAIL / SKIP + reason).
declare -A RESULT
declare -A DETAIL

echo "=================================================================="
echo " Harness engine — provider isolation smoke (retired adapters SKIP)"
echo " $(python --version 2>&1) | node $(node --version) | $(uname -m)"
echo "=================================================================="

# --- Per-provider auth status (uses the real orchestrator auth module) -----
echo
echo "--- Auth status (providers.auth) ---"
python -c "
from warden.providers.auth import is_authed, auth_hint
for p in ('claude','claude-cli','codex','openharness'):
    print(f'  {p:12s} authed={is_authed(p)!s:5s} — {auth_hint(p)}')
"
echo
echo "  Note: is_authed('codex') only checks OPENAI_API_KEY. The codex CLI can"
echo "  still work via CODEX_HOME/auth.json (ChatGPT OAuth) even when that shows"
echo "  False — the auth module doesn't inspect the mounted auth.json."

# --- Working dir: a trusted git repo (codex requires this; gotcha 1) -------
RUN_DIR="/work/run"
mkdir -p "$RUN_DIR"
cd "$RUN_DIR" || { echo "cannot cd to $RUN_DIR" >&2; exit 5; }
git init -q .
git config user.email "smoke@example.com"
git config user.name "smoke"
echo
echo "--- Run dir: $RUN_DIR (git-initialized so codex trusts it) ---"

# --- CLI-based providers: claude / claude-cli / codex ----------------------
# Runs the single-shot CLI, captures output, marks PASS/FAIL from the text.
run_cli_provider() {
    local provider="$1"
    echo
    echo "=================================================================="
    echo " [$provider] python -m warden.drive.cli --single \"$PROMPT\""
    echo "=================================================================="

    # Pre-flight: if NO credential was injected for this provider, SKIP it rather
    # than run+FAIL. Clean isolation legitimately omits creds we didn't inject; a
    # SKIP must not trip the gate (only a provider that ran and FAILED does).
    if ! python -c "import sys; from warden.providers.auth import is_authed; sys.exit(0 if is_authed('$provider') else 1)" 2>/dev/null; then
        RESULT[$provider]="SKIP"
        DETAIL[$provider]="no credential injected (is_authed False) — not run"
        echo "  SKIP: no credential injected for '$provider'."
        return
    fi

    # Pre-flight: a RETIRED adapter (factory raises NotImplementedError, D6/D7) is
    # a documented removal, NOT a failure — record SKIP so the four-provider gate
    # stays green. Live providers raise a different error (missing ctor args) or
    # construct, so only a genuine retirement is caught here.
    local retire_rc=0
    python -c "
import sys
from warden.providers import create_session
try:
    create_session('$provider')
except NotImplementedError:
    sys.exit(7)
except Exception:
    pass
" 2>/dev/null || retire_rc=$?
    if [ "$retire_rc" -eq 7 ]; then
        RESULT[$provider]="SKIP"
        DETAIL[$provider]="retired adapter (D6/D7) — use the SDK provider"
        echo "  SKIP: '$provider' is a retired adapter (D6/D7)."
        return
    fi

    local out rc
    out="$(timeout "$TIMEOUT_S" python -m warden.drive.cli \
        --provider "$provider" --single "$PROMPT" 2>&1)"
    rc=$?

    # Show a tail so the log stays readable.
    echo "$out" | tail -n 25

    if [ "$rc" -eq 124 ]; then
        RESULT[$provider]="FAIL"
        DETAIL[$provider]="timed out after ${TIMEOUT_S}s"
        return
    fi

    # Detect explicit error / auth failures the CLI surfaces.
    if echo "$out" | grep -qiE "\[ERROR\]|not authenticated|not found|invalid api key|unauthorized|401|403|Not inside a trusted directory|no known auth"; then
        RESULT[$provider]="FAIL"
        DETAIL[$provider]="$(echo "$out" | grep -iE '\[ERROR\]|authenticat|unauthorized|invalid|trusted|401|403' | head -n1 | cut -c1-120)"
        return
    fi

    if echo "$out" | grep -qi "hello"; then
        RESULT[$provider]="PASS"
        DETAIL[$provider]="replied: $(echo "$out" | grep -io 'hello' | head -n1)"
    else
        RESULT[$provider]="FAIL"
        DETAIL[$provider]="no 'hello' in output (rc=$rc)"
    fi
}

run_cli_provider "claude"
run_cli_provider "claude-cli"
run_cli_provider "codex"

# --- openharness: python probe (gotcha 2 — CLI suppresses stream_delta) -----
echo
echo "=================================================================="
echo " [openharness] python ChatAPI probe (CLI --single shows blank)"
echo "=================================================================="

OH_OUT="$(timeout "$TIMEOUT_S" python - "$PROMPT" <<'PY' 2>&1
import asyncio, sys
from warden import ChatAPI, MessageEvent
from warden.config import get_harness_config

PROMPT = sys.argv[1]

async def main():
    # ChatAPI now takes a HarnessConfig; select openharness on it, matching
    # drive/cli.py construction. OPENHARNESS_BASE_URL/MODEL come from env.
    config = get_harness_config()
    config.provider.provider = "openharness"
    api = ChatAPI(config, repo_path=".")
    await api.init()
    chunks = []
    try:
        async for ev in api.send(PROMPT):
            # openharness streams reply text as kind="stream_delta" — the CLI
            # --single printer suppresses these, hence this probe. Also accept
            # kind="text" for providers that emit whole-message text.
            if isinstance(ev, MessageEvent) and ev.kind in ("stream_delta", "text"):
                t = ev.content.get("text")
                if t:
                    chunks.append(t)
    finally:
        await api.close()
    text = "".join(chunks)
    print("PROBE_TEXT_START")
    print(text)
    print("PROBE_TEXT_END")
    print("PROBE_HELLO", "yes" if "hello" in text.lower() else "no")

asyncio.run(main())
PY
)"
OH_RC=$?
echo "$OH_OUT" | tail -n 30

if [ "$OH_RC" -eq 124 ]; then
    RESULT[openharness]="FAIL"
    DETAIL[openharness]="timed out after ${TIMEOUT_S}s (Ollama unreachable?)"
elif echo "$OH_OUT" | grep -qi "Cannot connect to Ollama\|Ollama returned status"; then
    RESULT[openharness]="FAIL"
    DETAIL[openharness]="Ollama unreachable at OPENHARNESS_BASE_URL"
elif echo "$OH_OUT" | grep -q "PROBE_HELLO yes"; then
    RESULT[openharness]="PASS"
    DETAIL[openharness]="probe text contained 'hello'"
elif echo "$OH_OUT" | grep -q "PROBE_HELLO no"; then
    RESULT[openharness]="FAIL"
    DETAIL[openharness]="probe ran but text had no 'hello' (model drift)"
else
    RESULT[openharness]="FAIL"
    DETAIL[openharness]="probe error (see log above)"
fi

# --- Final summary ---------------------------------------------------------
echo
echo "=================================================================="
echo " SUMMARY"
echo "=================================================================="
printf " %-14s %-6s %s\n" "PROVIDER" "RESULT" "DETAIL"
printf " %-14s %-6s %s\n" "--------" "------" "------"
for p in claude claude-cli codex openharness; do
    printf " %-14s %-6s %s\n" "$p" "${RESULT[$p]:-SKIP}" "${DETAIL[$p]:-no run}"
done
echo "=================================================================="

# --- Gate: exit code reflects pass/fail ------------------------------------
# The four-provider run is now a REAL GATE, not a demo. Rules:
#   * Any provider that RAN and FAILED -> non-zero exit (fail the gate).
#   * A provider that SKIPPED (no credential injected for it) does NOT fail the
#     gate on its own — isolation legitimately omits creds we didn't inject.
#   * SMOKE_REQUIRE (space/comma list) forces named providers to be PASS; if any
#     required provider is missing or not PASS, fail. Use this to assert a
#     specific leg (e.g. SMOKE_REQUIRE=openharness for the free Ollama leg).
GATE_RC=0

for p in claude claude-cli codex openharness; do
    if [ "${RESULT[$p]:-SKIP}" = "FAIL" ]; then
        echo " GATE: FAIL — provider '$p' ran and failed: ${DETAIL[$p]:-}" >&2
        GATE_RC=1
    fi
done

if [ -n "${SMOKE_REQUIRE:-}" ]; then
    for req in ${SMOKE_REQUIRE//,/ }; do
        if [ "${RESULT[$req]:-SKIP}" != "PASS" ]; then
            echo " GATE: FAIL — required provider '$req' is ${RESULT[$req]:-SKIP}, not PASS." >&2
            GATE_RC=1
        fi
    done
fi

if [ "$GATE_RC" -eq 0 ]; then
    echo " GATE: PASS — no tested provider failed."
fi
exit "$GATE_RC"
