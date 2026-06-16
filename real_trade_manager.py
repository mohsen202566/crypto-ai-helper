# real_trade_manager.py
# Safe real trading manager for Toobit Futures
# Default state: OFF / zero values / no real order
#
# Notes:
# - Real orders are still blocked by tobit_client unless REAL_TRADING_ENABLED=true.
# - This manager keeps an internal risk/profit state for REAL trading.
# - Protected balance logic:
#     * Initial capital starts protected_balance.
#     * Every full $1 of realized positive profit is added to protected_balance.
#     * Profit remainder below $1 stays in profit_carry_remainder.
#     * Daily loss lock is calculated from protected_balance, not only initial capital.

import time
from typing import Dict, Any, Optional

from data_store import load_json, save_json
from tobit_client import toobit_client


REAL_TRADE_FILE = "data/real_trade_state.json"

DEFAULT_REAL_LOCK_DURATION_HOURS = 1
DEFAULT_REAL_DAILY_LOSS_LIMIT_USD = 7.0


DEFAULT_REAL_TRADE_STATE = {
    "enabled": False,
    "exchange": "TOOBIT",
    "mode": "REAL",
    "emergency_stop": False,

    "initial_capital": 0.0,
    "balance": 0.0,
    "protected_balance": 0.0,
    "profit_carry_remainder": 0.0,

    "position_size_usd": 0.0,
    "leverage": 0.0,
    "max_positions": 0,

    "open_positions": {},
    "closed_positions": [],

    "total_realized_pnl": 0.0,
    "today_realized_pnl": 0.0,

    "daily_loss_limit_usd": DEFAULT_REAL_DAILY_LOSS_LIMIT_USD,
    "daily_lock_duration_hours": DEFAULT_REAL_LOCK_DURATION_HOURS,
    "daily_loss_locked_until": 0,
    "daily_lock_reason": "",

    "created_at": 0,
    "updated_at": 0,
}


def _now() -> int:
    return int(time.time())


def _round_usd(value: Any, digits: int = 6) -> float:
    try:
        return round(float(value), digits)
    except Exception:
        return 0.0


def _extract_toobit_usdt_balance(balance_result: Dict[str, Any]) -> Dict[str, Any]:
    """
    Extract USDT futures balance from Toobit balance response.
    Returns numeric strings so display does not break if Toobit changes precision.
    """
    out = {
        "ok": False,
        "balance": "0",
        "available_balance": "0",
        "error": "",
    }

    try:
        if not isinstance(balance_result, dict) or not balance_result.get("ok"):
            out["error"] = str((balance_result or {}).get("error") or (balance_result or {}).get("data") or "unknown")
            return out

        data = balance_result.get("data")

        # Common Toobit response from current bot:
        # [{'balance': '...', 'availableBalance': '...', 'asset': 'USDT', ...}]
        items = []
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            if isinstance(data.get("data"), list):
                items = data.get("data")
            elif isinstance(data.get("result"), list):
                items = data.get("result")
            else:
                items = [data]

        selected = None
        for item in items:
            if isinstance(item, dict) and str(item.get("asset") or item.get("coin") or "").upper() == "USDT":
                selected = item
                break
        if selected is None and items and isinstance(items[0], dict):
            selected = items[0]

        if not isinstance(selected, dict):
            out["error"] = "USDT balance item not found"
            return out

        balance = (
            selected.get("balance")
            or selected.get("walletBalance")
            or selected.get("totalBalance")
            or selected.get("equity")
            or "0"
        )
        available = (
            selected.get("availableBalance")
            or selected.get("available")
            or selected.get("free")
            or balance
            or "0"
        )

        out["ok"] = True
        out["balance"] = str(balance)
        out["available_balance"] = str(available)
        return out

    except Exception as e:
        out["error"] = str(e)
        return out




def _today_key(ts: Optional[int] = None) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts or _now()))


