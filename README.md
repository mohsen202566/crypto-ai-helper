# crypto-ai-helper

ربات فارسی اسکالپ ۵ دقیقه‌ای کریپتو با تحلیل کاملاً تکنیکال و امتیازدهی ساده بر اساس فقط سه اندیکاتور:

```text
RSI + MACD + ADX
```

ربات داده کندل‌ها را از OKX می‌گیرد، سیگنال‌ها را مانیتور می‌کند، نتیجه TP/SL را ثبت می‌کند و در صورت فعال بودن ترید واقعی، پوزیشن را از طریق Toobit باز می‌کند.

---

## ویژگی‌های اصلی

```text
تایم‌فریم: 5m
اسکن بازار: هر 20 ثانیه
مانیتورینگ سیگنال‌ها: هر 5 ثانیه
امتیاز سیگنال: از 100
حداقل امتیاز قابل قبول: 80
حداقل ADX قابل قبول: 20
نوع تحلیل: فقط RSI / MACD / ADX
داده بازار: فقط OKX
اجرای پوزیشن واقعی: فقط Toobit
نوع مارجین: Isolated
TP و SL: همراه پوزیشن واقعی، نه جداگانه
```

هیچ لایه اضافه‌ای مثل EMA، ATR، Volume، Breakout، Market Structure، Late Entry، فیلتر خبری یا خروج هوشمند داخل منطق سیگنال وجود ندارد.

---

## ساختار فایل‌ها

```text
config.py           تنظیمات اصلی، واچ‌لیست، تایم‌فریم، حد امتیاز، TP/SL، مسیر دیتابیس و تلگرام
okx_data.py         گرفتن کندل 5m و قیمت لحظه‌ای از OKX
indicators.py       محاسبه فقط RSI، MACD و ADX
scorer.py           امتیازدهی LONG/SHORT از 100 و ساخت Entry / TP / SL
storage.py          دیتابیس SQLite، تنظیمات پنل، سیگنال‌ها، آمار و اسلات‌ها
trade_manager.py    تصمیم واقعی/عادی، کنترل اسلات، کنترل پوزیشن تکراری و ارسال سفارش واقعی
monitor.py          مانیتورینگ همه سیگنال‌ها و ثبت TP/SL
bot_ui.py           پیام‌های فارسی تلگرام، پنل، آمار و دستورات کاربر
main.py             اجرای اصلی ربات، اسکن بازار و مانیتورینگ

toobit_client.py    اتصال به Toobit، موجودی، پوزیشن، لوریج، ایزوله، سفارش واقعی و PnL
requirements.txt    پکیج‌های لازم برای اجرا
README.md           همین فایل راهنما
```

---

## واچ‌لیست

واچ‌لیست شامل ۱۰ ارز نقدشونده و مناسب‌تر برای اسکالپ ۵ دقیقه‌ای است و بیت‌کوین و اتریوم داخل آن نیستند.

```text
SOL
XRP
DOGE
ADA
LTC
BCH
LINK
AVAX
DOT
TRX
```

فرمت OKX و Toobit در `config.py` جدا تعریف شده است:

```text
OKX:    SOL-USDT-SWAP
Toobit: SOL-SWAP-USDT
```

---

## منطق امتیازدهی

امتیاز هر جهت از ۱۰۰ محاسبه می‌شود:

```text
RSI   = 30 امتیاز
MACD  = 40 امتیاز
ADX   = 30 امتیاز
Total = 100 امتیاز
```

قانون صدور سیگنال:

```text
اگر ADX < 20  => رد کامل سیگنال
اگر LONG_SCORE >= 80 یا SHORT_SCORE >= 80 => صدور سیگنال
اگر هر دو جهت امتیاز بگیرند => جهت با امتیاز بالاتر انتخاب می‌شود
در غیر این صورت => بدون سیگنال
```

---

## TP و SL

TP و SL ثابت و ساده هستند:

```text
TP = 0.6%
SL = 0.4%
```

برای LONG:

```text
TP = Entry * 1.006
SL = Entry * 0.996
```

برای SHORT:

```text
TP = Entry * 0.994
SL = Entry * 1.004
```

---

## حالت‌های سیگنال

### سیگنال عادی

وقتی ترید واقعی خاموش باشد یا امکان باز کردن پوزیشن واقعی وجود نداشته باشد، سیگنال به‌صورت عادی ثبت می‌شود و همچنان تا TP یا SL مانیتور می‌شود.

### سیگنال واقعی

وقتی ترید واقعی فعال باشد، ربات قبل از ارسال سفارش بررسی می‌کند:

```text
ترید فعال باشد
اسلات خالی وجود داشته باشد
برای همان ارز سیگنال واقعی باز وجود نداشته باشد
برای همان ارز در Toobit پوزیشن باز وجود نداشته باشد
```

