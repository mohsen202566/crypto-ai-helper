#!/bin/bash
cd /root/crypto-ai-helper || exit 1
set -a
source .env
set +a
exec /root/crypto-ai-helper/venv/bin/python /root/crypto-ai-helper/main.py
