# syntax=docker/dockerfile:1
# Call Analyzer: slim, non-root image. Build from this directory:
#   docker build -t call-analyzer .
#   docker run --rm -p 8080:8080 -e CALL_ANALYZER_TOKEN=... -v analyzer-data:/data call-analyzer

# For reproducible production builds, pin by digest (see README "Docker"), e.g.
#   --build-arg PYTHON_IMAGE=python:3.12-slim-bookworm@sha256:<digest>
ARG PYTHON_IMAGE=python:3.12-slim-bookworm

# --- build: resolve the locked environment with uv ---------------------------------------------
FROM ${PYTHON_IMAGE} AS build
COPY --from=ghcr.io/astral-sh/uv:0.8.17 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app
# Dependencies first, so code edits don't invalidate this layer.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project
COPY README.md ./
COPY call_analyzer ./call_analyzer
COPY demo ./demo
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev

# --- runtime: no uv, no build tools, unprivileged user -----------------------------------------
FROM ${PYTHON_IMAGE}
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin analyzer \
    && mkdir /data && chown analyzer /data
COPY --from=build --chown=root:root /app /app
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    ANALYZER_HOST=0.0.0.0 \
    ANALYZER_PORT=8080 \
    ANALYZER_DB_PATH=/data/calls.db
WORKDIR /app
USER analyzer
VOLUME ["/data"]
EXPOSE 8080
# Python instead of curl (not in slim images); follows ANALYZER_PORT if it's overridden at run time.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('ANALYZER_PORT', '8080'), timeout=2)"]
CMD ["python", "-m", "call_analyzer", "serve"]
