# Multi Timeframe AI Futures Bot v1

نسخه جدید ربات بر اساس طراحی نهایی:

- 15 ارز
- حداقل سیگنال: 75
- 1H جهت اصلی، 40 امتیاز
- 4H جهت بزرگ‌تر، 7 امتیاز
- 15m تأیید setup، 18 امتیاز
- 5m فقط تایمینگ ورود، 4 امتیاز
- Late Entry Guard، 10 امتیاز
- TP/SL + Net Edge، 15 امتیاز
- Market Quality، 6 امتیاز
- یک TP و یک SL
- آمار لانگ/شورت جدا
- آمار عادی/واقعی جدا
- اسلات real_pending/opening با تأیید 70 ثانیه‌ای Toobit

## دستورات تلگرام

```text
پنل
وضعیت
آمار
آمار 7
ترید فعال
ترید خاموش
ترید دلار 20
ترید لوریج 10
حداکثر پوزیشن 3
راهنما
```

## نصب روی VPS

قبل از جایگزینی، بکاپ بگیر:

```bash
cd /root/crypto-ai-helper
cp -r /root/crypto-ai-helper /root/crypto-ai-helper-backup-$(date +%F-%H%M)
```

بعد فایل‌ها را جایگزین کن و تست بگیر:

```bash
cd /root/crypto-ai-helper
/root/crypto-ai-helper/venv/bin/python -m py_compile *.py
systemctl restart crypto-bot.service
journalctl -u crypto-bot -n 100 --no-pager
```

## نکته‌های حساس

- API key و secret را داخل تلگرام یا لاگ عمومی نفرست.
- `toobit_client.py` سفارش واقعی را در thread جدا اجرا می‌کند؛ خودش بعد از سفارش 70 ثانیه صبر می‌کند و پوزیشن را تأیید می‌کند.
- در زمان 70 ثانیه، اسلات در دیتابیس با `real_status=opening` رزرو می‌ماند تا سفارش تکراری ایجاد نشود.
- Toobit منبع حقیقت پول و پوزیشن واقعی است؛ دیتابیس منبع حقیقت تنظیمات ربات و آمار است.
