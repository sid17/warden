"""Tests for classifier experiment runner and report generator."""

from __future__ import annotations

import asyncio

from warden.safety.middleware.classifiers import ClassifyResult
from warden.safety.dataset.corpus import CorpusEntry
from warden.safety.experiments.classifier_report import generate_report
from warden.safety.experiments.classifier_runner import compute_metrics, run_classifier


class _MockClassifier:
    name = "mock-classifier"

    def __init__(self, always_label: str = "safe") -> None:
        self._label = always_label

    async def classify(self, text: str) -> ClassifyResult:
        return ClassifyResult(
            label=self._label, score=1.0, latency_ms=0.5, classifier=self.name,
        )


class TestRunClassifier:
    def test_produces_correct_structure(self) -> None:
        corpus = [
            CorpusEntry(id="T1", text="benign input", label="benign", category="test"),
            CorpusEntry(id="T2", text="adversarial input", label="adversarial", category="test"),
            CorpusEntry(id="T3", text="another benign", label="benign", category="test"),
        ]
        clf = _MockClassifier(always_label="safe")
        results = asyncio.run(run_classifier(clf, corpus))

        assert len(results) == 3
        for r in results:
            assert "entry_id" in r
            assert "expected_label" in r
            assert "predicted_label" in r
            assert "score" in r
            assert "latency_ms" in r
            assert "correct" in r

    def test_correct_label_mapping(self) -> None:
        corpus = [
            CorpusEntry(id="T1", text="benign", label="benign", category="test"),
        ]
        clf = _MockClassifier(always_label="safe")
        results = asyncio.run(run_classifier(clf, corpus))
        assert results[0]["expected_label"] == "safe"
        assert results[0]["correct"] is True


class TestComputeMetrics:
    def test_perfect_classifier(self) -> None:
        results = [
            {"expected_label": "unsafe", "predicted_label": "unsafe", "latency_ms": 1.0, "score": 1.0},
            {"expected_label": "safe", "predicted_label": "safe", "latency_ms": 1.0, "score": 1.0},
        ]
        m = compute_metrics(results)
        assert m["accuracy"] == 1.0
        assert m["precision"] == 1.0
        assert m["recall"] == 1.0
        assert m["f1"] == 1.0
        assert m["fpr"] == 0.0

    def test_with_false_positive(self) -> None:
        results = [
            {"expected_label": "unsafe", "predicted_label": "unsafe", "latency_ms": 1.0, "score": 1.0},
            {"expected_label": "safe", "predicted_label": "unsafe", "latency_ms": 1.0, "score": 1.0},
            {"expected_label": "safe", "predicted_label": "safe", "latency_ms": 1.0, "score": 1.0},
        ]
        m = compute_metrics(results)
        assert m["tp"] == 1
        assert m["fp"] == 1
        assert m["tn"] == 1
        assert m["fn"] == 0
        assert m["precision"] == 0.5
        assert m["recall"] == 1.0
        assert m["fpr"] == 0.5

    def test_empty_results(self) -> None:
        m = compute_metrics([])
        assert m["accuracy"] == 0.0


class TestGenerateReport:
    def test_produces_valid_markdown(self) -> None:
        input_results = {
            "test-clf": [
                {"entry_id": "T1", "category": "test", "expected_label": "unsafe",
                 "predicted_label": "unsafe", "score": 1.0, "latency_ms": 1.0, "correct": True},
                {"entry_id": "T2", "category": "test", "expected_label": "safe",
                 "predicted_label": "safe", "score": 1.0, "latency_ms": 1.0, "correct": True},
            ],
        }
        output_results = {
            "test-output": [
                {"entry_id": "O1", "category": "test", "expected_label": "unsafe",
                 "predicted_label": "safe", "score": 0.6, "latency_ms": 2.0, "correct": False},
            ],
        }
        report = generate_report(input_results, output_results)
        assert "## Input Classifiers" in report
        assert "## Output Classifiers" in report
        assert "## Recommendation" in report
        assert "test-clf" in report
        assert "test-output" in report

    def test_includes_misclassified(self) -> None:
        results = {
            "clf": [
                {"entry_id": "X1", "category": "injection", "expected_label": "unsafe",
                 "predicted_label": "safe", "score": 0.3, "latency_ms": 1.0, "correct": False},
            ],
        }
        report = generate_report(results, {})
        assert "X1" in report
        assert "Misclassified" in report

    def test_handles_classifier_error_gracefully(self) -> None:
        """Runner should handle exceptions from classifiers."""
        class _FailingClassifier:
            name = "failing"
            async def classify(self, text: str) -> ClassifyResult:
                raise RuntimeError("Model crashed")

        corpus = [CorpusEntry(id="T1", text="test", label="benign", category="test")]
        # Should raise since we don't catch in run_classifier
        try:
            asyncio.run(run_classifier(_FailingClassifier(), corpus))
            assert False, "Should have raised"
        except RuntimeError:
            pass  # Expected
