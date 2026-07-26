#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
export LOCAL_DEEPWIKI_ROOT="${LOCAL_DEEPWIKI_ROOT:-$ROOT}"

ENV_FILE="${LOCAL_DEEPWIKI_ENV_FILE:-$ROOT/.env}"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
fi

HOST="${LOCAL_DEEPWIKI_HTTP_HOST:-0.0.0.0}"
PORT="${LOCAL_DEEPWIKI_HTTP_PORT:-5555}"
SHUTDOWN_TIMEOUT="${LOCAL_DEEPWIKI_SHUTDOWN_TIMEOUT:-30}"

export PYTHONPATH="${ROOT}${PYTHONPATH:+:$PYTHONPATH}"

if [[ -x "$ROOT/.venv/bin/uvicorn" ]]; then
  exec "$ROOT/.venv/bin/uvicorn" deploy.http.mcp_http:app \
    --host "$HOST" \
    --port "$PORT" \
    --timeout-graceful-shutdown "$SHUTDOWN_TIMEOUT"
fi

if command -v uv >/dev/null 2>&1; then
  exec uv run uvicorn deploy.http.mcp_http:app \
    --host "$HOST" \
    --port "$PORT" \
    --timeout-graceful-shutdown "$SHUTDOWN_TIMEOUT"
fi

exec python3 -m uvicorn deploy.http.mcp_http:app \
  --host "$HOST" \
  --port "$PORT" \
  --timeout-graceful-shutdown "$SHUTDOWN_TIMEOUT"
