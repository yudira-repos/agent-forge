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
        alembic


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

# Non-root user for security
RUN useradd -m -u 1001 agentforge && chown -R agentforge:agentforge /app
USER agentforge

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

EXPOSE 8000

# Run Alembic migrations then start the server
CMD ["sh", "-c", "alembic upgrade head && uvicorn ui.backend.main:app --host 0.0.0.0 --port 8000 --workers 4"]
