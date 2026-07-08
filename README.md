# Crypto 1H Trend Pullback Toobit Bot

نسخه دقیق‌شده برای سیگنال‌دهی 1H با داده OKX و اجرای واقعی روی Toobit.

## قانون اصلی معماری

- تمام دیتای تحلیل، کندل، قیمت لحظه‌ای و مانیتور TP/SL فقط از OKX گرفته می‌شود.
- Toobit فقط هنگام باز کردن Real Order استفاده می‌شود.
- اگر Toobit خطا بدهد، سیگنال Normal ثبت می‌شود و اسکن نمی‌خوابد.
- Real و Normal از نظر منطق سیگنال هیچ تفاوتی ندارند؛ فقط نحوه اجرا فرق دارد.

## منطق سیگنال 1H دقیق‌شده

- 1H تایم اصلی ورود است.
- 4H فیلتر مادر است: اگر خلاف جهت 1H باشد رد می‌شود؛ اگر رنج/خنثی باشد، با 1H قوی اجازه سیگنال می‌دهد.
- 1H اگر رنج باشد، معامله رد می‌شود.
- ADX سخت‌گیر مطلق نیست: ADX بالای 20 قبول است؛ ADX از 16 به بالا اگر رو به رشد باشد قبول مشروط است؛ ADX زیر 14 رد کامل است.
- DMI باید با جهت معامله همسو باشد.
- پولبک نرم‌تر شده: تماس با EMA20/EMA50، نزدیکی به EMA20 تا 0.4ATR، یا اصلاح سبک + تریگر برگشتی قابل قبول است.
- ورود دیر هنوز رد می‌شود: فاصله زیاد از EMA20/EMA50، 6 کندل هم‌جهت پشت‌سرهم، یا ADX خیلی بالا و رو به افت.
- SL/TP فقط با ساختار و ATR تایم 1H ساخته می‌شود.

## تنظیمات پیش‌فرض مهم

```text
SIGNAL_SCORE_THRESHOLD = 75
STRONG_SCORE_THRESHOLD = 88
RR_NORMAL = 1.5
RR_STRONG = 2.0
HARD_RANGE_ADX = 14
MIN_TREND_ADX = 16
STRONG_TREND_ADX = 25
MAX_DISTANCE_EMA20_ATR = 1.8
MAX_DISTANCE_EMA50_ATR = 2.8
MIN_1H_RISK_ATR = 0.6
MAX_1H_RISK_ATR = 2.4
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
ردها
ردها 50
پوزیشن
کوین‌ها
وضعیت
راهنما
```

## نصب/آپدیت روی VPS

```bash
cd /root/crypto-ai-helper

git fetch --all --prune
git reset --hard origin/main

python -m py_compile *.py

systemctl daemon-reload
systemctl restart crypto-bot.service

sleep 5
systemctl status crypto-bot.service --no-pager
journalctl -u crypto-bot.service -n 100 --no-pager -o cat
```

اگر برنچ `master` است، به‌جای `origin/main` از `origin/master` استفاده کن.

## لاگ رد شدن‌ها

```bash
journalctl -u crypto-bot.service -f -o cat | grep --line-buffered -E "رد شد|رنج|خلاف جهت|ADX|DMI|پولبک|ورود دیر|سیگنال"
```

داخل تلگرام:

```text
ردها 50
```
