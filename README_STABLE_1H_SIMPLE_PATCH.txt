Crypto AI Helper - Stable 1H Simple Patch

هدف این پچ:
- برگشت به منطق ساده و پایدار ربات خودت، بدون لایه‌های سنگین Pro.
- تایم تصمیم و ورود اصلی = 1H.
- تشخیص جهت قبل از ورود: BTC + 1D + 4H + 1H.
- ورود فقط در ناحیه خوب: لانگ نزدیک کف/پولبک، شورت نزدیک سقف/پولبک.
- رد کردن ورودهایی که حرکتشان شروع شده یا دیر شده.
- TP/SL هوشمند روی 1H، با ATR، کف/قله، حمایت/مقاومت و RR.
- حداقل سود خالص بعد از کارمزد = 0.02 USDT.
- PNL نتیجه‌ها، آمار و پنل بعد از کارمزد/بافر حساب می‌شود.
- فقط دو نوع سیگنال باقی می‌ماند: REAL و NORMAL.
- یادگیری حذف نشده، اما با کلید 1H جدا می‌شود و نباید جهت/ورود را دور بزند.

فایل‌های اصلی تغییرکرده:
- config.py
- ai_brain.py
- market_context.py
- entry_zone.py
- tp_sl_engine.py
- utils.py
- monitor.py
- learning_engine.py
- telegram_bot.py
- storage.py
- main.py
- okx_data.py
- indicators.py
- range_learning.py
- start_bot.sh

نصب روی سرور:
cd /root/crypto-ai-helper || exit 1
sudo systemctl stop crypto-bot.service

BACKUP_DIR="/root/crypto_ai_stable_1h_backup_$(date +%F_%H%M%S)"
mkdir -p "$BACKUP_DIR"
cp .env "$BACKUP_DIR/.env" 2>/dev/null || true
cp -a data "$BACKUP_DIR/data" 2>/dev/null || true

unzip -o /root/crypto_ai_helper_stable_1h_patch.zip -d /root/crypto-ai-helper
cp "$BACKUP_DIR/.env" .env 2>/dev/null || true
chmod +x start_bot.sh

python3 -m py_compile *.py
sudo systemctl restart crypto-bot.service
sudo systemctl status crypto-bot.service --no-pager -l
journalctl -u crypto-bot.service -n 100 --no-pager

نکته:
- data پاک نمی‌شود.
- اگر قبلاً دیتای 5m داشتی، پاک نمی‌شود؛ اما کلیدهای جدید یادگیری با 1H جدا ذخیره می‌شوند.
- اگر سیگنال کم شد، علتش سخت‌تر شدن Direction Gate و Entry Zone است، نه خرابی ربات.
