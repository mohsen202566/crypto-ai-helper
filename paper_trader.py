
# -*- coding: utf-8 -*-
import json
import os
import time
from datetime import datetime

from auto_trade_config import (
    AUTO_TRADE_STATE_FILE,
    DEFAULT_TRADE_ENABLED,
    DEFAULT_TRADE_MODE,
    DEFAULT_START_BALANCE_USDT,
    DEFAULT_TRADE_MARGIN_USDT,
    DEFAULT_LEVERAGE,
    DEFAULT_MAX_OPEN_POSITIONS,
    DEFAULT_DAILY_MAX_LOSS_USDT,
    DEFAULT_COOLDOWN_AFTER_DAILY_LOSS_HOURS,
    MIN_TRADE_MARGIN_USDT,
    MAX_TRADE_MARGIN_USDT,
    MIN_LEVERAGE,
    MAX_LEVERAGE,
    MIN_MAX_OPEN_POSITIONS,
    MAX_MAX_OPEN_POSITIONS,
)


MIN_START_BALANCE_USDT = 5
MAX_START_BALANCE_USDT = 1000


def now_ts():
    return int(time.time())


def now_text():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def safe_float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return float(default)


def safe_int(value, default=0):
    try:
        return int(value)
    except Exception:
        return int(default)


def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else default
    except Exception:
        return default


def save_json(path, data):
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def default_state():
    return {
        "enabled": bool(DEFAULT_TRADE_ENABLED),
        "mode": DEFAULT_TRADE_MODE,
        "emergency_stop": False,

        "start_balance_usdt": float(DEFAULT_START_BALANCE_USDT),
        "paper_balance_usdt": float(DEFAULT_START_BALANCE_USDT),

        "trade_margin_usdt": float(DEFAULT_TRADE_MARGIN_USDT),
        "leverage": int(DEFAULT_LEVERAGE),
        "max_open_positions": int(DEFAULT_MAX_OPEN_POSITIONS),

        "daily_max_loss_usdt": float(DEFAULT_DAILY_MAX_LOSS_USDT),
        "cooldown_after_daily_loss_hours": int(DEFAULT_COOLDOWN_AFTER_DAILY_LOSS_HOURS),
        "cooldown_until": 0,

        "open_positions": [],
        "closed_positions": [],

        "created_at": now_ts(),
        "updated_at": now_ts(),
    }


def get_state():
    state = load_json(AUTO_TRADE_STATE_FILE, default_state())
    base = default_state()
    base.update(state or {})
    return base


def save_state(state):
    state["updated_at"] = now_ts()
    save_json(AUTO_TRADE_STATE_FILE, state)


def is_cooldown_active(state=None):
    state = state or get_state()
    return now_ts() < safe_int(state.get("cooldown_until"), 0)


def cooldown_text(state=None):
    state = state or get_state()
    until = safe_int(state.get("cooldown_until"), 0)
    if until <= now_ts():
        return "غیرفعال"
    return datetime.fromtimestamp(until).strftime("%Y-%m-%d %H:%M:%S")


def get_today_closed_positions(state=None):
    state = state or get_state()
    today = datetime.now().strftime("%Y-%m-%d")
    result = []
    for p in state.get("closed_positions", []):
        text = str(p.get("closed_at_text", ""))
        if text.startswith(today):
            result.append(p)
    return result


def get_today_pnl(state=None):
    state = state or get_state()
    return round(sum(safe_float(p.get("pnl_usdt")) for p in get_today_closed_positions(state)), 4)


def get_account_base_loss(state=None):
    """
    ضرر واقعی از اصل سرمایه را حساب می‌کند، نه مجموع SLها.
    اگر حساب هنوز بالاتر از بالانس شروع باشد، ضرر = 0 است.
    مثال: شروع 50، بالانس 53.5 => ضرر 0
    مثال: شروع 50، بالانس 43 => ضرر 7
    """
    state = state or get_state()
    start_balance = safe_float(state.get("start_balance_usdt"), DEFAULT_START_BALANCE_USDT)
    current_balance = safe_float(state.get("paper_balance_usdt"), start_balance)
    return round(max(0.0, start_balance - current_balance), 4)


