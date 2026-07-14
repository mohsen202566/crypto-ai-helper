#!/usr/bin/env bash
set -Eeuo pipefail

BOT_DIR="${BOT_DIR:-/root/forex-signal-bot}"
SERVICE="forex-signal-bot.service"
ENV_FILE="/etc/forex-signal-bot.env"

cd "$BOT_DIR"
python3 -c 'import sys; raise SystemExit("Python 3.10+ required") if sys.version_info < (3, 10) else None'
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest -v self_test.py

if [[ ! -f "$ENV_FILE" ]]; then
  sudo cp .env.example "$ENV_FILE"
  sudo chmod 600 "$ENV_FILE"
  echo "Created $ENV_FILE — fill Telegram/Toobit credentials before starting."
fi

sudo cp forex-signal-bot.service "/etc/systemd/system/$SERVICE"
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE"
echo "Service installed. After editing $ENV_FILE, run: sudo systemctl start $SERVICE"
