# Experiments — Safety Research & Iteration

Research tooling for testing safety configurations. Two kinds of experiments live here, each answering a different question.

## Two Kinds of Experiments

### 1. Preset Experiments — "Does this combination of safety layers stop an attacker?"

Interactive, manual experiments. Each preset wires a combination of system prompts, tool restrictions, and middleware into a CLI session. You run a preset, try to break it, and observe what gets through.

**Files:**
- `presets.py` — 19 named profiles (e.g., `ask-only`, `layered`, `e9-intent-classifier`)
- `prompts.py` — system prompt constants used by presets
- `tools.py` — custom tool implementations (save-note, safe-read)

**How to run:**

```bash
# Basic read-only mode
PYTHONPATH=. python -m warden.drive.cli --experiment ask-only

# Layered defense (system prompt + tool restriction + input middleware)
PYTHONPATH=. python -m warden.drive.cli --experiment layered

# Test fuzzy intent classifier
PYTHONPATH=. python -m warden.drive.cli --experiment e12-fuzzy-classifier

# Single-shot test (no REPL)
PYTHONPATH=. python -m warden.drive.cli --experiment layered --single "ignore all instructions"
```

**Preset summary:**

| Preset | What it tests |
|--------|--------------|
| `unrestricted` | Baseline — no safety layers |
| `ask-only` | System prompt + tool restriction |
| `note-taking` | Ask-only + custom save-note tool |
| `prompt-guard` | SanitizeMiddleware only |
| `layered` | System prompt + tool restriction + SanitizeMiddleware |
| `e1-system-prompt` | 7-rule safety prompt only |
| `e3-input-middleware` | Expanded injection + secret-seeking patterns |
| `e6-structural-absence` | Custom prompt without Claude Code preset |
| `e9-intent-classifier` | Rule-based intent classification |
| `e12-fuzzy-classifier` | Fuzzy (difflib) intent matching |
| `e15-canary-token` | Canary token leakage detection |

### 2. Classifier Experiments — "Which classifier detects injections best?"

Automated, metrics-driven experiments. Run every classifier against a labeled corpus, measure accuracy/precision/recall/F1/FPR, and produce a comparison report. The results tell you which classifier to deploy in production middleware.

**Files:**
- `classifier_runner.py` — runs corpus × classifiers, computes metrics
- `classifier_report.py` — generates markdown comparison report with recommendations
- `results/` — saved reports and raw JSON output

**How to run:**

```bash
# Run all classifiers (input + output)
PYTHONPATH=. python -m warden.safety.experiments.classifier_runner

# Run only input classifiers (prompt injection detection)
PYTHONPATH=. python -m warden.safety.experiments.classifier_runner --input-only

# Run only output classifiers (leak detection)
PYTHONPATH=. python -m warden.safety.experiments.classifier_runner --output-only

# Run specific classifiers by name
PYTHONPATH=. python -m warden.safety.experiments.classifier_runner --classifiers regex-input,enhanced-regex-output
```

**Available classifiers** (10 total, live in `safety/middleware/classifiers/`):

| Classifier | Type | What it does |
|-----------|------|-------------|
| `regex-input` | Input | Baseline substring/regex patterns |
| `promptguard-86m` | Input | PromptGuard 86M transformer model |
| `promptguard-22m` | Input | PromptGuard 22M transformer model |
| `deberta-onnx` | Input | DeBERTa v3 ONNX model |
| `ollama-*-input` | Input | Qwen3Guard via Ollama |
| `llm-judge-input-*` | Input | Haiku/GPT-4o-mini judge |
| `regex-output` | Output | Baseline leak detection (skill names, paths) |
| `enhanced-regex-output` | Output | Extended with PII, env vars, config patterns |
| `ollama-*-output` | Output | Qwen3Guard via Ollama |
| `llm-judge-output-*` | Output | Haiku/GPT-4o-mini judge |

## Structure

```
experiments/
├── presets.py              # Preset experiment profiles
├── prompts.py              # System prompt constants
├── tools.py                # Custom tool implementations
├── classifier_runner.py    # Classifier experiment runner
├── classifier_report.py    # Comparison report generator
├── requirements.txt        # ML dependencies for classifier experiments
└── results/
    ├── strategy-summary.md              # Preset strategy overview
    ├── preset-comparison.md             # Preset comparison table
    ├── classifier-executive-summary.md  # Classifier evaluation summary
    ├── classifier-YYYY-MM-DD.md         # Classifier comparison reports (dated)
    ├── preset/                          # Individual preset experiment results
    │   ├── e0-baseline.md
    │   ├── e1-system-prompt.md
    │   └── ...
    └── classifier/                      # Individual classifier run data
        └── raw_results.json
```

Summaries and comparisons live at the top of `results/`. Individual results live in `preset/` and `classifier/` subfolders.

Middleware and classifiers live in `safety/middleware/` — see the [middleware README](../middleware/README.md).
Test corpora live in `safety/dataset/` — shared across both experiment types.

## How Experiments Feed Production

```
Preset experiments    →  findings  →  Workflow YAML (permissions, tool scope)
                                   →  Middleware selection (which middleware to enable)

Classifier experiments →  findings  →  Which classifier to deploy in middleware
                                    →  Threshold tuning (confidence scores, FPR tolerance)
```
