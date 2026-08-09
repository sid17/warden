# Production Safety Readiness Checklist

> After auditing a workflow and deriving configs (see [audit-process.md](audit-process.md) — now automated via `derive_manifest.py`), use this checklist to deploy those configs in production.
>
> Audit itself is config-first: it is controlled by `config.observability.audit`
> (`AuditConfig`), populated by `AUDIT_ENABLED`/`AUDIT_RUN_ID`/`AUDIT_LOG_DIR` via
> the settings layer. Enable/disable it per environment by setting that config,
> not by reading env in hook code.

## 1. PreToolUse Hooks on Write/Edit (File Output Sanitization)

**What:** Before any Write or Edit tool call executes, a PreToolUse hook inspects the `tool_input` (file path + content) and blocks writes that contain leaked internals.

**Implementation:**
- Hook configured in settings.json with matcher `"Write|Edit"`
- Hook script receives `tool_input` with `file_path` and `content`
- Runs content through classifiers (from `safety/middleware/classifiers/`)
- If classifier flags content: exit code 2 with deny reason
- If classifier passes: exit code 0 (allow)

**Requirements:**
- [ ] Resolve symlinks before path checks
- [ ] Allow writes to expected output paths (from audit Path Access Map)
- [ ] Block writes to sensitive paths (`.claude/`, `.env`, `.ssh/`, etc.)
- [ ] Run content classifiers on write content (check for skill names, agent names, YAML frontmatter, `.claude/` paths)
- [ ] Configurable — can be enabled/disabled per workflow via agent YAML `hooks` field

## 2. Per-Agent Tool Scoping

**What:** Each sub-agent in the pipeline runs with only the tools it actually needs, based on audit findings.

**Implementation:**
- Agent `tools` / `disallowed_tools` populated from `derive_manifest.py`'s per-sub-agent proposal (converged agents only; root + `<80%`-stability agents are left broad)
- Agents that only read get `["Read", "Grep", "Glob"]`
- Agents that write get the specific tools they used (not blanket access)

**Requirements:**
- [ ] `derive_manifest.py` run over 2+ audited runs; the proposed diff reviewed
- [ ] Agent YAML definitions updated with `tools` field
- [ ] Test that pipeline still completes with restricted tool access

## 3. Per-Agent Permission Modes

**What:** Each sub-agent runs with the appropriate permission mode based on its role.

**Implementation:**
- Agent YAML frontmatter `permissionMode` field set per audit findings
- Read-only agents: `"default"` or `"plan"`
- Write agents: `"acceptEdits"` (auto-accept edits, prompt for other)
- Trusted pipeline agents: `"auto"` (only if justified by audit)

**Requirements:**
- [ ] Permission mode recommendations in safety config guide
- [ ] Agent YAML definitions updated with `permissionMode`
- [ ] No agent uses `"bypassPermissions"` unless explicitly justified

## 4. Runtime Hook Observability (OTel Export)

**What:** Connect audit hooks to an OTel backend for production monitoring.

**Implementation:**
- Hook scripts emit OTel spans via OTLP exporter (in addition to JSONL)
- Spans use OTel GenAI semantic conventions (already aligned in audit schema)
- Backend: Langfuse (open-source, OTLP endpoint) or Phoenix (OTel-native)

**Requirements:**
- [ ] OTel exporter added to hook scripts
- [ ] Backend deployed (Langfuse or Phoenix)
- [ ] Dashboard for tool call monitoring, sub-agent lifecycle, anomaly detection
- [ ] Alerting on unexpected tool usage patterns

## 5. Hook Performance Budget

**What:** Ensure hooks don't unacceptably slow production pipelines.

**Requirements:**
- [ ] Measure hook overhead per event (target: <10ms for logging hooks)
- [ ] Classifier hooks (PreToolUse) have latency budget (target: <500ms)
- [ ] Hooks can be selectively disabled per-environment (audit vs production)
- [ ] Hook failure does not crash the pipeline (graceful degradation)

## 6. End-to-End Validation

**What:** Run the pipeline with all safety layers active and verify it still produces correct output.

**Requirements:**
- [ ] Pipeline completes with per-agent tool scoping
- [ ] Pipeline completes with PreToolUse sanitization hooks
- [ ] Output quality matches unrestricted runs (diff comparison)
- [ ] No false positive blocks on legitimate pipeline actions
- [ ] Audit report generated for the safety-enabled run (compare to unrestricted)
- [ ] The reproducible audit gate passes: `warden/docker/run.sh --audit-trail` (fires an audited turn per provider, validates the JSONL trails, runs the derivation, asserts the AUD-3 governance stop)

## Priority Order

1. Per-agent tool scoping (highest impact, lowest complexity)
2. Per-agent permission modes (same)
3. PreToolUse hooks on Write/Edit (highest security value)
4. End-to-end validation (must follow 1-3)
5. OTel export (nice-to-have, not blocking)
6. Hook performance budget (matters only at scale)
