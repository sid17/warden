"""Tests for API-based LLM judge classifiers."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch


from warden.safety.middleware.classifiers import ClassifyResult
from warden.safety.middleware.classifiers.llm_judge import LLMJudgeInputClassifier, _parse_response
from warden.safety.middleware.classifiers.llm_output_judge import LLMJudgeOutputClassifier


class TestParseResponse:
    def test_valid_json(self) -> None:
        label, score = _parse_response('{"label": "unsafe", "confidence": 0.95}')
        assert label == "unsafe"
        assert score == 0.95

    def test_markdown_code_block(self) -> None:
        raw = '```json\n{"label": "safe", "confidence": 0.8}\n```'
        label, score = _parse_response(raw)
        assert label == "safe"

    def test_fallback_keyword(self) -> None:
        label, score = _parse_response("this is unsafe")
        assert label == "unsafe"

    def test_unparseable(self) -> None:
        label, score = _parse_response("gibberish")
        assert label == "uncertain"


class TestLLMJudgeInputClassifier:
    def test_init_anthropic(self) -> None:
        clf = LLMJudgeInputClassifier(provider="anthropic")
        assert clf.name == "llm-judge-input-anthropic"

    def test_init_openai(self) -> None:
        clf = LLMJudgeInputClassifier(provider="openai")
        assert clf.name == "llm-judge-input-openai"

    def test_api_error_returns_uncertain(self) -> None:
        clf = LLMJudgeInputClassifier(provider="anthropic")
        with patch.object(clf, "_call_api", new_callable=AsyncMock, side_effect=Exception("API down")):
            result = asyncio.run(clf.classify("test"))
        assert result.label == "uncertain"
        assert result.score == 0.0
        assert isinstance(result, ClassifyResult)


class TestLLMJudgeOutputClassifier:
    def test_init_anthropic(self) -> None:
        clf = LLMJudgeOutputClassifier(provider="anthropic")
        assert clf.name == "llm-judge-output-anthropic"

    def test_api_error_returns_uncertain(self) -> None:
        clf = LLMJudgeOutputClassifier(provider="anthropic")
        with patch.object(clf, "_call_api", new_callable=AsyncMock, side_effect=Exception("API down")):
            result = asyncio.run(clf.classify("test"))
        assert result.label == "uncertain"
        assert result.score == 0.0

    def test_successful_classify(self) -> None:
        clf = LLMJudgeOutputClassifier(provider="anthropic")
        mock_response = '{"label": "unsafe", "confidence": 0.9}'
        with patch.object(clf, "_call_api", new_callable=AsyncMock, return_value=mock_response):
            result = asyncio.run(clf.classify("leaked skills"))
        assert result.label == "unsafe"
        assert result.score == 0.9