def _new_day_if_needed(state: Dict[str, Any]) -> None:
    today = _today_key()
    last_day = str(state.get("daily_pnl_day") or "")
    if last_day != today:
        state["daily_pnl_day"] = today
        state["today_realized_pnl"] = 0.0
        state["daily_loss_locked_until"] = 0
        state["daily_lock_reason"] = ""


def load_real_trade_state() -> Dict[str, Any]:
    state = load_json(REAL_TRADE_FILE, DEFAULT_REAL_TRADE_STATE.copy())

    if not isinstance(state, dict):
        state = DEFAULT_REAL_TRADE_STATE.copy()

    changed = False
    for k, v in DEFAULT_REAL_TRADE_STATE.items():
        if k not in state:
            state[k] = v
            changed = True

    initial = _round_usd(state.get("initial_capital", 0))
    total_pnl = _round_usd(state.get("total_realized_pnl", 0))

    if _round_usd(state.get("balance", 0)) <= 0 and initial > 0:
        state["balance"] = _round_usd(initial + total_pnl)
        changed = True

    if _round_usd(state.get("protected_balance", 0)) <= 0 and initial > 0:
        state["protected_balance"] = initial
        changed = True

    if _round_usd(state.get("daily_loss_limit_usd", 0)) <= 0:
        state["daily_loss_limit_usd"] = DEFAULT_REAL_DAILY_LOSS_LIMIT_USD
        changed = True

    if int(state.get("daily_lock_duration_hours", 0) or 0) <= 0:
        state["daily_lock_duration_hours"] = DEFAULT_REAL_LOCK_DURATION_HOURS
        changed = True

    state.setdefault("closed_positions", [])
    state.setdefault("daily_lock_reason", "")
    state.setdefault("daily_pnl_day", _today_key())

    before_day = state.get("daily_pnl_day")
    _new_day_if_needed(state)
    if state.get("daily_pnl_day") != before_day:
        changed = True

    if changed:
        save_real_trade_state(state)

    return state


def save_real_trade_state(state: Dict[str, Any]) -> None:
    state["updated_at"] = _now()
    if not state.get("created_at"):
        state["created_at"] = _now()
    save_json(REAL_TRADE_FILE, state)


def get_real_loss_from_protected(state: Optional[Dict[str, Any]] = None) -> float:
    state = state or load_real_trade_state()
    protected = _round_usd(state.get("protected_balance", 0))
    balance = _round_usd(state.get("balance", state.get("initial_capital", 0)))
    return _round_usd(max(0.0, protected - balance))


def _apply_full_dollar_profit_to_protected(state: Dict[str, Any], pnl_usd: float) -> int:
    pnl_usd = float(pnl_usd)
    if pnl_usd <= 0:
        return 0

    remainder = _round_usd(state.get("profit_carry_remainder", 0))
    total_remainder = remainder + pnl_usd
    whole_dollars = int(total_remainder)

    if whole_dollars > 0:
        state["protected_balance"] = _round_usd(
            _round_usd(state.get("protected_balance", state.get("initial_capital", 0))) + whole_dollars
        )
        total_remainder -= whole_dollars

    state["profit_carry_remainder"] = _round_usd(max(0.0, total_remainder))
    return whole_dollars


def _maybe_apply_daily_lock(state: Dict[str, Any]) -> bool:
    if int(state.get("daily_loss_locked_until", 0) or 0) > _now():
        return True

    max_loss = _round_usd(state.get("daily_loss_limit_usd", DEFAULT_REAL_DAILY_LOSS_LIMIT_USD))
    if max_loss <= 0:
        return False

    loss_from_protected = get_real_loss_from_protected(state)
    if loss_from_protected >= max_loss:
        hours = int(state.get("daily_lock_duration_hours", DEFAULT_REAL_LOCK_DURATION_HOURS) or DEFAULT_REAL_LOCK_DURATION_HOURS)
        hours = max(1, min(hours, 168))
        state["enabled"] = False
        state["daily_loss_locked_until"] = _now() + hours * 3600
        state["daily_lock_reason"] = (
            f"افت {round(loss_from_protected, 4)}$ از سرمایه محافظت‌شده "
            f"(حد مجاز: {round(max_loss, 4)}$)"
        )
        return True

    return False


