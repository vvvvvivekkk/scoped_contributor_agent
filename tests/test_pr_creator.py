"""
tests/test_pr_creator.py
-------------------------
Unit tests for PRCreator.
Mocks GitHub API calls — no real network requests made.
"""

import pytest
from unittest.mock import MagicMock, patch
import json

from bot.core.pr_creator import PRCreator, PRCreatorError
from bot.core.issue_fetcher import Issue
from bot.core.ai_engine import AnalysisResult
from bot.utils.config_loader import ConfigLoader
from bot.utils.logger import BotLogger


# ── Fixtures ──────────────────────────────────────────────────────── #

@pytest.fixture
def mock_config():
    config = MagicMock(spec=ConfigLoader)
    config.require.side_effect = lambda key: {
        "github.token": "fake_token",
        "github.username": "bot_user",
    }[key]
    config.get.side_effect = lambda key, default=None: {
        "github.api_base": "https://api.github.com",
        "bot.max_prs_per_day": 3,
        "bot.comment_before_pr": True,
    }.get(key, default)
    return config


@pytest.fixture
def mock_logger():
    logger = MagicMock(spec=BotLogger)
    return logger


@pytest.fixture
def pr_creator(mock_config, mock_logger):
    return PRCreator(mock_config, mock_logger)


@pytest.fixture
def sample_issue():
    return Issue(
        id=99,
        number=42,
        title="Fix null reference in parser",
        body="Crashes when input is None.",
        repo_full_name="upstream_owner/my-repo",
        html_url="https://github.com/upstream_owner/my-repo/issues/42",
    )


@pytest.fixture
def sample_analysis():
    return AnalysisResult(
        fix_description="Add null check in parse()",
        affected_files=["parser.py"],
        confidence_score=0.88,
        pr_title="fix: resolve issue #42 - Fix null reference in parser",
        pr_description="This PR fixes #42 by adding a null check.",
    )


# ── Tests ──────────────────────────────────────────────────────────── #

class TestPRCreator:

    def test_check_daily_limit_under_limit(self, pr_creator):
        assert pr_creator.check_daily_limit(prs_today=1) is True

    def test_check_daily_limit_at_limit(self, pr_creator):
        assert pr_creator.check_daily_limit(prs_today=3) is False

    def test_check_daily_limit_over_limit(self, pr_creator):
        assert pr_creator.check_daily_limit(prs_today=5) is False

    def test_comment_on_issue_dry_run(self, pr_creator, sample_issue):
        result = pr_creator.comment_on_issue(sample_issue, dry_run=True)
        assert result is None  # No comment posted in dry run

    @patch("bot.core.pr_creator.requests.post")
    def test_comment_on_issue_success(self, mock_post, pr_creator, sample_issue):
        mock_post.return_value = MagicMock(
            status_code=201,
            json=lambda: {"html_url": "https://github.com/comment/1"},
        )
        mock_post.return_value.raise_for_status = MagicMock()
        result = pr_creator.comment_on_issue(sample_issue, dry_run=False)
        assert result == "https://github.com/comment/1"
        mock_post.assert_called_once()

    def test_create_pr_dry_run(self, pr_creator, sample_issue, sample_analysis):
        result = pr_creator.create_pull_request(
            issue=sample_issue,
            branch_name="auto-fix/42",
            analysis_result=sample_analysis,
            modified_files=["parser.py"],
            dry_run=True,
        )
        assert result is not None
        assert "DRY RUN" in result

    @patch("bot.core.pr_creator.requests.get")
    @patch("bot.core.pr_creator.requests.post")
    def test_create_pr_success(self, mock_post, mock_get, pr_creator, sample_issue, sample_analysis):
        # Mock GET request for default branch fetch
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {"default_branch": "main"},
        )
        mock_get.return_value.raise_for_status = MagicMock()

        # Mock POST for PR creation
        mock_post.return_value = MagicMock(
            status_code=201,
            json=lambda: {"html_url": "https://github.com/upstream_owner/my-repo/pull/7"},
        )
        mock_post.return_value.raise_for_status = MagicMock()

        result = pr_creator.create_pull_request(
            issue=sample_issue,
            branch_name="auto-fix/42",
            analysis_result=sample_analysis,
            modified_files=["parser.py"],
            dry_run=False,
        )

        assert result == "https://github.com/upstream_owner/my-repo/pull/7"

    @patch("bot.core.pr_creator.requests.get")
    @patch("bot.core.pr_creator.requests.post")
    def test_create_pr_raises_on_422(self, mock_post, mock_get, pr_creator, sample_issue, sample_analysis):
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {"default_branch": "main"},
        )
        mock_get.return_value.raise_for_status = MagicMock()

        mock_post.return_value = MagicMock(
            status_code=422,
            json=lambda: {"message": "A pull request already exists for this branch."},
            text="A pull request already exists",
        )
        mock_post.return_value.raise_for_status = MagicMock(
            side_effect=Exception("422 Unprocessable Entity")
        )

        with pytest.raises((PRCreatorError, Exception)):
            pr_creator.create_pull_request(
                issue=sample_issue,
                branch_name="auto-fix/42",
                analysis_result=sample_analysis,
                modified_files=["parser.py"],
                dry_run=False,
            )

    def test_pr_body_contains_issue_reference(self, pr_creator, sample_issue, sample_analysis):
        """Verify the generated PR body references the issue number."""
        from bot.core.pr_creator import PR_BODY_TEMPLATE
        body = PR_BODY_TEMPLATE.format(
            pr_description=sample_analysis.pr_description,
            fix_description=sample_analysis.fix_description,
            modified_files_list="- `parser.py`",
            clone_url="https://github.com/upstream_owner/my-repo",
            repo_name="my-repo",
            branch_name="auto-fix/42",
            confidence_score=sample_analysis.confidence_score,
            issue_number=sample_issue.number,
        )
        assert "Closes #42" in body
        assert "parser.py" in body
        assert "88%" in body