اگر همه شروط درست باشد:

```text
Margin Mode روی ISOLATED تنظیم می‌شود
Leverage تنظیم و تایید می‌شود
پوزیشن Market با TP و SL همراه باز می‌شود
بعد از 70 ثانیه بررسی می‌شود که پوزیشن واقعاً باز شده یا نه
اگر باز نشده باشد، اسلات آزاد می‌شود
```

اگر هر شرطی برقرار نباشد، سیگنال واقعی باز نمی‌شود و به سیگنال عادی تبدیل می‌شود.

---

## پنل فارسی

پنل با دستور `/پنل` نمایش داده می‌شود و شامل این موارد است:

```text
وضعیت ترید: فعال / خاموش
مارجین قابل استفاده Toobit
دلار هر پوزیشن
لوریج
حداکثر پوزیشن
اسلات‌های پر
اسلات‌های خالی
سود/ضرر امروز واقعی
سود/ضرر امروز تقریبی
```

سود/ضرر واقعی امروز از Toobit خوانده می‌شود. اگر خواندن آن ممکن نباشد، مقدار دیتابیس داخلی نمایش داده می‌شود.

---

## دستورات فارسی

```text
/پنل
/ترید_فعال
/ترید_خاموش
/ترید_دلار 50
/ترید_لوریج 10
/حداکثر_پوزیشن 5
/آمار
/آمار 3
/آمار 7
```

محدودیت‌ها:

```text
دلار هر پوزیشن: 1 تا 10000 USDT
لوریج: 1 تا 100
حداکثر پوزیشن همزمان: 1 تا 100
آمار: 1 تا 7 روز
```

---

## آمار

آمار عادی و واقعی جدا نمایش داده می‌شود.

برای سیگنال‌های عادی:

```text
تعداد کل
تعداد TP
تعداد SL
تعداد باز
وین‌ریت
سود/ضرر تقریبی
```

برای سیگنال‌های واقعی:

```text
تعداد کل
تعداد TP
تعداد SL
تعداد باز
وین‌ریت
سود/ضرر واقعی
```

نتیجه هر سیگنال به‌صورت ریپلای روی پیام اصلی سیگنال ارسال می‌شود.

---

## فایل .env

کلیدها داخل کد نوشته نمی‌شوند و باید روی VPS داخل `.env` یا environment تنظیم شوند.

نمونه:

```env
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
TELEGRAM_CHAT_ID=your_telegram_chat_id

TOOBIT_API_KEY=your_toobit_api_key
TOOBIT_SECRET_KEY=your_toobit_secret_key
TOOBIT_BASE_URL=https://api.toobit.com
```

نام‌های قدیمی `TOBIT_API_KEY`، `TOBIT_SECRET_KEY` و `TOBIT_BASE_URL` هم برای سازگاری پشتیبانی می‌شوند، ولی نام درست پیشنهادی `TOOBIT_*` است.

متغیرهای اختیاری:

```env
BOT_DATA_DIR=data
BOT_DB_PATH=data/bot.sqlite3
OKX_BASE_URL=https://www.okx.com
```

---

## نصب روی VPS

```bash
cd /root/crypto-ai-helper
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

تست کدها:

```bash
python -m py_compile *.py
```

اجرای ربات:

```bash
python main.py
```

---

## اجرای پیشنهادی با systemd

نمونه سرویس:

```ini
[Unit]
Description=Crypto AI Helper Scalp 5m Bot
After=network.target

[Service]
WorkingDirectory=/root/crypto-ai-helper
EnvironmentFile=/root/crypto-ai-helper/.env
ExecStart=/root/crypto-ai-helper/.venv/bin/python /root/crypto-ai-helper/main.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

دستورهای مدیریت سرویس:

```bash
sudo systemctl daemon-reload
sudo systemctl enable crypto-bot.service
sudo systemctl restart crypto-bot.service
sudo systemctl status crypto-bot.service --no-pager
journalctl -u crypto-bot -n 100 --no-pager
```

---

## نکات مهم اجرا

```text
اول با ترید خاموش اجرا کن.
اول پنل را چک کن.
بعد از درست بودن موجودی و اتصال Toobit، با دلار کم تست کن.
برای هر ارز فقط یک پوزیشن واقعی مجاز است.
وقتی اسلات‌ها پر باشند، سیگنال‌ها عادی می‌شوند.
همه سیگنال‌های عادی و واقعی مانیتور می‌شوند.
```

---

## تست نهایی قبل از استفاده واقعی

```bash
python -m py_compile *.py
python main.py
```

اگر ربات بدون خطا اجرا شد و پنل مقدار مارجین Toobit را نشان داد، اتصال اصلی آماده است.
