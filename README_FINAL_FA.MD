# نسخه نهایی ربات Crypto AI Futures

این ZIP برای جایگزینی سورس کد ساخته شده و فایل‌های محرمانه/داده‌های اجرای قبلی داخل آن قرار داده نشده‌اند.

## قابلیت‌های موجود
- تحلیل کلاسیک تکنیکال با EMA، RSI، MACD، VWAP، ATR، ADX و Buy/Sell Power
- سیگنال مستقیم فقط بعد از کامل شدن ورود
- Auto Signal با Slot Manager
- وقتی Slot پر باشد، سیگنال‌های مناسب به Ghost Learning می‌روند، نه تلگرام
- Auto Tracking همه سیگنال‌ها تا TP1 یا SL
- پاسخ TP/SL به پیام اصلی سیگنال
- Paper Trade و آمار
- AI Memory، Coin Learning، Coin Risk، Coin Rotation، Ghost Signals، SR Learning
- دستورات فارسی AI، آمار، بررسی بازار، رفتار کوین، بهترین/بدترین کوین‌ها، اسلات‌ها و وضعیت ترید

## نکته مهم آپدیت روی VPS
برای اینکه حافظه AI و آمار پاک نشود، پوشه/فایل‌های data و JSONهای اجرایی را حذف نکنید.
این ZIP خودش data runtime ندارد و برای جایگزینی کد امن‌تر است.

## نصب/آپدیت سریع VPS
```bash
cd /root/crypto-ai-helper
git pull
python3 -m py_compile *.py
sudo systemctl restart crypto-bot.service
sudo journalctl -u crypto-bot -n 50 --no-pager
```

## Environment های لازم
از `.env.example` الگو بگیرید و در systemd یا محیط اجرا مقداردهی کنید.
