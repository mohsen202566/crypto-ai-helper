"""تلگرام، دستورات فارسی و تمام پنل‌ها در یک فایل."""
from __future__ import annotations

import json
import queue
import re
import threading
import time
from typing import Any

import requests

import config
from bot import BotEngine
from storage import Storage
from toobit_client import ToobitClient
from utils import logger, normalize_command, now_ms, parse_number


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

ترید | پنل | وضعیت | موجودی
آمار
پوزیشن
کوین‌ها
سلامت | چک توبیت

ترید فعال
ترید خاموش
ترید دلار 5        (۱ تا ۱۰۰۰۰ USDT)
ترید لوریج 10     (۱ تا ۱۰۰)
حداکثر پوزیشن 3  (۱ تا ۲۰۰)

نام‌های جایگزین:
توبیت روشن | توبیت خاموش
دلار ترید 5 | مارجین ترید 5 | ترید مارجین 5
لوریج ترید 10
تعداد اسلات 3 | ترید اسلات 3

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

    wallet = float(account.get("wallet_balance", account.get("balance", 0)) or 0)
    equity = float(account.get("equity", account.get("balance", wallet)) or 0)
    available = float(account.get("available") or 0)
    position_margin = float(account.get("position_margin") or 0)
    order_margin = float(account.get("order_margin") or 0)
    used_margin = float(account.get("used_margin") or (position_margin + order_margin))
    floating = float(account.get("unrealized_pnl") or 0)

    virtual_active = sum(
        1 for item in active
        if item.get("mode") == "VIRTUAL" and item.get("status") in {"ACTIVE", "OPEN", "PENDING_OPEN"}
    )
    external_open = int(slots.get("external_open", 0))
    snapshot_error = str(account.get("error") or "").strip()
    error_line = f"\n⚠️ خطای آخر Toobit: {snapshot_error}" if snapshot_error else ""

    return f"""
📊 پنل ترید
━━━━━━━━━━━━━━━━━━
ترید واقعی: {trade}
ربات: {startup}
Toobit: {connected}
آخرین بروزرسانی Toobit: {_age(account.get('updated_at'))} قبل{error_line}

💰 موجودی کیف پول Toobit: {_n(wallet)} USDT
💎 اکویتی حساب: {_n(equity)} USDT
💵 مارجین آزاد: {_n(available)} USDT
📍 مارجین پوزیشن‌ها: {_n(position_margin)} USDT
🧾 مارجین سفارش‌ها: {_n(order_margin)} USDT
📌 مارجین استفاده‌شده: {_n(used_margin)} USDT
📈 سود/ضرر شناور: {_n(floating)} USDT

دلار هر پوزیشن: {_n(settings.get('trade_margin_usdt'))} USDT
لوریج: {int(settings.get('leverage', 0))}x
مارجین: Isolated اجباری
حداکثر پوزیشن واقعی: {slots['max']}
اسلات پُر: {slots['used']}
اسلات خالی: {slots['free']}
پوزیشن باز Toobit: {slots.get('toobit_open', account.get('open_positions', 0))}
پوزیشن تأییدشده ربات: {slots['open']}
Pending Open ربات: {slots['pending']}
پوزیشن دستی/خارج از ربات: {external_open}
سیگنال Virtual فعال: {virtual_active}

سود/ضرر امروز Real: {_n(pnl.get('today'))} USDT
سود/ضرر کل Real: {_n(pnl.get('total'))} USDT

قراردادهای فعال Toobit: {len(storage.contracts())}
Watchlist پامپ: {len(settings.get('watchlist') or [])}
نامزدهای تحلیل عمیق: {len(settings.get('deep_candidates') or [])}
آخرین اسکن بازار: {_age(settings.get('last_scan_ms'))} قبل
━━━━━━━━━━━━━━━━━━
""".strip()

