"""
tests/test_ai_engine.py
------------------------
Unit tests for AIEngine.
Uses mocked LLM providers — no real API calls made.
"""

import json
import pytest
from unittest.mock import MagicMock, patch
from pathlib import Path
import tempfile
import os

from bot.core.ai_engine import AIEngine, AnalysisResult, Patch, OpenAIProvider, AnthropicProvider
from bot.core.issue_fetcher import Issue
from bot.utils.config_loader import ConfigLoader
from bot.utils.logger import BotLogger


# ── Fixtures ──────────────────────────────────────────────────────── #

@pytest.fixture
def mock_config():
    config = MagicMock(spec=ConfigLoader)
    config.get.side_effect = lambda key, default=None: {
        "ai.provider": "openai",
        "ai.model": "gpt-4o",
        "ai.max_tokens": 1000,
        "ai.temperature": 0.2,
    }.get(key, default)
    config.require.side_effect = lambda key: {
        "ai.api_key": "sk-test-key",
    }.get(key, "test-value")
    return config


@pytest.fixture
def mock_logger():
    return MagicMock(spec=BotLogger)


@pytest.fixture
def sample_issue():
    return Issue(
        id=12345,
        number=42,
        title="Fix division by zero in calculator",
        body="When input is 0, the function crashes with ZeroDivisionError.",
        repo_full_name="owner/my-repo",
        html_url="https://github.com/owner/my-repo/issues/42",
    )


MOCK_ANALYSIS_JSON = {
    "affected_files": ["calculator.py"],
    "fix_description": "Add zero division guard in divide()",
    "patches": [
        {
            "file": "calculator.py",
            "original": "return a / b",
            "replacement": "if b == 0:\n        raise ValueError('Division by zero')\n    return a / b",
        }
    ],
    "confidence_score": 0.92,
    "pr_title": "fix: resolve issue #42 - Fix division by zero in calculator",
    "pr_description": "## Summary\n\nFixes #42 by adding a zero-division guard.",
}

MOCK_RELEVANT_FILES_JSON = {
    "relevant_files": ["calculator.py"],
    "reasoning": "The issue mentions division by zero which is in calculator.py",
}


# ── Tests ──────────────────────────────────────────────────────────── #

class TestAIEngine:

    def _make_engine(self, mock_config, mock_logger):
        with patch("bot.core.ai_engine.OpenAIProvider") as MockProvider:
            mock_provider = MagicMock()
            MockProvider.return_value = mock_provider
            engine = AIEngine(mock_config, mock_logger)
            engine.provider = mock_provider
        return engine

    def test_parse_json_strips_code_fences(self, mock_config, mock_logger):
        engine = self._make_engine(mock_config, mock_logger)
        raw = f"```json\n{json.dumps(MOCK_ANALYSIS_JSON)}\n```"
        parsed = engine._parse_json(raw)
        assert parsed["confidence_score"] == 0.92

    def test_parse_json_plain(self, mock_config, mock_logger):
        engine = self._make_engine(mock_config, mock_logger)
        raw = json.dumps(MOCK_RELEVANT_FILES_JSON)
        parsed = engine._parse_json(raw)
        assert "relevant_files" in parsed

    def test_analyze_issue_returns_result(self, mock_config, mock_logger, sample_issue):
        engine = self._make_engine(mock_config, mock_logger)
        engine.provider.complete.return_value = json.dumps(MOCK_ANALYSIS_JSON)

        result = engine.analyze_issue(sample_issue, {"calculator.py": "return a / b"})

        assert isinstance(result, AnalysisResult)
        assert result.confidence_score == 0.92
        assert len(result.patches) == 1
        assert result.patches[0].file == "calculator.py"

    def test_analyze_issue_handles_bad_json(self, mock_config, mock_logger, sample_issue):
        engine = self._make_engine(mock_config, mock_logger)
        engine.provider.complete.return_value = "not valid json at all"

        result = engine.analyze_issue(sample_issue, {"calculator.py": "..."})

        assert result.confidence_score == 0.0
        assert result.patches == []

    def test_get_relevant_files_filters_nonexistent(
        self, mock_config, mock_logger, sample_issue, tmp_path
    ):
        engine = self._make_engine(mock_config, mock_logger)
        engine.provider.complete.return_value = json.dumps({
            "relevant_files": ["real_file.py", "ghost_file.py"],
            "reasoning": "...",
        })

        # Only create real_file.py
        (tmp_path / "real_file.py").write_text("x = 1")

        result = engine.get_relevant_files(sample_issue, str(tmp_path))
        assert "real_file.py" in result
        assert "ghost_file.py" not in result

    def test_list_python_files_excludes_venv(self, mock_config, mock_logger, tmp_path):
        engine = self._make_engine(mock_config, mock_logger)
        (tmp_path / "main.py").write_text("x = 1")
        venv = tmp_path / "venv"
        venv.mkdir()
        (venv / "lib.py").write_text("y = 2")

        files = engine._list_python_files(str(tmp_path))
        assert "main.py" in files
        assert not any("venv" in f for f in files)

    def test_format_file_contents_truncates(self, mock_config, mock_logger):
        engine = self._make_engine(mock_config, mock_logger)
        content = "x" * 20000
        formatted = engine._format_file_contents({"bigfile.py": content})
        assert "truncated" in formatted
        assert len(formatted) < 15000

    def test_confidence_score_below_threshold_detected(self, mock_config, mock_logger, sample_issue):
        engine = self._make_engine(mock_config, mock_logger)
        low_conf = dict(MOCK_ANALYSIS_JSON, confidence_score=0.3)
        engine.provider.complete.return_value = json.dumps(low_conf)

        result = engine.analyze_issue(sample_issue, {"f.py": "code"})
        assert result.confidence_score == 0.3
        # Caller is responsible for checking the threshold
