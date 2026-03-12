FROM python:3.12-slim

WORKDIR /app

# Install uv then use it to install the package into the system Python
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY pyproject.toml ./
COPY src/ ./src/

RUN uv pip install --system .

# Non-root user for security
RUN useradd --create-home --shell /bin/bash bridge
USER bridge

ENV CONFIG_PATH=/config/config.yaml

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import tesira_bridge" || exit 1

ENTRYPOINT ["python", "-m", "tesira_bridge.main"]
