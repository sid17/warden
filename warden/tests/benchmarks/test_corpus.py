"""Tests for corpus loader and classifier protocol."""

from __future__ import annotations

from warden.safety.middleware.classifiers import ClassifyResult
from warden.safety.dataset.corpus import load_input_corpus, load_output_corpus


class TestInputCorpus:
    def test_returns_non_empty_list(self) -> None:
        corpus = load_input_corpus()
        assert len(corpus) >= 30

    def test_has_both_labels(self) -> None:
        corpus = load_input_corpus()
        labels = {e.label for e in corpus}
        assert labels == {"benign", "adversarial"}

    def test_entries_have_required_fields(self) -> None:
        for entry in load_input_corpus():
            assert entry.id, "Empty id in entry"
            assert entry.text, f"Empty text in {entry.id}"
            assert entry.label, f"Empty label in {entry.id}"
            assert entry.category, f"Empty category in {entry.id}"


class TestOutputCorpus:
    def test_returns_non_empty_list(self) -> None:
        corpus = load_output_corpus()
        assert len(corpus) >= 20

    def test_has_both_labels(self) -> None:
        corpus = load_output_corpus()
        labels = {e.label for e in corpus}
        assert labels == {"leaked", "clean"}

    def test_entries_have_required_fields(self) -> None:
        for entry in load_output_corpus():
            assert entry.id, "Empty id in entry"
            assert entry.text, f"Empty text in {entry.id}"
            assert entry.label, f"Empty label in {entry.id}"
            assert entry.category, f"Empty category in {entry.id}"


class TestClassifyResult:
    def test_instantiation(self) -> None:
        result = ClassifyResult(
            label="safe",
            score=0.95,
            latency_ms=1.23,
            classifier="test-classifier",
        )
        assert result.label == "safe"
        assert result.score == 0.95
        assert result.latency_ms == 1.23
        assert result.classifier == "test-classifier"

    def test_all_label_values(self) -> None:
        for label in ("safe", "unsafe", "uncertain"):
            result = ClassifyResult(
                label=label, score=1.0, latency_ms=0.0, classifier="test",
            )
            assert result.label == label
