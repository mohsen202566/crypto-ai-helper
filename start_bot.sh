#!/bin/bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR" || exit 1

if [ -f ".env" ]; then
  set -a
  # shellcheck disable=SC1091
  source ".env"
  set +a
fi

export PYTHONUNBUFFERED=1

if [ -x "$PROJECT_DIR/.venv/bin/python" ]; then
  PYTHON="$PROJECT_DIR/.venv/bin/python"
elif [ -x "$PROJECT_DIR/venv/bin/python" ]; then
  PYTHON="$PROJECT_DIR/venv/bin/python"
else
  PYTHON="$(command -v python3 || true)"
fi

if [ -z "${PYTHON:-}" ] || [ ! -x "$PYTHON" ]; then
  echo "ERROR: python executable not found. Create venv or install python3." >&2
  exit 1
fi

exec "$PYTHON" "$PROJECT_DIR/main.py"