def is_real_trade_ready() -> tuple[bool, str]:
    state = load_real_trade_state()

    if not state.get("enabled"):
        return False, "ترید واقعی خاموش است"

    if state.get("emergency_stop"):
        return False, "توقف اضطراری فعال است"

    if state.get("daily_loss_locked_until", 0) > _now():
        remaining = round((int(state.get("daily_loss_locked_until", 0)) - _now()) / 3600, 2)
        return False, f"قفل ضرر روزانه فعال است؛ حدود {remaining} ساعت باقی مانده"

    if float(state.get("initial_capital", 0)) <= 0:
        return False, "سرمایه ترید تنظیم نشده است"

    if float(state.get("position_size_usd", 0)) <= 0:
        return False, "حجم هر پوزیشن تنظیم نشده است"

    if float(state.get("leverage", 0)) <= 0:
        return False, "لوریج تنظیم نشده است"

    if int(state.get("max_positions", 0)) <= 0:
        return False, "حداکثر پوزیشن تنظیم نشده است"

    if len(state.get("open_positions", {})) >= int(state.get("max_positions", 0)):
        return False, "ظرفیت پوزیشن‌ها پر است"

    _maybe_apply_daily_lock(state)
    if state.get("daily_loss_locked_until", 0) > _now():
        save_real_trade_state(state)
        return False, "قفل ضرر روزانه فعال شد"

    return True, "آماده ترید واقعی"


def set_real_initial_capital(amount: float) -> str:
    state = load_real_trade_state()
    amount = float(amount)

    if amount <= 0:
        return "❌ سرمایه باید بیشتر از صفر باشد."

    state["initial_capital"] = amount

    if _round_usd(state.get("balance", 0)) <= 0 or not state.get("closed_positions"):
        state["balance"] = amount
    if _round_usd(state.get("protected_balance", 0)) <= 0 or not state.get("closed_positions"):
        state["protected_balance"] = amount
        state["profit_carry_remainder"] = 0.0

    if float(state.get("daily_loss_limit_usd", 0)) <= 0:
        state["daily_loss_limit_usd"] = DEFAULT_REAL_DAILY_LOSS_LIMIT_USD

    save_real_trade_state(state)
    return f"✅ سرمایه ترید واقعی تنظیم شد: {amount}$"


def set_real_position_size(amount: float) -> str:
    state = load_real_trade_state()
    amount = float(amount)

    if amount < 1 or amount > 1000000:
        return "❌ حجم پوزیشن باید بین 1 تا 1000000 دلار باشد."

    state["position_size_usd"] = amount
    save_real_trade_state(state)
    return f"✅ حجم هر پوزیشن واقعی تنظیم شد: {amount}$"


def set_real_leverage(leverage: float) -> str:
    state = load_real_trade_state()
    leverage = float(leverage)

    if leverage < 1 or leverage > 1000000:
        return "❌ لوریج باید بین 1 تا 1000000 باشد."

    state["leverage"] = leverage
    save_real_trade_state(state)
    return f"✅ لوریج ترید واقعی تنظیم شد: {leverage}x"


def set_real_max_positions(count: int) -> str:
    state = load_real_trade_state()
    count = int(count)

    if count < 1 or count > 100:
        return "❌ حداکثر پوزیشن باید بین 1 تا 100 باشد."

    state["max_positions"] = count
    save_real_trade_state(state)
    return f"✅ حداکثر پوزیشن واقعی تنظیم شد: {count}"


