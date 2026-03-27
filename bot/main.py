"""
bot/main.py
-----------
Main orchestrator for the autonomous GitHub contributor bot.
Ties together all core modules and runs on a 6-hour schedule via APScheduler.

Usage:
    python -m bot.main                    # Start scheduled mode (every 6h)
    python -m bot.main --run-once         # Run once immediately
    python -m bot.main --run-once --dry-run  # Dry run (no real API calls)
"""

import signal
import sys
import time
from typing import Optional

import click
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from bot.core.ai_engine import AIEngine
from bot.core.fixer import Fixer, FixerError
from bot.core.issue_fetcher import IssueFetcher, Issue
from bot.core.pr_creator import PRCreator, PRCreatorError
from bot.core.repo_manager import RepoManager, RepoManagerError
from bot.core.validator import Validator
from bot.utils.config_loader import ConfigLoader, ConfigError
from bot.utils.logger import BotLogger


class ContributorBot:
    """
    The main orchestrator that runs the full contribution pipeline.

    Pipeline for each issue:
      1. Check daily PR limit
      2. Fork + Clone repository
      3. Create feature branch
      4. AI: identify relevant files
      5. AI: analyze issue + generate fix
      6. Check confidence score
      7. Apply patches + validate diff size
      8. Run flake8 + pytest
      9. Commit + Push
     10. Comment on issue + Create PR
     11. Log results + Cleanup
    """

    def __init__(self, config: ConfigLoader, logger: BotLogger, dry_run: bool = False):
        self.config = config
        self.logger = logger
        self.dry_run = dry_run

        # Initialize all core components
        self.issue_fetcher = IssueFetcher(config, logger)
        self.repo_manager = RepoManager(config, logger)
        self.ai_engine = AIEngine(config, logger)
        self.fixer = Fixer(config, logger)
        self.validator = Validator(config, logger, ai_engine=self.ai_engine)
        self.pr_creator = PRCreator(config, logger)

        self.min_confidence = float(config.get("bot.min_confidence_score", 0.70))

    # ------------------------------------------------------------------ #
    # Main Run Loop                                                        #
    # ------------------------------------------------------------------ #

    def run(self) -> dict:
        """
        Execute one full cycle of the contribution pipeline.

        Returns:
            Summary dict with counts of attempted/succeeded/skipped/failed
        """
        summary = {"attempted": 0, "succeeded": 0, "skipped": 0, "failed": 0}

        self.logger.info(
            "bot_run_started",
            dry_run=self.dry_run,
            max_prs=self.config.get("bot.max_prs_per_day"),
        )

        # Fetch candidate issues
        try:
            issues = self.issue_fetcher.fetch()
        except Exception as e:
            self.logger.error("issue_fetch_failed", error=str(e))
            return summary

        if not issues:
            self.logger.info("no_issues_found")
            return summary

        self.logger.info("processing_issues", count=len(issues))

        for issue in issues:
            # Check daily PR limit before each attempt
            prs_today = self.logger.get_prs_today()
            if not self.pr_creator.check_daily_limit(prs_today):
                self.logger.info("daily_limit_reached_stopping")
                break

            summary["attempted"] += 1
            self.logger.record_attempt(issue.html_url)

            result = self._process_issue(issue)

            if result == "success":
                summary["succeeded"] += 1
            elif result == "skipped":
                summary["skipped"] += 1
            else:
                summary["failed"] += 1

        self.logger.info("bot_run_complete", **summary)
        return summary

    # ------------------------------------------------------------------ #
    # Per-Issue Pipeline                                                   #
    # ------------------------------------------------------------------ #

    def _process_issue(self, issue: Issue) -> str:
        """
        Run the full contribution pipeline for a single issue.

        Returns:
            "success", "skipped", or "failed"
        """
        self.logger.info(
            "processing_issue",
            issue_url=issue.html_url,
            title=issue.title[:80],
            repo=issue.repo_full_name,
        )

        repo_path: Optional[str] = None
        branch_name: Optional[str] = None

        try:
            # ── Step 1: Fork & Clone ──────────────────────────────────── #
            fork_full_name = self.repo_manager.fork_repo(issue.repo_full_name)

            # Brief pause to allow GitHub to finish processing the fork
            time.sleep(3)

            repo_path = self.repo_manager.clone_repo(fork_full_name, issue.id)
            branch_name = self.repo_manager.create_branch(repo_path, issue.number)
            self.repo_manager.setup_env(repo_path)

            # ── Step 2: AI – Identify Relevant Files ──────────────────── #
            relevant_files = self.ai_engine.get_relevant_files(issue, repo_path)
            if not relevant_files:
                self.logger.warning("no_relevant_files_found", issue_url=issue.html_url)
                self.logger.record_pr_skipped("no_relevant_files", issue.html_url)
                return "skipped"

            # ── Step 3: AI – Analyze Issue & Generate Fix ─────────────── #
            file_contents = self.ai_engine.read_file_contents(repo_path, relevant_files)
            analysis = self.ai_engine.analyze_issue(issue, file_contents)

            # ── Step 4: Confidence Gate ───────────────────────────────── #
            if analysis.confidence_score < self.min_confidence:
                self.logger.warning(
                    "low_confidence_skipping",
                    score=analysis.confidence_score,
                    threshold=self.min_confidence,
                    issue_url=issue.html_url,
                )
                self.logger.record_pr_skipped(
                    f"low_confidence({analysis.confidence_score:.2f})", issue.html_url
                )
                return "skipped"

            if not analysis.patches:
                self.logger.warning("no_patches_generated", issue_url=issue.html_url)
                self.logger.record_pr_skipped("no_patches", issue.html_url)
                return "skipped"

            # ── Step 5: Apply Patches ─────────────────────────────────── #
            if not self.dry_run:
                modified_files = self.fixer.apply_patches(analysis, repo_path)

                # ── Step 6: Validate Diff Size ───────────────────────── #
                diff_lines = self.fixer.validate_diff_size(repo_path)
                self.logger.info("diff_size_ok", lines_changed=diff_lines)

                # ── Step 7: Run Validation (flake8 + pytest) ─────────── #
                validation = self.validator.validate(repo_path, analysis, modified_files)
                if not validation.passed:
                    self.logger.warning(
                        "validation_failed_discarding",
                        details=validation.details,
                        issue_url=issue.html_url,
                    )
                    self.repo_manager.discard_changes(repo_path)
                    self.logger.record_failure("validation_failed", issue.html_url)
                    return "failed"

                # ── Step 8: Commit & Push ─────────────────────────────── #
                committed = self.repo_manager.commit_changes(repo_path, issue, analysis.pr_title)
                if not committed:
                    self.logger.warning("nothing_committed", issue_url=issue.html_url)
                    self.logger.record_pr_skipped("nothing_to_commit", issue.html_url)
                    return "skipped"

                self.repo_manager.push_branch(repo_path, branch_name)
            else:
                modified_files = [p.file for p in analysis.patches]
                self.logger.info(
                    "dry_run_patch_summary",
                    **self.fixer.patch_summary(analysis),
                )

            # ── Step 9: Check for Duplicate PR ───────────────────────── #
            existing_pr = self.pr_creator.check_existing_pr(issue, branch_name)
            if existing_pr:
                self.logger.info("pr_already_exists", pr_url=existing_pr)
                self.logger.record_pr_skipped("pr_already_exists", issue.html_url)
                return "skipped"

            # ── Step 10: Comment on Issue & Create PR ─────────────────── #
            self.pr_creator.comment_on_issue(issue, dry_run=self.dry_run)

            pr_url = self.pr_creator.create_pull_request(
                issue=issue,
                branch_name=branch_name,
                analysis_result=analysis,
                modified_files=modified_files,
                dry_run=self.dry_run,
            )

            self.logger.record_pr_created(pr_url or "dry-run", issue.html_url)
            return "success"

        except FixerError as e:
            self.logger.error("fixer_error", error=str(e), issue_url=issue.html_url)
            if repo_path:
                self.repo_manager.discard_changes(repo_path)
            self.logger.record_failure(f"fixer_error: {e}", issue.html_url)
            return "failed"

        except RepoManagerError as e:
            self.logger.error("repo_error", error=str(e), issue_url=issue.html_url)
            self.logger.record_failure(f"repo_error: {e}", issue.html_url)
            return "failed"

        except PRCreatorError as e:
            self.logger.error("pr_error", error=str(e), issue_url=issue.html_url)
            self.logger.record_failure(f"pr_error: {e}", issue.html_url)
            return "failed"

        except Exception as e:
            self.logger.error("unexpected_error", error=str(e), issue_url=issue.html_url)
            self.logger.record_failure(f"unexpected: {e}", issue.html_url)
            return "failed"

        finally:
            # Always clean up the temp clone
            if repo_path:
                self.repo_manager.cleanup(issue.id)


