# syntax=docker/dockerfile:1
#
# Invariant #5, stated as intent: the runtime image carries only what the app needs to
# run. Not "under N megabytes" — a size number is a proxy that can pass while shipping
# junk, and fail while shipping only essentials. `scripts/check_image.py` enforces the
# real rule (installed distributions == the resolved runtime closure; no toolchain, no
# package manager, no dev deps, no caches) and reports size as information.
#
# ---- builder: resolves deps with uv; never ships -------------------------------
FROM python:3.14-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
# UV_COMPILE_BYTECODE stays off: .pyc caches are ~14 MB of the venv and are not needed
# to run (CPython regenerates them in memory). plan.md §4 asks for no __pycache__ layers.
ENV UV_COMPILE_BYTECODE=0 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock* ./
# --extra postgres is warranted from Phase 4 on: compose.yaml runs this image against
# Postgres, and code now uses the driver (storage_postgres.py / archive_postgres.py /
# alembic migrations). It was correctly absent before that, when nothing imported it.
# The stdio install stays thin — those modules are imported lazily, only when DATABASE_URL
# is set, so `pip install pse-edge-mcp` without the extra still runs.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev --no-editable --extra postgres --extra auth || \
    uv sync --no-install-project --no-dev --no-editable --extra postgres --extra auth
COPY src ./src
COPY README.md ./
# --no-editable installs the project as a real wheel into the venv, so the runtime
# stage needs no copy of src/ and no .pth indirection. An editable install is a
# development convenience and has no place in a shipped image.
RUN --mount=type=cache,target=/root/.cache/uv uv sync --no-dev --no-editable --extra postgres --extra auth
# Defensive: strip any bytecode a wheel brought with it, before the venv is copied out.
RUN find /app/.venv -name '__pycache__' -type d -prune -exec rm -rf {} + \
 && find /app/.venv -name '*.pyc' -delete

# ---- runtime: slim, non-root, no build tools, no secrets -----------------------
FROM python:3.14-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH="/app/.venv/bin:$PATH"
# pip ships in the base image, but nothing at runtime needs a package manager; removing
# it means a compromised container cannot install anything. This is attack-surface
# reduction, NOT a size win — those bytes live in the base layer and stay in the image.
RUN rm -rf /usr/local/lib/python3.*/site-packages/pip* /usr/local/bin/pip* \
 && groupadd -r app && useradd -r -g app -d /app app
WORKDIR /app
COPY --from=builder --chown=app:app /app/.venv /app/.venv
# Migrations ship with the image because applying them is part of operating it — compose
# runs `alembic upgrade head` from this same image before the app starts. A few KB, and the
# alternative (schema created at runtime) is exactly what plan §5 rules out.
COPY --chown=app:app alembic.ini ./
COPY --chown=app:app migrations ./migrations
USER app
EXPOSE 8000
# Secrets (DATABASE_URL etc.) arrive at runtime via env/env_file — never baked in.
ENTRYPOINT ["pse-edge-mcp"]
CMD ["--http", "--port", "8000"]