def set_real_daily_loss_limit(amount: float) -> str:
    state = load_real_trade_state()
    amount = float(amount)

    if amount <= 0:
        return "❌ حد ضرر روزانه باید بیشتر از صفر باشد."

    state["daily_loss_limit_usd"] = round(amount, 4)
    _maybe_apply_daily_lock(state)
    save_real_trade_state(state)
    return f"✅ حد ضرر روزانه واقعی تنظیم شد: {round(amount, 4)}$"


def set_real_lock_duration_hours(hours: int) -> str:
    state = load_real_trade_state()
    hours = int(hours)

    if hours < 1 or hours > 168:
        return "❌ زمان قفل باید بین 1 تا 168 ساعت باشد."

    state["daily_lock_duration_hours"] = hours
    save_real_trade_state(state)
    return f"✅ زمان قفل ضرر واقعی تنظیم شد: {hours} ساعت"


def enable_real_trading() -> str:
    state = load_real_trade_state()

    checks = [
        ("initial_capital", "سرمایه ترید"),
        ("position_size_usd", "حجم هر پوزیشن"),
        ("leverage", "لوریج"),
        ("max_positions", "حداکثر پوزیشن"),
    ]

    missing = []
    for key, label in checks:
        if float(state.get(key, 0)) <= 0:
            missing.append(label)

    if missing:
        return "❌ ترید واقعی فعال نشد.\nاول این موارد را تنظیم کن:\n" + "\n".join(f"• {x}" for x in missing)

    _maybe_apply_daily_lock(state)
    if int(state.get("daily_loss_locked_until", 0) or 0) > _now():
        save_real_trade_state(state)
        return "❌ ترید واقعی فعال نشد.\nقفل ضرر روزانه فعال است."

    state["enabled"] = True
    state["emergency_stop"] = False
    save_real_trade_state(state)

    return "✅ ترید واقعی فعال شد.\n⚠️ از این لحظه فقط سیگنال‌های واجد شرایط می‌توانند سفارش واقعی ثبت کنند."


def disable_real_trading() -> str:
    state = load_real_trade_state()
    state["enabled"] = False
    save_real_trade_state(state)
    return "⛔ ترید واقعی خاموش شد."


def activate_real_emergency_stop() -> str:
    state = load_real_trade_state()
    state["enabled"] = False
    state["emergency_stop"] = True
    save_real_trade_state(state)
    return "🚨 توقف اضطراری فعال شد و ترید واقعی خاموش شد."


def reset_real_trade_state() -> str:
    state = DEFAULT_REAL_TRADE_STATE.copy()
    state["created_at"] = _now()
    state["updated_at"] = _now()
    state["daily_pnl_day"] = _today_key()
    save_real_trade_state(state)
    return "✅ تنظیمات ترید واقعی ریست شد. همه مقدارها صفر و ترید خاموش است."


def record_realized_pnl(
    pnl_usd: float,
    signal_id: Optional[str] = None,
    symbol: Optional[str] = None,
    direction: Optional[str] = None,
    result: str = "REALIZED",
    exit_price: Optional[float] = None,
) -> Dict[str, Any]:
    state = load_real_trade_state()
    _new_day_if_needed(state)

    pnl_usd = _round_usd(pnl_usd)
    state["balance"] = _round_usd(_round_usd(state.get("balance", state.get("initial_capital", 0))) + pnl_usd)
    state["total_realized_pnl"] = _round_usd(_round_usd(state.get("total_realized_pnl", 0)) + pnl_usd)
    state["today_realized_pnl"] = _round_usd(_round_usd(state.get("today_realized_pnl", 0)) + pnl_usd)

    protected_added = _apply_full_dollar_profit_to_protected(state, pnl_usd)

    closed_record = {
        "signal_id": signal_id,
        "symbol": symbol,
        "direction": direction,
        "result": result,
        "exit_price": exit_price,
        "pnl_usd": pnl_usd,
        "protected_added": protected_added,
        "balance_after": state.get("balance"),
        "protected_balance_after": state.get("protected_balance"),
        "closed_at": _now(),
    }
    state.setdefault("closed_positions", []).append(closed_record)
    state["closed_positions"] = state["closed_positions"][-2000:]

    _maybe_apply_daily_lock(state)
    save_real_trade_state(state)

    return {
        "ok": True,
        "pnl_usd": pnl_usd,
        "protected_added": protected_added,
        "balance": state.get("balance"),
        "protected_balance": state.get("protected_balance"),
        "daily_locked": int(state.get("daily_loss_locked_until", 0) or 0) > _now(),
        "loss_from_protected": get_real_loss_from_protected(state),
    }


