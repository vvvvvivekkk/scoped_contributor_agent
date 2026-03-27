"""
tests/test_validator.py
------------------------
Unit tests for Validator.
Mocks subprocess calls — no real pytest/flake8 invocations.
"""

import pytest
from unittest.mock import MagicMock, patch, mock_open
from pathlib import Path

from bot.core.validator import Validator, ValidationResult
from bot.core.ai_engine import AnalysisResult, Patch
from bot.utils.config_loader import ConfigLoader
from bot.utils.logger import BotLogger


@pytest.fixture
def mock_config():
    return MagicMock(spec=ConfigLoader)


@pytest.fixture
def mock_logger():
    return MagicMock(spec=BotLogger)


@pytest.fixture
def mock_ai_engine():
    engine = MagicMock()
    engine.generate_basic_test.return_value = (
        "import pytest\n\ndef test_placeholder():\n    assert True\n"
    )
    return engine


@pytest.fixture
def validator(mock_config, mock_logger, mock_ai_engine):
    return Validator(mock_config, mock_logger, ai_engine=mock_ai_engine)


@pytest.fixture
def sample_analysis():
    return AnalysisResult(
        fix_description="Add zero division guard",
        patches=[Patch(file="calc.py", original="a/b", replacement="a/b if b != 0 else 0")],
        confidence_score=0.9,
    )


class TestValidator:

    @patch("bot.core.validator.subprocess.run")
    def test_flake8_pass(self, mock_run, validator):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        passed, output = validator.run_flake8("/fake/repo", ["calc.py"])
        assert passed is True

    @patch("bot.core.validator.subprocess.run")
    def test_flake8_fail(self, mock_run, validator):
        mock_run.return_value = MagicMock(
            returncode=1, stdout="calc.py:5:1: E302 expected 2 blank lines", stderr=""
        )
        passed, output = validator.run_flake8("/fake/repo", ["calc.py"])
        assert passed is False
        assert "E302" in output

    @patch("bot.core.validator.subprocess.run")
    def test_pytest_pass(self, mock_run, validator):
        mock_run.return_value = MagicMock(returncode=0, stdout="1 passed", stderr="")
        passed, output = validator.run_pytest("/fake/repo")
        assert passed is True
        assert "passed" in output

    @patch("bot.core.validator.subprocess.run")
    def test_pytest_fail(self, mock_run, validator):
        mock_run.return_value = MagicMock(returncode=1, stdout="1 failed", stderr="")
        passed, output = validator.run_pytest("/fake/repo")
        assert passed is False

    @patch("bot.core.validator.subprocess.run")
    def test_pytest_no_tests_collected_treated_as_pass(self, mock_run, validator):
        # Exit code 5 = no tests collected
        mock_run.return_value = MagicMock(returncode=5, stdout="no tests ran", stderr="")
        passed, output = validator.run_pytest("/fake/repo")
        assert passed is True

    def test_tests_exist_returns_true(self, validator, tmp_path):
        (tmp_path / "test_something.py").write_text("def test_foo(): pass")
        assert validator._tests_exist(str(tmp_path)) is True

    def test_tests_exist_returns_false(self, validator, tmp_path):
        (tmp_path / "main.py").write_text("x = 1")
        assert validator._tests_exist(str(tmp_path)) is False

    @patch("bot.core.validator.subprocess.run")
    def test_validate_passes_all(self, mock_run, validator, sample_analysis, tmp_path):
        # Mock both flake8 and pytest to pass
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        # Create fake test file so tests_existed = True
        (tmp_path / "test_x.py").write_text("def test_pass(): assert True")

        result = validator.validate(str(tmp_path), sample_analysis, ["calc.py"])

        assert result.passed is True
        assert result.flake8_passed is True
        assert result.pytest_passed is True

    @patch("bot.core.validator.subprocess.run")
    def test_validate_fails_on_flake8(self, mock_run, validator, sample_analysis, tmp_path):
        mock_run.return_value = MagicMock(
            returncode=1, stdout="E302", stderr=""
        )
        result = validator.validate(str(tmp_path), sample_analysis, ["calc.py"])
        assert result.passed is False
        assert result.flake8_passed is False

    @patch("bot.core.validator.subprocess.run")
    def test_generates_test_when_none_exist(self, mock_run, validator, sample_analysis, tmp_path):
        # flake8 passes, pytest passes
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        (tmp_path / "calc.py").write_text("def divide(a, b): return a / b")

        result = validator.validate(str(tmp_path), sample_analysis, ["calc.py"])

        # Validator should have called generate_basic_test
        validator.ai_engine.generate_basic_test.assert_called_once()
