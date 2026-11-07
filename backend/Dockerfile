# syntax=docker/dockerfile:1
FROM python:3.12-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.12.3 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app
ARG INSTALL_DEV=false
COPY pyproject.toml uv.lock ./
RUN if [ "$INSTALL_DEV" = "true" ]; then \
      uv sync --frozen; \
    else \
      uv sync --frozen --no-dev; \
    fi

FROM python:3.12-slim AS runtime
ARG UID=1000
ARG GID=1000
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"
RUN groupadd --gid "${GID}" app \
 && useradd --uid "${UID}" --gid "${GID}" --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin app
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY pyproject.toml alembic.ini ./
COPY alembic ./alembic
COPY sounding ./sounding
# Pre-compiled: the root filesystem is read-only, so Python cannot write .pyc files at run time.
RUN python -m compileall -q sounding alembic
USER app
# The worker and web entry points arrive in later phases (specs/phase1.md); the CLI works now.
CMD ["python", "-m", "sounding", "status"]
