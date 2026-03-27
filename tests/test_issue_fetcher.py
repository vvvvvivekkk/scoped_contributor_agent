"""
tests/test_issue_fetcher.py
----------------------------
Unit tests for IssueFetcher.
Uses mocked HTTP responses — no real GitHub API calls made.
"""

import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone, timedelta

from bot.core.issue_fetcher import IssueFetcher, Issue
from bot.utils.config_loader import ConfigLoader
from bot.utils.logger import BotLogger


# ── Fixtures ──────────────────────────────────────────────────────── #

@pytest.fixture
def mock_config():
    config = MagicMock(spec=ConfigLoader)
    config.require.return_value = "fake_token"
    config.get.side_effect = lambda key, default=None: {
        "bot.max_issues_per_run": 5,
        "bot.repo_allowlist": [],
        "bot.skip_issue_labels": ["discussion", "question", "wontfix", "invalid"],
        "bot.max_repo_stars": 5000,
        "bot.min_repo_stars": 10,
        "bot.max_repo_inactivity_days": 90,
        "bot.search_labels": ["good first issue"],
        "bot.search_language": "python",
        "github.api_base": "https://api.github.com",
    }.get(key, default)
    return config


@pytest.fixture
def mock_logger():
    return MagicMock(spec=BotLogger)


@pytest.fixture
def fetcher(mock_config, mock_logger):
    return IssueFetcher(mock_config, mock_logger)


def _make_raw_issue(
    number=42,
    title="Fix null pointer in utils",
    body="There is a null pointer bug when X is None.",
    repo_name="owner/my-repo",
    labels=None,
    stars=100,
    pushed_days_ago=5,
):
    """Helper: create a fake raw GitHub issue dict."""
    pushed_at = (datetime.now(tz=timezone.utc) - timedelta(days=pushed_days_ago)).isoformat()
    labels = labels or [{"name": "good first issue"}]
    return {
        "id": 1001,
        "number": number,
        "title": title,
        "body": body,
        "html_url": f"https://github.com/{repo_name}/issues/{number}",
        "repository_url": f"https://api.github.com/repos/{repo_name}",
        "labels": labels,
        "pull_request": None,
        "_repo_meta": {
            "stargazers_count": stars,
            "pushed_at": pushed_at,
            "language": "Python",
        },
    }


# ── Tests ──────────────────────────────────────────────────────────── #

class TestIssueFetcher:

    def test_build_search_query(self, fetcher):
        query = fetcher._build_search_query()
        assert "language:python" in query
        assert "state:open" in query
        assert '"good first issue"' in query

    def test_parse_issue_success(self, fetcher):
        raw = _make_raw_issue()
        fetcher._repo_cache["owner/my-repo"] = raw["_repo_meta"]
        issue = fetcher._parse_issue(raw)
        assert issue is not None
        assert issue.number == 42
        assert issue.repo_full_name == "owner/my-repo"
        assert issue.repo_stars == 100

    def test_filter_skips_discussion_label(self, fetcher):
        raw = _make_raw_issue(labels=[{"name": "good first issue"}, {"name": "discussion"}])
        fetcher._repo_cache["owner/my-repo"] = raw["_repo_meta"]
        issue = fetcher._parse_issue(raw)
        assert issue is not None
        assert not fetcher._passes_filters(issue, raw)

    def test_filter_skips_too_many_stars(self, fetcher):
        raw = _make_raw_issue(stars=10000)
        fetcher._repo_cache["owner/my-repo"] = raw["_repo_meta"]
        issue = fetcher._parse_issue(raw)
        assert not fetcher._passes_filters(issue, raw)

    def test_filter_skips_too_few_stars(self, fetcher):
        raw = _make_raw_issue(stars=2)
        fetcher._repo_cache["owner/my-repo"] = raw["_repo_meta"]
        issue = fetcher._parse_issue(raw)
        assert not fetcher._passes_filters(issue, raw)

    def test_filter_skips_inactive_repo(self, fetcher):
        raw = _make_raw_issue(pushed_days_ago=120)
        fetcher._repo_cache["owner/my-repo"] = raw["_repo_meta"]
        issue = fetcher._parse_issue(raw)
        assert not fetcher._passes_filters(issue, raw)

    def test_filter_skips_pull_requests(self, fetcher):
        raw = _make_raw_issue()
        raw["pull_request"] = {"url": "..."}
        fetcher._repo_cache["owner/my-repo"] = raw["_repo_meta"]
        issue = fetcher._parse_issue(raw)
        assert not fetcher._passes_filters(issue, raw)

    def test_filter_skips_empty_body(self, fetcher):
        raw = _make_raw_issue(body="")
        fetcher._repo_cache["owner/my-repo"] = raw["_repo_meta"]
        issue = fetcher._parse_issue(raw)
        assert not fetcher._passes_filters(issue, raw)

    def test_filter_accepts_valid_issue(self, fetcher):
        raw = _make_raw_issue()
        fetcher._repo_cache["owner/my-repo"] = raw["_repo_meta"]
        issue = fetcher._parse_issue(raw)
        assert fetcher._passes_filters(issue, raw)

    def test_allowlist_enforcement(self, fetcher, mock_config):
        mock_config.get.side_effect = lambda key, default=None: {
            "bot.repo_allowlist": ["allowed/repo"],
            "bot.skip_issue_labels": [],
            "bot.max_repo_stars": 5000,
            "bot.min_repo_stars": 10,
            "bot.max_repo_inactivity_days": 90,
        }.get(key, default)
        raw = _make_raw_issue(repo_name="other/repo")
        fetcher._repo_cache["other/repo"] = raw["_repo_meta"]
        issue = fetcher._parse_issue(raw)
        assert not fetcher._passes_filters(issue, raw)

    @patch("bot.core.issue_fetcher.requests.get")
    def test_fetch_returns_filtered_issues(self, mock_get, fetcher):
        raw = _make_raw_issue()
        fetcher._repo_cache["owner/my-repo"] = raw["_repo_meta"]

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"items": [raw]}
        mock_response.headers = {"X-RateLimit-Remaining": "30", "X-RateLimit-Reset": "9999999999"}
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response

        # Mock the _get_repo_metadata to use cache
        issues = fetcher.fetch(limit=5)
        assert len(issues) == 1
        assert issues[0].number == 42