def check_daily_loss_lock(state):
    # قفل ضرر باید فقط وقتی فعال شود که ضرر از اصل سرمایه به حد مجاز برسد،
    # نه وقتی چند SL از سودهای قبلی کم شده‌اند.
    account_base_loss = get_account_base_loss(state)
    max_loss = abs(safe_float(state.get("daily_max_loss_usdt"), DEFAULT_DAILY_MAX_LOSS_USDT))

    if account_base_loss >= max_loss:
        hours = safe_int(state.get("cooldown_after_daily_loss_hours"), DEFAULT_COOLDOWN_AFTER_DAILY_LOSS_HOURS)
        state["enabled"] = False
        state["cooldown_until"] = now_ts() + hours * 3600
        save_state(state)
        return True

    return False


def find_open_position_by_symbol(state, symbol):
    for p in state.get("open_positions", []):
        if p.get("symbol") == symbol and p.get("status") == "OPEN":
            return p
    return None


def find_open_position_by_signal_id(state, signal_id):
    if not signal_id:
        return None
    for p in state.get("open_positions", []):
        if p.get("signal_id") == signal_id and p.get("status") == "OPEN":
            return p
    return None


def open_slots_count(state=None):
    state = state or get_state()
    max_pos = safe_int(state.get("max_open_positions"), DEFAULT_MAX_OPEN_POSITIONS)
    open_count = len([p for p in state.get("open_positions", []) if p.get("status") == "OPEN"])
    return max(0, max_pos - open_count)


def calculate_unrealized_or_realized_pnl(direction, entry, exit_price, margin, leverage):
    entry = safe_float(entry)
    exit_price = safe_float(exit_price)
    margin = safe_float(margin)
    leverage = safe_float(leverage)

    if entry <= 0:
        return 0.0, 0.0

    if direction == "LONG":
        move_pct = ((exit_price - entry) / entry) * 100
    elif direction == "SHORT":
        move_pct = ((entry - exit_price) / entry) * 100
    else:
        move_pct = 0.0

    pnl_usdt = margin * leverage * (move_pct / 100)
    return round(pnl_usdt, 4), round(move_pct, 4)


def can_open_new_trade(signal):
    state = get_state()

    if not state.get("enabled"):
        return False, "ترید غیرفعال است."

    if state.get("mode") != "PAPER":
        return False, "فعلاً فقط حالت Paper فعال است."

    if state.get("emergency_stop"):
        return False, "توقف اضطراری فعال است."

    if is_cooldown_active(state):
        return False, f"قفل ضرر روزانه فعال است تا: {cooldown_text(state)}"

    if check_daily_loss_lock(state):
        return False, f"حد ضرر روزانه فعال شد. ترید تا {cooldown_text(state)} متوقف شد."

    if not signal:
        return False, "سیگنال نامعتبر است."

    if signal.get("status") != "ACTIVE":
        return False, "فقط سیگنال ACTIVE معامله می‌شود."

    if signal.get("direction") not in ["LONG", "SHORT"]:
        return False, "جهت سیگنال معتبر نیست."

    risk_raw = signal.get("risk_level")
    risk = str(risk_raw or "").strip().upper()
    low_risk_values = ["LOW", "LOW_RISK", "پایین", "ریسک پایین", "کم", "LOW ✅"]

    if risk not in [str(x).strip().upper() for x in low_risk_values]:
        shown_risk = risk_raw if risk_raw not in [None, ""] else "نامشخص"
        return False, f"فقط سیگنال‌های ریسک پایین معامله می‌شوند. ریسک این سیگنال: {shown_risk}"

    if signal.get("tp1") is None or signal.get("stop_loss") is None:
        return False, "TP1 یا SL در سیگنال وجود ندارد."

    if open_slots_count(state) <= 0:
        return False, "ظرفیت پوزیشن‌ها کامل است."

    symbol = signal.get("symbol")
    if find_open_position_by_symbol(state, symbol):
        return False, f"روی {symbol} از قبل پوزیشن باز وجود دارد."

    signal_id = signal.get("signal_id") or signal.get("id")
    if find_open_position_by_signal_id(state, signal_id):
        return False, "این سیگنال قبلاً تبدیل به پوزیشن شده است."

    return True, "ok"


