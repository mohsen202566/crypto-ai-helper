#!/usr/bin/env bash
set -Eeuo pipefail

BOT_DIR="${BOT_DIR:-/root/forex-signal-bot}"
SERVICE="${BOT_SERVICE:-forex-signal-bot.service}"
BRANCH="${BOT_BRANCH:-main}"

cd "$BOT_DIR"
python3 -c 'import sys; raise SystemExit("Python 3.10+ required") if sys.version_info < (3, 10) else None'

git pull origin "$BRANCH"

if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv .venv
fi

.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt

# The tests are offline and never send an exchange order.
.venv/bin/python -m unittest -v self_test.py
.venv/bin/python -m py_compile ./*.py
.venv/bin/python -m compileall -q .

sudo systemctl restart "$SERVICE"
sudo systemctl status "$SERVICE" --no-pager
journalctl -u "$SERVICE" -n 80 -f
