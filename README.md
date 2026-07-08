# Crypto 1H Trend Pullback Toobit Bot - OKX Only Data

این نسخه برای جلوگیری از HTTP 429 و فشار API توبیت اصلاح شده است.

## قانون اصلی

- تمام دیتای بازار، کندل، قیمت، تحلیل، اسکن، مانیتور TP/SL و نتیجه‌گیری سیگنال از OKX گرفته می‌شود.
- Toobit در چرخه اسکن صدا زده نمی‌شود.
- Toobit در مانیتور خودکار صدا زده نمی‌شود.
- پنل تلگرام برای خواندن موجودی/مارجین Toobit صدا نمی‌زند.
- Toobit فقط هنگام باز کردن Real Order استفاده می‌شود.
- TP/SL همراه سفارش Real به Toobit ارسال می‌شود؛ مانیتور نتیجه همچنان با قیمت OKX انجام می‌شود.

## منطق استراتژی

- جهت مادر از 4H
- ورود از 1H
- SL/TP متناسب با 1H
- RR پیش‌فرض 1.5
- RR=2 فقط برای سیگنال خیلی قوی
- فیلتر رنج با ADX/DMI، شیب EMA50 و موقعیت قیمت
- فیلتر ورود دیر با فاصله قیمت از EMA20/EMA50 نسبت به ATR
- استاپ: ساختار 1H + بافر ATR

## نمادها

نمادهای OKX و Toobit از `symbols_config.py` خوانده می‌شوند.
اگر OKX برای یک ارز خطا دهد، فقط همان ارز موقتاً رد می‌شود و ربات ادامه می‌دهد.
اگر Toobit موقع باز کردن Real خطا دهد، Real باز نمی‌شود و سیگنال Normal ثبت می‌شود.

## دستورات تلگرام حفظ شده

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
پوزیشن
کوین‌ها
وضعیت
راهنما
```

## نصب / آپدیت VPS

```bash
cd /root/crypto-ai-helper

git fetch --all --prune
git reset --hard origin/main

python -m py_compile *.py
systemctl daemon-reload
systemctl restart crypto-bot.service
sleep 5
systemctl status crypto-bot.service --no-pager
journalctl -u crypto-bot.service -n 100 --no-pager
```

اگر branch پروژه `master` است، به‌جای `origin/main` از `origin/master` استفاده کن.
