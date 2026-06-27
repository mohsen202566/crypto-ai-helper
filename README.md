# crypto-ai-helper

ربات فارسی اسکالپ کریپتو با هسته تحلیل تکنیکال ساده، سیگنال معمولی، مدیریت ترید واقعی Toobit، پنل فارسی، آمار سود/ضرر و مانیتورینگ دقیق TP/SL.

## ساختار فایل‌ها

```text
bot.py              اجرای اصلی ربات، دستورات فارسی، پنل، اسکن، مانیتورینگ
config.py           تنظیمات اصلی، کوین‌ها، وزن‌ها، محدودیت‌ها
strategy.py         هسته تحلیل تکنیکال و امتیازدهی LONG/SHORT
tp_sl_engine.py     محاسبه Entry / TP / SL / RR
state_store.py      ذخیره وضعیت، اسلات‌ها، آمار، سیگنال‌ها و معاملات
toobit_client.py    اتصال به Toobit، موجودی، پوزیشن، سفارش واقعی
start_bot.sh        اجرای ربات روی VPS
```

## منطق اصلی ربات

ربات دنبال‌کننده کندل نیست؛ هدف آن تشخیص زودهنگام جهت و قدرت حرکت برای اسکالپ است.

### Direction Score

```text
EMA20/50          30
RSI Slope         25
MACD Cross        25
Market Structure 20
```

قانون تصمیم:

```text
LONG_SCORE >= 60 و SHORT_SCORE <= 35  => LONG
SHORT_SCORE >= 60 و LONG_SCORE <= 35  => SHORT
غیر از این => NO_TRADE
```

### Strength Score

```text
ATR Expansion     15
Volume > MA20     15
Breakout          20
```

حداقل قدرت لازم برای ورود:

```text
25 از 50
```

## TP / SL

```text
Weak   RR = 1.0
Normal RR = 1.2
Strong RR = 1.5
```

بعد از صدور سیگنال، ربات فقط TP یا SL را مانیتور می‌کند.

```text
بدون خروج زمانی
بدون خروج هوشمند
بدون تحلیل مجدد بعد از ورود
```

## واچ‌لیست

```text
SOLUSDT
DOGEUSDT
XRPUSDT
BNBUSDT
AVAXUSDT
LINKUSDT
ADAUSDT
SUIUSDT
INJUSDT
ARBUSDT
```

## حالت‌های ربات

### سیگنال معمولی

اگر سیگنال روشن باشد، ربات سیگنال صادر می‌کند و نتیجه TP/SL را مانیتور می‌کند.

### ترید واقعی Toobit

اگر ترید روشن باشد، ربات بعد از صدور سیگنال تلاش می‌کند پوزیشن واقعی در Toobit باز کند.

قوانین Toobit:

```text
Margin Mode = isolated
Margin هر پوزیشن = مقدار دلار تنظیم‌شده توسط کاربر
Leverage = مقدار تنظیم‌شده توسط کاربر
برای هر کوین فقط یک پوزیشن Toobit مجاز است
بعد از سفارش، 70 ثانیه بررسی می‌شود که پوزیشن واقعاً باز شده یا نه
اگر باز نشده باشد، اسلات آزاد می‌شود
```

## دستورات فارسی

```text
/وضعیت
/روشن_ترید
/خاموش_ترید
/روشن_سیگنال
/خاموش_سیگنال
/سرمایه
/تنظیم_دلار 5
/تنظیم_لوریج 10
/تنظیم_اسلات 3
/پوزیشنها
/امروز
/آمار
/سود
/تاریخچه
```

## محدودیت‌های پنل

```text
دلار هر معامله: 1 تا 10000 USDT
لوریج: 1 تا 100
حداکثر پوزیشن همزمان: 1 تا 100
```

## اجرای ربات روی VPS

```bash
cd /root/crypto-ai-helper
chmod +x start_bot.sh
./start_bot.sh
```

یا با systemd:

```bash
sudo systemctl restart crypto-bot.service
sudo systemctl status crypto-bot.service --no-pager
journalctl -u crypto-bot -n 100 --no-pager
```

## فایل .env

نمونه متغیرهای لازم:

```env
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
TELEGRAM_CHAT_ID=your_chat_id
TOBIT_API_KEY=your_toobit_api_key
TOBIT_SECRET_KEY=your_toobit_secret_key
TOBIT_BASE_URL=https://api.toobit.com
```

## تست قبل از اجرا

```bash
python -m py_compile *.py
```

اگر خطایی نبود، ربات آماده اجراست.
