# syntax=docker/dockerfile:1.7
ARG PYTHON_IMAGE=python:3.12.11-slim-bookworm
FROM ${PYTHON_IMAGE} AS builder

ARG UV_VERSION=0.12.5
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/codito-venv

WORKDIR /build
RUN python -m pip install --no-cache-dir "uv==${UV_VERSION}"

COPY pyproject.toml uv.lock ./
COPY packages/protocol/pyproject.toml packages/protocol/README.md packages/protocol/
COPY relay/pyproject.toml relay/
COPY agent/pyproject.toml agent/
RUN uv sync --frozen --no-dev --package codito-relay --no-install-workspace

COPY packages/protocol packages/protocol
COPY relay relay
RUN uv sync --frozen --no-dev --package codito-relay --no-editable \
    && /opt/codito-venv/bin/python relay/manage.py collectstatic --noinput

ARG PYTHON_IMAGE=python:3.12.11-slim-bookworm
FROM ${PYTHON_IMAGE} AS runtime

ARG VERSION=0.0.0-dev
ARG VCS_REF=unknown
LABEL org.opencontainers.image.title="Codito Relay" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.source="https://github.com/akbaramd/CoditoMcp"

ENV PATH=/opt/codito-venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DJANGO_DEBUG=false \
    FORWARDED_ALLOW_IPS=127.0.0.1 \
    PORT=8094

RUN groupadd --system --gid 10001 codito \
    && useradd --system --uid 10001 --gid codito --home-dir /nonexistent --shell /usr/sbin/nologin codito

COPY --from=builder /opt/codito-venv /opt/codito-venv
COPY --from=builder --chown=codito:codito /build/relay /app/relay

WORKDIR /app
USER 10001:10001
EXPOSE 8094
STOPSIGNAL SIGTERM

CMD ["python", "-m", "uvicorn", "codito_relay.asgi:application", "--app-dir", "/app/relay", "--host", "0.0.0.0", "--port", "8094", "--workers", "2", "--proxy-headers", "--no-access-log", "--ws-ping-interval", "20", "--ws-ping-timeout", "20", "--timeout-graceful-shutdown", "30"]
