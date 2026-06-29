#!/usr/bin/env bash
set -Eeuo pipefail

# اجرای ربات اسکالپ کلاسیک ۵ دقیقه‌ای
# این اسکریپت از هر مسیری اجرا شود، خودش وارد پوشه پروژه می‌شود.

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

if [ ! -f ".env" ]; then
  echo "❌ فایل .env پیدا نشد. اول فایل .env.example را به .env کپی و تنظیم کن."
  exit 1
fi

set -a
# shellcheck disable=SC1091
source .env
set +a

if [ -x "$PROJECT_DIR/venv/bin/python" ]; then
  PYTHON_BIN="$PROJECT_DIR/venv/bin/python"
elif [ -x "$PROJECT_DIR/.venv/bin/python" ]; then
  PYTHON_BIN="$PROJECT_DIR/.venv/bin/python"
else
  PYTHON_BIN="python3"
fi

exec "$PYTHON_BIN" "$PROJECT_DIR/main.py"
