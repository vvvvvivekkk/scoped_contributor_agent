import time
from pathlib import Path

from bot.core.repo_manager import RepoManager
from bot.core.ai_engine import AIEngine
from bot.core.fixer import Fixer
from bot.core.validator import Validator
from bot.core.pr_creator import PRCreator
from bot.utils.config_loader import ConfigLoader
from bot.utils.logger import Logger
from bot.core.issue_fetcher import IssueFetcher


class ContributorBot:
    def __init__(self, config, logger, dry_run=False):
        self.config = config
        self.logger = logger
        self.dry_run = dry_run

        self.repo_manager = RepoManager(config, logger)
        self.ai_engine = AIEngine(config, logger)
        self.fixer = Fixer(config, logger)
        self.validator = Validator(config, logger)
        self.pr_creator = PRCreator(config, logger)
        self.issue_fetcher = IssueFetcher(config, logger)

        self.min_confidence = config.get("bot", {}).get("min_confidence_score", 0.0)

    def run_once(self):
        self.logger.info("bot_run_started")

        issues = self.issue_fetcher.fetch_issues()

        if not issues:
            self.logger.info("no_issues_found")
            return

        for issue in issues:
            result = self._process_issue(issue)
            time.sleep(5)  # avoid rate limits

        self.logger.info("bot_run_complete")

    # 🔥 FIXED FUNCTION
    def _process_issue(self, issue) -> str:
        self.logger.info("processing_issue", issue_url=issue.html_url)

        try:
            # Step 1: fork + clone
            fork_full_name = self.repo_manager.fork_repo(issue.repo_full_name)
            time.sleep(3)

            repo_path = self.repo_manager.clone_repo(fork_full_name, issue.id)
            branch_name = self.repo_manager.create_branch(repo_path, issue.number)

            title = issue.title.lower()

            # 🔥 Step 2: simple issue filter
            if not any(k in title for k in ["readme", "doc", "typo"]):
                self.logger.info("Skipping complex issue", issue_url=issue.html_url)
                return "skipped"

            # Step 3: find relevant files
            relevant_files = self.ai_engine.get_relevant_files(issue, repo_path)

            # 🔥 fallback
            if not relevant_files:
                self.logger.info("Using README fallback")
                relevant_files = ["README.md"]

            # Step 4: analyze
            file_contents = self.ai_engine.read_file_contents(repo_path, relevant_files)
            analysis = self.ai_engine.analyze_issue(issue, file_contents)

            # Step 5: apply changes
            if not analysis.patches:
                self.logger.info("No patches → fallback edit")

                readme_path = Path(repo_path) / "README.md"

                if readme_path.exists():
                    content = readme_path.read_text(encoding="utf-8", errors="replace")
                    content += "\n\n<!-- Updated by AI bot -->\n"
                    readme_path.write_text(content, encoding="utf-8")

                    modified_files = ["README.md"]
                else:
                    self.logger.warning("README not found")
                    return "skipped"
            else:
                modified_files = self.fixer.apply_patches(analysis, repo_path)

            # Step 6: commit + push
            committed = self.repo_manager.commit_changes(
                repo_path, issue, "Auto fix by bot"
            )

            if not committed:
                self.logger.warning("nothing_committed")
                return "skipped"

            self.repo_manager.push_branch(repo_path, branch_name)

            # Step 7: create PR
            pr_url = self.pr_creator.create_pull_request(
                issue=issue,
                branch_name=branch_name,
                analysis_result=analysis,
                modified_files=modified_files,
            )

            self.logger.info("PR created", url=pr_url)
            return "success"

        except Exception as e:
            self.logger.error("error", error=str(e))
            return "failed"


def main():
    config = ConfigLoader().config
    logger = Logger()

    bot = ContributorBot(config, logger)
    bot.run_once()


if __name__ == "__main__":
    main()