def open_paper_trade(signal):
    ok, reason = can_open_new_trade(signal)
    if not ok:
        return False, f"⚠️ Paper Trade باز نشد\nعلت: {reason}"

    state = get_state()

    entry = safe_float(signal.get("entry") or signal.get("activated_price") or signal.get("price"))
    margin = safe_float(state.get("trade_margin_usdt"), DEFAULT_TRADE_MARGIN_USDT)
    leverage = safe_int(state.get("leverage"), DEFAULT_LEVERAGE)
    notional = margin * leverage
    quantity = 0 if entry <= 0 else notional / entry

    position = {
        "id": f"PAPER_{signal.get('symbol')}_{now_ts()}",
        "signal_id": signal.get("signal_id") or signal.get("id"),
        "symbol": signal.get("symbol"),
        "direction": signal.get("direction"),
        "status": "OPEN",

        "entry": entry,
        "stop_loss": safe_float(signal.get("stop_loss")),
        "tp1": safe_float(signal.get("tp1")),
        "tp2": None,

        "margin_usdt": margin,
        "leverage": leverage,
        "notional_usdt": round(notional, 4),
        "quantity": round(quantity, 8),

        "source_message_id": signal.get("message_id"),
        "chat_id": signal.get("chat_id"),

        "opened_at": now_ts(),
        "opened_at_text": now_text(),
    }

    state["open_positions"].append(position)
    save_state(state)

    return True, (
        f"🧪 Paper Trade باز شد\n\n"
        f"نماد: {position['symbol']}\n"
        f"جهت: {'لانگ' if position['direction'] == 'LONG' else 'شورت'}\n"
        f"ورود: {position['entry']}\n"
        f"SL: {position['stop_loss']}\n"
        f"TP1: {position['tp1']}\n"
        f"مارجین: {position['margin_usdt']}$\n"
        f"لوریج: {position['leverage']}x\n"
        f"ارزش پوزیشن: {position['notional_usdt']}$\n"
        f"پوزیشن‌های باز: {len(state['open_positions'])}/{state['max_open_positions']}"
    )


def close_paper_trade_by_signal(signal, result_type, exit_price):
    state = get_state()
    signal_id = signal.get("signal_id") or signal.get("id")
    symbol = signal.get("symbol")

    position = find_open_position_by_signal_id(state, signal_id)
    if not position and symbol:
        position = find_open_position_by_symbol(state, symbol)

    if not position:
        return False, "پوزیشن Paper مربوط به این سیگنال پیدا نشد."

    exit_price = safe_float(exit_price)
    pnl_usdt, move_pct = calculate_unrealized_or_realized_pnl(
        position.get("direction"),
        position.get("entry"),
        exit_price,
        position.get("margin_usdt"),
        position.get("leverage"),
    )

    closed = dict(position)
    closed["status"] = result_type
    closed["exit_price"] = exit_price
    closed["pnl_usdt"] = pnl_usdt
    closed["move_percent"] = move_pct
    closed["closed_at"] = now_ts()
    closed["closed_at_text"] = now_text()

    state["open_positions"] = [
        p for p in state.get("open_positions", [])
        if p.get("id") != position.get("id")
    ]
    state["closed_positions"].append(closed)
    state["paper_balance_usdt"] = round(safe_float(state.get("paper_balance_usdt")) + pnl_usdt, 4)

    check_daily_loss_lock(state)
    save_state(state)

    icon = "✅" if result_type == "TP1" else "❌"
    return True, (
        f"{icon} Paper Trade بسته شد\n\n"
        f"نماد: {closed['symbol']}\n"
        f"نتیجه: {result_type}\n"
        f"ورود: {closed['entry']}\n"
        f"خروج: {closed['exit_price']}\n"
        f"سود/ضرر: {closed['pnl_usdt']}$\n"
        f"درصد حرکت: {closed['move_percent']}٪\n"
        f"بالانس Paper: {state['paper_balance_usdt']}$"
    )


