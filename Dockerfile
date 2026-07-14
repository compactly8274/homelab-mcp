# syntax=docker/dockerfile:1.7
#
# homelab-mcp: multi-stage Dockerfile.
#
#   - builder: install uv, sync project deps into a venv
#   - runtime: copy the venv + project; non-root user; tini; healthcheck
#
# Build:   docker buildx build --platform linux/amd64,linux/arm64 -t homelab-mcp .
# Publish: published to GHCR by .github/workflows/build.yml on every 'v*' tag.
#

ARG PYTHON_VERSION=3.12
ARG UV_VERSION=0.5.7

# -- builder ---------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS builder

ARG UV_VERSION

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/install

# uv is a single-binary installer. We pin the version to keep builds
# deterministic.
RUN apt-get update -qq && \
    apt-get install -y --no-install-recommends curl ca-certificates && \
    rm -rf /var/lib/apt/lists/* && \
    curl -fsSL https://astral.sh/uv/${UV_VERSION}/install.sh | sh && \
    mv /root/.local/bin/uv /usr/local/bin/uv

WORKDIR /src

# Copy only the project metadata first to maximize Docker layer caching.
COPY pyproject.toml ./
COPY homelab_mcp ./homelab_mcp

# Install with dev deps so tests can run in CI builds.
# The runtime stage only sees /install, which contains the final venv.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --extra dev --no-install-project --no-editable

# -- runtime ---------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS runtime

LABEL org.opencontainers.image.title="homelab-mcp" \
      org.opencontainers.image.description="MCP server: homelab diagnostics + auto-update pipeline" \
      org.opencontainers.image.source="https://github.com/anthropic-experimental/homelab-mcp" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/install/bin:${PATH}" \
    HOMELAB_MCP_STATE_DIR=/data \
    HOMELAB_MCP_PORT=18790

# The runtime stage carries only the venv + the package + the entrypoint.
COPY --from=builder /install /install
COPY homelab_mcp /install/lib/python3.12/site-packages/homelab_mcp
COPY scripts/entrypoint.sh /usr/local/bin/entrypoint.sh

# Non-root user. uid 1000 matches the typical first non-system user
# (the 'homelab' user on TrueNAS, the 'nobody' group default on Unraid,
# and the default in most container runtimes).
RUN groupadd -g 1000 homelab && \
    useradd -u 1000 -g 1000 -d /data -s /sbin/nologin homelab && \
    chmod +x /usr/local/bin/entrypoint.sh && \
    mkdir -p /data && \
    chown -R homelab:homelab /data

# tini for proper signal handling (PID 1 zombie reaping).
RUN apt-get update -qq && \
    apt-get install -y --no-install-recommends tini ca-certificates && \
    rm -rf /var/lib/apt/lists/*

USER homelab
WORKDIR /data

# SSE/MCP transport. Exposed on 18790 by default.
EXPOSE 18790

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request, sys; \
        r = urllib.request.urlopen('http://127.0.0.1:18790/health', timeout=4); \
        sys.exit(0 if r.status == 200 else 1)" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
CMD ["daemon"]
