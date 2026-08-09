# Middleware — Input/Output Filtering & Classifiers

Production safety middleware. Input middleware blocks prompt injection before it reaches the LLM. Output filters detect leaked internal information in model responses. Classifiers provide the detection logic used by both middleware and classifier experiments.

## Structure

```
middleware/
├── input/
│   ├── sanitize.py    # SanitizeMiddleware (basic), E3ExpandedMiddleware (extended patterns + secret-seeking)
│   ├── intent.py      # IntentClassifierMiddleware (rule-based), FuzzyIntentClassifier (difflib)
│   └── canary.py      # check_canary() — detects planted canary token leakage
├── output/
│   ├── filters.py     # check_output_for_leaks() (skill/agent/path detection), StreamingOutputFilter
│   └── sanitize.py    # sanitize_output() — replaces sensitive patterns in tool output
└── classifiers/       # Content classification implementations
    ├── regex_input.py, regex_output.py       # Baseline substring/regex
    ├── enhanced_regex.py                      # Extended with PII/path/env patterns
    ├── promptguard_86m.py, promptguard_22m.py # PromptGuard transformer models
    ├── deberta_onnx.py                        # DeBERTa v3 ONNX
    ├── ollama_guard.py, ollama_output.py      # Qwen3Guard via Ollama
    └── llm_judge.py, llm_output_judge.py      # Haiku/GPT-4o-mini LLM judges
```

## How Middleware Is Wired

The orchestrator runs middleware in a pipeline before sending to the LLM provider:

```python
for mw in self._middleware:
    result = await mw.before_send(content, context)
    if isinstance(result, RejectResult):
        # Message blocked — return error to user
        return
    content = result  # Pass modified content to next middleware
```

Middleware is injected via:
- **Experiment presets** — `--experiment prompt-guard` wires `SanitizeMiddleware`
- **Workflow config** — future: workflow YAML will specify middleware directly

## Classifier Protocol

All classifiers implement the same protocol (defined in `classifiers/__init__.py`):

```python
class Classifier(Protocol):
    name: str
    async def classify(self, text: str) -> ClassifyResult: ...
```

Classifiers are used by the classifier experiment runner (`experiments/classifier_runner.py`) to evaluate detection accuracy, and can be wired into middleware for production use.
