# Haiplane Hub — the image a reader builds to try the hub (#944, #1336).
# There is no registry publish: `docker compose up --build` IS the
# distribution. Bases are digest-pinned so a rebuild cannot float onto
# a moved tag. Pins recorded 2026-09-23 (multi-arch indexes):
#   python:3.11-slim  (matches requires-python and the mypy target)
#   ghcr.io/astral-sh/uv:0.12.18
FROM python:3.11-slim@sha256:da047cb8f9d1d98e5c070f5300ba9f7274e33b8fc0e5be5ed88740aed1b95ba9

COPY --from=ghcr.io/astral-sh/uv:0.12.18@sha256:3adc3706091ce7c2fe595e669628caedd6d951551b92b258b7e7dbe06d9440bc /uv /uvx /bin/

WORKDIR /app

# Copy, don't hardlink: overlay filesystems reject links across layers.
ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    HAIPLANE_HUB_HOST=0.0.0.0 \
    HAIPLANE_HUB_DB=/data/hub.db

# Dependency layer first so a code edit does not re-resolve the lock.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY hub ./hub
COPY scripts ./scripts
RUN uv sync --frozen --no-dev

COPY deploy/docker/entrypoint.sh /entrypoint.sh
# /data exists even when the caller forgets the volume; compose still
# mounts ./data over it, and the process stays root so that mount is writable.
RUN chmod +x /entrypoint.sh && mkdir -p /data

EXPOSE 8080

ENTRYPOINT ["/entrypoint.sh"]
