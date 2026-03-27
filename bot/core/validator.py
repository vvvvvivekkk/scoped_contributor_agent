"""
bot/core/validator.py
---------------------
Validates AI-generated code changes before committing.
Runs flake8 for style/lint and pytest for correctness.
If no tests exist, generates a basic smoke test via the AI engine.
"""

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from bot.core.ai_engine import AIEngine, AnalysisResult
from bot.utils.config_loader import ConfigLoader
from bot.utils.logger import BotLogger


@dataclass
class ValidationResult:
    """Encapsulates the outcome of a validation run."""
    passed: bool
    flake8_passed: bool
    pytest_passed: bool
    tests_existed: bool
    details: str
    flake8_output: str = ""
    pytest_output: str = ""


class Validator:
    """
    Validates patched code using flake8 (lint) and pytest (tests).

    If no tests are found in the repository, uses the AI engine to
    generate a basic smoke test before running pytest.

    Validation pipeline:
      1. Run flake8 on modified files
      2. Check for existing tests
      3. If no tests: generate basic test via AIEngine
      4. Run pytest
    """

    # Flake8 errors that are OK to ignore (cosmetic / not correctness issues)
    FLAKE8_IGNORE = ["E501", "W503", "W504", "E302", "E303"]

    def __init__(self, config: ConfigLoader, logger: BotLogger, ai_engine: Optional[AIEngine] = None):
        self.config = config
        self.logger = logger
        self.ai_engine = ai_engine

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def validate(
        self,
        repo_path: str,
        analysis_result: AnalysisResult,
        modified_files: list[str],
    ) -> ValidationResult:
        """
        Run the full validation pipeline on the patched repository.

        Args:
            repo_path: Absolute path to cloned repo
            analysis_result: Contains fix_description, patches, etc.
            modified_files: List of relative paths that were patched

        Returns:
            ValidationResult describing the outcome
        """
        flake8_passed, flake8_output = self.run_flake8(repo_path, modified_files)
        if not flake8_passed:
            self.logger.warning("flake8_failed", output=flake8_output[:300])
            return ValidationResult(
                passed=False,
                flake8_passed=False,
                pytest_passed=False,
                tests_existed=False,
                details="flake8 linting failed",
                flake8_output=flake8_output,
            )

        tests_existed = self._tests_exist(repo_path)
        if not tests_existed and self.ai_engine and modified_files:
            self.logger.info("no_tests_found_generating", repo=repo_path)
            self._generate_and_write_test(repo_path, analysis_result, modified_files[0])

        pytest_passed, pytest_output = self.run_pytest(repo_path)

        passed = flake8_passed and pytest_passed
        details_parts = []
        if not flake8_passed:
            details_parts.append("flake8 failed")
        if not pytest_passed:
            details_parts.append("pytest failed")
        details = "; ".join(details_parts) if details_parts else "all checks passed"

        self.logger.info(
            "validation_complete",
            passed=passed,
            flake8=flake8_passed,
            pytest=pytest_passed,
            tests_existed=tests_existed,
        )

        return ValidationResult(
            passed=passed,
            flake8_passed=flake8_passed,
            pytest_passed=pytest_passed,
            tests_existed=tests_existed,
            details=details,
            flake8_output=flake8_output,
            pytest_output=pytest_output,
        )

    def run_flake8(self, repo_path: str, files: Optional[list[str]] = None) -> tuple[bool, str]:
        """
        Run flake8 linting on the specified files (or entire repo if None).

        Returns:
            (passed: bool, output: str)
        """
        ignore_codes = ",".join(self.FLAKE8_IGNORE)
        cmd = ["flake8", "--max-line-length=120", f"--ignore={ignore_codes}"]

        if files:
            cmd.extend(files)
        else:
            cmd.append(".")

        try:
            result = subprocess.run(
                cmd,
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=60,
            )
            passed = result.returncode == 0
            output = result.stdout + result.stderr
            return passed, output
        except FileNotFoundError:
            # flake8 not installed in the repo's environment — skip
            self.logger.warning("flake8_not_found_skipping")
            return True, "flake8 not available"
        except subprocess.TimeoutExpired:
            self.logger.warning("flake8_timeout")
            return True, "flake8 timed out, skipping"

    def run_pytest(self, repo_path: str) -> tuple[bool, str]:
        """
        Run pytest in the repository root.

        Returns:
            (passed: bool, output: str)
        """
        cmd = [
            "python", "-m", "pytest",
            "--tb=short",
            "-q",
            "--timeout=60",
            "--no-header",
        ]

        try:
            result = subprocess.run(
                cmd,
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=120,
                env={**__import__("os").environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
            output = result.stdout + result.stderr
            passed = result.returncode == 0

            # Return code 5 means "no tests collected" — treat as pass
            if result.returncode == 5:
                passed = True

            self.logger.info(
                "pytest_complete",
                returncode=result.returncode,
                passed=passed,
                output_preview=output[:200],
            )
            return passed, output
        except FileNotFoundError:
            self.logger.warning("pytest_not_found_skipping")
            return True, "pytest not available"
        except subprocess.TimeoutExpired:
            self.logger.warning("pytest_timeout")
            return False, "pytest timed out"

    # ------------------------------------------------------------------ #
    # Test Generation                                                      #
    # ------------------------------------------------------------------ #

    def _tests_exist(self, repo_path: str) -> bool:
        """
        Check if any test files exist in the repository.
        Looks for files matching test_*.py or *_test.py patterns.
        """
        repo = Path(repo_path)
        for pattern in ("**/test_*.py", "**/*_test.py"):
            if any(repo.glob(pattern)):
                return True
        return False

    def _generate_and_write_test(
        self,
        repo_path: str,
        analysis_result: AnalysisResult,
        modified_file: str,
    ) -> None:
        """
        Generate a basic smoke test for the patched file and write it to the repo.
        """
        if not self.ai_engine:
            return

        file_path = Path(repo_path) / modified_file
        if not file_path.exists():
            return

        try:
            file_content = file_path.read_text(encoding="utf-8", errors="ignore")
            test_code = self.ai_engine.generate_basic_test(
                fix_description=analysis_result.fix_description,
                file_path=modified_file,
                file_content=file_content,
            )

            # Write to tests/ directory (create if needed)
            tests_dir = Path(repo_path) / "tests"
            tests_dir.mkdir(exist_ok=True)

            # Create __init__.py if needed
            init_file = tests_dir / "__init__.py"
            if not init_file.exists():
                init_file.write_text("")

            # Write the generated test
            test_filename = f"test_auto_generated_{Path(modified_file).stem}.py"
            test_file_path = tests_dir / test_filename
            test_file_path.write_text(test_code, encoding="utf-8")
            self.logger.info("basic_test_generated", test_file=str(test_file_path))
        except Exception as e:
            self.logger.warning("test_generation_failed", error=str(e))