def stats_panel(storage: Storage) -> str:
    stats = storage.stats()
    blocks: list[str] = []
    for mode, icon, title in (("REAL", "🔴", "Toobit واقعی"), ("VIRTUAL", "🔵", "مجازی")):
        row = stats.get(mode, {})
        blocks.append(
            f"{icon} {title}\n"
            f"کل: {row.get('total', 0)} | فعال: {row.get('active', 0)}\n"
            f"TP: {row.get('tp', 0)} | Stop: {row.get('stop', 0)} | Trail: {row.get('trail_exit', 0)}\n"
            f"بستن دستی: {row.get('manual_close', 0)} | بازنشدن: {row.get('failed_open', 0)} | لغو: {row.get('cancelled', 0)}\n"
            f"برد: {row.get('wins', 0)} | باخت: {row.get('losses', 0)} | Win: {_n(row.get('win_rate'), 1)}%\n"
            f"امروز: {_n(row.get('today_pnl'))} | کل: {_n(row.get('net_pnl'))} USDT"
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
    reasons = signal.get("reasons") or []
    reason_text = "\n".join(f"• {item}" for item in reasons[:6]) or "• تأییدهای ثابت استراتژی"
    metrics = signal.get("metrics") or {}
    mode = str(signal.get("mode") or "VIRTUAL")
    real_status = (
        "\nوضعیت اجرا: PENDING_OPEN؛ بررسی پوزیشن Toobit بعد از ۷۰ ثانیه"
        if mode == "REAL" and signal.get("status") == "PENDING_OPEN"
        else ""
    )
    virtual_note = (
        f"\nعلت مجازی: {signal.get('virtual_reason')}"
        if mode == "VIRTUAL" and signal.get("virtual_reason")
        else ""
    )
    entry = float(signal.get("entry") or 0)
    sl = float(signal.get("sl") or 0)
    tp = float(signal.get("tp") or 0)
    risk = abs(sl - entry)
    reward = abs(entry - tp)
    rr = reward / risk if risk > 0 else 0
    return f"""
{'🔴' if mode == 'REAL' else '🔵'} سیگنال {mode} #{signal.get('id')}
━━━━━━━━━━━━━━━━━━
{signal.get('canonical')} | SHORT
ورود: {_price(entry)}
TP ایمنی: {_price(tp)}
SL: {_price(sl)}
RR: {_n(rr)}

مارجین: {_n(signal.get('margin_usdt'))} USDT
لوریج: {signal.get('leverage')}x
ارزش پوزیشن: {_n(signal.get('notional_usdt'))} USDT
سود خالص پیش‌بینی‌شده: {_n(signal.get('expected_net_profit'))} USDT
امتیاز سیگنال: {_n(signal.get('signal_score'), 1)}
تعداد تأییدها: {signal.get('confirmations')}
مدیریت خروج: Trailing Stop پویا{real_status}{virtual_note}

رشد 24h: {_n(metrics.get('pump_24h_percent'), 1)}%
رشد 15m: {_n(metrics.get('pump_15m_percent'), 1)}%
فشار فروش: {_n(float(metrics.get('sell_aggression') or 0) * 100, 1)}%
Funding: {_n(float(metrics.get('funding_rate') or 0) * 100, 4)}%
Open Interest: {_n(metrics.get('open_interest'), 2)}
Long/Short: {_n(metrics.get('long_short_ratio'), 2)}

دلایل:
{reason_text}
━━━━━━━━━━━━━━━━━━
""".strip()


def position_open_message(signal: dict[str, Any]) -> str:
    return f"""
🟢 پوزیشن Toobit تأیید شد #{signal.get('id')}
━━━━━━━━━━━━━━━━━━
{signal.get('canonical')} | SHORT
قیمت ورود واقعی: {_price(signal.get('actual_entry') or signal.get('entry'))}
TP ایمنی: {_price(signal.get('tp'))}
SL: {_price(signal.get('sl'))}
مارجین: {_n(signal.get('margin_usdt'))} USDT × {signal.get('leverage')}x
اسلات تا بسته‌شدن قطعی پوزیشن پُر می‌ماند.
━━━━━━━━━━━━━━━━━━
""".strip()


def failed_open_message(signal: dict[str, Any]) -> str:
    return f"""
⚠️ پوزیشن Toobit باز نشد #{signal.get('id')}
━━━━━━━━━━━━━━━━━━
{signal.get('canonical')} | SHORT
بعد از مهلت تأیید، پوزیشن یا نتیجه تحقق‌یافته‌ای پیدا نشد.
اسلات واقعی آزاد شد و نتیجه FAILED_OPEN ثبت شد.
━━━━━━━━━━━━━━━━━━
""".strip()


def result_message(signal: dict[str, Any]) -> str:
    result = str(signal.get("result") or signal.get("status") or "UNKNOWN")
    pnl = float(signal.get("net_pnl") or 0)
    icon = {
        "TP": "✅",
        "TRAIL_EXIT": "✅" if pnl >= 0 else "❌",
        "STOP": "❌",
        "FAILED_OPEN": "⚠️",
        "MANUAL_CLOSE": "ℹ️",
        "CANCELLED": "⛔",
    }.get(result, "ℹ️")
    created = int(signal.get("created_at") or now_ms())
    closed = int(signal.get("closed_at") or now_ms())
    duration = max(0, (closed - created) // 60000)
    return f"""
{icon} نتیجه #{signal.get('id')} | {signal.get('mode')}
━━━━━━━━━━━━━━━━━━
{signal.get('canonical')} | SHORT
نتیجه: {result}
ورود: {_price(signal.get('actual_entry') or signal.get('entry'))}
خروج: {_price(signal.get('close_price'))}
سود/ضرر خالص: {_n(pnl)} USDT
مدت معامله: {duration} دقیقه
━━━━━━━━━━━━━━━━━━
""".strip()

class CommandRouter:
    def __init__(self, storage: Storage, toobit: ToobitClient):
        self.storage = storage
        self.toobit = toobit

    def _set_float(self, value_text: str, key: str, label: str, lo: float, hi: float, suffix: str) -> str:
        try:
            value = parse_number(value_text)
        except ValueError:
            return f"❌ مقدار {label} نامعتبر است. بازه: {lo:g} تا {hi:g} {suffix}"
        if value < lo or value > hi:
            return f"❌ مقدار خارج از بازه است. بازه مجاز: {lo:g} تا {hi:g} {suffix}"
        value = round(float(value), 8)
        self.storage.set_setting(key, value)
        return f"✅ {label}: {value:g} {suffix}\nتمام سیگنال‌های بعدی از این مقدار استفاده می‌کنند."

    def _set_int(self, value_text: str, key: str, label: str, lo: int, hi: int, suffix: str) -> str:
        try:
            parsed = parse_number(value_text)
        except ValueError:
            return f"❌ مقدار {label} نامعتبر است. بازه: {lo} تا {hi} {suffix}"
        if not parsed.is_integer():
            return f"❌ مقدار {label} باید عدد صحیح باشد."
        value = int(parsed)
        if value < lo or value > hi:
            return f"❌ مقدار خارج از بازه است. بازه مجاز: {lo} تا {hi} {suffix}"
        self.storage.set_setting(key, value)
        slots = self.storage.slot_counts() if key == "max_open_positions" else None
        extra = f"\nوضعیت اسلات اکنون: {slots['used']} پُر / {slots['free']} خالی" if slots else ""
        suffix_text = f" {suffix}" if suffix else ""
        return f"✅ {label}: {value}{suffix_text}\nتمام سیگنال‌های بعدی از این مقدار استفاده می‌کنند.{extra}"

    @staticmethod
    def _match_value(command: str, patterns: tuple[str, ...]) -> str | None:
        for pattern in patterns:
            match = re.fullmatch(pattern, command)
            if match:
                return str(match.group(1)).strip()
        return None

    def handle(self, text: str) -> str:
        t = normalize_command(text)
        if not t or t in {"/start", "/help", "start", "راهنما", "کمک", "help"}:
            return help_text()
        if t in {"ترید", "پنل", "وضعیت", "پنل ترید", "موجودی", "/trade", "/status"}:
            return trade_panel(self.storage)
        if t in {"آمار", "پنل آمار", "/stats"}:
            return stats_panel(self.storage)
        if t in {"پوزیشن", "پوزیشن‌ها", "پوزیشن ها", "/positions"}:
            return positions_panel(self.storage)
        if t in {"کوین‌ها", "کوین ها", "ارزها", "/coins"}:
            return coins_panel(self.storage)
        if t in {"سلامت", "health", "هلس", "چک توبیت", "/health"}:
            return health_panel(self.storage, self.toobit)

        if t in {"ترید فعال", "توبیت روشن"}:
            self.storage.set_setting("real_trade_enabled", True)
            snap = self.storage.account_snapshot()
            warning = "" if snap.get("connected") else "\n⚠️ Toobit خصوصی فعلاً متصل نیست؛ تا اتصال سالم، سیگنال‌های معتبر Virtual می‌شوند."
            return "✅ ترید واقعی فعال شد. فقط سیگنال معتبر با اسلات آزاد و اتصال سالم وارد Toobit می‌شود." + warning
        if t in {"ترید خاموش", "توبیت خاموش"}:
            self.storage.set_setting("real_trade_enabled", False)
            return "⛔ ترید واقعی خاموش شد. سفارش جدید باز نمی‌شود؛ سیگنال‌های جدید Virtual و پوزیشن‌های Real باز همچنان مانیتور می‌شوند."

        value = self._match_value(t, (
            r"(?:ترید\s*دلار|دلار\s*ترید|مارجین\s*ترید|ترید\s*مارجین)\s*[:=]?\s*(.+)",
        ))
        if value is not None:
            return self._set_float(
                value, "trade_margin_usdt", "دلار هر پوزیشن",
                config.TRADE_MARGIN_MIN, config.TRADE_MARGIN_MAX, "USDT",
            )

        value = self._match_value(t, (r"(?:ترید\s*لوریج|لوریج\s*ترید)\s*[:=]?\s*(.+)",))
        if value is not None:
            return self._set_int(
                value, "leverage", "لوریج",
                config.LEVERAGE_MIN, config.LEVERAGE_MAX, "x",
            )

        value = self._match_value(t, (
            r"(?:حداکثر\s*پوزیشن|تعداد\s*اسلات|ترید\s*اسلات)\s*[:=]?\s*(.+)",
        ))
        if value is not None:
            return self._set_int(
                value, "max_open_positions", "حداکثر پوزیشن واقعی",
                config.MAX_POSITIONS_MIN, config.MAX_POSITIONS_MAX, "",
            )

        if t == "ریست سود":
            self.storage.reset_pnl(total=False)
            return "✅ مبنای سود/ضرر امروز Real صفر شد. نتیجه‌های خام، آمار و تاریخچه حذف نشدند."
        if t == "ریست سود کل":
            self.storage.reset_pnl(total=True)
            return "✅ مبنای سود/ضرر امروز و کل Real صفر شد. نتیجه‌های خام، آمار و تاریخچه حذف نشدند."
        if t == "لاگ رد فعال":
            self.storage.set_setting("reject_log_enabled", True)
            return "✅ چاپ زنده دلایل رد سیگنال در لاگ VPS فعال شد."
        if t == "لاگ رد خاموش":
            self.storage.set_setting("reject_log_enabled", False)
            return "✅ چاپ دلایل رد خاموش شد؛ خطاهای حیاتی همچنان ثبت می‌شوند."
        return "دستور نامعتبر است.\n\n" + help_text()

class TelegramBot:
    """Synchronous Telegram long-polling client with durable offset and owner binding.

    Polling requires only a bot token.  If no owner/chat id is configured, the first
    private human chat is bound atomically in the runtime database.  A configured
    numeric chat/user id or ``@username`` is still enforced when present.
    """

    def __init__(self, storage: Storage, engine: BotEngine, toobit: ToobitClient):
        self.storage = storage
        self.engine = engine
        self.toobit = toobit
        self.router = CommandRouter(storage, toobit)
        self.token = str(config.TELEGRAM_BOT_TOKEN or "").strip()
        configured = str(config.TELEGRAM_CHAT_ID or "").strip()
        persisted = str(storage.get_setting("telegram_chat_id", "") or "").strip()
        self.owner_ref = configured or persisted
        self.chat_id = persisted or (configured if configured.lstrip("-").isdigit() else "")
        if configured and configured.lstrip("-").isdigit() and configured != persisted:
            self.storage.set_setting("telegram_chat_id", configured)
            self.chat_id = configured
        self.poll_session = requests.Session()
        self.send_session = requests.Session()
        self.stop_event = threading.Event()

    @property
    def enabled(self) -> bool:
        # getUpdates itself only needs a token. Chat ownership can be bound later.
        return bool(self.token)

    def _api_url(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self.token}/{method}"

    @staticmethod
    def _masked_chat_id(chat_id: str) -> str:
        text = str(chat_id)
        return ("*" * max(0, len(text) - 4)) + text[-4:] if text else "unbound"

    @staticmethod
    def _response_json(response: requests.Response) -> dict[str, Any]:
        try:
            data = response.json()
        except Exception as exc:
            body = str(getattr(response, "text", ""))[:300]
            raise RuntimeError(f"Telegram JSON نامعتبر: {body}") from exc
        if not isinstance(data, dict):
            raise RuntimeError("Telegram پاسخ JSON دیکشنری نداد")
        return data

    def _bind_chat(self, chat_id: str, reason: str) -> None:
        chat_id = str(chat_id).strip()
        if not chat_id:
            return
        self.chat_id = chat_id
        self.owner_ref = chat_id
        self.storage.set_setting("telegram_chat_id", chat_id)
        self.storage.add_event("TELEGRAM_OWNER_BOUND", reason, payload={"chat_id": chat_id})
        logger.warning("TELEGRAM_OWNER_BOUND | chat_id=%s | %s", self._masked_chat_id(chat_id), reason)

    def _is_authorized(self, message: dict[str, Any]) -> tuple[bool, str]:
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        incoming_chat_id = str(chat.get("id") or "").strip()
        incoming_user_id = str(sender.get("id") or "").strip()
        username = str(sender.get("username") or "").strip().lower()
        chat_type = str(chat.get("type") or "").lower()

        owner = str(self.owner_ref or self.chat_id or "").strip()
        if not owner:
            # Safe zero-config recovery: only a private human can claim an unbound bot.
            if chat_type == "private" and incoming_chat_id and not bool(sender.get("is_bot")):
                self._bind_chat(incoming_chat_id, "اولین گفت‌وگوی خصوصی پس از نبود Chat ID")
                return True, incoming_chat_id
            return False, incoming_chat_id

        if owner.startswith("@"):
            authorized = bool(username and username == owner[1:].lower())
        else:
            authorized = owner in {incoming_chat_id, incoming_user_id}

        if authorized:
            # Convert username/user-id ownership to the actual replyable private chat id.
            if incoming_chat_id and self.chat_id != incoming_chat_id and chat_type == "private":
                self._bind_chat(incoming_chat_id, "تطبیق مالک تنظیم‌شده با Chat ID واقعی")
            return True, incoming_chat_id
        return False, incoming_chat_id

    def send_message(
        self,
        text: str,
        reply_to_message_id: int | None = None,
        *,
        chat_id: str | int | None = None,
    ) -> int | None:
        target = str(chat_id if chat_id is not None else self.chat_id or self.owner_ref or "").strip()
        if not self.token or not target:
            logger.error(
                "TELEGRAM_SEND_DISABLED | token=%s chat_id=%s",
                "set" if self.token else "missing",
                "set" if target else "missing/unbound",
            )
            return None
        payload: dict[str, Any] = {"chat_id": target, "text": str(text)}
        if reply_to_message_id:
            payload["reply_to_message_id"] = int(reply_to_message_id)
            payload["allow_sending_without_reply"] = True
        try:
            response = self.send_session.post(self._api_url("sendMessage"), json=payload, timeout=15)
            data = self._response_json(response)
            if not response.ok or not data.get("ok"):
                description = data.get("description") or f"HTTP {response.status_code}"
                raise RuntimeError(str(description))
            return int((data.get("result") or {}).get("message_id") or 0) or None
        except Exception as exc:
            self.storage.set_health("telegram", "warning", f"send failed: {exc}")
            logger.warning("TELEGRAM_SEND_ERROR | target=%s | %s", self._masked_chat_id(target), str(exc)[:240])
            return None

    def _prepare_polling(self) -> None:
        response = self.poll_session.post(
            self._api_url("deleteWebhook"),
            json={"drop_pending_updates": False},
            timeout=15,
        )
        data = self._response_json(response)
        if not response.ok or not data.get("ok"):
            raise RuntimeError(data.get("description") or f"deleteWebhook HTTP {response.status_code}")

        response = self.poll_session.get(self._api_url("getMe"), timeout=15)
        data = self._response_json(response)
        if not response.ok or not data.get("ok"):
            raise RuntimeError(data.get("description") or f"getMe HTTP {response.status_code}")
        username = str((data.get("result") or {}).get("username") or "unknown")
        logger.info(
            "TELEGRAM_POLL_READY | bot=@%s | owner=%s | build=%s",
            username,
            self._masked_chat_id(self.chat_id or self.owner_ref),
            config.BUILD_VERSION,
        )

    def _commit_offset(self, update_id: int) -> int:
        update_id = max(0, int(update_id))
        self.storage.set_telegram_offset(update_id)
        return update_id

    def poll_loop(self) -> None:
        logger.info(
            "TELEGRAM_THREAD_START | token=%s | owner=%s",
            "set" if self.token else "missing",
            self._masked_chat_id(self.chat_id or self.owner_ref),
        )
        if not self.token:
            detail = "متغیر TELEGRAM_BOT_TOKEN/BOT_TOKEN تنظیم نشده است"
            self.storage.set_health("telegram", "warning", detail)
            logger.error("TELEGRAM_DISABLED | %s", detail)
            while not self.stop_event.wait(5):
                pass
            return

        prepared = False
        offset = self.storage.telegram_offset()
        while not self.stop_event.is_set():
            try:
                if not prepared:
                    self._prepare_polling()
                    self.storage.set_health("telegram", "ok", "polling")
                    prepared = True

                params: dict[str, Any] = {
                    "timeout": max(1, int(config.TELEGRAM_POLL_TIMEOUT)),
                    "allowed_updates": json.dumps(["message", "edited_message"], separators=(",", ":")),
                }
                if offset > 0:
                    params["offset"] = offset + 1
                response = self.poll_session.get(
                    self._api_url("getUpdates"),
                    params=params,
                    timeout=max(10, int(config.TELEGRAM_POLL_TIMEOUT) + 10),
                )
                data = self._response_json(response)
                if not response.ok or not data.get("ok"):
                    description = str(data.get("description") or f"HTTP {response.status_code}")
                    if response.status_code == 409 or "webhook" in description.lower():
                        prepared = False
                    raise RuntimeError(description)

                results = data.get("result") or []
                if not isinstance(results, list):
                    raise RuntimeError("getUpdates.result لیست نیست")

                for update in results:
                    if not isinstance(update, dict):
                        continue
                    update_id = int(update.get("update_id") or 0)
                    msg = update.get("message") or update.get("edited_message")
                    if not isinstance(msg, dict):
                        offset = self._commit_offset(max(offset, update_id))
                        continue

                    authorized, incoming_chat_id = self._is_authorized(msg)
                    if not authorized:
                        self.storage.add_event(
                            "TELEGRAM_SECURITY",
                            f"پیام غیرمجاز نادیده گرفته شد: chat={incoming_chat_id}",
                        )
                        logger.warning(
                            "TELEGRAM_UNAUTHORIZED_CHAT | chat_id=%s | user_id=%s | username=%s",
                            incoming_chat_id,
                            str((msg.get("from") or {}).get("id") or ""),
                            str((msg.get("from") or {}).get("username") or ""),
                        )
                        offset = self._commit_offset(max(offset, update_id))
                        continue

                    text = str(msg.get("text") or "").strip()
                    if not text:
                        offset = self._commit_offset(max(offset, update_id))
                        continue

                    normalized = normalize_command(text)
                    logger.info(
                        "TELEGRAM_COMMAND | chat_id=%s | message_id=%s | %s",
                        self._masked_chat_id(incoming_chat_id),
                        int(msg.get("message_id") or 0),
                        normalized[:160],
                    )
                    reply = self.router.handle(text)
                    sent_id = self.send_message(
                        reply,
                        int(msg.get("message_id") or 0) or None,
                        chat_id=incoming_chat_id,
                    )
                    if sent_id is None:
                        # Do not consume the update: after a transient send failure it is retried.
                        raise RuntimeError("پاسخ دستور به تلگرام ارسال نشد؛ offset ذخیره نشد")
                    offset = self._commit_offset(max(offset, update_id))
            except Exception as exc:
                if self.stop_event.is_set():
                    break
                self.storage.set_health("telegram", "warning", f"poll failed: {exc}")
                logger.warning("TELEGRAM_POLL_ERROR | %s", str(exc)[:300])
                self.stop_event.wait(3)

    def notification_loop(self) -> None:
        logger.info("TELEGRAM_NOTIFY_START")
        while not self.stop_event.is_set():
            # Preserve queued signal/result notifications until a replyable numeric chat
            # has been configured or learned from the first authorized private message.
            if not self.chat_id:
                self.stop_event.wait(1)
                continue
            try:
                item = self.engine.notifications.get(timeout=1)
            except queue.Empty:
                continue
            try:
                signal = self.storage.get_signal(int(item["signal_id"]))
                if not signal:
                    continue
                kind = str(item.get("type") or "")
                if kind == "signal":
                    self.send_message(signal_message(signal))
                elif kind == "position_open":
                    self.send_message(position_open_message(signal))
                elif kind == "failed_open":
                    self.send_message(failed_open_message(signal))
                elif kind == "result":
                    self.send_message(result_message(signal))
                else:
                    logger.warning("TELEGRAM_NOTIFY_UNKNOWN | %s", kind)
            except Exception as exc:
                self.storage.set_health("telegram_notify", "warning", str(exc))
                logger.warning("TELEGRAM_NOTIFY_ERROR | %s", str(exc)[:240])
            finally:
                self.engine.notifications.task_done()

    def stop(self) -> None:
        self.stop_event.set()
        self.poll_session.close()
        self.send_session.close()
