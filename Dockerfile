# Haiplane Hub — cold-start image (#944).
# python:3.11-slim + uv; dependencies resolved from uv.lock for reproducibility.
FROM python:3.11-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Dependency layer first so code edits do not re-resolve the lock.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY hub ./hub
COPY scripts ./scripts
RUN uv sync --frozen --no-dev

COPY deploy/docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV PATH="/app/.venv/bin:$PATH" \
    HAIPLANE_HUB_HOST=0.0.0.0 \
    HAIPLANE_HUB_DB=/data/hub.db

EXPOSE 8080

ENTRYPOINT ["/entrypoint.sh"]
