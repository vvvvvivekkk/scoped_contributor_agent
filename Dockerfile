# ── Stage 1: Builder ──────────────────────────────────────────────── #
FROM python:3.11-slim AS builder

WORKDIR /app

# Install system dependencies for git operations
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt


# ── Stage 2: Runtime ──────────────────────────────────────────────── #
FROM python:3.11-slim

LABEL maintainer="contributor-bot"
LABEL description="Autonomous GitHub Open Source Contributor Bot"
LABEL version="1.0.0"

WORKDIR /app

# Install git (required for cloning/branching/pushing)
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    && rm -rf /var/lib/apt/lists/*

# Copy installed packages from builder
COPY --from=builder /root/.local /root/.local

# Make sure scripts in .local are usable
ENV PATH=/root/.local/bin:$PATH

# Copy application source
COPY bot/ ./bot/
COPY config.yaml .

# Create directories for logs and temp clones
RUN mkdir -p /app/logs /tmp/contributor_bot

# Configure git global identity (used for commits)
RUN git config --global user.email "bot@contributor.ai" && \
    git config --global user.name "Contributor Bot" && \
    git config --global safe.directory '*'

# Set Python path
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

# Expose log volume
VOLUME ["/app/logs"]

# Health check — verifies the bot module is importable
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "from bot.main import ContributorBot; print('OK')" || exit 1

# Default command: run in scheduled mode
CMD ["python", "-m", "bot.main"]
