# syntax=docker/dockerfile:1.7
ARG PYTHON_IMAGE=python:3.12-slim-bookworm

FROM ${PYTHON_IMAGE} AS builder
ARG SERVICE=ui
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY packages ./packages
COPY services ./services
RUN uv sync --frozen --package mf-${SERVICE} --no-dev

FROM ${PYTHON_IMAGE}
ARG SERVICE=ui
RUN apt-get update && apt-get install -y --no-install-recommends \
      tini ca-certificates libpq5 curl \
 && rm -rf /var/lib/apt/lists/* \
 && useradd -u 10001 -m -d /app app
COPY --from=builder /app/.venv /app/.venv
COPY packages/mf-core /app/packages/mf-core
COPY services/${SERVICE} /app/services/${SERVICE}
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH="/app/packages/mf-core:/app/services/${SERVICE}" \
    PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER 10001
WORKDIR /app
ENTRYPOINT ["/usr/bin/tini", "--"]