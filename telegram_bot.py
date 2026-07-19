"""تلگرام، دستورات فارسی و تمام پنل‌ها در یک فایل."""
from __future__ import annotations

import threading
import time
from typing import Any

import requests

import config
from bot import BotEngine
from storage import Storage
from toobit_client import ToobitClient
from utils import normalize_command, now_ms, parse_number


def _n(value: Any, digits: int = 2) -> str:
    try:
        return f"{float(value):,.{digits}f}"
    except (TypeError, ValueError):
        return "نامشخص"


def _price(value: Any) -> str:
    try:
        x = float(value)
        if x >= 1000:
            return f"{x:,.2f}"
        if x >= 1:
            return f"{x:.6f}".rstrip("0").rstrip(".")
        return f"{x:.10f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return "نامشخص"


def _age(ts: int | None) -> str:
    if not ts:
        return "نامشخص"
    seconds = max(0, (now_ms() - int(ts)) // 1000)
    return f"{seconds} ثانیه" if seconds < 60 else f"{seconds // 60} دقیقه"


def help_text() -> str:
    return """
📌 دستورات فارسی

ترید | پنل | وضعیت
آمار
پوزیشن
کوین‌ها
سلامت

ترید فعال
ترید خاموش
ترید دلار 5        (۱ تا ۱۰۰۰۰)
ترید لوریج 10     (۱ تا ۱۰۰)
حداکثر پوزیشن 3  (۱ تا ۲۰۰)

نام‌های قدیمی:
توبیت روشن | توبیت خاموش
دلار ترید 5 | لوریج ترید 10

ریست سود
ریست سود کل
لاگ رد فعال
لاگ رد خاموش
""".strip()


def trade_panel(storage: Storage) -> str:
    settings = storage.settings()
    account = storage.account_snapshot()
    slots = storage.slot_counts()
    pnl = storage.displayed_real_pnl()
    active = storage.active_signals()
    trade = "🟢 فعال" if settings.get("real_trade_enabled") else "🔴 خاموش"
    connected = "🟢 متصل" if account.get("connected") else "🔴 قطع"
    startup = "✅ آماده" if settings.get("startup_ready") else f"⏳ {settings.get('startup_phase', 'BOOT')}"
    used_margin = float(account.get("position_margin") or 0) + float(account.get("order_margin") or 0)
    return f"""
📊 پنل ترید
━━━━━━━━━━━━━━━━━━
ترید واقعی: {trade}
ربات: {startup}
Toobit: {connected}
آخرین بروزرسانی Toobit: {_age(account.get('updated_at'))} قبل

💰 موجودی Toobit: {_n(account.get('balance'))} USDT
💵 مارجین آزاد: {_n(account.get('available'))} USDT
📌 مارجین استفاده‌شده: {_n(used_margin)} USDT
📈 سود/ضرر شناور: {_n(account.get('unrealized_pnl'))} USDT

دلار هر پوزیشن: {_n(settings.get('trade_margin_usdt'))} USDT
لوریج: {int(settings.get('leverage', 0))}x
مارجین: Isolated اجباری
حداکثر پوزیشن واقعی: {slots['max']}
اسلات پُر: {slots['used']}
اسلات خالی: {slots['free']}
پوزیشن باز Toobit: {slots['toobit_open']}
Real باز/Pending ربات: {slots['open']} / {slots['pending']}
سیگنال Virtual فعال: {sum(x.get('mode') == 'VIRTUAL' for x in active)}

سود/ضرر امروز Real: {_n(pnl.get('today'))} USDT
سود/ضرر کل Real: {_n(pnl.get('total'))} USDT

قراردادهای فعال: {len(storage.contracts())}
Watchlist پامپ: {len(settings.get('watchlist') or [])}
نامزدهای عمیق: {len(settings.get('deep_candidates') or [])}
آخرین اسکن: {_age(settings.get('last_scan_ms'))} قبل
━━━━━━━━━━━━━━━━━━
""".strip()


def stats_panel(storage: Storage) -> str:
    stats = storage.stats()
    blocks = []
    for mode, icon, title in (("REAL", "🔴", "Toobit واقعی"), ("VIRTUAL", "🔵", "مجازی")):
        row = stats.get(mode, {})
        blocks.append(
            f"{icon} {title}\n"
            f"کل: {row.get('total', 0)} | فعال: {row.get('active', 0)}\n"
            f"برد: {row.get('wins', 0)} | باخت: {row.get('losses', 0)} | Win: {_n(row.get('win_rate'), 1)}%\n"
            f"سود خالص ثبت‌شده: {_n(row.get('net_pnl'))} USDT"
        )
    return "📊 آمار سیگنال‌ها\n━━━━━━━━━━━━━━━━━━\n" + "\n\n".join(blocks) + "\n━━━━━━━━━━━━━━━━━━"


def positions_panel(storage: Storage) -> str:
    signals = storage.active_signals()
    slots = storage.slot_counts()
    if not signals:
        return f"📌 پوزیشن فعالی وجود ندارد.\nاسلات واقعی: {slots['used']}/{slots['max']}"
    rows = [f"📌 پوزیشن‌های فعال | اسلات واقعی {slots['used']}/{slots['max']}"]
    for item in signals:
        trail = item.get("trailing_stop")
        rows.append(
            f"\n{'🔴' if item.get('mode') == 'REAL' else '🔵'} {item.get('canonical')} | SHORT | {item.get('status')}\n"
            f"ورود: {_price(item.get('entry'))}\n"
            f"SL: {_price(item.get('sl'))} | TP ایمنی: {_price(item.get('tp'))}\n"
            f"بهترین قیمت: {_price(item.get('best_price'))} | Trailing: {_price(trail) if trail else 'فعال نشده'}\n"
            f"امتیاز: {_n(item.get('signal_score'), 1)} | مارجین: {_n(item.get('margin_usdt'))} × {item.get('leverage')}x"
        )
    return "\n".join(rows)


def coins_panel(storage: Storage) -> str:
    watch = storage.get_setting("watchlist", []) or []
    deep = {x.get("canonical") for x in (storage.get_setting("deep_candidates", []) or [])}
    if not watch:
        return f"🪙 قرارداد فعال Toobit: {len(storage.contracts())}\nفعلاً پامپ واجد شرایطی در Watchlist نیست."
    rows = [f"🪙 Watchlist پامپ‌ها | قرارداد فعال: {len(storage.contracts())}"]
    for idx, item in enumerate(watch, 1):
        mark = "🔥" if item.get("canonical") in deep else "👀"
        rows.append(
            f"{idx}. {mark} {item.get('canonical')} | رشد 24h: {_n(item.get('change_24h'), 1)}% "
            f"| حجم: {_n(item.get('quote_volume'), 0)} | اسپرد: {_n(float(item.get('spread') or 0) * 100, 3)}%"
        )
    return "\n".join(rows)


def health_panel(storage: Storage, toobit: ToobitClient) -> str:
    rate = toobit.rate.snapshot()
    rows = [
        "🩺 سلامت ربات",
        "━━━━━━━━━━━━━━━━━━",
        f"دیتابیس: {'✅ سالم' if storage.integrity_check() else '❌ مشکل'}",
        f"API وزن کل 60ثانیه: {rate['total_60s']}/{rate['total_limit']}",
        f"API وزن بازار: {rate['market_60s']}/{rate['market_limit']}",
        f"Rate cooldown: {_n(rate['blocked_for_seconds'], 1)} ثانیه",
    ]
    for item in storage.health_rows():
        icon = "✅" if item["level"] == "ok" else "⚠️"
        rows.append(f"{icon} {item['component']}: {item['message']} ({_age(item['updated_at'])} قبل)")
    return "\n".join(rows)


def signal_message(signal: dict[str, Any]) -> str:
    reasons = "، ".join(signal.get("reasons") or [])
    metrics = signal.get("metrics") or {}
    virtual_note = f"\nعلت مجازی: {signal.get('virtual_reason')}" if signal.get("mode") == "VIRTUAL" else ""
    return f"""
{'🔴' if signal.get('mode') == 'REAL' else '🔵'} سیگنال {signal.get('mode')}
━━━━━━━━━━━━━━━━━━
ارز: {signal.get('canonical')}
جهت: SHORT
امتیاز: {_n(signal.get('signal_score'), 1)}
تأییدها: {signal.get('confirmations')}

ورود: {_price(signal.get('entry'))}
SL: {_price(signal.get('sl'))}
TP ایمنی: {_price(signal.get('tp'))}
مدیریت اصلی: Trailing Stop پویا

رشد 24h: {_n(metrics.get('pump_24h_percent'), 1)}%
رشد 15m: {_n(metrics.get('pump_15m_percent'), 1)}%
فشار فروش: {_n(float(metrics.get('sell_aggression') or 0) * 100, 1)}%
Funding: {_n(float(metrics.get('funding_rate') or 0) * 100, 4)}%
Long/Short: {_n(metrics.get('long_short_ratio'), 2)}

دلایل: {reasons}
مارجین: {_n(signal.get('margin_usdt'))} USDT × {signal.get('leverage')}x{virtual_note}
━━━━━━━━━━━━━━━━━━
""".strip()


def result_message(signal: dict[str, Any]) -> str:
    pnl = float(signal.get("net_pnl") or 0)
    icon = "✅" if pnl > 0 else "❌"
    return f"""
{icon} نتیجه {signal.get('mode')}
━━━━━━━━━━━━━━━━━━
ارز: {signal.get('canonical')} | SHORT
نتیجه: {signal.get('result')}
ورود: {_price(signal.get('entry'))}
خروج: {_price(signal.get('close_price'))}
سود/ضرر خالص: {_n(pnl)} USDT
━━━━━━━━━━━━━━━━━━
""".strip()


class CommandRouter:
    def __init__(self, storage: Storage, toobit: ToobitClient):
        self.storage = storage
        self.toobit = toobit

    def _set_float(self, raw: str, prefix: str, key: str, label: str, lo: float, hi: float, suffix: str) -> str:
        try:
            value = parse_number(raw[len(prefix):])
        except ValueError:
            return f"❌ مقدار {label} نامعتبر است. بازه: {lo:g} تا {hi:g} {suffix}"
        if value < lo or value > hi:
            return f"❌ مقدار خارج از بازه است. بازه مجاز: {lo:g} تا {hi:g} {suffix}"
        self.storage.set_setting(key, value)
        return f"✅ {label}: {value:g} {suffix}\nتمام سیگنال‌های جدید از این مقدار استفاده می‌کنند."

    def _set_int(self, raw: str, prefix: str, key: str, label: str, lo: int, hi: int, suffix: str) -> str:
        try:
            parsed = parse_number(raw[len(prefix):])
        except ValueError:
            return f"❌ مقدار {label} نامعتبر است. بازه: {lo} تا {hi} {suffix}"
        if not parsed.is_integer():
            return f"❌ مقدار {label} باید عدد صحیح باشد."
        value = int(parsed)
        if value < lo or value > hi:
            return f"❌ مقدار خارج از بازه است. بازه مجاز: {lo} تا {hi} {suffix}"
        self.storage.set_setting(key, value)
        return f"✅ {label}: {value} {suffix}\nتمام سیگنال‌های جدید از این مقدار استفاده می‌کنند."

    def handle(self, text: str) -> str:
        t = normalize_command(text)
        if not t or t in {"/start", "start", "راهنما", "کمک", "help"}:
            return help_text()
        if t in {"ترید", "پنل", "وضعیت", "پنل ترید"}:
            return trade_panel(self.storage)
        if t in {"آمار", "پنل آمار"}:
            return stats_panel(self.storage)
        if t in {"پوزیشن", "پوزیشن‌ها", "پوزیشن ها"}:
            return positions_panel(self.storage)
        if t in {"کوین‌ها", "کوین ها", "ارزها"}:
            return coins_panel(self.storage)
        if t in {"سلامت", "health", "هلس"}:
            return health_panel(self.storage, self.toobit)
        if t in {"ترید فعال", "توبیت روشن"}:
            self.storage.set_setting("real_trade_enabled", True)
            snap = self.storage.account_snapshot()
            warning = "" if snap.get("connected") else "\n⚠️ Toobit خصوصی فعلاً متصل نیست؛ تا اتصال سالم، سیگنال‌ها Virtual می‌شوند."
            return "✅ ترید واقعی فعال شد. سیگنال معتبر در صورت اسلات و اتصال سالم وارد Toobit می‌شود." + warning
        if t in {"ترید خاموش", "توبیت خاموش"}:
            self.storage.set_setting("real_trade_enabled", False)
            return "⛔ ترید واقعی خاموش شد. سیگنال‌های جدید Virtual می‌شوند؛ Realهای باز همچنان مانیتور می‌شوند."
        if t.startswith("ترید دلار "):
            return self._set_float(t, "ترید دلار ", "trade_margin_usdt", "دلار هر پوزیشن", config.TRADE_MARGIN_MIN, config.TRADE_MARGIN_MAX, "USDT")
        if t.startswith("دلار ترید "):
            return self._set_float(t, "دلار ترید ", "trade_margin_usdt", "دلار هر پوزیشن", config.TRADE_MARGIN_MIN, config.TRADE_MARGIN_MAX, "USDT")
        if t.startswith("ترید لوریج "):
            return self._set_int(t, "ترید لوریج ", "leverage", "لوریج", config.LEVERAGE_MIN, config.LEVERAGE_MAX, "x")
        if t.startswith("لوریج ترید "):
            return self._set_int(t, "لوریج ترید ", "leverage", "لوریج", config.LEVERAGE_MIN, config.LEVERAGE_MAX, "x")
        if t.startswith("حداکثر پوزیشن "):
            return self._set_int(t, "حداکثر پوزیشن ", "max_open_positions", "حداکثر پوزیشن", config.MAX_POSITIONS_MIN, config.MAX_POSITIONS_MAX, "")
        if t == "ریست سود":
            self.storage.reset_pnl(total=False)
            return "✅ مبنای سود/ضرر امروز صفر شد. تاریخچه و آمار خام حذف نشدند."
        if t == "ریست سود کل":
            self.storage.reset_pnl(total=True)
            return "✅ مبنای سود/ضرر کل صفر شد. تاریخچه و آمار خام حذف نشدند."
        if t == "لاگ رد فعال":
            self.storage.set_setting("reject_log_enabled", True)
            return "✅ لاگ دلایل رد فعال شد."
        if t == "لاگ رد خاموش":
            self.storage.set_setting("reject_log_enabled", False)
            return "✅ لاگ دلایل رد خاموش شد؛ خطاهای مهم همچنان ثبت می‌شوند."
        return "دستور نامعتبر است.\n\n" + help_text()


class TelegramBot:
    def __init__(self, storage: Storage, engine: BotEngine, toobit: ToobitClient):
        self.storage = storage
        self.engine = engine
        self.toobit = toobit
        self.router = CommandRouter(storage, toobit)
        self.token = config.TELEGRAM_BOT_TOKEN
        self.chat_id = config.TELEGRAM_CHAT_ID
        self.poll_session = requests.Session()
        self.send_session = requests.Session()
        self.stop_event = threading.Event()

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def send_message(self, text: str, reply_to_message_id: int | None = None) -> int | None:
        if not self.enabled:
            return None
        payload: dict[str, Any] = {"chat_id": self.chat_id, "text": text}
        if reply_to_message_id:
            payload["reply_to_message_id"] = reply_to_message_id
            payload["allow_sending_without_reply"] = True
        try:
            response = self.send_session.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json=payload,
                timeout=10,
            )
            data = response.json()
            return int((data.get("result") or {}).get("message_id") or 0) or None if data.get("ok") else None
        except Exception as exc:
            self.storage.set_health("telegram", "warning", f"send failed: {exc}")
            return None

    def poll_loop(self) -> None:
        if not self.enabled:
            self.storage.set_health("telegram", "warning", "Token/Chat ID تنظیم نشده")
            while not self.stop_event.wait(5):
                pass
            return
        offset = self.storage.telegram_offset()
        self.storage.set_health("telegram", "ok", "polling")
        while not self.stop_event.is_set():
            try:
                response = self.poll_session.get(
                    f"https://api.telegram.org/bot{self.token}/getUpdates",
                    params={"offset": offset + 1, "timeout": config.TELEGRAM_POLL_TIMEOUT},
                    timeout=config.TELEGRAM_POLL_TIMEOUT + 5,
                )
                data = response.json()
                for update in data.get("result", []):
                    offset = max(offset, int(update.get("update_id", 0)))
                    self.storage.set_telegram_offset(offset)
                    msg = update.get("message") or {}
                    if str((msg.get("chat") or {}).get("id") or "") != str(self.chat_id):
                        self.storage.add_event("TELEGRAM_SECURITY", "دستور chat_id غیرمجاز نادیده گرفته شد")
                        continue
                    text = str(msg.get("text") or "").strip()
                    if text:
                        self.send_message(self.router.handle(text), int(msg.get("message_id") or 0) or None)
            except Exception as exc:
                self.storage.set_health("telegram", "warning", f"poll failed: {exc}")
                self.stop_event.wait(2)

    def notification_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                item = self.engine.notifications.get(timeout=1)
            except Exception:
                continue
            try:
                signal = self.storage.get_signal(int(item["signal_id"]))
                if not signal:
                    continue
                if item["type"] == "signal":
                    self.send_message(signal_message(signal))
                elif item["type"] == "position_open":
                    self.send_message(f"✅ پوزیشن REAL بازشدن در Toobit تأیید شد.\n{signal.get('canonical')} | SHORT\nورود واقعی: {_price(signal.get('actual_entry') or signal.get('entry'))}")
                elif item["type"] == "failed_open":
                    self.send_message(f"❌ سفارش REAL باز نشد.\n{signal.get('canonical')} | اسلات آزاد شد.")
                elif item["type"] == "result":
                    self.send_message(result_message(signal))
            finally:
                self.engine.notifications.task_done()

    def stop(self) -> None:
        self.stop_event.set()
        self.poll_session.close()
        self.send_session.close()
