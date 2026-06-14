#!/bin/bash
cd /root/crypto-ai-helper
source venv/bin/activate
set -a
source .env
set +a
exec python3 -u bot.py
