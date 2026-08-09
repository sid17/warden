"""Classifier experiment runner — corpus × classifiers, measure metrics."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from datetime import datetime
from pathlib import Path

from warden.safety.middleware.classifiers import ClassifyResult
from warden.safety.dataset.corpus import CorpusEntry, load_input_corpus, load_output_corpus

_RESULTS_DIR = Path(__file__).parent / "results"
_CLASSIFIER_DATA_DIR = _RESULTS_DIR / "classifier"

# Label mapping: corpus label → classifier label
_LABEL_MAP = {
    "adversarial": "unsafe",
    "benign": "safe",
    "leaked": "unsafe",
    "clean": "safe",
}


# ---------------------------------------------------------------------------
# Classifier registry
# ---------------------------------------------------------------------------

def _build_registry() -> dict[str, tuple[type, str]]:
    """Discover available classifiers. Returns {name: (class, type)}."""
    registry: dict[str, tuple[type, str]] = {}

    # Input classifiers
    from warden.safety.middleware.classifiers.regex_input import RegexInputClassifier
    registry[RegexInputClassifier.name] = (RegexInputClassifier, "input")

    try:
        from warden.safety.middleware.classifiers.promptguard_86m import PromptGuard86MClassifier
        registry[PromptGuard86MClassifier.name] = (PromptGuard86MClassifier, "input")
    except (ImportError, RuntimeError):
        pass

    try:
        from warden.safety.middleware.classifiers.promptguard_22m import PromptGuard22MClassifier
        registry[PromptGuard22MClassifier.name] = (PromptGuard22MClassifier, "input")
    except (ImportError, RuntimeError):
        pass

    try:
        from warden.safety.middleware.classifiers.deberta_onnx import DeBertaONNXClassifier
        registry[DeBertaONNXClassifier.name] = (DeBertaONNXClassifier, "input")
    except (ImportError, RuntimeError):
        pass

    from warden.safety.middleware.classifiers.ollama_guard import OllamaGuardInputClassifier
    registry[OllamaGuardInputClassifier.name] = (OllamaGuardInputClassifier, "input")

    from warden.safety.middleware.classifiers.llm_judge import LLMJudgeInputClassifier
    registry["llm-judge-input-anthropic"] = (
        lambda: LLMJudgeInputClassifier(provider="anthropic"), "input",
    )
    registry["llm-judge-input-openai"] = (
        lambda: LLMJudgeInputClassifier(provider="openai"), "input",
    )

    # Output classifiers
    from warden.safety.middleware.classifiers.regex_output import RegexOutputClassifier
    registry[RegexOutputClassifier.name] = (RegexOutputClassifier, "output")

    from warden.safety.middleware.classifiers.enhanced_regex import EnhancedRegexOutputClassifier
    registry[EnhancedRegexOutputClassifier.name] = (EnhancedRegexOutputClassifier, "output")

    from warden.safety.middleware.classifiers.ollama_output import OllamaGuardOutputClassifier
    registry[OllamaGuardOutputClassifier.name] = (OllamaGuardOutputClassifier, "output")

    from warden.safety.middleware.classifiers.llm_output_judge import LLMJudgeOutputClassifier
    registry["llm-judge-output-anthropic"] = (
        lambda: LLMJudgeOutputClassifier(provider="anthropic"), "output",
    )
    registry["llm-judge-output-openai"] = (
        lambda: LLMJudgeOutputClassifier(provider="openai"), "output",
    )

    return registry


# ---------------------------------------------------------------------------
# Core runner
# ---------------------------------------------------------------------------

async def run_classifier(
    classifier: object,
    corpus: list[CorpusEntry],
) -> list[dict]:
    """Run a single classifier against all corpus entries."""
    results = []
    for entry in corpus:
        expected = _LABEL_MAP.get(entry.label, entry.label)
        result: ClassifyResult = await classifier.classify(entry.text)  # type: ignore[union-attr]
        results.append({
            "entry_id": entry.id,
            "category": entry.category,
            "expected_label": expected,
            "predicted_label": result.label,
            "score": result.score,
            "latency_ms": result.latency_ms,
            "correct": result.label == expected,
        })
    return results


def compute_metrics(results: list[dict]) -> dict:
    """Compute accuracy, precision, recall, F1, FPR, and latency stats."""
    tp = sum(1 for r in results if r["expected_label"] == "unsafe" and r["predicted_label"] == "unsafe")
    tn = sum(1 for r in results if r["expected_label"] == "safe" and r["predicted_label"] == "safe")
    fp = sum(1 for r in results if r["expected_label"] == "safe" and r["predicted_label"] == "unsafe")
    fn = sum(1 for r in results if r["expected_label"] == "unsafe" and r["predicted_label"] != "unsafe")

    total = len(results)
    accuracy = (tp + tn) / total if total else 0.0
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    fpr = fp / (fp + tn) if (fp + tn) else 0.0

    latencies = sorted(r["latency_ms"] for r in results)
    p50 = latencies[len(latencies) // 2] if latencies else 0.0
    p95_idx = min(int(math.ceil(len(latencies) * 0.95)) - 1, len(latencies) - 1)
    p95 = latencies[p95_idx] if latencies else 0.0

    return {
        "accuracy": accuracy,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "fpr": fpr,
        "latency_p50_ms": p50,
        "latency_p95_ms": p95,
        "tp": tp, "tn": tn, "fp": fp, "fn": fn,
        "total": total,
    }


async def run_all_classifiers(
    classifiers: list[tuple[str, object]],
    corpus: list[CorpusEntry],
) -> dict[str, list[dict]]:
    """Run all classifiers against a corpus. Returns {name: results}."""
    all_results = {}
    for name, clf in classifiers:
        print(f"  Running {name}...", end=" ", flush=True)
        try:
            results = await run_classifier(clf, corpus)
            metrics = compute_metrics(results)
            correct = sum(1 for r in results if r["correct"])
            print(f"{correct}/{len(results)} correct (acc={metrics['accuracy']:.1%})")
            all_results[name] = results
        except Exception as e:
            print(f"FAILED: {e}")
    return all_results


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Classifier experiment runner")
    parser.add_argument("--input-only", action="store_true", help="Only run input classifiers")
    parser.add_argument("--output-only", action="store_true", help="Only run output classifiers")
    parser.add_argument("--classifiers", type=str, help="Comma-separated classifier names to run")
    args = parser.parse_args()

    registry = _build_registry()

    # Filter by type
    if args.input_only:
        registry = {k: v for k, v in registry.items() if v[1] == "input"}
    elif args.output_only:
        registry = {k: v for k, v in registry.items() if v[1] == "output"}

    # Filter by name
    if args.classifiers:
        names = {n.strip() for n in args.classifiers.split(",")}
        registry = {k: v for k, v in registry.items() if k in names}

    if not registry:
        print("No classifiers selected. Use --classifiers or check available names.")
        sys.exit(1)

    # Instantiate classifiers
    input_clfs: list[tuple[str, object]] = []
    output_clfs: list[tuple[str, object]] = []

    for name, (cls_or_factory, cls_type) in registry.items():
        try:
            clf = cls_or_factory()
            if cls_type == "input":
                input_clfs.append((name, clf))
            else:
                output_clfs.append((name, clf))
        except Exception as e:
            print(f"  Skipping {name}: {e}")

    input_corpus = load_input_corpus()
    output_corpus = load_output_corpus()

    input_results: dict[str, list[dict]] = {}
    output_results: dict[str, list[dict]] = {}

    if input_clfs:
        print(f"\n=== Input Classifiers ({len(input_corpus)} entries) ===")
        input_results = asyncio.run(run_all_classifiers(input_clfs, input_corpus))

    if output_clfs:
        print(f"\n=== Output Classifiers ({len(output_corpus)} entries) ===")
        output_results = asyncio.run(run_all_classifiers(output_clfs, output_corpus))

    # Save raw results
    _CLASSIFIER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    raw_path = _CLASSIFIER_DATA_DIR / "raw_results.json"
    with open(raw_path, "w") as f:
        json.dump({"input": input_results, "output": output_results}, f, indent=2)
    print(f"\nRaw results saved to {raw_path}")

    # Generate report (top-level results/ with classifier- prefix)
    from warden.safety.experiments.classifier_report import generate_report
    report = generate_report(input_results, output_results)
    date_str = datetime.now().strftime("%Y-%m-%d")
    report_path = _RESULTS_DIR / f"classifier-{date_str}.md"
    with open(report_path, "w") as f:
        f.write(report)
    print(f"Report saved to {report_path}")


if __name__ == "__main__":
    main()
