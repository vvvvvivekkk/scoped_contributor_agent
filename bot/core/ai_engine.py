"""
bot/core/ai_engine.py
---------------------
Provider-agnostic AI engine using a strategy pattern.
Supports OpenAI and Anthropic. Returns structured JSON analysis
including affected files, patches, confidence score, and PR description.
"""

import json
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from tenacity import retry, stop_after_attempt, wait_exponential

from bot.core.issue_fetcher import Issue
from bot.utils.config_loader import ConfigLoader
from bot.utils.logger import BotLogger

# ------------------------------------------------------------------ #
# Prompt Templates                                                     #
# ------------------------------------------------------------------ #

SYSTEM_PROMPT = """You are an expert Python software engineer and open-source contributor.
Your task is to analyze GitHub issues and generate minimal, correct, production-quality fixes.
You must ALWAYS respond with valid JSON only — no markdown fences, no prose outside the JSON.
"""

RELEVANT_FILES_PROMPT = """You are analyzing a GitHub issue to identify which files need to be changed.

Issue Title: {title}
Issue Body:
{body}

Repository file list (relative paths):
{file_list}

Return a JSON object with this exact structure:
{{
  "relevant_files": ["path/to/file1.py", "path/to/file2.py"],
  "reasoning": "Brief explanation of why these files are relevant"
}}

Only include .py files that are directly related to the issue.
Maximum 5 files. If no files seem clearly relevant, return an empty list.
"""

ANALYSIS_AND_FIX_PROMPT = """You are an expert Python open-source contributor. Analyze this GitHub issue and generate a fix.

Issue Title: {title}
Issue Number: #{number}
Issue Body:
{body}

File Contents:
{file_contents}

Generate a minimal, correct fix. Return a JSON object with this EXACT structure:
{{
  "affected_files": ["path/to/file.py"],
  "fix_description": "One-line description of what the fix does",
  "patches": [
    {{
      "file": "path/to/file.py",
      "original": "EXACT original code block to replace (must match file exactly)",
      "replacement": "EXACT replacement code"
    }}
  ],
  "confidence_score": 0.85,
  "pr_title": "fix: resolve issue #{number} - short description",
  "pr_description": "## Summary\\n\\nThis PR fixes #{number} by ...\\n\\n## Changes Made\\n\\n- ...\\n\\n## Testing\\n\\n- ..."
}}

Rules:
- confidence_score: 0.0-1.0. Set < 0.6 if you are unsure about the fix.
- patches[].original: Must be an EXACT substring of the file content (will be used with str.replace).
- patches[].replacement: The corrected code replacing the original block.
- Keep diffs minimal — change only what is necessary.
- If the issue cannot be fixed with a code change (e.g., it's a documentation or design discussion), set confidence_score to 0.1 and patches to [].
- Only generate patches for Python (.py) files.
"""

BASIC_TEST_PROMPT = """Generate a minimal pytest test for the following fix:

Fix Description: {fix_description}
Modified File: {file_path}
File Content After Fix:
{file_content}

Return valid Python code for a test file. The test should:
1. Import the relevant function/class from the fixed file
2. Test the specific behavior that was fixed
3. Use pytest style (no unittest)
4. Be runnable with `pytest`

Return ONLY the Python code, no JSON, no explanation.
"""


# ------------------------------------------------------------------ #
# Result Types                                                         #
# ------------------------------------------------------------------ #

@dataclass
class Patch:
    file: str
    original: str
    replacement: str


@dataclass
class AnalysisResult:
    affected_files: list[str] = field(default_factory=list)
    fix_description: str = ""
    patches: list[Patch] = field(default_factory=list)
    confidence_score: float = 0.0
    pr_title: str = ""
    pr_description: str = ""
    raw_response: str = ""


# ------------------------------------------------------------------ #
# Provider Abstraction                                                 #
# ------------------------------------------------------------------ #

class BaseProvider(ABC):
    """Abstract base class for AI providers."""

    @abstractmethod
    def complete(self, system: str, user: str) -> str:
        """Send a completion request and return the response text."""
        ...


