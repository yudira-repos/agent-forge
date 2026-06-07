#!/bin/sh
set -e

PORT="${PORT:-8000}"
WORKERS="${WORKERS:-1}"

echo "AgentForge starting on 0.0.0.0:${PORT} (workers=${WORKERS})"
echo "Ensure Railway public domain target port matches ${PORT}"

exec uvicorn ui.backend.main:app \
  --host 0.0.0.0 \
  --port "$PORT" \
  --workers "$WORKERS"
