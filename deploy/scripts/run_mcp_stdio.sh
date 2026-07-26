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

export PYTHONPATH="${ROOT}${PYTHONPATH:+:$PYTHONPATH}"

if command -v uv >/dev/null 2>&1; then
  exec uv run deepwiki mcp
elif [[ -x "$ROOT/.venv/bin/deepwiki" ]]; then
  exec "$ROOT/.venv/bin/deepwiki" mcp
else
  exec python3 -m local_deepwiki.server
fi
