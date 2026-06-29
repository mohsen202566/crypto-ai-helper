#!/bin/bash
set -e
cd "$(dirname "$0")" || exit 1

if [ -f .env ]; then
  set -a
  source .env
  set +a
fi

if [ -x "venv/bin/python" ]; then
  exec "venv/bin/python" main.py
elif [ -x "/root/venv/bin/python" ]; then
  exec "/root/venv/bin/python" main.py
else
  exec python3 main.py
fi