class OpenAIProvider(BaseProvider):
    """OpenAI chat completion provider."""

    def __init__(self, api_key: str, model: str, max_tokens: int, temperature: float):
        from openai import OpenAI
        self.client = OpenAI(api_key=api_key)
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=4, max=30))
    def complete(self, system: str, user: str) -> str:
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=self.max_tokens,
            temperature=self.temperature,
        )
        return response.choices[0].message.content or ""


class AnthropicProvider(BaseProvider):
    """Anthropic Claude completion provider."""

    def __init__(self, api_key: str, model: str, max_tokens: int, temperature: float):
        import anthropic
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=4, max=30))
    def complete(self, system: str, user: str) -> str:
        response = self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return response.content[0].text or ""


# ------------------------------------------------------------------ #
# AI Engine                                                            #
# ------------------------------------------------------------------ #

class AIEngine:
    """
    Provider-agnostic AI engine for issue analysis and fix generation.

    Usage:
        engine = AIEngine(config, logger)
        relevant = engine.get_relevant_files(issue, repo_path)
        result = engine.analyze_issue(issue, file_contents)
        if result.confidence_score >= 0.7:
            # apply patches
    """

    MAX_FILE_SIZE_BYTES = 50_000  # Skip files larger than 50KB
    MAX_FILE_CONTENT_CHARS = 8_000  # Truncate individual file content in prompt

    def __init__(self, config: ConfigLoader, logger: BotLogger):
        self.config = config
        self.logger = logger
        self.provider = self._init_provider()

    def _init_provider(self) -> BaseProvider:
        """Initialize the correct AI provider from config."""
        provider_name = self.config.get("ai.provider", "openai")
        model = self.config.get("ai.model", "gpt-4o")
        max_tokens = int(self.config.get("ai.max_tokens", 4096))
        temperature = float(self.config.get("ai.temperature", 0.2))
        api_key = self.config.require("ai.api_key")

        if provider_name == "openai":
            return OpenAIProvider(api_key, model, max_tokens, temperature)
        elif provider_name == "anthropic":
            return AnthropicProvider(api_key, model, max_tokens, temperature)
        else:
            raise ValueError(f"Unknown AI provider: {provider_name}. Use 'openai' or 'anthropic'.")

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def get_relevant_files(self, issue: Issue, repo_path: str) -> list[str]:
        """
        Ask the LLM which Python files in the repo are relevant to the issue.

        Returns:
            List of relative file paths (e.g., ["src/utils.py"])
        """
        file_list = self._list_python_files(repo_path)
        if not file_list:
            return []

        prompt = RELEVANT_FILES_PROMPT.format(
            title=issue.title,
            body=issue.body[:3000],
            file_list="\n".join(file_list[:200]),  # Limit to 200 files
        )

        self.logger.debug("requesting_relevant_files", issue_id=issue.id)
        raw = self.provider.complete(SYSTEM_PROMPT, prompt)

        try:
            data = self._parse_json(raw)
            files = data.get("relevant_files", [])
            # Validate that returned paths actually exist
            valid_files = [
                f for f in files
                if (Path(repo_path) / f).exists() and f.endswith(".py")
            ]
            self.logger.info(
                "relevant_files_identified",
                count=len(valid_files),
                files=valid_files,
            )
            return valid_files
        except (json.JSONDecodeError, KeyError) as e:
            self.logger.warning("relevant_files_parse_error", error=str(e))
            return []

    def analyze_issue(self, issue: Issue, file_contents: dict[str, str]) -> AnalysisResult:
        """
        Analyze the issue and generate a fix using the LLM.

        Args:
            issue: The Issue to analyze
            file_contents: Dict of {relative_path: file_content}

        Returns:
            AnalysisResult with patches and confidence score
        """
        # Format file contents for the prompt
        formatted = self._format_file_contents(file_contents)

        prompt = ANALYSIS_AND_FIX_PROMPT.format(
            title=issue.title,
            number=issue.number,
            body=issue.body[:3000],
            file_contents=formatted,
        )

        self.logger.info("analyzing_issue", issue_id=issue.id, title=issue.title[:60])
        raw = self.provider.complete(SYSTEM_PROMPT, prompt)

        return self._parse_analysis(raw, issue)

    def generate_basic_test(
        self, fix_description: str, file_path: str, file_content: str
    ) -> str:
        """
        Generate a minimal pytest test for the given fix.

        Returns:
            Python test code as a string
        """
        prompt = BASIC_TEST_PROMPT.format(
            fix_description=fix_description,
            file_path=file_path,
            file_content=file_content[:self.MAX_FILE_CONTENT_CHARS],
        )
        raw = self.provider.complete(SYSTEM_PROMPT, prompt)
        # Strip markdown code fences if present
        return self._strip_code_fences(raw)

    # ------------------------------------------------------------------ #
    # File Scanning                                                        #
    # ------------------------------------------------------------------ #

    def read_file_contents(self, repo_path: str, relative_paths: list[str]) -> dict[str, str]:
        """
        Read the contents of the specified files.

        Skips files that are too large or don't exist.
        """
        contents = {}
        for rel_path in relative_paths:
            full_path = Path(repo_path) / rel_path
            if not full_path.exists():
                continue
            if full_path.stat().st_size > self.MAX_FILE_SIZE_BYTES:
                self.logger.debug("file_too_large_skipped", file=rel_path)
                continue
            try:
                contents[rel_path] = full_path.read_text(encoding="utf-8", errors="ignore")
            except IOError as e:
                self.logger.warning("file_read_error", file=rel_path, error=str(e))
        return contents

    def _list_python_files(self, repo_path: str) -> list[str]:
        """Return all Python file paths relative to repo_path."""
        py_files = []
        repo = Path(repo_path)
        for p in repo.rglob("*.py"):
            # Skip hidden dirs, virtual envs, and test artifacts
            rel = str(p.relative_to(repo))
            if any(part.startswith(".") or part in ("venv", "__pycache__", ".git", "node_modules")
                   for part in Path(rel).parts):
                continue
            py_files.append(rel)
        return sorted(py_files)

    # ------------------------------------------------------------------ #
    # Parsing Helpers                                                      #
    # ------------------------------------------------------------------ #

    def _parse_analysis(self, raw: str, issue: Issue) -> AnalysisResult:
        """Parse the LLM's JSON analysis response into an AnalysisResult."""
        try:
            data = self._parse_json(raw)
            patches = [
                Patch(
                    file=p["file"],
                    original=p["original"],
                    replacement=p["replacement"],
                )
                for p in data.get("patches", [])
                if p.get("file") and p.get("original") is not None
            ]
            result = AnalysisResult(
                affected_files=data.get("affected_files", []),
                fix_description=data.get("fix_description", ""),
                patches=patches,
                confidence_score=float(data.get("confidence_score", 0.0)),
                pr_title=data.get("pr_title", f"fix: resolve issue #{issue.number}"),
                pr_description=data.get("pr_description", ""),
                raw_response=raw,
            )
            self.logger.info(
                "analysis_complete",
                confidence=result.confidence_score,
                patches=len(patches),
                issue_id=issue.id,
            )
            return result
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            self.logger.error("analysis_parse_error", error=str(e), raw=raw[:300])
            return AnalysisResult(confidence_score=0.0, raw_response=raw)

    def _parse_json(self, text: str) -> dict:
        """
        Parse JSON from LLM output, stripping markdown code fences if present.
        """
        # Remove markdown code fences
        cleaned = self._strip_code_fences(text)
        # Find the first JSON object
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if match:
            return json.loads(match.group())
        return json.loads(cleaned)

    def _strip_code_fences(self, text: str) -> str:
        """Remove ```json ... ``` or ``` ... ``` wrappers from LLM output."""
        text = re.sub(r"^```[a-z]*\n?", "", text.strip())
        text = re.sub(r"\n?```$", "", text.strip())
        return text.strip()

    def _format_file_contents(self, file_contents: dict[str, str]) -> str:
        """Format file contents for inclusion in the LLM prompt."""
        parts = []
        for path, content in file_contents.items():
            truncated = content[:self.MAX_FILE_CONTENT_CHARS]
            if len(content) > self.MAX_FILE_CONTENT_CHARS:
                truncated += "\n... [truncated]"
            parts.append(f"=== {path} ===\n{truncated}")
        return "\n\n".join(parts)
