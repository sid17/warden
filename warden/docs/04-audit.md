# The Harness — Audit

> Companion to [`01-conceptual-model.md`](./01-conceptual-model.md). Audit is the **durable, ordered record
> of what a run actually did** — and, uniquely among the cross-cutting services, it is the harness's
> **feedback loop for configuring a workflow's permissions**. This doc answers three questions
> directly: *how we use audit* (§3), *how it differs from observability* (§2), and *how it drives
> permission config* (§4).

---

## 1. What audit is

An append-only record of the agent's **actions** — every tool call, on which file, by which
(sub)agent, and how it ended — including a governance **`stopped`** (a run killed for budget or
deadline, [`01` §7e](./01-conceptual-model.md#s7e)), which is exactly the kind of event that must be
forensically recorded. It comes in two complementary forms:

- **Hook logs (in-sandbox, offline analysis).** Each provider's native hook system fires on every
  tool call and writes a JSONL line — one file per run. Opt-in via `AUDIT_ENABLED=1`. This is the
  form used for **post-hoc safety analysis and permission tuning** (§3, §4).
- **The durable event log (control-plane, run history).** The append-only `run_events` the control
  plane mirrors out of the run ([`02-observability.md`](./02-observability.md) §6). This is the form used
  for **compliance, resume, idempotency, and "what happened on run 4821?"** (§5).

Both are *audit* in the sense of a faithful after-the-fact record; they differ in where they live and
what they're for. The rest of this doc is mostly about the first (it's the permission-config loop);
§5 ties in the second.

---

## 2. Audit vs observability — the explicit distinction

They are independent systems solving different problems (this is the question that most often gets
blurred):

| | **Observability** (telemetry) | **Audit** |
|---|---|---|
| Purpose | real-time health, cost, session analytics | post-hoc compliance, permission tuning, safety analysis |
| Output | Tempo traces · Prometheus metrics · Langfuse sessions | JSONL files on disk · the durable `run_events` log |
| Activation | always-on (infra-level) | opt-in (`AUDIT_ENABLED=1`) / control-plane mirror |
| Retention | backend-managed (Tempo, Langfuse DB) | local files, user-managed / durable app-owned log |
| Consumers | Grafana, the Langfuse UI | aggregation reports, human review, permission derivation |

> **Telemetry answers "is it healthy, and what did it cost?" Audit answers "exactly what did it do,
> in order, and was it allowed to?"**

They share no code, no data, and no dependencies. Run them together or apart.

---

## 3. How we use audit — the loop

Audit's day job is turning *observed behavior* into *permission config*. The loop:

```
  run the workspace UNRESTRICTED (AUDIT_ENABLED=1)
     │  provider hooks fire on every tool call
     ▼
  JSONL, one file per run  (logs/{run_id}.jsonl)
     │  aggregate across 2–3 runs
     ▼
  audit report  ──▶  human / agent derives per-agent permission config  (§4)
```

The aggregation report has four sections, each pointing at a specific permission decision:

| Report section | What it produces | Feeds |
|---|---|---|
| **Tool Usage Matrix** | per-agent tool call counts | tools never used → `disallowed_tools` candidates |
| **Path Access Map** | per-agent read/write paths | paths outside expectation → file-glob restrictions |
| **Command Inventory** | per-agent Bash commands, sensitive flags | `Bash` restrictions |
| **Convergence Analysis** | tool stability across runs | `<80%` → behavior still varies → *widen*, don't restrict |

**Provider mechanism (the same multiplex-vs-isolate split as telemetry).** Both providers converge on
one `AuditEvent`/JSONL schema, but capture it differently:

- **`claude` (SDK)** — in-process async callbacks on `ClaudeAgentOptions.hooks`. Fast, and because the
  SDK multiplexes sub-agent events onto the same stream, audit sees **everything** (parent + every
  sub-agent's tool calls).
- **`openharness`** — command hooks that spawn a subprocess per event. Fully isolated, but sub-agents
  run as their own subprocesses, so audit sees only **spawn/exit** — not the sub-agent's internal
  tool calls. Same visibility split as [`02-observability.md`](./02-observability.md) §4 — and the
  ideal closes it the same way, by auditing the sub-agent subprocess too.

---

## 4. Using audit to configure a workflow's permissions

This is where audit closes the loop back onto the **permission seam** ([§7a](./01-conceptual-model.md#s7a)).
The concept note's permission chain is only as good as the manifest it enforces — and audit is how
that manifest is **derived from real behavior instead of guessed.**

**The derivation, per agent:**

| From the report | Becomes |
|---|---|
| tools never used | `disallowed_tools` (the `ToolScope` static gate — [§7a](./01-conceptual-model.md#s7a) stage 1) |
| path access map | file-access globs (read dirs / write dirs) in the workflow manifest |
| command inventory | `Bash` allow/deny |
| convergence `<80%` | leave broad — a still-varying agent isn't ready to lock down |

**A concrete example** (an example course-authoring audit, 8 runs / 5 agents):

| Agent | Derived `disallowed_tools` | Path enforcement | Convergence |
|---|---|---|---|
| researcher | Agent, Edit, Glob, Grep | `Write` → research dir only | 100% |
| writer | Agent, Bash, Edit, Glob, Grep | `Read` → research, `Write` → module dir | 100% |
| explorer | Agent, Bash, Edit, Write (read-only) | — | 100% |
| root (orchestrator) | none (needs all tools) | monitor, don't restrict | 0% (expected) |

Two observations that shape the design:

- **Restrict per sub-agent, not globally.** The root orchestrator legitimately needs every tool (its
  0% convergence is *expected* — different runs exercise different capabilities). Safety comes from
  scoping the **sub-agents** it spawns, not from crippling the root.
- **Path restriction needs a hook, not the permission callback.** The SDK's `can_use_tool` doesn't
  fire for auto-allowed `Read` ([`03-safety.md`](./03-safety.md) §5), so per-*path* enforcement is a
  `PreToolUse` hook that blocks reads/writes outside expected dirs. Audit's Path Access Map is exactly
  what tells you which dirs those are.

**Then encode it in the workflow manifest.** The derived tool/file permissions become the workflow's
`permissions` block ([§4](./01-conceptual-model.md#s4), [§7a](./01-conceptual-model.md#s7a)), enforced every
session. So the pipeline is:

```
  audit → report → derived permissions → workflow manifest → enforced every session
                                                    │
                                        re-audit on drift ─┘
```

This is what makes the §7a manifest **empirical**: least-privilege stops being aspirational and
becomes measured, and re-auditing catches an agent whose behavior has drifted past its permissions.

---

## 5. The durable event log as an audit trail

The second form of audit (§1) is the control plane's append-only `run_events` log
([`02-observability.md`](./02-observability.md) §6): monotonic `seq`, primary key `(run_id, seq)`, mirrored
out of the sandbox so it survives teardown. One primitive delivers **idempotency, no-double-billing,
resume, and the audit trail** at once — *"the log **is** the ordered, durable history of the run."*
Recovery reads the log ("what durably happened, what comes next?"), never process liveness.

Where the hook logs (§3) are a *development-time* instrument for tuning permissions, the `run_events`
log is the *runtime* record of every run for compliance and forensics. Both are audit; they serve the
before (configure the workflow) and the after (account for what it did).
