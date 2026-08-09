"""Tests for enhanced regex output classifier."""

from __future__ import annotations

import asyncio

from warden.safety.middleware.classifiers import ClassifyResult
from warden.safety.middleware.classifiers.enhanced_regex import EnhancedRegexOutputClassifier


class TestEnhancedRegexOutputClassifier:
    def setup_method(self) -> None:
        self.clf = EnhancedRegexOutputClassifier()

    def test_catches_email_pii(self) -> None:
        result = asyncio.run(self.clf.classify("Contact john@example.com for access"))
        assert result.label == "unsafe"

    def test_catches_file_path(self) -> None:
        result = asyncio.run(self.clf.classify("Located at /Users/john/secret"))
        assert result.label == "unsafe"

    def test_catches_env_var(self) -> None:
        result = asyncio.run(self.clf.classify("export API_KEY=abc123"))
        assert result.label == "unsafe"

    def test_catches_env_file(self) -> None:
        result = asyncio.run(self.clf.classify("Check the .env file for config"))
        assert result.label == "unsafe"

    def test_passes_clean_output(self) -> None:
        result = asyncio.run(self.clf.classify("The server starts on port 8000."))
        assert result.label == "safe"

    def test_catches_baseline_skill_names(self) -> None:
        result = asyncio.run(self.clf.classify(
            "I have kickoff, grilling, and dev skills"
        ))
        assert result.label == "unsafe"

    def test_catches_claude_path(self) -> None:
        result = asyncio.run(self.clf.classify("Config is in .claude/settings.json"))
        assert result.label == "unsafe"

    def test_catches_credentials_file(self) -> None:
        result = asyncio.run(self.clf.classify("Load from credentials.json"))
        assert result.label == "unsafe"

    def test_catches_ssh_path(self) -> None:
        result = asyncio.run(self.clf.classify("Copy the key from ~/.ssh/id_rsa"))
        assert result.label == "unsafe"

    def test_returns_valid_classify_result(self) -> None:
        result = asyncio.run(self.clf.classify("hello"))
        assert isinstance(result, ClassifyResult)
        assert result.classifier == "enhanced-regex-ner-output"
        assert result.latency_ms >= 0.0
