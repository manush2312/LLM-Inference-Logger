# Backend image. Serves both the API and the ingestion worker -- same package,
# different entrypoint -- so the event contract, ORM models and repositories are
# imported rather than reimplemented, and producer and consumer cannot drift.

# --- Builder: resolve dependencies into a virtualenv --------------------------
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependency manifests first, in their own layer: application code changes on
# every commit, dependencies rarely, so this layer stays cached across builds.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

COPY app ./app
COPY alembic.ini ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# --- Runtime: no uv, no build tools, no root ---------------------------------
FROM python:3.12-slim-bookworm AS runtime

# curl is for the container healthcheck only; nothing in the app shells out.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 app

WORKDIR /app

COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --from=builder --chown=app:app /app/app /app/app
COPY --from=builder --chown=app:app /app/alembic.ini /app/alembic.ini

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Unprivileged: a container that never needs to write to its own filesystem
# should not be able to.
USER app

EXPOSE 8000

# Overridden to `llm-worker` for the ingestion service.
CMD ["llm-api"]
