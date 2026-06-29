#!/bin/bash
cd /root/crypto-ai-helper-1h || exit 1
set -a
source .env
set +a
exec /root/crypto-ai-helper-1h/venv/bin/python /root/crypto-ai-helper-1h/main.py
