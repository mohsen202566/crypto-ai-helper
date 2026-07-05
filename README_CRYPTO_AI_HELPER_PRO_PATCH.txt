Crypto AI Helper Pro Patch - Regime + Normal/Real/Reject

هدف:
- حفظ ۳۰ ارز ثابت، دستورات تلگرام، Toobit execution، دیتای SQLite و منطق سیگنال فعلی.
- حذف Watch از سیگنال‌های جدید.
- خروجی جدید: NORMAL / NORMAL_CONTROLLED / REAL / REJECT.
- تقویت تشخیص رفتار بازار قبل از قضاوت سیگنال.
- نگه‌داشتن یادگیری چندسطحی و Forensic AI.
- توسعه shadow_tests به Exit Replay با net_profit/exit_price/MFE/MAE.

تغییرهای اصلی:
1) market_state.py
   - Regime Detector جدید با TREND/RANGE/NOISE/BREAKOUT/FAKE_BREAKOUT/CLIMAX/REVERSAL/HIGH_VOLATILITY/LOW_VOLUME.
   - استفاده از StopOnly indicators فقط برای تشخیص رفتار و ریسک، نه ساخت سیگنال خام.

2) ai_brain.py
   - DecisionLayer اضافه شد.
   - Real فقط وقتی Context + Range + Regime اجازه بدهند.
   - حالت‌های ریسکی به NORMAL_CONTROLLED تبدیل می‌شوند، نه Watch.
   - Reject همچنان NO_SIGNAL می‌ماند.

3) safety_layer.py / adaptive_fix_engine.py / risk_guard.py
   - Watch حذف شد.
   - WATCH_ONLY قدیمی به REJECT تبدیل می‌شود.
   - ریسک‌های قابل کنترل → NORMAL_CONTROLLED.
   - ریسک‌های غیرقابل معامله → REJECT.

4) storage.py
   - recommended_action قدیمی WATCH_ONLY به REJECT مهاجرت داده می‌شود.
   - shadow_tests با exit_price/net_profit/mfe_pct/mae_pct توسعه داده شد.
   - PnL اصلی فقط Real+Normal است.
   - Watch قدیمی فقط legacy_watch/legacy_watch_pnl گزارش می‌شود و در PnL قابل معامله دخالت ندارد.

5) monitor.py / trade_manager.py / telegram_bot.py
   - سیگنال Watch جدید ساخته نمی‌شود.
   - result_source جدید فقط normal / normal_on_real / toobit_real است.
   - پیام سیگنال تصمیم NORMAL_CONTROLLED/REAL/NORMAL را نشان می‌دهد.

نصب روی سرور:
cd /root/crypto-ai-helper || exit 1
sudo systemctl stop crypto-bot.service
mkdir -p /root/crypto_ai_helper_backup_$(date +%F_%H%M%S)
BACKUP_DIR=$(ls -td /root/crypto_ai_helper_backup_* | head -1)
cp .env "$BACKUP_DIR/.env" 2>/dev/null || true
cp fundamental_events.json "$BACKUP_DIR/fundamental_events.json" 2>/dev/null || true
cp -a data "$BACKUP_DIR/data" 2>/dev/null || true
unzip -o /root/crypto_ai_helper_pro_patch.zip -d /root/crypto-ai-helper
cp "$BACKUP_DIR/.env" .env 2>/dev/null || true
[ -f fundamental_events.json ] || echo '{"events": []}' > fundamental_events.json
chmod +x start_bot.sh
python3 -m py_compile *.py
sudo systemctl restart crypto-bot.service
sudo systemctl status crypto-bot.service --no-pager -l
journalctl -u crypto-bot.service -n 100 --no-pager

نکته مهم:
- فایل data/bot.db پاک نمی‌شود.
- reset_stats همچنان اگر خودتان دستور «حذف آمار تایید» بدهید آمار را پاک می‌کند؛ بدون آن هیچ دیتایی حذف نمی‌شود.