def reset_trade_stats():
    """Reset Paper Trading history while preserving current trade settings."""
    state = get_state()
    start_balance = safe_float(state.get("start_balance_usdt"), DEFAULT_START_BALANCE_USDT)

    state["paper_balance_usdt"] = round(start_balance, 4)
    state["open_positions"] = []
    state["closed_positions"] = []
    state["cooldown_until"] = 0
    state["emergency_stop"] = False
    state["stats_reset_at"] = now_ts()
    state["stats_reset_at_text"] = now_text()

    save_state(state)
    return (
        "✅ آمار Paper Trading ریست شد.\n"
        f"💰 سرمایه اولیه: {state['start_balance_usdt']}$\n"
        f"💰 بالانس فعلی Paper: {state['paper_balance_usdt']}$"
    )


def set_trade_balance(value):
    """Set Paper Trading starting balance and reset Paper Trading stats."""
    raw_value = safe_float(value, -1)

    # سرمایه باید عدد صحیح باشد تا خطای تایپی مثل 50.5 وارد آمار نشود.
    try:
        text_value = str(value).strip()
        if not text_value.isdigit():
            return False, "❌ فرمت درست: سرمایه ترید 50"
    except Exception:
        return False, "❌ فرمت درست: سرمایه ترید 50"

    balance = int(raw_value)
    if balance < MIN_START_BALANCE_USDT or balance > MAX_START_BALANCE_USDT:
        return False, f"❌ سرمایه ترید باید بین {MIN_START_BALANCE_USDT} تا {MAX_START_BALANCE_USDT} دلار باشد."

    state = get_state()
    state["start_balance_usdt"] = float(balance)
    state["paper_balance_usdt"] = float(balance)
    state["open_positions"] = []
    state["closed_positions"] = []
    state["cooldown_until"] = 0
    state["emergency_stop"] = False
    state["stats_reset_at"] = now_ts()
    state["stats_reset_at_text"] = now_text()

    save_state(state)
    return True, (
        f"✅ سرمایه Paper Trading روی {balance}$ تنظیم شد.\n"
        "📊 آمار ترید ریست شد و از نو شروع شد.\n"
        f"💰 بالانس فعلی Paper: {balance}$"
    )


def set_trade_enabled(enabled):
    state = get_state()
    state["enabled"] = bool(enabled)
    if enabled:
        state["emergency_stop"] = False
    save_state(state)
    return "✅ ترید فعال شد." if enabled else "⛔ ترید غیرفعال شد. پوزیشن جدید باز نمی‌شود."


def emergency_stop():
    state = get_state()
    state["enabled"] = False
    state["emergency_stop"] = True
    save_state(state)
    return "🚨 توقف اضطراری فعال شد. هیچ پوزیشن جدیدی باز نمی‌شود."


def set_trade_margin(value):
    value = safe_float(value)
    if value < MIN_TRADE_MARGIN_USDT or value > MAX_TRADE_MARGIN_USDT:
        return False, f"عدد باید بین {MIN_TRADE_MARGIN_USDT} تا {MAX_TRADE_MARGIN_USDT} دلار باشد."

    state = get_state()
    state["trade_margin_usdt"] = value
    save_state(state)
    return True, f"✅ حجم هر پوزیشن برای معاملات بعدی روی {value}$ تنظیم شد."


def set_leverage(value):
    value = safe_int(value)
    if value < MIN_LEVERAGE or value > MAX_LEVERAGE:
        return False, f"لوریج باید بین {MIN_LEVERAGE} تا {MAX_LEVERAGE} باشد."

    state = get_state()
    state["leverage"] = value
    save_state(state)
    return True, f"✅ لوریج معاملات بعدی روی {value}x تنظیم شد."


def set_max_open_positions(value):
    value = safe_int(value)
    if value < MIN_MAX_OPEN_POSITIONS or value > MAX_MAX_OPEN_POSITIONS:
        return False, f"عدد باید بین {MIN_MAX_OPEN_POSITIONS} تا {MAX_MAX_OPEN_POSITIONS} باشد."

    state = get_state()
    state["max_open_positions"] = value
    save_state(state)
    return True, f"✅ حداکثر پوزیشن همزمان روی {value} تنظیم شد."


