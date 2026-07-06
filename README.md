# Crypto 4H Simple Toobit Bot

پروژه نهایی ریشه‌ای برای GitHub/VPS. فایل‌ها مستقیم در ریشه ZIP هستند و مستقیم می‌توانند در ریشه ریپو قرار بگیرند.

## فایل‌های حذف‌شده و ممنوع در این نسخه

این نسخه عمداً این فایل‌ها را ندارد تا در Git pull و آپدیت‌های VPS دردسر درست نکنند:

- `.env.example`
- `.gitignore`
- `run.sh`
- `__pycache__`
- هر فایل shell یا فایل مخفی اضافه

## نکته خیلی مهم

`toobit_client.py` از فایل قبلی پروژه کپی شده و منطقش تغییر داده نشده است؛ فقط اسم فایل استاندارد ریشه‌ای `toobit_client.py` است تا import مستقیم کار کند.

## قوانین قفل‌شده

- تحلیل فقط از OKX.
- اجرای واقعی فقط روی Toobit.
- حداکثر ۳۰ ارز در هر چرخه.
- نمادها بین OKX و Toobit با نگاشت داخلی هماهنگ می‌شوند.
- اگر خطای یک ارز رخ دهد، کل ربات نمی‌خوابد و بقیه ارزها ادامه می‌دهند.
- سیگنال فقط وقتی صادر می‌شود که جهت 1D و 4H همسو باشد.
- امتیاز از ۱۰۰ است.
- زیر ۷۰: بدون سیگنال.
- ۷۰ تا ۸۴: RR = 1.5.
- ۸۵ به بالا: RR = 2.
- SL فقط مخصوص 4H است؛ نه 5m و نه 1H.
- کارمزد رفت‌وبرگشت ثابت پیش‌فرض: 0.05 USDT.
- فعلاً حمایت/مقاومت، AI، DCA، Martingale، trailing و چند TP نداریم.
- اگر ترید خاموش باشد یا اسلات پر باشد، سیگنال فقط Normal ثبت می‌شود.
- اگر ترید روشن و اسلات آزاد باشد، Real روی Toobit باز می‌شود.
- اگر اسلات پر باشد، بعد از ۷۰ ثانیه Toobit چک می‌شود؛ اگر پوزیشن هنوز هست هیچ، اگر نیست اسلات آزاد می‌شود.
- مانیتورینگ TP/SL، PnL خام، PnL خالص، MFE/MAE، مدت و وضعیت Real/Normal را در SQLite ثبت می‌کند.

## فایل‌های اصلی

- `bot.py` / `main.py` — اجرای ربات و پنل تلگرام.
- `config.py` — تنظیمات از Environment Variables.
- `toobit_client.py` — کلاینت قبلی Toobit.
- `okx_data.py` — کندل و قیمت OKX.
- `strategy_4h_simple.py` — موتور سیگنال 4H.
- `runtime_safety_4h.py` — ضدکرش، خطای ارزها، ۳۰ ارز، اسلات و قانون ۷۰ ثانیه.
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

در systemd می‌توانی Environment را همان‌جا تعریف کنی تا نیازی به فایل `.env` داخل پروژه نباشد:

```ini
[Unit]
Description=Crypto 4H Simple Toobit Bot
After=network.target

[Service]
WorkingDirectory=/root/crypto-4h-bot
Environment=TELEGRAM_BOT_TOKEN=PUT_TOKEN_HERE
Environment=TELEGRAM_CHAT_ID=PUT_CHAT_ID_HERE
Environment=TOOBIT_API_KEY=PUT_KEY_HERE
Environment=TOOBIT_API_SECRET=PUT_SECRET_HERE
ExecStart=/root/crypto-4h-bot/.venv/bin/python /root/crypto-4h-bot/main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```
