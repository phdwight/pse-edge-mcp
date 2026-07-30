# syntax=docker/dockerfile:1
# ---- builder: resolves deps with uv; never ships -------------------------------
FROM python:3.14-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock* ./
# No --extra postgres: the Postgres storage backend is Phase 4 and nothing in src/
# imports sqlalchemy/asyncpg/alembic yet, so shipping them cost 44 MB (a third of the
# venv) and broke the <200 MB budget in CI. Re-add the extra when Phase 4 lands code
# that actually uses it.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev || \
    uv sync --no-install-project --no-dev
COPY src ./src
COPY README.md ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --no-dev

# ---- runtime: slim, non-root, no build tools, no secrets -----------------------
FROM python:3.14-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH="/app/.venv/bin:$PATH"
RUN groupadd -r app && useradd -r -g app -d /app app
WORKDIR /app
COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --from=builder --chown=app:app /app/src /app/src
USER app
EXPOSE 8000
# Secrets (DATABASE_URL etc.) arrive at runtime via env/env_file — never baked in.
ENTRYPOINT ["pse-edge-mcp"]
CMD ["--http", "--port", "8000"]
