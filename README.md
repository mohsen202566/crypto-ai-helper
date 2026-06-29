# Crypto AI Helper 1H — Wave Exit Fix

این بسته برای نسخه ۱ ساعته است و عمداً حساسیت خروج آن مثل ۵ دقیقه‌ای نیست.

## قفل‌های پیاده‌شده

- AI Exit قبل از ۱۰ دقیقه فعال بودن پوزیشن اجرا نمی‌شود.
- TP به صورت پیش‌فرض Target Zone ذهنی است، نه خروج اجباری.
- خروج با یک ضعف انجام نمی‌شود؛ حداقل ۳ ضعف معتبر لازم است.
- برگشت کوچک با بافر نویز تطبیقی بر اساس ATR همان ارز/جهت فیلتر می‌شود.
- Giveback باید از نویز طبیعی بیشتر شود.
- بعد از AI_EXIT، قفل خشک ۲ ساعته وجود ندارد؛ فقط همان موج قبلی بلاک می‌شود.
- اگر پولبک/ریست واقعی یا شکست تازه ساخته شود، همان ارز/جهت دوباره می‌تواند سیگنال بدهد.

## فایل‌های تغییر کرده

- config.py
- exit_engine.py
- monitor.py
- storage.py
- trade_manager.py
- toobit_client.py
- tp_sl_result_engine.py
- bot_ui.py

## تنظیمات پیشنهادی .env

```env
AI_EXIT_ENABLED=true
AI_EXIT_TP_IS_TARGET_ZONE=true
AI_EXIT_MIN_ACTIVE_SECONDS=600
AI_EXIT_TARGET_ZONE_RATIO=0.95
AI_EXIT_WEAKNESS_CONFIRMATIONS=3
AI_EXIT_NOISE_ATR_MULTIPLIER=0.35
AI_EXIT_MIN_GIVEBACK_PCT=0.0012
AI_EXIT_GIVEBACK_RATIO=0.55
AI_EXIT_RISKY_GIVEBACK_RATIO=0.48
AI_EXIT_MIN_PROFIT_PCT=0.0035
AI_EXIT_BREAKEVEN_BUFFER_PCT=0.0004
AI_EXIT_DAMAGE_CONTROL_ADVERSE_RATIO=0.60
AI_EXIT_REVERSAL_TICKS=8
AI_EXIT_RECENT_TICKS=48

AI_REENTRY_COOLDOWN_SECONDS=900
AI_REENTRY_REQUIRE_NEW_SETUP=true
AI_REENTRY_MIN_RESET_ATR=0.35

TOOBIT_PLACE_REAL_TP=false
```

اگر خواستی TP واقعی هم روی Toobit ثبت شود:

```env
TOOBIT_PLACE_REAL_TP=true
AI_EXIT_TP_IS_TARGET_ZONE=false
```

## اجرای VPS

```bash
cd /root/crypto-ai-helper
python3 -m py_compile *.py
sudo systemctl restart crypto-bot.service
sudo systemctl status crypto-bot.service --no-pager -l
journalctl -u crypto-bot.service -f
```

## چک دیتابیس بعد از اجرا

```bash
sqlite3 -header -column data/crypto_ai_helper_1h.sqlite3 \
"select id,created_at,symbol_name,direction,status,score,entry,tp,sl,exit_price,ai_exit_status,ai_exit_score,giveback_pct,approx_pnl,result_source
 from signals
 order by id desc
 limit 20;"
```

```bash
sqlite3 -header -column data/crypto_ai_helper_1h.sqlite3 \
"select symbol_name,direction,code,count(*) as n,max(created_at) as last_seen
 from rejection_log
 where code='SAME_WAVE_COOLDOWN'
 group by symbol_name,direction,code;"
```

## تست انجام‌شده

- py_compile روی فایل‌های تغییرکرده بدون خطا انجام شد.
- migration دیتابیس تست شد و ستون‌های AI Exit ساخته شدند.
- Direction / ai_controller دست زده نشده تا خطای قفل‌شدن سیگنال مثل نسخه ۵ دقیقه‌ای تکرار نشود.