# ------------------------------------------------------------------ #
# CLI Entry Point                                                       #
# ------------------------------------------------------------------ #

@click.command()
@click.option("--run-once", is_flag=True, default=False, help="Run once and exit (no scheduler)")
@click.option("--dry-run", is_flag=True, default=False, help="Log actions without making real API calls")
@click.option("--config", "config_path", default=None, help="Path to config.yaml")
@click.option("--env", "env_path", default=None, help="Path to .env file")
def main(run_once: bool, dry_run: bool, config_path: Optional[str], env_path: Optional[str]):
    """Autonomous GitHub Open Source Contributor Bot."""

    # Load configuration
    try:
        config = ConfigLoader(config_path=config_path, env_file=env_path)
    except ConfigError as e:
        click.echo(f"❌ Configuration error: {e}", err=True)
        sys.exit(1)

    log_dir = config.get("bot.log_dir", "logs")
    logger = BotLogger(log_dir=log_dir)

    if dry_run:
        logger.info("dry_run_mode_enabled")
        click.echo("🧪 DRY RUN MODE — No real API calls will be made.")

    bot = ContributorBot(config=config, logger=logger, dry_run=dry_run)

    # Graceful shutdown handler
    def handle_shutdown(sig, frame):
        logger.info("shutdown_signal_received", signal=sig)
        bot.repo_manager.cleanup_all()
        click.echo("\n👋 Contributor bot stopped gracefully.")
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    if run_once:
        click.echo("🚀 Running contributor bot (one-shot)...")
        summary = bot.run()
        click.echo(
            f"\n✅ Done! Attempted: {summary['attempted']} | "
            f"Succeeded: {summary['succeeded']} | "
            f"Skipped: {summary['skipped']} | "
            f"Failed: {summary['failed']}"
        )
    else:
        # Schedule to run every 6 hours
        scheduler = BlockingScheduler(timezone="UTC")
        trigger = CronTrigger(hour="*/6")

        scheduler.add_job(bot.run, trigger=trigger, id="contributor_bot", replace_existing=True)

        click.echo("⏰ Contributor bot scheduled (every 6 hours). Press Ctrl+C to stop.")
        logger.info("scheduler_started", schedule="every_6_hours")

        # Run immediately on startup, then follow schedule
        click.echo("🚀 Running first cycle now...")
        bot.run()

        try:
            scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            logger.info("scheduler_stopped")
            click.echo("\n👋 Scheduler stopped.")


if __name__ == "__main__":
    main()
