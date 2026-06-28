# AI Helper Hunter Bot v2

ربات شکارچی شروع حرکت + AI واقعی + اجرای هماهنگ با Toobit.

## قفل‌های اصلی

- 14 ارز ترید: SOL, XRP, DOGE, ADA, LTC, BCH, LINK, AVAX, DOT, TRX, SUI, NEAR, APT, INJ
- BTC/ETH فقط برای Market Context
- اسکن کامل هر 60 ثانیه
- اسکن Watchlist هر 15 ثانیه
- حداقل امتیاز سیگنال: 75
- Watch بدون اسپم؛ فقط پیام کوتاه «آماده ورود»
- AI Confidence و AI Experience در پیام سیگنال
- حداقل سود دلاری و درصدی قابل تنظیم
- اسلات‌های real/reserved/opening/opened هماهنگ با چک 70 ثانیه‌ای Toobit
- خطای یک نماد فقط همان نماد را skip یا موقتاً غیرفعال می‌کند

## دستورات

```text
پنل
وضعیت
ترید
آمار
آمار 7
هوش مصنوعی
Ai
هوش
مصنوعی
ترید فعال
ترید خاموش
ترید دلار 20
ترید لوریج 10
حداکثر پوزیشن 3
حداقل سود 1
درصد سود 0.10
حذف آمار
حذف آمار تایید
ریست یادگیری
ریست یادگیری تایید
راهنما
```

## نصب

```bash
cd /root
cp -r /root/crypto-ai-helper /root/crypto-ai-helper-backup-$(date +%F-%H%M)
# فایل‌های این نسخه را داخل /root/crypto-ai-helper جایگزین کن
cd /root/crypto-ai-helper
/root/crypto-ai-helper/venv/bin/python -m py_compile *.py
systemctl restart crypto-bot.service
journalctl -u crypto-bot -n 100 --no-pager
```

## نکته امنیتی

`.env` را عمومی نکن. توکن تلگرام و کلیدهای Toobit نباید داخل لاگ یا پیام دیده شوند.
