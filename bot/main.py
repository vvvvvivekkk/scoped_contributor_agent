def _process_issue(self, issue: Issue) -> str:
    self.logger.info("processing_issue", issue_url=issue.html_url)

    try:
        fork_full_name = self.repo_manager.fork_repo(issue.repo_full_name)
        time.sleep(3)

        repo_path = self.repo_manager.clone_repo(fork_full_name, issue.id)
        branch_name = self.repo_manager.create_branch(repo_path, issue.number)

        title = issue.title.lower()

        # 🔥 simple issue filter
        if not any(k in title for k in ["readme", "doc", "typo"]):
            self.logger.info("Skipping complex issue")
            return "skipped"

        relevant_files = self.ai_engine.get_relevant_files(issue, repo_path)

        # 🔥 fallback
        if not relevant_files:
            self.logger.info("Using README fallback")
            relevant_files = ["README.md"]

        file_contents = self.ai_engine.read_file_contents(repo_path, relevant_files)
        analysis = self.ai_engine.analyze_issue(issue, file_contents)

        if not analysis.patches:
            readme_path = Path(repo_path) / "README.md"
            if readme_path.exists():
                content = readme_path.read_text()
                readme_path.write_text(content + "\n\n<!-- AI update -->\n")
                modified_files = ["README.md"]
            else:
                return "skipped"
        else:
            modified_files = self.fixer.apply_patches(analysis, repo_path)

        self.repo_manager.commit_changes(repo_path, issue, "Auto fix")
        self.repo_manager.push_branch(repo_path, branch_name)

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
