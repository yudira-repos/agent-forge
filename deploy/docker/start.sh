#!/bin/sh
set -e

# Railway injects PORT. The public domain target port must match this value.
if [ -z "$PORT" ]; then
  PORT=8000
  echo "WARNING: PORT not set — using default 8000"
fi
WORKERS="${WORKERS:-1}"

echo "=== AgentForge startup ==="
echo "PORT=${PORT}"
echo "WORKERS=${WORKERS}"
echo "Railway domain target port must equal ${PORT}"
echo "=========================="

exec uvicorn ui.backend.main:app \
  --host 0.0.0.0 \
  --port "$PORT" \
  --workers "$WORKERS" \
  --proxy-headers \
  --forwarded-allow-ips="*"
