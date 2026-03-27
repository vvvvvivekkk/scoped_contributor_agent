"""
bot/utils/logger.py
-------------------
Structured JSON logger and statistics tracker for the contributor bot.
Writes human-readable logs to stdout and JSON records to logs/bot.log.
Persists run statistics (attempted, success, merged) in logs/stats.json.
"""

import json
import logging
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional


class BotLogger:
    """
    Centralized logger for the contributor bot.

    Provides:
    - Structured JSON log records → logs/bot.log
    - Human-readable console output via stdlib logging
    - Daily stats tracking → logs/stats.json
    """

    def __init__(self, log_dir: str = "logs", level: str = "INFO"):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)

        self.log_file = self.log_dir / "bot.log"
        self.stats_file = self.log_dir / "stats.json"

        self._setup_stdlib_logger(level)
        self._stats = self._load_stats()

    # ------------------------------------------------------------------ #
    # Public logging API                                                   #
    # ------------------------------------------------------------------ #

    def info(self, event: str, **kwargs: Any) -> None:
        """Log an informational event."""
        self._log("INFO", event, **kwargs)

    def warning(self, event: str, **kwargs: Any) -> None:
        """Log a warning."""
        self._log("WARNING", event, **kwargs)

    def error(self, event: str, **kwargs: Any) -> None:
        """Log an error."""
        self._log("ERROR", event, **kwargs)

    def debug(self, event: str, **kwargs: Any) -> None:
        """Log a debug message."""
        self._log("DEBUG", event, **kwargs)

    # ------------------------------------------------------------------ #
    # Stats tracking                                                       #
    # ------------------------------------------------------------------ #

    def record_attempt(self, issue_url: str) -> None:
        """Record that we attempted to fix an issue."""
        today = self._today()
        self._ensure_day(today)
        self._stats[today]["attempted"] += 1
        self._stats[today]["issues"].append(issue_url)
        self._save_stats()

    def record_pr_created(self, pr_url: str, issue_url: str) -> None:
        """Record a successfully created PR."""
        today = self._today()
        self._ensure_day(today)
        self._stats[today]["prs_created"] += 1
        self._stats[today]["pr_urls"].append(pr_url)
        self._save_stats()
        self.info("pr_created", pr_url=pr_url, issue_url=issue_url)

    def record_pr_skipped(self, reason: str, issue_url: str) -> None:
        """Record that we skipped an issue."""
        today = self._today()
        self._ensure_day(today)
        self._stats[today]["skipped"] += 1
        self._save_stats()
        self.info("pr_skipped", reason=reason, issue_url=issue_url)

    def record_failure(self, reason: str, issue_url: Optional[str] = None) -> None:
        """Record a failed attempt."""
        today = self._today()
        self._ensure_day(today)
        self._stats[today]["failed"] += 1
        self._save_stats()
        self.error("attempt_failed", reason=reason, issue_url=issue_url)

    def get_prs_today(self) -> int:
        """Return how many PRs have been created today."""
        today = self._today()
        return self._stats.get(today, {}).get("prs_created", 0)

    def get_stats(self) -> dict:
        """Return the full stats dictionary."""
        return dict(self._stats)

    # ------------------------------------------------------------------ #
    # Private helpers                                                      #
    # ------------------------------------------------------------------ #

    def _log(self, level: str, event: str, **kwargs: Any) -> None:
        """Write a structured log record to file and console."""
        record = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "level": level,
            "event": event,
            **kwargs,
        }

        # Write JSON record to log file
        with open(self.log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")

        # Write human-readable output to console
        log_fn = getattr(self._logger, level.lower(), self._logger.info)
        extra_parts = " | ".join(f"{k}={v}" for k, v in kwargs.items())
        message = f"{event}" + (f" | {extra_parts}" if extra_parts else "")
        log_fn(message)

    def _setup_stdlib_logger(self, level: str) -> None:
        """Configure the stdlib logger with a formatted console handler."""
        self._logger = logging.getLogger("contributor_bot")
        self._logger.setLevel(getattr(logging, level.upper(), logging.INFO))

        if not self._logger.handlers:
            handler = logging.StreamHandler(sys.stdout)
            formatter = logging.Formatter(
                fmt="%(asctime)s [%(levelname)s] %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
            handler.setFormatter(formatter)
            self._logger.addHandler(handler)

    def _load_stats(self) -> dict:
        """Load existing stats from disk, or start fresh."""
        if self.stats_file.exists():
            try:
                with open(self.stats_file, "r") as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                return {}
        return {}

    def _save_stats(self) -> None:
        """Persist stats to disk."""
        with open(self.stats_file, "w") as f:
            json.dump(self._stats, f, indent=2)

    def _today(self) -> str:
        """Return today's date as ISO string (YYYY-MM-DD)."""
        return date.today().isoformat()

    def _ensure_day(self, day: str) -> None:
        """Initialize stats bucket for a given day if not present."""
        if day not in self._stats:
            self._stats[day] = {
                "attempted": 0,
                "prs_created": 0,
                "skipped": 0,
                "failed": 0,
                "issues": [],
                "pr_urls": [],
            }
