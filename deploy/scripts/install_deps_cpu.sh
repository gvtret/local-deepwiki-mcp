#!/usr/bin/env bash
# Install local-deepwiki deps with CPU-only torch (no nvidia CUDA wheels).
# Host has no GPU — PyPI's default torch would pull ~several GB of nvidia-*.
# pyproject.toml pins torch via tool.uv.sources → download.pytorch.org/whl/cpu.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
export PATH="${HOME}/.local/bin:${PATH}"

if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="${HOME}/.local/bin:${PATH}"
fi

echo "=== uv sync (CPU torch from pytorch.org/whl/cpu) ==="
uv sync
uv pip install --python "$ROOT/.venv/bin/python" -r deploy/requirements-http.txt

echo "=== verifying no nvidia/cuda packages ==="
if uv pip list --python "$ROOT/.venv/bin/python" 2>/dev/null | grep -iE '^(nvidia-|cuda-)'; then
  echo "ERROR: nvidia/cuda packages present — refuse install" >&2
  uv pip list --python "$ROOT/.venv/bin/python" | grep -iE '^(nvidia-|cuda-)' >&2 || true
  exit 1
fi

"$ROOT/.venv/bin/python" -c "import torch; print('torch', torch.__version__, 'cuda', torch.cuda.is_available())"
echo "=== deps OK (CPU-only) ==="