def get_real_trade_status_text() -> str:
    state = load_real_trade_state()
    open_count = len(state.get("open_positions", {}))
    max_positions = int(state.get("max_positions", 0))
    free_slots = max(max_positions - open_count, 0)

    approx_position = float(state.get("position_size_usd", 0)) * float(state.get("leverage", 0))

    status = "✅ فعال" if state.get("enabled") else "⛔ غیرفعال"
    emergency = "فعال" if state.get("emergency_stop") else "غیرفعال"

    lock_line = "غیرفعال"
    if int(state.get("daily_loss_locked_until", 0) or 0) > _now():
        remaining = round((int(state.get("daily_loss_locked_until", 0)) - _now()) / 3600, 2)
        lock_line = f"فعال، حدود {remaining} ساعت باقی مانده"

    toobit_balance_line = "بالانس واقعی توبیت: نامشخص"
    try:
        toobit_balance_info = _extract_toobit_usdt_balance(toobit_client.get_account_balance())
        if toobit_balance_info.get("ok"):
            toobit_balance_line = (
                f"بالانس واقعی توبیت: {toobit_balance_info.get('balance')}$\n"
                f"بالانس قابل استفاده توبیت: {toobit_balance_info.get('available_balance')}$"
            )
        else:
            toobit_balance_line = f"بالانس واقعی توبیت: خطا ({str(toobit_balance_info.get('error'))[:120]})"
    except Exception as e:
        toobit_balance_line = f"بالانس واقعی توبیت: خطا ({str(e)[:120]})"

    return (
        "🤖 وضعیت ترید واقعی توبیت\n"
        f"وضعیت: {status}\n"
        f"حالت: REAL\n"
        f"صرافی: TOOBIT\n"
        f"توقف اضطراری: {emergency}\n\n"
        f"سرمایه اولیه: {state.get('initial_capital', 0)}$\n"
        f"{toobit_balance_line}\n"
        f"بالانس حسابداری داخلی: {round(float(state.get('balance', 0)), 4)}$\n"
        f"سرمایه محافظت‌شده: {round(float(state.get('protected_balance', 0)), 4)}$\n"
        f"سود ذخیره زیر 1 دلار: {round(float(state.get('profit_carry_remainder', 0)), 4)}$\n\n"
        f"حجم هر پوزیشن: {state.get('position_size_usd', 0)}$\n"
        f"لوریج: {state.get('leverage', 0)}x\n"
        f"حجم تقریبی پوزیشن: {round(approx_position, 4)}$\n\n"
        f"پوزیشن باز: {open_count}/{max_positions}\n"
        f"اسلات خالی: {free_slots}\n\n"
        f"سود/ضرر امروز: {round(float(state.get('today_realized_pnl', 0)), 4)}$\n"
        f"سود/ضرر کل: {round(float(state.get('total_realized_pnl', 0)), 4)}$\n"
        f"افت از سرمایه محافظت‌شده: {get_real_loss_from_protected(state)}$\n"
        f"حد ضرر روزانه: {state.get('daily_loss_limit_usd', 0)}$\n"
        f"زمان قفل: {state.get('daily_lock_duration_hours', DEFAULT_REAL_LOCK_DURATION_HOURS)} ساعت\n"
        f"قفل ضرر روزانه: {lock_line}"
    )


