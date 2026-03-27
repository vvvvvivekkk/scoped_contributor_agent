"""
bot/utils/config_loader.py
--------------------------
Loads configuration from config.yaml and overlays environment variables.
Supports dot-notation access (e.g., config.get("bot.max_prs_per_day")).
"""

import os
import re
from pathlib import Path
from typing import Any, Optional

import yaml
from dotenv import load_dotenv


class ConfigError(Exception):
    """Raised when a required configuration value is missing or invalid."""
    pass


class ConfigLoader:
    """
    Loads and provides access to the application configuration.

    Priority (highest to lowest):
      1. Environment variables
      2. config.yaml values
      3. Defaults defined in this class
    """

    # Default values if not set in config.yaml or env
    DEFAULTS: dict = {
        "github.api_base": "https://api.github.com",
        "ai.provider": "openai",
        "ai.model": "gpt-4o",
        "ai.max_tokens": 4096,
        "ai.temperature": 0.2,
        "bot.max_prs_per_day": 3,
        "bot.max_diff_lines": 200,
        "bot.min_confidence_score": 0.70,
        "bot.max_issues_per_run": 10,
        "bot.max_repo_inactivity_days": 90,
        "bot.max_repo_stars": 5000,
        "bot.min_repo_stars": 10,
        "bot.comment_before_pr": True,
        "bot.tmp_dir": "/tmp/contributor_bot",
        "bot.log_dir": "logs",
        "bot.retry_attempts": 3,
        "bot.retry_backoff_seconds": 5,
    }

    # Map env var names → config dot-keys (env takes priority)
    ENV_OVERRIDES: dict = {
        "GITHUB_TOKEN": "github.token",
        "GITHUB_USERNAME": "github.username",
        "OPENAI_API_KEY": "ai.api_key",
        "ANTHROPIC_API_KEY": "ai.api_key",
        "AI_MODEL": "ai.model",
        "BOT_MAX_PRS_PER_DAY": "bot.max_prs_per_day",
        "BOT_MIN_CONFIDENCE_SCORE": "bot.min_confidence_score",
    }

    def __init__(self, config_path: Optional[str] = None, env_file: Optional[str] = None):
        # Load .env file if present
        env_path = env_file or Path(__file__).parent.parent.parent / ".env"
        if Path(str(env_path)).exists():
            load_dotenv(env_path)

        # Determine config.yaml path
        if config_path is None:
            config_path = Path(__file__).parent.parent.parent / "config.yaml"

        self._raw: dict = self._load_yaml(str(config_path))
        self._flat: dict = self._flatten(self._raw)
        self._apply_defaults()
        self._apply_env_overrides()
        self._resolve_env_placeholders()
        self._validate()

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def get(self, key: str, default: Any = None) -> Any:
        """
        Retrieve a config value by dot-notation key.

        Example:
            config.get("bot.max_prs_per_day")  → 3
        """
        return self._flat.get(key, default)

    def require(self, key: str) -> Any:
        """Like get(), but raises ConfigError if the value is missing."""
        val = self._flat.get(key)
        if val is None or val == "":
            raise ConfigError(f"Required config key '{key}' is missing or empty.")
        return val

    def as_dict(self) -> dict:
        """Return the full flat config as a dictionary."""
        return dict(self._flat)

    # ------------------------------------------------------------------ #
    # Private helpers                                                      #
    # ------------------------------------------------------------------ #

    def _load_yaml(self, path: str) -> dict:
        """Load and parse a YAML config file."""
        if not Path(path).exists():
            raise ConfigError(f"Config file not found: {path}")
        with open(path, "r") as f:
            data = yaml.safe_load(f) or {}
        return data

    def _flatten(self, d: dict, prefix: str = "") -> dict:
        """
        Recursively flatten a nested dict to dot-notation keys.

        {'github': {'token': 'abc'}} → {'github.token': 'abc'}
        """
        result = {}
        for key, value in d.items():
            full_key = f"{prefix}.{key}" if prefix else key
            if isinstance(value, dict):
                result.update(self._flatten(value, full_key))
            else:
                result[full_key] = value
        return result

    def _apply_defaults(self) -> None:
        """Fill in missing keys with default values."""
        for key, value in self.DEFAULTS.items():
            if key not in self._flat:
                self._flat[key] = value

    def _apply_env_overrides(self) -> None:
        """Override config values with environment variable values."""
        for env_var, config_key in self.ENV_OVERRIDES.items():
            env_val = os.environ.get(env_var)
            if env_val:
                # Type-cast if the current value is numeric
                current = self._flat.get(config_key)
                if isinstance(current, int):
                    try:
                        env_val = int(env_val)
                    except ValueError:
                        pass
                elif isinstance(current, float):
                    try:
                        env_val = float(env_val)
                    except ValueError:
                        pass
                self._flat[config_key] = env_val

    def _resolve_env_placeholders(self) -> None:
        """
        Resolve ${VAR_NAME} placeholders in string config values.

        Example: "${GITHUB_TOKEN}" → value of GITHUB_TOKEN env var
        """
        pattern = re.compile(r"\$\{([^}]+)\}")
        for key, value in self._flat.items():
            if isinstance(value, str):
                def replacer(match):
                    env_var = match.group(1)
                    return os.environ.get(env_var, match.group(0))
                self._flat[key] = pattern.sub(replacer, value)

    def _validate(self) -> None:
        """Validate that critical config keys are present."""
        required_keys = [
            "github.token",
            "github.username",
            "ai.provider",
            "ai.model",
        ]
        missing = []
        for key in required_keys:
            val = self._flat.get(key, "")
            # Values that are still placeholders (not resolved from env) are invalid
            if not val or val.startswith("${"):
                missing.append(key)

        if missing:
            raise ConfigError(
                f"Missing required configuration keys: {missing}\n"
                "Please set them in config.yaml or as environment variables."
            )
