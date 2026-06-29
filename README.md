# Crypto AI Helper 1H Soft AI

ربات Crypto Futures یک‌ساعته با تحلیل OKX و اجرای واقعی فقط روی Toobit.

این نسخه از منطق Soft AI نسخه ۵ دقیقه‌ای ساخته شده، اما برای اسکالپ ۱ ساعته هماهنگ شده است:

- تایم اصلی تصمیم: `1H`
- کانتکست روند: `4H`
- ورود دقیق‌تر: `30m` و تایید کوتاه‌تر `15m`
- مانیتور TP/SL: هر ۱۰ ثانیه
- Full Scan: هر ۵ دقیقه
- Watch Scan: هر ۳۰ ثانیه
- دیتای تحلیل: OKX
- اجرای واقعی: Toobit Futures

## قانون اصلی AI

بجز کنترل‌های پنل ترید، همه چیز زیر تصمیم نرم و یادگیرنده AI است.

کنترل‌های کاربر:

- ترید روشن/خاموش
- دلار/مارجین هر معامله
- لوریج
- حداکثر پوزیشن/اسلات

تصمیم‌های AI:

- Watch / Normal / Real
- Threshold سیگنال
- Threshold واقعی
- Entry Precision
- Entry Quality
- TP/SL
- Early Exit
- Pattern وزن‌دهی
- Noise / Market Mode
- Real Block یا Normal شدن
- سخت/نرم شدن برای هر ارز، جهت، الگو، بازه امتیاز، و حالت بازار

هیچ reject تحلیلی خشک وجود ندارد. ضعف تحلیلی فقط تبدیل می‌شود به Watch، Normal، Internal Learning یا Real Block. فقط Safety اجرای واقعی hard است: API، اسلات، duplicate، sync قیمت OKX/Toobit، net profit واقعی، و تایید سفارش Toobit.

## اندیکاتورهای 1H

این نسخه اندیکاتورها را برای ۱ ساعت هماهنگ کرده است:

- EMA 20 / 50 / 200
- RSI 14
- MACD 12/26/9
- ADX 14 + DI+/DI-
- ATR 14
- Bollinger Bands 20/2
- Relative Volume
- Rolling VWAP 24 کندلی
- حمایت/مقاومت و Order Block روی ساختار 1H

## ارزهای فعال ۱۲تایی

- SOL
- XRP
- DOGE
- AVAX
- LINK
- ADA
- SUI
- LTC
- NEAR
- APT
- ARB
- OP

BTC و ETH برای context بازار استفاده می‌شوند، نه سیگنال اصلی.

## Threshold شروع

این‌ها فقط مقدار شروع‌اند، نه قانون دائمی:

- `BASE_SIGNAL_THRESHOLD=68`
- `BASE_REAL_THRESHOLD=76`

بعد از یادگیری، AI برای هر symbol + direction + pattern + entry_quality + market_mode + score bucket خودش تنظیم می‌کند.

## دستورات تلگرام

- `پنل`
- `آمار`
- `آمار 7`
- `هوش`
- `یادگیری`
- `پیشنهاد`
- `ارزها`
- `شکار`
- `زنده`
- `ردها`
- `سیگنال‌ها`
- `ترید روشن`
- `ترید خاموش`
- `ترید دلار 20`
- `ترید لوریج 10`
- `حداکثر پوزیشن 3`
- `حذف آمار`
- `ریست یادگیری`
- `راهنما`

## نصب روی VPS

```bash
cd /root/crypto-ai-helper-1h
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
nano .env
python3 -m py_compile *.py
python3 main.py
```

برای systemd از سرویس فعلی پروژه می‌توانی فقط مسیر پوشه و نام سرویس را تغییر بدهی.

## چک زنده DB

```bash
sqlite3 -header -column data/crypto_ai_helper_1h.sqlite3 \
"select id,created_at,symbol_name,direction,score,threshold,real_threshold,signal_type,real_status,status,entry_quality,message_id from signals order by id desc limit 20;"
```

```bash
sqlite3 -header -column data/crypto_ai_helper_1h.sqlite3 \
"select symbol_name,direction,score,ai_confidence,updated_at,expires_at from watchlist order by score desc;"
```

## پیشنهاد دلار/لوریج

AI می‌تواند بر اساس نتایج ۱ ساعته، سود خالص Real، MAE/MFE و TP/SL پیشنهاد بدهد که دلار هر پوزیشن یا لوریج بهتر است کمتر/بیشتر شود.
این فقط پیشنهاد است؛ ربات هیچ‌وقت مارجین، لوریج، اسلات یا Trading ON/OFF را خودش تغییر نمی‌دهد. اعمال تغییر فقط با دستور پنل انجام می‌شود.
