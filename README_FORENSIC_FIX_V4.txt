Crypto AI Helper - Forensic AI V4 fixes

Changes:
1) Stop-forensic-only indicators added:
   - ATR Percentile (ATRP)
   - Choppiness Index (CHOP)
   - Bollinger Width (BBW)
   - Keltner/Bollinger squeeze ratio (SQZ)
   - Donchian position/breakout (DONCH)
   These are stored/displayed for stop analysis only and are not used in RangeLearningEngine feature keys.

2) Adaptive treatment text fixed:
   - No more contradiction: if SL/TP was not actually changed, the message says the fix was rejected due to RR/net profit.
   - If SL/TP changes, final TP/SL/RR in the signal and the treatment text match.
   - TP_OK is not shown for a bad profile like TP 2 / SL 5; the previous/dominant failure cause remains.
   - Exact range sample and similar memory are separated in the treatment text.

3) Range learning zero-sample check:
   - If exact range samples are zero but symbol+direction has learning, the bot uses symbol+direction as a fallback for caution.
   - It never allows Real just from the fallback; exact range still learns normally when signals close.

4) PnL display fixed:
   - Real+Normal net PnL is separated from Watch learning PnL.
   - Total experimental PnL is still available, but Watch is no longer mixed into tradable PnL.

5) Stop guard text fixed:
   - It now says Real/Normal is limited and Watch is used for learning, not that the signal is hidden.

6) start_bot.sh path set for /root/crypto-ai-helper.

Install on server:
cd /root/crypto-ai-helper || exit 1
sudo systemctl stop crypto-bot.service
mkdir -p /root/crypto_ai_v4_backup_$(date +%F_%H%M%S)
BACKUP_DIR=$(ls -td /root/crypto_ai_v4_backup_* | head -1)
cp .env "$BACKUP_DIR/.env" 2>/dev/null || true
cp fundamental_events.json "$BACKUP_DIR/fundamental_events.json" 2>/dev/null || true
cp -a data "$BACKUP_DIR/data" 2>/dev/null || true
unzip -o /root/crypto_ai_helper_forensic_fix_v4.zip -d /root/crypto-ai-helper
cp "$BACKUP_DIR/.env" .env 2>/dev/null || true
[ -f fundamental_events.json ] || echo '{"events": []}' > fundamental_events.json
chmod +x start_bot.sh
python3 -m py_compile *.py
sudo systemctl restart crypto-bot.service
sudo systemctl status crypto-bot.service --no-pager -l
journalctl -u crypto-bot.service -n 100 --no-pager
