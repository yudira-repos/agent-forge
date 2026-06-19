# ── Stage 1: builder ──────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /app

# Install build deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libpq-dev && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
COPY agentforge/ ./agentforge/

# Install the framework + all production deps
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir \
        ".[all]" \
        fastapi \
        uvicorn[standard] \
        sqlalchemy[asyncio] \
        asyncpg \
        aiosqlite \
        pydantic-settings \
        python-jose[cryptography] \
        passlib[bcrypt] \
        slowapi \
        structlog \
        prometheus-fastapi-instrumentator \
        httpx \
        authlib \
        alembic \
        "langgraph>=0.2" \
        "langchain-anthropic>=0.2" \
        "langchain-core>=0.2" \
        "opentelemetry-api>=1.24" \
        "opentelemetry-sdk>=1.24" \
        "opentelemetry-exporter-otlp-proto-http>=1.24" \
        "opentelemetry-semantic-conventions>=0.45b0"


# ── Stage 2: runtime ──────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

WORKDIR /app

# Runtime system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 curl && rm -rf /var/lib/apt/lists/*

# Copy installed packages from builder
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy application code
COPY agentforge/ ./agentforge/
COPY ui/ ./ui/
COPY examples/ ./examples/
COPY deploy/docker/start.sh ./start.sh

# Non-root user for security
RUN chmod +x ./start.sh && \
    useradd -m -u 1001 agentforge && chown -R agentforge:agentforge /app
USER agentforge

# Health check (must match the port uvicorn binds to)
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD sh -c 'curl -f "http://localhost:${PORT:-8000}/health" || exit 1'

EXPOSE 8000

# Railway: domain target port must match PORT (see deploy logs for "starting on 0.0.0.0:XXXX").
CMD ["./start.sh"]
