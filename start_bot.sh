#!/bin/bash
cd /root/crypto-ai-helper || exit 1

if [ -f .env ]; then
  set -a
  source .env
  set +a
fi

if [ -x /root/crypto-ai-helper/venv/bin/python ]; then
  exec /root/crypto-ai-helper/venv/bin/python /root/crypto-ai-helper/main.py
elif [ -x /root/venv/bin/python ]; then
  exec /root/venv/bin/python /root/crypto-ai-helper/main.py
else
  exec python3 /root/crypto-ai-helper/main.py
fi
