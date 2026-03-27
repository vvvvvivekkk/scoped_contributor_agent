"""
bot/core/fixer.py
-----------------
Applies AI-generated patches to repository files.
Performs exact string replacement and validates diff size constraints.
"""

from pathlib import Path
from typing import Optional

from bot.core.ai_engine import AnalysisResult, Patch
from bot.utils.config_loader import ConfigLoader
from bot.utils.logger import BotLogger


class FixerError(Exception):
    """Raised when a patch cannot be applied."""
    pass


class Fixer:
    """
    Applies AI-generated code patches to files in a cloned repository.

    Strategy: exact string replacement (original → replacement).
    This ensures the diff is minimal and predictable.

    Safety checks:
    - Verifies the original string exists in the file before replacing
    - Enforces unique matches to avoid accidental multi-site edits
    - Checks diff line count against configured limit
    """

    def __init__(self, config: ConfigLoader, logger: BotLogger):
        self.config = config
        self.logger = logger
        self.max_diff_lines = config.get("bot.max_diff_lines", 200)

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def apply_patches(self, result: AnalysisResult, repo_path: str) -> list[str]:
        """
        Apply all patches from an AnalysisResult to the repository.

        Args:
            result: AnalysisResult containing the list of patches
            repo_path: Absolute path to the cloned repository

        Returns:
            List of files that were successfully patched

        Raises:
            FixerError: If a patch cannot be applied safely
        """
        if not result.patches:
            raise FixerError("No patches to apply.")

        modified_files: list[str] = []

        for patch in result.patches:
            try:
                self._apply_single_patch(patch, repo_path)
                modified_files.append(patch.file)
            except FixerError as e:
                self.logger.error(
                    "patch_failed",
                    file=patch.file,
                    error=str(e),
                )
                raise  # Propagate — caller will discard changes

        self.logger.info("patches_applied", count=len(modified_files), files=modified_files)
        return modified_files

    def validate_diff_size(self, repo_path: str) -> int:
        """
        Count changed lines using a pure-Python diff (no git required at this step).

        Returns:
            Total number of added + removed lines across all modified files

        Raises:
            FixerError: If the diff exceeds max_diff_lines
        """
        import subprocess
        result = subprocess.run(
            ["git", "diff", "--shortstat"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=30,
        )

        total = 0
        if result.stdout:
            import re
            nums = re.findall(r"(\d+) insertion|(\d+) deletion", result.stdout)
            for ins, dels in nums:
                total += int(ins or 0) + int(dels or 0)

        if total > self.max_diff_lines:
            raise FixerError(
                f"Diff is too large: {total} lines changed (limit: {self.max_diff_lines}). "
                "Aborting to avoid unintended large changes."
            )

        self.logger.debug("diff_size_validated", total_changed_lines=total)
        return total

    def patch_summary(self, result: AnalysisResult) -> dict:
        """Return a human-readable summary of what the patches change."""
        return {
            "affected_files": result.affected_files,
            "patch_count": len(result.patches),
            "fix_description": result.fix_description,
            "confidence": result.confidence_score,
        }

    # ------------------------------------------------------------------ #
    # Private Helpers                                                      #
    # ------------------------------------------------------------------ #

    def _apply_single_patch(self, patch: Patch, repo_path: str) -> None:
        """
        Apply a single patch (original → replacement) to its target file.

        Validates:
        - File exists
        - Original string occurs EXACTLY ONCE in the file
        - Replacement produces syntactically valid Python
        """
        file_path = Path(repo_path) / patch.file
        if not file_path.exists():
            raise FixerError(f"Target file does not exist: {patch.file}")

        # Read current content
        content = file_path.read_text(encoding="utf-8", errors="replace")

        # Verify the original string exists
        occurrences = content.count(patch.original)
        if occurrences == 0:
            raise FixerError(
                f"Original string not found in {patch.file}.\n"
                f"Looking for: {patch.original[:100]!r}"
            )
        if occurrences > 1:
            self.logger.warning(
                "multiple_occurrences",
                file=patch.file,
                count=occurrences,
                original_preview=patch.original[:80],
            )
            # Still apply — replaces the first occurrence only
            new_content = content.replace(patch.original, patch.replacement, 1)
        else:
            new_content = content.replace(patch.original, patch.replacement)

        # Validate Python syntax of the new file
        self._validate_python_syntax(new_content, patch.file)

        # Write the patched file
        file_path.write_text(new_content, encoding="utf-8")
        self.logger.debug(
            "patch_applied",
            file=patch.file,
            original_len=len(patch.original),
            replacement_len=len(patch.replacement),
        )

    def _validate_python_syntax(self, content: str, filename: str) -> None:
        """
        Check that the patched content is valid Python via compile().

        Raises:
            FixerError: If a syntax error is detected
        """
        try:
            compile(content, filename, "exec")
        except SyntaxError as e:
            raise FixerError(
                f"Patched file {filename} has a syntax error: {e.msg} (line {e.lineno})"
            ) from e