def get_toobit_balance_text() -> str:
    result = toobit_client.get_account_balance()

    if not result.get("ok"):
        return f"❌ دریافت بالانس توبیت ناموفق بود:\n{result.get('error') or result.get('data')}"

    info = _extract_toobit_usdt_balance(result)
    if info.get("ok"):
        return (
            "✅ بالانس توبیت\n"
            f"بالانس کل USDT: {info.get('balance')}$\n"
            f"بالانس قابل استفاده USDT: {info.get('available_balance')}$"
        )

    return f"✅ پاسخ بالانس توبیت:\n{result.get('data')}"


def calculate_order_quantity(entry_price: float) -> float:
    state = load_real_trade_state()
    position_usd = float(state.get("position_size_usd", 0))
    leverage = float(state.get("leverage", 0))

    if entry_price <= 0 or position_usd <= 0 or leverage <= 0:
        return 0.0

    notional = position_usd * leverage
    quantity = notional / float(entry_price)
    return round(quantity, 6)


def open_real_position_from_signal(signal: Dict[str, Any]) -> Dict[str, Any]:
    ready, reason = is_real_trade_ready()
    if not ready:
        return {"ok": False, "blocked": True, "error": reason}

    symbol = signal.get("symbol")
    direction = signal.get("direction")
    entry = float(signal.get("entry", 0))
    tp1 = signal.get("tp1")
    sl = signal.get("sl") or signal.get("stop_loss")
    signal_id = signal.get("signal_id") or signal.get("id") or f"{symbol}_{direction}_{_now()}"

    if not symbol or not direction or entry <= 0:
        return {"ok": False, "error": "اطلاعات سیگنال ناقص است"}

    state = load_real_trade_state()

    if signal_id in state.get("open_positions", {}):
        return {"ok": False, "blocked": True, "error": "این سیگنال قبلاً پوزیشن باز دارد"}

    quantity = calculate_order_quantity(entry)
    if quantity <= 0:
        return {"ok": False, "error": "محاسبه حجم سفارش نامعتبر است"}

    order_result = toobit_client.place_market_order(
        symbol=symbol,
        direction=direction,
        quantity=quantity,
        take_profit=tp1,
        stop_loss=sl,
    )

    if not order_result.get("ok"):
        return order_result

    state["open_positions"][signal_id] = {
        "signal_id": signal_id,
        "symbol": symbol,
        "direction": direction,
        "entry": entry,
        "tp1": tp1,
        "tp2": signal.get("tp2"),
        "sl": sl,
        "quantity": quantity,
        "position_size_usd": state.get("position_size_usd", 0),
        "leverage": state.get("leverage", 0),
        "opened_at": _now(),
        "exchange_order": order_result.get("data"),
    }

    save_real_trade_state(state)

    return {
        "ok": True,
        "signal_id": signal_id,
        "symbol": symbol,
        "direction": direction,
        "quantity": quantity,
        "order": order_result.get("data"),
    }


def close_real_position(
    signal_id: str,
    pnl_usd: Optional[float] = None,
    exit_price: Optional[float] = None,
    result_type: str = "MANUAL_CLOSE",
) -> Dict[str, Any]:
    state = load_real_trade_state()
    pos = state.get("open_positions", {}).get(signal_id)

    if not pos:
        return {"ok": False, "error": "پوزیشن پیدا نشد"}

    result = toobit_client.close_market_position(
        symbol=pos["symbol"],
        direction=pos["direction"],
        quantity=float(pos["quantity"]),
    )

    if not result.get("ok"):
        return result

    state["open_positions"].pop(signal_id, None)
    save_real_trade_state(state)

    accounting = None
    if pnl_usd is not None:
        accounting = record_realized_pnl(
            pnl_usd=float(pnl_usd),
            signal_id=signal_id,
            symbol=pos.get("symbol"),
            direction=pos.get("direction"),
            result=result_type,
            exit_price=exit_price,
        )

    return {
        "ok": True,
        "closed": True,
        "signal_id": signal_id,
        "exchange_result": result.get("data"),
        "accounting": accounting,
    }
