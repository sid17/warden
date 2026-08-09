# safety/ — L3: the safety pipeline

> **Layer / role:** L3 cross-cutting — the safety pipeline; a peer of `persistence/` and `observability/`.
> **Implements:** [`01-conceptual-model`](../docs/01-conceptual-model.md) §5 L3, §7a (permission enforcement), §7b (middleware); the full model is [`03-safety.md`](../docs/03-safety.md).
> **Inbound:** the orchestrator (L2) runs the middleware pipeline and calls `PermissionChecker` in the permission chain; realizes the `seams/` middleware + permission-handler protocols.
> **Outbound:** `seams/` (protocols it implements), `workspace/` (the workflow rules the checker evaluates), `schemas/`.

Four modules: **permissions** (production rule enforcement — stage 2 of the permission
chain), **middleware** (production input/output filtering and classifiers — the middleware
seam's implementations), **dataset** (labeled test corpora), and **experiments** (research
for iterating on safety configurations).

```
safety/
├── permissions/       # PRODUCTION — workflow rule enforcement (checker + sensitive paths)
├── middleware/        # PRODUCTION — input/output filtering + classifiers
├── dataset/           # SHARED — labeled corpora for evaluation and testing
└── experiments/       # RESEARCH — safety experimentation + benchmarks (not runtime)
```

## How the Four Modules Work Together

| | [Permissions](permissions/) | [Middleware](middleware/) | [Dataset](dataset/) | [Experiments](experiments/) |
|---|---|---|---|---|
| **Purpose** | Enforce tool/file access rules from workflow YAML | Filter input for injection, scan output for leaks, classify content | Labeled test data for evaluation | Test and iterate on safety configurations |
| **When it runs** | Every chat session, always on | When wired by experiment preset or workflow config | Loaded by experiments and tests | On-demand via CLI `--experiment` flag or classifier runner |
| **Who uses it** | Orchestrator calls `PermissionChecker` before every tool execution | Orchestrator runs middleware pipeline before LLM calls | Classifier runner, middleware tests | Developer runs experiments to find the right safety config |
| **What it controls** | Tool allow/deny lists, file access globs, sensitive path detection | Prompt injection blocking, output leak detection, content classification | Adversarial/benign inputs, leaked/clean outputs | System prompts, preset profiles, custom tools, classifier evaluation |

## How They Connect

Experiments discover what safety configurations work. Those findings get encoded as:
- Workflow YAML rules → consumed by **permissions** at runtime
- Middleware classes → deployed in **middleware/** for production use
- Classifier selections → live in **middleware/classifiers/** for both evaluation and production
- Tool scope defaults → configured in workflow YAML `allowed_tools` field

```
Experiments (research)  →  findings  →  Permissions config (production)
                                    →  Middleware classes (production)
                                    →  Workflow YAML (production)

Dataset (test data)  →  used by  →  Classifier experiments (evaluation)
                                 →  Middleware tests (regression)
```

See each subfolder's README for detail:
- [permissions/README.md](permissions/README.md) — checker, sensitive paths, workflow integration
- [middleware/README.md](middleware/README.md) — input/output filtering, classifiers, wiring
- [experiments/README.md](experiments/README.md) — preset experiments, classifier experiments, how to run
