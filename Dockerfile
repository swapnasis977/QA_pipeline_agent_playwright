# Dockerfile for the Python QA pipeline (qa_pipeline/ + agents/ + utils/).
# This builds the orchestrator/pipeline side only — the generated
# Playwright/TypeScript workspace (workspaces/<app>/, written by the
# framework scaffolder at run time) is a separate concern and is not baked
# into this image.
#
# Node.js is required IN THIS SAME IMAGE (not just in the generated
# workspace's own tooling) because:
#   - agents/executor.py shells out to `npx playwright test`
#   - utils/playwright_mcp_client.py can launch `npx @playwright/mcp@latest`
#
# We install Node via NodeSource's setup script on top of python:3.11-slim
# (Debian-based) rather than a multi-stage `FROM node:20-slim AS node_base`
# copy-in, because the runtime dependency here is "the `node`/`npx`
# executables plus their own npm-managed packages fetched at run time via
# `npx`" — not a large pre-built node_modules tree to copy from another
# stage. A single apt-based install keeps the Dockerfile simpler for that
# use case; switch to the multi-stage copy-in pattern if you later need to
# pre-bake specific npm packages at build time instead of relying on `npx`.

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \n    PYTHONDONTWRITEBYTECODE=1 \n    PIP_NO_CACHE_DIR=1

# ---- Node.js 20.x via NodeSource ----
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ---- Python dependencies first, for layer caching ----
COPY pyproject.toml requirements.txt README.md ./
RUN pip install --no-cache-dir -r requirements.txt

# ---- Application code ----
COPY agents/ ./agents/
COPY utils/ ./utils/
COPY skills/ ./skills/
COPY instructions/ ./instructions/
COPY qa_pipeline/ ./qa_pipeline/

# Install the project itself (editable, no deps re-fetch — already installed above)
RUN pip install --no-cache-dir --no-deps -e .

# Run as an unprivileged user. The pipeline writes only under /app (runs/, workspaces/,
# profiles/*/brain/), which this user owns; nothing needs root once the image is built.
# Mount your profiles/ and .env at run time - they are deliberately not baked in
# (see .dockerignore): a secret copied into an image layer stays in it.
RUN useradd --create-home --shell /usr/sbin/nologin qa \n    && chown -R qa:qa /app
USER qa

# Real invocations need real CLI args and a populated environment (API keys,
# --story/--repo-dir, etc.) — a container run with none of that should show
# usage/help, not crash on a missing required argument.
ENTRYPOINT ["python", "-m", "qa_pipeline.cli"]
CMD ["--help"]