def format_trade_status():
    state = get_state()
    open_count = len(state.get("open_positions", []))
    max_pos = safe_int(state.get("max_open_positions"), DEFAULT_MAX_OPEN_POSITIONS)

    return f"""🤖 وضعیت ترید

وضعیت: {'فعال ✅' if state.get('enabled') else 'غیرفعال ⛔'}
حالت: {state.get('mode')}
توقف اضطراری: {'فعال 🚨' if state.get('emergency_stop') else 'غیرفعال'}

بالانس Paper: {state.get('paper_balance_usdt')}$
حجم هر پوزیشن: {state.get('trade_margin_usdt')}$
لوریج: {state.get('leverage')}x

پوزیشن باز: {open_count}/{max_pos}
اسلات خالی: {open_slots_count(state)}

سود/ضرر امروز: {get_today_pnl(state)}$
ضرر از اصل سرمایه: {get_account_base_loss(state)}$
حد ضرر از اصل سرمایه: {state.get('daily_max_loss_usdt')}$
قفل ضرر روزانه: {cooldown_text(state)}
"""


def format_open_positions():
    state = get_state()
    positions = state.get("open_positions", [])

    if not positions:
        return "هیچ پوزیشن Paper بازی وجود ندارد."

    lines = ["📌 پوزیشن‌های باز Paper\n"]
    for i, p in enumerate(positions, start=1):
        lines.append(
            f"{i}) {p.get('symbol')} | {p.get('direction')}\n"
            f"ورود: {p.get('entry')}\n"
            f"SL: {p.get('stop_loss')}\n"
            f"TP1: {p.get('tp1')}\n"
            f"مارجین: {p.get('margin_usdt')}$ | لوریج: {p.get('leverage')}x\n"
        )

    return "\n".join(lines)


def format_trade_stats():
    state = get_state()
    closed = state.get("closed_positions", [])
    open_positions = state.get("open_positions", [])

    tp1 = len([p for p in closed if p.get("status") == "TP1"])
    sl = len([p for p in closed if p.get("status") == "SL"])
    total = tp1 + sl
    win_rate = round((tp1 / total) * 100, 1) if total else 0

    total_pnl = round(sum(safe_float(p.get("pnl_usdt")) for p in closed), 4)
    today_pnl = get_today_pnl(state)

    return f"""📊 آمار ترید Paper

وضعیت: {'فعال ✅' if state.get('enabled') else 'غیرفعال ⛔'}

بالانس شروع: {state.get('start_balance_usdt')}$
بالانس فعلی Paper: {state.get('paper_balance_usdt')}$

پوزیشن باز: {len(open_positions)}
پوزیشن بسته‌شده: {total}

TP1: {tp1}
SL: {sl}
Win Rate: {win_rate}٪

سود/ضرر امروز: {today_pnl}$
سود/ضرر کل: {total_pnl}$
ضرر از اصل سرمایه: {get_account_base_loss(state)}$

حجم هر پوزیشن: {state.get('trade_margin_usdt')}$
لوریج: {state.get('leverage')}x
حداکثر پوزیشن: {state.get('max_open_positions')}
"""


def format_trade_settings():
    state = get_state()
    return f"""⚙️ تنظیمات ترید

حالت: {state.get('mode')}
ترید: {'فعال' if state.get('enabled') else 'غیرفعال'}

حجم هر پوزیشن: {state.get('trade_margin_usdt')}$
لوریج: {state.get('leverage')}x
حداکثر پوزیشن: {state.get('max_open_positions')}

حد ضرر روزانه: {state.get('daily_max_loss_usdt')}$
توقف بعد از حد ضرر: {state.get('cooldown_after_daily_loss_hours')} ساعت

TP: فقط TP1
SL: همان SL سیگنال
مارجین: Isolated در نسخه واقعی
Auto Margin Add: OFF در نسخه واقعی
"""


def format_empty_slots():
    state = get_state()
    return f"""🧩 اسلات پوزیشن

حداکثر پوزیشن: {state.get('max_open_positions')}
پوزیشن باز: {len(state.get('open_positions', []))}
اسلات خالی: {open_slots_count(state)}
"""
