"""
bot/core/repo_manager.py
------------------------
Manages all Git and GitHub repository operations:
  - Forking repos to the bot's account
  - Cloning, branching, committing, and pushing via subprocess
  - Environment setup (virtualenv + requirements.txt)
  - Cleanup of temporary directories
"""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

import requests
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from bot.core.issue_fetcher import Issue
from bot.utils.config_loader import ConfigLoader
from bot.utils.logger import BotLogger


class RepoManagerError(Exception):
    """Raised when a repository operation fails."""
    pass


class RepoManager:
    """
    Handles all Git repository operations for the contributor bot.

    Workflow:
      1. fork_repo()       → fork to bot's GH account
      2. clone_repo()      → clone fork locally
      3. create_branch()   → create auto-fix/<issue_id> branch
      4. setup_env()       → install requirements.txt if present
      ... (apply patches externally) ...
      5. commit_changes()  → git add + commit
      6. push_branch()     → push to origin
      7. cleanup()         → remove temp directory
    """

    def __init__(self, config: ConfigLoader, logger: BotLogger):
        self.config = config
        self.logger = logger
        self.token = config.require("github.token")
        self.username = config.require("github.username")
        self.api_base = config.get("github.api_base", "https://api.github.com")
        self.headers = {
            "Authorization": f"token {self.token}",
            "Accept": "application/vnd.github.v3+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        self._active_clones: dict[str, str] = {}  # issue_id → tmp_dir

    # ------------------------------------------------------------------ #
    # Fork Management                                                      #
    # ------------------------------------------------------------------ #

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=4, max=30),
        retry=retry_if_exception_type(requests.RequestException),
    )
    def fork_repo(self, repo_full_name: str) -> str:
        """
        Fork the given repository to the bot's GitHub account.

        Returns:
            The full name of the fork (e.g., "bot_user/repo")
        """
        owner, repo = repo_full_name.split("/", 1)
        fork_full_name = f"{self.username}/{repo}"

        # Check if fork already exists
        check_url = f"{self.api_base}/repos/{fork_full_name}"
        resp = requests.get(check_url, headers=self.headers, timeout=30)
        if resp.status_code == 200:
            self.logger.info("fork_already_exists", fork=fork_full_name)
            return fork_full_name

        # Create the fork
        fork_url = f"{self.api_base}/repos/{repo_full_name}/forks"
        resp = requests.post(fork_url, headers=self.headers, timeout=30)
        if resp.status_code not in (200, 202):
            raise RepoManagerError(
                f"Failed to fork {repo_full_name}: {resp.status_code} {resp.text[:200]}"
            )

        self.logger.info("fork_created", source=repo_full_name, fork=fork_full_name)
        return fork_full_name

    # ------------------------------------------------------------------ #
    # Clone & Branch                                                       #
    # ------------------------------------------------------------------ #

    def clone_repo(self, fork_full_name: str, issue_id: int) -> str:
        """
        Clone the forked repository into a temporary directory.

        Args:
            fork_full_name: "username/repo"
            issue_id: Used to create a unique temp directory

        Returns:
            Absolute path to the cloned repo directory
        """
        tmp_base = self.config.get("bot.tmp_dir", "/tmp/contributor_bot")
        tmp_dir = os.path.join(tmp_base, f"issue_{issue_id}")

        # Clean up existing clone if present
        if os.path.exists(tmp_dir):
            shutil.rmtree(tmp_dir)
        os.makedirs(tmp_dir, exist_ok=True)

        # Embed token in URL for auth
        clone_url = f"https://{self.username}:{self.token}@github.com/{fork_full_name}.git"
        self._run_git(["git", "clone", clone_url, tmp_dir], cwd="/tmp", masked_url=True)
        self._active_clones[str(issue_id)] = tmp_dir

        self.logger.info("repo_cloned", fork=fork_full_name, path=tmp_dir)
        return tmp_dir

    def create_branch(self, repo_path: str, issue_id: int) -> str:
        """
        Create and checkout a new branch for the fix.

        Branch name format: auto-fix/<issue_id>

        Returns:
            The branch name created
        """
        branch_name = f"auto-fix/{issue_id}"

        # Ensure we're on the default branch first
        self._run_git(["git", "fetch", "origin"], cwd=repo_path)
        result = self._run_git(
            ["git", "symbolic-ref", "refs/remotes/origin/HEAD"], cwd=repo_path, check=False
        )
        default_branch = "main"
        if result.returncode == 0 and result.stdout.strip():
            # Extract branch name from e.g. "refs/remotes/origin/main"
            default_branch = result.stdout.strip().split("/")[-1]

        self._run_git(["git", "checkout", default_branch], cwd=repo_path)
        self._run_git(["git", "pull", "origin", default_branch], cwd=repo_path)
        self._run_git(["git", "checkout", "-b", branch_name], cwd=repo_path)

        self.logger.info("branch_created", branch=branch_name, path=repo_path)
        return branch_name

    # ------------------------------------------------------------------ #
    # Environment Setup                                                    #
    # ------------------------------------------------------------------ #

    def setup_env(self, repo_path: str) -> bool:
        """
        Install Python dependencies if requirements.txt exists.

        Uses a subprocess pip install; skips gracefully if file is missing.

        Returns:
            True if setup was performed, False if skipped
        """
        req_file = Path(repo_path) / "requirements.txt"
        if not req_file.exists():
            self.logger.debug("no_requirements_file", path=repo_path)
            return False

        self.logger.info("installing_requirements", path=repo_path)
        result = subprocess.run(
            ["pip", "install", "-r", "requirements.txt", "--quiet"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            self.logger.warning(
                "requirements_install_failed",
                stderr=result.stderr[:500],
            )
            return False
        return True

    # ------------------------------------------------------------------ #
    # Commit & Push                                                        #
    # ------------------------------------------------------------------ #

    def commit_changes(self, repo_path: str, issue: Issue, message: Optional[str] = None) -> bool:
        """
        Stage all changes and commit them with a standardized message.

        Commit format: "fix: resolve issue #<id> - <short_title>"

        Returns:
            True if commit was made, False if nothing to commit
        """
        # Check if there are any changes to commit
        status = self._run_git(["git", "status", "--porcelain"], cwd=repo_path)
        if not status.stdout.strip():
            self.logger.warning("no_changes_to_commit", path=repo_path)
            return False

        # Configure git identity for the commit
        self._run_git(
            ["git", "config", "user.email", f"{self.username}@users.noreply.github.com"],
            cwd=repo_path,
        )
        self._run_git(
            ["git", "config", "user.name", self.username],
            cwd=repo_path,
        )

        # Stage all modified files
        self._run_git(["git", "add", "-A"], cwd=repo_path)

        # Build commit message
        short_title = issue.title[:60].rstrip()
        commit_msg = message or f"fix: resolve issue #{issue.number} - {short_title}"
        self._run_git(["git", "commit", "-m", commit_msg], cwd=repo_path)

        self.logger.info("changes_committed", message=commit_msg)
        return True

    def push_branch(self, repo_path: str, branch_name: str) -> None:
        """Push the fix branch to the origin (fork) remote."""
        self._run_git(
            ["git", "push", "--set-upstream", "origin", branch_name],
            cwd=repo_path,
        )
        self.logger.info("branch_pushed", branch=branch_name)

    # ------------------------------------------------------------------ #
    # Diff Inspection                                                      #
    # ------------------------------------------------------------------ #

    def count_diff_lines(self, repo_path: str) -> int:
        """
        Return the total number of changed lines in the current diff.

        Used to enforce the max_diff_lines safety limit.
        """
        result = self._run_git(["git", "diff", "--stat", "HEAD"], cwd=repo_path, check=False)
        if not result.stdout:
            # No committed changes yet — check unstaged diff
            result = self._run_git(["git", "diff", "--stat"], cwd=repo_path, check=False)

        total_changes = 0
        for line in result.stdout.splitlines():
            # Lines like: " 3 files changed, 25 insertions(+), 5 deletions(-)"
            if "changed" in line:
                import re
                nums = re.findall(r"(\d+) insertion|(\d+) deletion", line)
                for ins, dels in nums:
                    total_changes += int(ins or 0) + int(dels or 0)

        return total_changes

    def discard_changes(self, repo_path: str) -> None:
        """Roll back all uncommitted changes in the repo."""
        self._run_git(["git", "checkout", "--", "."], cwd=repo_path, check=False)
        self._run_git(["git", "clean", "-fd"], cwd=repo_path, check=False)
        self.logger.info("changes_discarded", path=repo_path)

    # ------------------------------------------------------------------ #
    # Cleanup                                                              #
    # ------------------------------------------------------------------ #

    def cleanup(self, issue_id: int) -> None:
        """Remove the temporary clone directory for the given issue."""
        tmp_dir = self._active_clones.pop(str(issue_id), None)
        if tmp_dir and os.path.exists(tmp_dir):
            shutil.rmtree(tmp_dir, ignore_errors=True)
            self.logger.info("cleanup_done", path=tmp_dir)

    def cleanup_all(self) -> None:
        """Remove all active temporary clones."""
        for issue_id in list(self._active_clones.keys()):
            self.cleanup(int(issue_id))

    # ------------------------------------------------------------------ #
    # Private Helpers                                                      #
    # ------------------------------------------------------------------ #

    def _run_git(
        self,
        cmd: list[str],
        cwd: str,
        check: bool = True,
        masked_url: bool = False,
    ) -> subprocess.CompletedProcess:
        """
        Run a git command via subprocess.

        Args:
            cmd: Command and arguments list
            cwd: Working directory
            check: Raise RepoManagerError on non-zero exit code
            masked_url: If True, mask credentials in log output

        Returns:
            CompletedProcess result
        """
        log_cmd = ["git", "***masked***"] if masked_url else cmd
        self.logger.debug("git_command", cmd=" ".join(log_cmd), cwd=cwd)

        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=180,
        )

        if check and result.returncode != 0:
            stderr = result.stderr[:500] if not masked_url else "***masked***"
            raise RepoManagerError(
                f"Git command failed (exit {result.returncode}): {' '.join(log_cmd)}\n"
                f"stderr: {stderr}"
            )

        return result
