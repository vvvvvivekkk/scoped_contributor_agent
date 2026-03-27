# 🤖 Autonomous GitHub Open Source Contributor Bot

A **production-ready, fully autonomous** Python bot that discovers beginner-friendly GitHub issues, uses an LLM to generate targeted fixes, validates them, and opens pull requests — all with minimal human intervention.

---

## ✨ Features

| Feature | Details |
|---|---|
| 🔍 **Issue Discovery** | GitHub Search API with `good first issue` + `help wanted` labels |
| 🧠 **AI-Powered Fixes** | OpenAI GPT-4o or Anthropic Claude — configurable via `config.yaml` |
| 🔒 **Safety Gates** | Confidence score ≥ 0.70, max diff 200 lines, daily PR limit |
| ✅ **Validation** | flake8 lint + pytest before any commit |
| 🔄 **Auto Test Gen** | Generates basic tests if a repo has none |
| 📝 **Rich PRs** | Structured PR body with references, file list, and confidence score |
| 💬 **Issue Comments** | Notifies maintainers before opening a PR |
| ⏰ **Scheduler** | APScheduler cron: runs every 6 hours |
| 🐳 **Docker Ready** | Multi-stage Dockerfile + docker-compose |
| 📊 **Logging** | Structured JSON logs + daily statistics in `logs/stats.json` |

---

## 📁 Project Structure

```
scoped_contributor_agent/
├── bot/
│   ├── core/
│   │   ├── issue_fetcher.py     # GitHub Search API + multi-stage filtering
│   │   ├── repo_manager.py      # Fork, clone, branch, commit, push
│   │   ├── ai_engine.py         # OpenAI/Anthropic provider strategy
│   │   ├── fixer.py             # Patch application + syntax validation
│   │   ├── validator.py         # flake8 + pytest + test generation
│   │   └── pr_creator.py        # PR creation + issue commenting
│   ├── utils/
│   │   ├── config_loader.py     # YAML + env var config
│   │   └── logger.py            # JSON logging + stats tracking
│   └── main.py                  # Orchestrator + CLI + APScheduler
├── tests/
│   ├── test_issue_fetcher.py
│   ├── test_ai_engine.py
│   ├── test_validator.py
│   └── test_pr_creator.py
├── config.yaml                  # Bot configuration
├── .env.example                 # Environment variable template
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
└── pytest.ini
```

---

## 🚀 Quick Start

### 1. Clone & Install

```bash
git clone <this-repo>
cd scoped_contributor_agent
pip install -r requirements.txt
```

### 2. Configure Environment

```bash
cp .env.example .env
# Edit .env with your credentials:
nano .env
```

**Required environment variables:**

| Variable | Description |
|---|---|
| `GITHUB_TOKEN` | GitHub Personal Access Token (scopes: `repo`, `read:user`) |
| `GITHUB_USERNAME` | Your GitHub username (the bot's account) |
| `OPENAI_API_KEY` | OpenAI API key (if using OpenAI provider) |
| `ANTHROPIC_API_KEY` | Anthropic API key (if using Anthropic provider) |

### 3. Configure the Bot

Edit `config.yaml` to customize:

```yaml
ai:
  provider: "openai"   # or "anthropic"
  model: "gpt-4o"

bot:
  max_prs_per_day: 3
  min_confidence_score: 0.70
  repo_allowlist:
    - "specific/repo"    # Leave empty to allow all repos
```

### 4. Run

```bash
# Single run (recommended for first test)
python -m bot.main --run-once

# Dry run (no real API calls — great for testing)
python -m bot.main --run-once --dry-run

# Scheduled mode (runs every 6 hours)
python -m bot.main
```

---

## 🐳 Docker

```bash
# Build and run with Docker Compose
cp .env.example .env  # Fill in credentials
docker-compose up -d

# View logs
docker-compose logs -f

# Run once then stop
docker-compose run --rm contributor-bot python -m bot.main --run-once

# Dry run in Docker
docker-compose run --rm contributor-bot python -m bot.main --run-once --dry-run
```

---

## 🧪 Testing

```bash
# Run full test suite (no API keys needed — all mocked)
pytest

# With coverage
pip install pytest-cov
pytest --cov=bot --cov-report=term-missing
```

All 4 test modules use mocked external calls:
- `tests/test_issue_fetcher.py` — filtering logic
- `tests/test_ai_engine.py` — LLM parsing and analysis
- `tests/test_validator.py` — subprocess mocking for flake8/pytest
- `tests/test_pr_creator.py` — GitHub API mocking

---

## 🔁 Pipeline Flow

```
For each discovered issue:
  ┌─────────────────────────────────────────┐
  │ 1. Check daily PR limit                  │
  │ 2. Fork + Clone repository               │
  │ 3. Create branch: auto-fix/<issue_id>    │
  │ 4. AI: identify relevant Python files    │
  │ 5. AI: analyze issue + generate patches  │
  │ 6. Gate: confidence score ≥ 0.70         │
  │ 7. Apply patches (exact string replace)  │
  │ 8. Gate: diff ≤ 200 lines                │
  │ 9. Run flake8 + pytest                   │
  │ 10. Commit + push to fork                │
  │ 11. Comment on issue                     │
  │ 12. Create pull request                  │
  │ 13. Log + Cleanup                        │
  └─────────────────────────────────────────┘
```

---

## 🛡️ Safety Controls

| Control | Setting | Default |
|---|---|---|
| Max PRs/day | `bot.max_prs_per_day` | 3 |
| Max diff size | `bot.max_diff_lines` | 200 |
| Confidence gate | `bot.min_confidence_score` | 0.70 |
| Repo allowlist | `bot.repo_allowlist` | [] (all allowed) |
| Skip issue labels | `bot.skip_issue_labels` | discussion, question, wontfix |
| Max repo stars | `bot.max_repo_stars` | 5000 |
| Min repo stars (inactive check) | `bot.min_repo_stars` | 10 |
| Repo inactivity cutoff | `bot.max_repo_inactivity_days` | 90 |

---

## 📊 Logs & Monitoring

Logs are written to:
- `logs/bot.log` — JSON-structured event log
- `logs/stats.json` — Daily statistics

Example `stats.json`:
```json
{
  "2024-12-01": {
    "attempted": 5,
    "prs_created": 2,
    "skipped": 2,
    "failed": 1,
    "pr_urls": [
      "https://github.com/owner/repo/pull/123",
      "https://github.com/owner2/repo2/pull/456"
    ]
  }
}
```

---

## ⚙️ GitHub Token Setup

1. Go to [GitHub Settings → Developer Settings → Personal Access Tokens](https://github.com/settings/tokens)
2. Create a **Classic token** with these scopes:
   - `repo` (full control of private repositories)
   - `read:user` (read user profile data)
   - `workflow` (update GitHub Actions workflows)
3. Paste the token into `.env` as `GITHUB_TOKEN`

---

## 🔄 Changing AI Provider

**Switch to Anthropic Claude:**

```yaml
# config.yaml
ai:
  provider: "anthropic"
  model: "claude-3-5-sonnet-20241022"
```

```bash
# .env
ANTHROPIC_API_KEY=sk-ant-your_key_here
```

---

## ⚠️ Responsible Use

- **Always test in dry-run mode first**: `--dry-run`
- **Use the allowlist** to restrict to specific repos during testing
- **Review all PRs** before they are merged — this bot is a productivity tool, not a replacement for human judgment
- **Respect rate limits**: The bot has built-in backoff, but avoid running too frequently
- **Check repo contribution guidelines** before the bot opens PRs

---

## 📄 License

MIT — use freely, contribute back!
