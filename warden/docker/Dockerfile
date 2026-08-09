# Containerized harness engine (drive.cli + harness_api) — runs the three live
# providers (claude SDK, codex SDK, openharness) in isolation; the retired
# claude-cli / codex-exec adapters are gated off at the factory. Credentials are
# injected at `docker run` time via env / bind-mounts (see docker/run.sh);
# nothing secret is ever baked into the image.
FROM python:3.14-slim-bookworm

# --- OS deps ---------------------------------------------------------------
# git is REQUIRED: the codex SDK runs against the working dir as a trusted git
# repo (no --skip-git-repo-check), so it must be a git repo.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
        git \
    && rm -rf /var/lib/apt/lists/*

# --- Node 20 (for the claude + codex CLIs) ---------------------------------
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# The `claude` binary backs the claude SDK provider; `codex` backs the codex
# SDK provider. claude-code is PINNED: 2.1.226 is the CLI bundled by claude-agent-sdk
# 0.2.134 (our locked SDK), kept in lockstep since they share a control protocol.
# 2.1.226 also carries the earlier background-subagent robustness fixes (2.1.206
# hang-then-abort, 2.1.216 startup-window cancellation) relevant to the B1 sub-agent
# stall. The SDK 0.2.127+ pin is what fixes the premature-stdin-close race (empty
# zero-token result on the post-approval resume) — see research-stream-closed-resume.md.
RUN npm install -g @anthropic-ai/claude-code@2.1.226 @openai/codex \
    && npm cache clean --force

# --- Python deps -----------------------------------------------------------
COPY docker/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
# EXT-C3 multi-replica bed: the harness's shared Postgres backends are an optional
# extra (not in the exported requirements.txt). Install the two drivers so the
# `--c3-2c` gate can exercise the Postgres run-registry / event-log / task-lock
# (asyncpg) and the sync HITL defer store (psycopg v3). Credential-free at run time.
RUN pip install --no-cache-dir "asyncpg>=0.30" "psycopg[binary]>=3.2.10"

# --- App -------------------------------------------------------------------
WORKDIR /app
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1
# The harness engine is a set of sibling packages under warden/; copy
# them into /app/warden/ so they import as `warden.*`
# (PYTHONPATH=/app; `engines` resolves as a PEP-420 namespace package). The engine
# is self-contained (no external/monorepo imports), so ONLY warden/ is copied here. A
# product profile is NOT baked in: it is loaded at runtime by fully-qualified
# WARDEN_PROFILE and its package is bind-mounted (see your profile's run.sh).
COPY __init__.py __main__.py /app/warden/
COPY config/        /app/warden/config/
COPY workspace/     /app/warden/workspace/
COPY providers/     /app/warden/providers/
COPY orchestrator/  /app/warden/orchestrator/
COPY drive/         /app/warden/drive/
COPY safety/        /app/warden/safety/
COPY persistence/   /app/warden/persistence/
COPY observability/ /app/warden/observability/
COPY seams/         /app/warden/seams/
COPY schemas/       /app/warden/schemas/
COPY harness_api/   /app/warden/harness_api/

# Ship the e2e smoke DRIVERS (not the whole test tree) so the bed's
# `--perm-smoke` mode can run the T4 permission-bridge leg in-image. These are
# thin drivers that import only shipped harness code; keep them in sync with
# tests/e2e/. The empty package __init__s let them import as
# `warden.tests.e2e.*`.
COPY tests/__init__.py           /app/warden/tests/__init__.py
COPY tests/e2e/__init__.py       /app/warden/tests/e2e/__init__.py
COPY tests/e2e/t4_perm_smoke.py  /app/warden/tests/e2e/t4_perm_smoke.py
COPY tests/e2e/t_custom_tool.py  /app/warden/tests/e2e/t_custom_tool.py
COPY tests/e2e/t3_isolation.py   /app/warden/tests/e2e/t3_isolation.py
COPY tests/e2e/openharness_perm_smoke.py /app/warden/tests/e2e/openharness_perm_smoke.py
COPY tests/e2e/codex_auth_smoke.py /app/warden/tests/e2e/codex_auth_smoke.py
COPY tests/e2e/codex_perm_smoke.py /app/warden/tests/e2e/codex_perm_smoke.py
COPY tests/e2e/codex_custom_tool_smoke.py /app/warden/tests/e2e/codex_custom_tool_smoke.py
COPY tests/e2e/auth_typed_smoke.py /app/warden/tests/e2e/auth_typed_smoke.py
# Session resume-recall gate (docs/08): the shared driver + the three thin
# per-provider entry points for the `--*-session` bed modes.
COPY tests/e2e/governance_budget_smoke.py /app/warden/tests/e2e/governance_budget_smoke.py
COPY tests/e2e/_session_recall.py /app/warden/tests/e2e/_session_recall.py
COPY tests/e2e/claude_session_smoke.py /app/warden/tests/e2e/claude_session_smoke.py
COPY tests/e2e/codex_session_smoke.py /app/warden/tests/e2e/codex_session_smoke.py
COPY tests/e2e/openharness_session_smoke.py /app/warden/tests/e2e/openharness_session_smoke.py
# Crash-recovery gate (docs/08 S6): shared driver + three thin entry points.
COPY tests/e2e/_session_crash.py /app/warden/tests/e2e/_session_crash.py
COPY tests/e2e/claude_crash_smoke.py /app/warden/tests/e2e/claude_crash_smoke.py
COPY tests/e2e/codex_crash_smoke.py /app/warden/tests/e2e/codex_crash_smoke.py
COPY tests/e2e/openharness_crash_smoke.py /app/warden/tests/e2e/openharness_crash_smoke.py
# M6 durable-HITL gate (docs/07-durable-hitl §7): one driver, all three providers.
COPY tests/e2e/m6_hitl_smoke.py /app/warden/tests/e2e/m6_hitl_smoke.py
COPY tests/e2e/m6_gate_smoke.py /app/warden/tests/e2e/m6_gate_smoke.py
# EXT-C3 multi-replica two-container gate (docs/09 §7): one driver, all modes.
COPY tests/e2e/c3_multireplica_smoke.py /app/warden/tests/e2e/c3_multireplica_smoke.py

# BUILD-TIME SANITY CHECK: fail the build early if any import dep is missing.
RUN python -c "import warden.drive.cli; import warden.providers.openharness.session; import warden.harness_api.app; print('import OK')"

# --- Entrypoint ------------------------------------------------------------
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
