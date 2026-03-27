"""
bot/core/issue_fetcher.py
-------------------------
Discovers beginner-friendly GitHub issues using the GitHub Search API.
Applies multiple filters to ensure we only target actionable, appropriate issues.
"""

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import requests
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from bot.utils.config_loader import ConfigLoader
from bot.utils.logger import BotLogger


@dataclass
class Issue:
    """Represents a GitHub issue to be processed."""
    id: int
    number: int
    title: str
    body: str
    repo_full_name: str        # e.g., "owner/repo"
    html_url: str
    labels: list = field(default_factory=list)
    repo_stars: int = 0
    repo_updated_at: Optional[str] = None
    repo_language: Optional[str] = None


class IssueFetcher:
    """
    Fetches and filters GitHub issues suitable for automated fixing.

    Uses GitHub's /search/issues endpoint with label + language filters,
    then applies a secondary filter pass locally to avoid:
      - Too-large repos (too complex)
      - Inactive repos (no recent commits)
      - Issues with skip labels (discussion, question, etc.)
      - Repos not on the allowlist (if configured)
    """

    SEARCH_URL = "https://api.github.com/search/issues"
    REPOS_URL = "https://api.github.com/repos/{repo}"

    def __init__(self, config: ConfigLoader, logger: BotLogger):
        self.config = config
        self.logger = logger
        self.token = config.require("github.token")
        self.headers = {
            "Authorization": f"token {self.token}",
            "Accept": "application/vnd.github.v3+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        self._repo_cache: dict = {}  # Cache repo metadata to avoid repeated API calls

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def fetch(self, limit: Optional[int] = None) -> list[Issue]:
        """
        Fetch and return a list of filtered issues ready for processing.

        Args:
            limit: Maximum number of issues to return (uses config default if None)
        Returns:
            List of Issue dataclass objects
        """
        max_issues = limit or self.config.get("bot.max_issues_per_run", 10)
        query = self._build_search_query()

        self.logger.info("fetching_issues", query=query, limit=max_issues)

        raw_issues = self._search_issues(query, per_page=30)
        self.logger.info("raw_issues_found", count=len(raw_issues))

        filtered: list[Issue] = []
        for raw in raw_issues:
            if len(filtered) >= max_issues:
                break
            issue = self._parse_issue(raw)
            if issue and self._passes_filters(issue, raw):
                filtered.append(issue)
                self.logger.debug(
                    "issue_accepted",
                    issue_url=issue.html_url,
                    title=issue.title[:80],
                )

        self.logger.info("issues_after_filtering", count=len(filtered))
        return filtered

    # ------------------------------------------------------------------ #
    # Search & Parsing                                                     #
    # ------------------------------------------------------------------ #

    def _build_search_query(self) -> str:
        """
        Construct the GitHub issue search query string.

        Combines configured labels, language filter, and state=open.
        """
        labels = self.config.get("bot.search_labels", ["good first issue"])
        language = self.config.get("bot.search_language", "python")

        parts = [f'language:{language}', "state:open", "is:issue", "no:assignee"]
        for label in labels:
            parts.append(f'label:"{label}"')

        return " ".join(parts)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        retry=retry_if_exception_type(requests.RequestException),
        reraise=True,
    )
    def _search_issues(self, query: str, per_page: int = 30) -> list[dict]:
        """Call GitHub Search API and return raw issue dicts."""
        params = {
            "q": query,
            "sort": "updated",
            "order": "desc",
            "per_page": per_page,
        }
        response = requests.get(self.SEARCH_URL, headers=self.headers, params=params, timeout=30)
        response.raise_for_status()

        data = response.json()
        # GitHub Search API rate limit: 30 req/min for authenticated users
        remaining = int(response.headers.get("X-RateLimit-Remaining", 1))
        if remaining < 5:
            reset_ts = int(response.headers.get("X-RateLimit-Reset", time.time() + 60))
            sleep_secs = max(0, reset_ts - int(time.time())) + 2
            self.logger.warning("rate_limit_low", remaining=remaining, sleeping_secs=sleep_secs)
            time.sleep(sleep_secs)

        return data.get("items", [])

    def _parse_issue(self, raw: dict) -> Optional[Issue]:
        """Convert a raw GitHub API issue dict to an Issue dataclass."""
        try:
            repo_full_name = raw["repository_url"].split("repos/")[-1]
            labels = [lbl["name"] for lbl in raw.get("labels", [])]
            repo_meta = self._get_repo_metadata(repo_full_name)

            return Issue(
                id=raw["id"],
                number=raw["number"],
                title=raw.get("title", ""),
                body=raw.get("body", "") or "",
                repo_full_name=repo_full_name,
                html_url=raw.get("html_url", ""),
                labels=labels,
                repo_stars=repo_meta.get("stargazers_count", 0),
                repo_updated_at=repo_meta.get("pushed_at"),
                repo_language=repo_meta.get("language"),
            )
        except (KeyError, IndexError) as e:
            self.logger.warning("issue_parse_error", error=str(e))
            return None

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=15),
        retry=retry_if_exception_type(requests.RequestException),
        reraise=True,
    )
    def _get_repo_metadata(self, repo_full_name: str) -> dict:
        """Fetch repo metadata (stars, last push, language) with local caching."""
        if repo_full_name in self._repo_cache:
            return self._repo_cache[repo_full_name]

        url = self.REPOS_URL.format(repo=repo_full_name)
        response = requests.get(url, headers=self.headers, timeout=30)
        if response.status_code == 404:
            return {}
        response.raise_for_status()
        meta = response.json()
        self._repo_cache[repo_full_name] = meta
        return meta

    # ------------------------------------------------------------------ #
    # Filtering Logic                                                      #
    # ------------------------------------------------------------------ #

    def _passes_filters(self, issue: Issue, raw: dict) -> bool:
        """
        Apply all configured filters to determine if an issue should be processed.

        Returns True if the issue passes all checks.
        """
        # 1. Check repo allowlist
        allowlist = self.config.get("bot.repo_allowlist", [])
        if allowlist and issue.repo_full_name not in allowlist:
            self.logger.debug(
                "issue_skipped_allowlist",
                repo=issue.repo_full_name,
            )
            return False

        # 2. Skip issues with forbidden labels
        skip_labels = set(self.config.get("bot.skip_issue_labels", []))
        issue_labels = set(issue.labels)
        if issue_labels & skip_labels:
            self.logger.debug(
                "issue_skipped_labels",
                matched_labels=list(issue_labels & skip_labels),
                issue_url=issue.html_url,
            )
            return False

        # 3. Skip repos with too many stars (too complex)
        max_stars = self.config.get("bot.max_repo_stars", 5000)
        if issue.repo_stars > max_stars:
            self.logger.debug(
                "issue_skipped_too_popular",
                stars=issue.repo_stars,
                repo=issue.repo_full_name,
            )
            return False

        # 4. Skip repos with too few stars (too inactive / unknown)
        min_stars = self.config.get("bot.min_repo_stars", 10)
        if issue.repo_stars < min_stars:
            self.logger.debug(
                "issue_skipped_too_small",
                stars=issue.repo_stars,
                repo=issue.repo_full_name,
            )
            return False

        # 5. Skip repos with no recent commits
        max_inactivity_days = self.config.get("bot.max_repo_inactivity_days", 90)
        if issue.repo_updated_at:
            last_push = datetime.fromisoformat(
                issue.repo_updated_at.replace("Z", "+00:00")
            )
            cutoff = datetime.now(tz=timezone.utc) - timedelta(days=max_inactivity_days)
            if last_push < cutoff:
                self.logger.debug(
                    "issue_skipped_inactive_repo",
                    last_push=issue.repo_updated_at,
                    repo=issue.repo_full_name,
                )
                return False

        # 6. Skip pull requests (GitHub search may return PRs too)
        if raw.get("pull_request"):
            return False

        # 7. Skip issues with no body (can't understand the problem)
        if not issue.body or len(issue.body.strip()) < 20:
            self.logger.debug("issue_skipped_no_body", issue_url=issue.html_url)
            return False

        return True
