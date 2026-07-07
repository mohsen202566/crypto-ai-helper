# Crypto 1H Trend Pullback Toobit Bot

نسخه ریشه‌ای برای GitHub/VPS. فایل‌ها مستقیم در ریشه ZIP هستند و مستقیم می‌توانند در ریشه ریپو قرار بگیرند.

## هدف نسخه جدید

این نسخه منطق قبلی 4H را به سیستم 1H تبدیل کرده است:

- جهت مادر از 4H گرفته می‌شود.
- ورود، استاپ و تیپی از 1H گرفته می‌شود.
- استاپ دیگر 4H نیست؛ `SL = ساختار 1H + بافر ATR`.
- TP با RR پیش‌فرض `1.5R` چیده می‌شود.
- سیگنال خیلی قوی، فقط اگر ADX رو به رشد باشد، می‌تواند `2R` بگیرد.
- ورود دیر، رنج، EMA صاف، قیمت بین EMA50/EMA200، و کندل‌های کشیده آخر روند رد می‌شوند.

## فایل‌های حذف‌شده و ممنوع در این نسخه

این نسخه عمداً این فایل‌ها را ندارد تا در Git pull و آپدیت‌های VPS دردسر درست نکنند:

- `.env.example`
- `.gitignore`
- `run.sh`
- `__pycache__`
- هر فایل shell یا فایل مخفی اضافه

## قوانین قفل‌شده

- تحلیل فقط از OKX.
- اجرای واقعی فقط روی Toobit.
- حداکثر پیش‌فرض ۵۰ ارز در هر چرخه.
- نمادها قبل از تحلیل با Toobit چک می‌شوند؛ اگر نماد در Toobit نبود، همان ارز رد می‌شود.
- اگر OKX برای نماد/کندل خطا بدهد، همان ارز رد می‌شود و کل ربات نمی‌خوابد.
- جهت 4H و 1H باید همسو باشد.
- امتیاز از ۱۰۰ است.
- زیر ۸۰: بدون سیگنال.
- ۸۰ تا ۸۴: RR = 1.5.
- ۸۵ به بالا: RR = 2 فقط وقتی ADX قوی و رو به رشد باشد؛ در غیر این صورت RR = 1.5.
- SL فقط مخصوص 1H است؛ نه 5m و نه 4H.
- کارمزد رفت‌وبرگشت ثابت پیش‌فرض: 0.05 USDT.
- فعلاً حمایت/مقاومت، AI، DCA، Martingale، trailing و چند TP نداریم.
- اگر ترید خاموش باشد یا اسلات پر باشد، سیگنال فقط Normal ثبت می‌شود.
- اگر ترید روشن و اسلات آزاد باشد، Real روی Toobit باز می‌شود.
- اگر اسلات پر باشد، بعد از ۷۰ ثانیه Toobit چک می‌شود؛ اگر پوزیشن هنوز هست هیچ، اگر نیست اسلات آزاد می‌شود.
- مانیتورینگ TP/SL، PnL خام، PnL خالص، MFE/MAE، مدت و وضعیت Real/Normal را در SQLite ثبت می‌کند.

## منطق نهایی سیگنال

### جهت لانگ

```text
Close_4H > EMA200_4H
EMA50_4H > EMA200_4H
EMA50_4H شیب مثبت
Close_1H > EMA200_1H
EMA50_1H > EMA200_1H
EMA50_1H شیب مثبت
+DI > -DI
```

### جهت شورت

```text
Close_4H < EMA200_4H
EMA50_4H < EMA200_4H
EMA50_4H شیب منفی
Close_1H < EMA200_1H
EMA50_1H < EMA200_1H
EMA50_1H شیب منفی
-DI > +DI
```

### فیلترهای رد معامله

```text
ADX 1H < 20 => رد
EMA50 صاف نسبت به ATR => رد
قیمت بین EMA50 و EMA200 => رد
فاصله قیمت از EMA20 بیشتر از 1.5ATR => رد
فاصله قیمت از EMA50 بیشتر از 2.5ATR => رد
۵ کندل از ۶ کندل اخیر هم‌رنگ جهت معامله => رد
ADX بالای 40 ولی در حال افت => رد
پولبک/تریگر معتبر نداشته باشد => رد
ریسک کمتر از 0.7ATR یا بیشتر از 2.2ATR => رد
```

### ورود

ورود روی بسته‌شدن کندل 1H انجام می‌شود؛ فقط وقتی قیمت پولبک به EMA20/EMA50 داده و کندل تأیید هم‌جهت بسته شده باشد.

### استاپ و تیپی

```text
LONG SL = min(swing_low_1H, EMA50_1H) - 0.2ATR
SHORT SL = max(swing_high_1H, EMA50_1H) + 0.2ATR
TP = Entry ± RR * Risk
```

## فایل‌های اصلی

- `bot.py` / `main.py` — اجرای ربات و پنل تلگرام.
- `config.py` — تنظیمات از Environment Variables.
- `toobit_client.py` — اجرای واقعی Toobit؛ دست‌نخورده و سازگار با نسخه قبلی.
- `okx_data.py` — کندل و قیمت OKX با retry و رد خطای هر نماد.
- `strategy_4h_simple.py` — موتور سیگنال 1H؛ نام فایل برای سازگاری با ایمپورت قبلی حفظ شده است.
- `runtime_safety_4h.py` — ضدکرش، خطای ارزها، ۵۰ ارز، اسلات و قانون ۷۰ ثانیه؛ نام فایل برای سازگاری حفظ شده است.
- `monitor.py` / `monitoring_result_4h.py` — ثبت نتیجه دقیق مانیتورینگ.
- `storage.py` — دیتابیس SQLite، آمار، تنظیمات، سیگنال‌ها.
- `telegram_ui.py` / `telegram_client.py` — پنل و پیام‌های تلگرام.
- `utils.py` — ابزارهای مشترک و نگاشت نمادها.

## نصب روی VPS بدون فایل env.example و بدون sh

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

متغیرها را مستقیم در VPS یا systemd تنظیم کن. نمونه export دستی:

```bash
export TELEGRAM_BOT_TOKEN="..."
export TELEGRAM_CHAT_ID="..."
export TOOBIT_API_KEY="..."
export TOOBIT_API_SECRET="..."
python main.py
```

## دستورات تلگرام

```text
ترید
ترید فعال
ترید خاموش
ترید دلار 10
ترید لوریج 10
حداکثر پوزیشن 3
سرمایه ترید 100
حداقل سود خالص 0.01
آمار
آمار 7
پوزیشن
کوین‌ها
وضعیت
راهنما
```

## اجرای دائمی با systemd، بدون فایل اضافه داخل پروژه

```ini
[Unit]
Description=Crypto 1H Trend Pullback Toobit Bot
After=network.target

[Service]
WorkingDirectory=/root/crypto-1h-bot
Environment=TELEGRAM_BOT_TOKEN=PUT_TOKEN_HERE
Environment=TELEGRAM_CHAT_ID=PUT_CHAT_ID_HERE
Environment=TOOBIT_API_KEY=PUT_KEY_HERE
Environment=TOOBIT_API_SECRET=PUT_SECRET_HERE
ExecStart=/root/crypto-1h-bot/.venv/bin/python /root/crypto-1h-bot/main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```
