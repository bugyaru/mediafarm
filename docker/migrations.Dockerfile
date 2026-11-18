FROM python:3.12-slim-bookworm AS builder
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY packages ./packages

RUN apt-get update && apt-get install -y --no-install-recommends build-essential libnfs-dev \
 && rm -rf /var/lib/apt/lists/*

RUN uv sync --frozen --package mf-core --no-dev

FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends libpq5 ca-certificates libnfs13 \
 && rm -rf /var/lib/apt/lists/* && useradd -u 10001 -m -d /app app
COPY --from=builder /app/.venv /app/.venv
COPY packages/mf-core /app/packages/mf-core
COPY migrations /app/migrations
COPY alembic.ini /app/alembic.ini
ENV PATH="/app/.venv/bin:$PATH" PYTHONPATH="/app/packages/mf-core"
USER 10001
WORKDIR /app
CMD ["alembic", "upgrade", "head"]