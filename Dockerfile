FROM python:3.12-slim AS builder

WORKDIR /app

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY pyproject.toml ./
COPY src/ ./src/

RUN uv sync --no-dev


FROM python:3.12-slim

WORKDIR /app

# Non-root user for security
RUN useradd --create-home --shell /bin/bash bridge
USER bridge

COPY --from=builder --chown=bridge /app /app

ENV PYTHONPATH=/app/src
ENV CONFIG_PATH=/config/config.yaml

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import socket; s=socket.create_connection(('localhost', 9000), 2)" || exit 1

ENTRYPOINT ["python", "-m", "tesira_bridge.main"]
