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





def _flatten_dicts(value: Any) -> list:
    """Best-effort flattening for nested Toobit response dict/list shapes."""
    out = []
    if isinstance(value, dict):
        out.append(value)
        for v in value.values():
            if isinstance(v, (dict, list)):
                out.extend(_flatten_dicts(v))
    elif isinstance(value, list):
        for item in value:
            out.extend(_flatten_dicts(item))
    return out


def _safe_float_any(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or str(value).strip() == "":
            return default
        return float(value)
    except Exception:
        return default


def _extract_order_execution_info(order_result: Dict[str, Any]) -> Dict[str, Any]:
    """
    Extract execution info from Toobit order response.

    Critical safety rule:
    - PENDING_NEW with executedQty=0 is NOT a real open position.
    - Only executedQty>0 or avgPrice>0/position recovery should be treated as an open trade.
    """
    info = {
        "status": "",
        "executed_qty": 0.0,
        "orig_qty": 0.0,
        "avg_price": 0.0,
        "order_id": "",
        "client_order_id": "",
        "raw_found": False,
    }

    if not isinstance(order_result, dict):
        return info

    for item in _flatten_dicts(order_result.get("data")):
        if not isinstance(item, dict):
            continue

        # Prefer rows that look like an order response.
        if not any(k in item for k in ("status", "executedQty", "origQty", "orderId", "clientOrderId", "avgPrice")):
            continue

        info["raw_found"] = True

        status = item.get("status") or item.get("orderStatus") or item.get("state") or info["status"]
        if status is not None:
            info["status"] = str(status).upper()

        info["executed_qty"] = max(
            info["executed_qty"],
            _safe_float_any(item.get("executedQty")),
            _safe_float_any(item.get("cumQty")),
            _safe_float_any(item.get("filledQty")),
            _safe_float_any(item.get("dealQty")),
        )
        info["orig_qty"] = max(
            info["orig_qty"],
            _safe_float_any(item.get("origQty")),
            _safe_float_any(item.get("quantity")),
            _safe_float_any(item.get("qty")),
        )
        info["avg_price"] = max(
            info["avg_price"],
            _safe_float_any(item.get("avgPrice")),
            _safe_float_any(item.get("priceAvg")),
            _safe_float_any(item.get("filledAvgPrice")),
        )

        if item.get("orderId"):
            info["order_id"] = str(item.get("orderId"))
        if item.get("clientOrderId"):
            info["client_order_id"] = str(item.get("clientOrderId"))

    return info


def _order_is_confirmed_position(order_result: Dict[str, Any]) -> tuple[bool, Dict[str, Any]]:
    """
    Returns True only when the exchange response/recovery confirms an actual position.

    Some Toobit order responses are accepted but remain PENDING_NEW with executedQty=0.
    Those must not consume internal slots or be tracked as open positions.
    """
    if not isinstance(order_result, dict) or not order_result.get("ok"):
        return False, _extract_order_execution_info(order_result)

    # tobit_client_position_recovery.py sets this when it verified a real open position after an API error.
    if order_result.get("recovered_after_error"):
        info = _extract_order_execution_info(order_result)
        info["recovered_after_error"] = True
        return True, info

    info = _extract_order_execution_info(order_result)
    status = str(info.get("status") or "").upper()
    executed_qty = _safe_float_any(info.get("executed_qty"))
    avg_price = _safe_float_any(info.get("avg_price"))

    # Real fills.
    if executed_qty > 0 or avg_price > 0:
        return True, info

    # Explicit pending/new/no-fill statuses are not positions.
    if status in {"PENDING_NEW", "NEW", "PENDING", "CREATED", "ACCEPTED"}:
        return False, info

    # If Toobit changes shape and no clear execution fields exist, be conservative.
    return False, info


def _cleanup_unfilled_internal_positions(state: Dict[str, Any]) -> int:
    """
    Remove stale internal open_positions that are only unfilled Toobit orders.

    This fixes cases where Toobit returned PENDING_NEW/executedQty=0 but the bot
    incorrectly consumed a slot.
    """
    open_positions = state.get("open_positions", {})
    if not isinstance(open_positions, dict) or not open_positions:
        return 0

    removed = 0
    for sid, pos in list(open_positions.items()):
        if not isinstance(pos, dict):
            continue

        exchange_order = pos.get("exchange_order")
        fake_result = {"ok": True, "data": exchange_order}
        confirmed, info = _order_is_confirmed_position(fake_result)

        opened_at = int(pos.get("opened_at", 0) or 0)
        age = _now() - opened_at if opened_at else 999999

        # Remove only clearly unfilled/pending records. Keep unknown old data safer.
        status = str(info.get("status") or "").upper()
        executed_qty = _safe_float_any(info.get("executed_qty"))
        if (not confirmed) and status in {"PENDING_NEW", "NEW", "PENDING", "CREATED", "ACCEPTED"} and executed_qty <= 0 and age >= 10:
            open_positions.pop(sid, None)
            removed += 1

    return removed



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

    if _cleanup_unfilled_internal_positions(state) > 0:
        changed = True

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

    try:
        bal_info = _extract_toobit_usdt_balance(toobit_client.get_account_balance())
        if bal_info.get("ok"):
            available = float(bal_info.get("available_balance") or 0)
            needed = float(state.get("position_size_usd", 0) or 0)
            if available < needed:
                return False, f"بالانس قابل استفاده توبیت کافی نیست ({available}$ < {needed}$)"
    except Exception:
        # Do not crash status/commands if Toobit balance check temporarily fails.
        pass

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
    open_positions = state.get("open_positions", {})
    if not isinstance(open_positions, dict):
        open_positions = {}

    try:
        open_count = len(open_positions)
    except Exception:
        open_count = 0

    try:
        max_positions = int(float(state.get("max_positions", 0) or 0))
    except Exception:
        max_positions = 0

    free_slots = max(max_positions - open_count, 0)

    position_size = _round_usd(state.get("position_size_usd", 0))
    leverage = _round_usd(state.get("leverage", 0))
    approx_position = _round_usd(position_size * leverage, 4)

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

    ready, reason = is_real_trade_ready()
    readiness = "آماده سفارش واقعی" if ready else reason

    return (
        "🤖 وضعیت ترید واقعی توبیت\n"
        f"وضعیت: {status}\n"
        f"حالت: REAL\n"
        f"صرافی: TOOBIT\n"
        f"توقف اضطراری: {emergency}\n"
        f"آمادگی: {readiness}\n\n"
        f"{toobit_balance_line}\n\n"
        f"حجم هر پوزیشن: {position_size}$\n"
        f"لوریج: {leverage}x\n"
        f"حجم تقریبی پوزیشن: {approx_position}$\n\n"
        f"پوزیشن باز: {open_count}/{max_positions}\n"
        f"اسلات خالی: {free_slots}\n\n"
        f"سود/ضرر امروز: {round(float(state.get('today_realized_pnl', 0)), 4)}$\n"
        f"سود/ضرر کل: {round(float(state.get('total_realized_pnl', 0)), 4)}$\n"
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

    confirmed_position, execution_info = _order_is_confirmed_position(order_result)
    if not confirmed_position:
        return {
            "ok": False,
            "blocked": True,
            "error": (
                "سفارش توسط توبیت پذیرفته شد اما هنوز اجرا نشده است؛ "
                "برای جلوگیری از اسلات اشتباه، وارد لیست پوزیشن‌های باز نشد."
            ),
            "order_status": execution_info.get("status"),
            "executed_qty": execution_info.get("executed_qty"),
            "orig_qty": execution_info.get("orig_qty"),
            "order_id": execution_info.get("order_id"),
            "client_order_id": execution_info.get("client_order_id"),
            "exchange_result": order_result.get("data"),
        }

    state = load_real_trade_state()
    if signal_id in state.get("open_positions", {}):
        return {"ok": False, "blocked": True, "error": "این سیگنال قبلاً پوزیشن باز دارد"}

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
        "execution_info": execution_info,
    }

    save_real_trade_state(state)

    return {
        "ok": True,
        "signal_id": signal_id,
        "symbol": symbol,
        "direction": direction,
        "quantity": quantity,
        "order": order_result.get("data"),
        "execution_info": execution_info,
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

# ---------------------------------------------------------------------------
# Robust Toobit synchronization layer
# Added to fix accepted/pending orders that later become real positions and to
# keep internal slots aligned with actual exchange positions.
# ---------------------------------------------------------------------------

PENDING_ORDER_POLL_SECONDS = 8.0
PENDING_ORDER_POLL_INTERVAL = 1.0


def _plain_symbol_from_toobit(value: Any) -> str:
    raw = str(value or "").upper().strip()
    if not raw:
        return ""
    raw = raw.replace("/", "").replace("_", "-")
    if "-SWAP-USDT" in raw:
        return raw.replace("-SWAP-USDT", "USDT")
    if "-SWAP-USDC" in raw:
        return raw.replace("-SWAP-USDC", "USDC")
    raw = raw.replace("-", "")
    return raw


def _position_symbol_from_item(item: Dict[str, Any]) -> str:
    for key in ("symbol", "contractCode", "instrument", "instId", "pair"):
        if item.get(key):
            return _plain_symbol_from_toobit(item.get(key))
    return ""


def _position_direction_from_item(item: Dict[str, Any]) -> str:
    text = " ".join(str(item.get(k, "")) for k in (
        "side", "positionSide", "direction", "positionType", "holdSide", "tradeSide"
    )).upper()
    qty = _safe_float_any(
        item.get("positionAmt") or item.get("positionSize") or item.get("size") or item.get("qty")
        or item.get("quantity") or item.get("positionQuantity") or item.get("availablePosition")
    )
    if "SHORT" in text or "SELL" in text:
        return "SHORT"
    if "LONG" in text or "BUY" in text:
        return "LONG"
    if qty < 0:
        return "SHORT"
    return "LONG"


def _position_entry_from_item(item: Dict[str, Any]) -> float:
    for key in ("entryPrice", "avgPrice", "openPrice", "positionAvgPrice", "averagePrice"):
        v = _safe_float_any(item.get(key))
        if v > 0:
            return v
    return 0.0


def _position_leverage_from_item(item: Dict[str, Any]) -> float:
    for key in ("leverage", "lever", "leverageValue"):
        v = _safe_float_any(item.get(key))
        if v > 0:
            return v
    return 0.0


def _position_qty_from_item(item: Dict[str, Any]) -> float:
    if hasattr(toobit_client, "_position_qty"):
        try:
            return float(toobit_client._position_qty(item))
        except Exception:
            pass
    for key in ("positionAmt", "positionSize", "size", "qty", "quantity", "positionQuantity", "availablePosition"):
        v = _safe_float_any(item.get(key))
        if v != 0:
            return abs(v)
    return 0.0


def get_toobit_open_positions_normalized(symbol: Optional[str] = None) -> Dict[str, Any]:
    """Return normalized open futures positions from Toobit."""
    try:
        result = toobit_client.get_positions(symbol=symbol) if symbol else toobit_client.get_positions()
        if not isinstance(result, dict) or not result.get("ok"):
            return {"ok": False, "error": str((result or {}).get("error") or (result or {}).get("data") or "position fetch failed"), "positions": []}

        if hasattr(toobit_client, "_flatten_position_items"):
            items = toobit_client._flatten_position_items(result)
        else:
            items = _flatten_dicts(result.get("data"))

        positions = []
        for item in items:
            if not isinstance(item, dict):
                continue
            qty = _position_qty_from_item(item)
            if qty <= 0:
                continue
            sym = _position_symbol_from_item(item)
            if not sym:
                continue
            direction = _position_direction_from_item(item)
            positions.append({
                "symbol": sym,
                "direction": direction,
                "quantity": qty,
                "entry": _position_entry_from_item(item),
                "leverage": _position_leverage_from_item(item),
                "raw": item,
            })
        return {"ok": True, "positions": positions, "raw": result}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300], "positions": []}


def _internal_position_matches_exchange(pos: Dict[str, Any], ex: Dict[str, Any]) -> bool:
    return (
        str(pos.get("symbol") or "").upper() == str(ex.get("symbol") or "").upper()
        and str(pos.get("direction") or "").upper() == str(ex.get("direction") or "").upper()
    )


def sync_real_positions_with_toobit(state: Optional[Dict[str, Any]] = None, *, save: bool = True) -> Dict[str, Any]:
    """
    Reconcile internal open_positions with live Toobit futures positions.

    Safety behavior:
    - If exchange has a position that the bot missed, create a recovered internal slot.
    - If internal slot exists but no matching exchange position exists after a grace period,
      mark/remove it to prevent status saying a fake position is open.
    - Never closes exchange positions automatically.
    """
    state = state or load_json(REAL_TRADE_FILE, DEFAULT_REAL_TRADE_STATE.copy())
    if not isinstance(state, dict):
        state = DEFAULT_REAL_TRADE_STATE.copy()
    state.setdefault("open_positions", {})
    if not isinstance(state.get("open_positions"), dict):
        state["open_positions"] = {}

    result = get_toobit_open_positions_normalized()
    if not result.get("ok"):
        return {"ok": False, "error": result.get("error"), "state": state, "exchange_positions": []}

    exchange_positions = result.get("positions") or []
    open_positions = state["open_positions"]
    now = _now()
    added = 0
    removed = 0

    # Add missing exchange positions as recovered slots so status and capacity are truthful.
    for ex in exchange_positions:
        exists = any(_internal_position_matches_exchange(pos, ex) for pos in open_positions.values() if isinstance(pos, dict))
        if exists:
            continue
        sid = f"RECOVERED_{ex.get('symbol')}_{ex.get('direction')}_{now}_{added}"
        open_positions[sid] = {
            "signal_id": sid,
            "symbol": ex.get("symbol"),
            "direction": ex.get("direction"),
            "entry": ex.get("entry") or 0,
            "tp1": None,
            "tp2": None,
            "sl": None,
            "quantity": ex.get("quantity") or 0,
            "position_size_usd": state.get("position_size_usd", 0),
            "leverage": ex.get("leverage") or state.get("leverage", 0),
            "opened_at": now,
            "recovered_from_exchange": True,
            "exchange_position": ex.get("raw"),
            "warning": "این پوزیشن در صرافی باز بود ولی داخل اسلات ربات نبود؛ خودکار sync شد.",
        }
        added += 1

    # Remove stale internal slots only if exchange confirms no matching live position.
    for sid, pos in list(open_positions.items()):
        if not isinstance(pos, dict):
            open_positions.pop(sid, None)
            removed += 1
            continue
        match = any(_internal_position_matches_exchange(pos, ex) for ex in exchange_positions)
        if match:
            continue
        age = now - int(pos.get("opened_at", 0) or 0)
        status = str(((pos.get("execution_info") or {}).get("status")) or "").upper()
        if age >= 15 or status in {"PENDING_NEW", "NEW", "PENDING", "CREATED", "ACCEPTED"}:
            pos.setdefault("closed_or_missing_at", now)
            state.setdefault("orphaned_internal_positions", []).append(pos)
            state["orphaned_internal_positions"] = state["orphaned_internal_positions"][-200:]
            open_positions.pop(sid, None)
            removed += 1

    if save and (added or removed):
        save_real_trade_state(state)

    return {"ok": True, "added": added, "removed": removed, "state": state, "exchange_positions": exchange_positions}


def _wait_for_exchange_position(symbol: str, direction: str, quantity: float, timeout: float = PENDING_ORDER_POLL_SECONDS) -> Dict[str, Any]:
    end = time.time() + float(timeout)
    last_error = ""
    while time.time() <= end:
        try:
            if hasattr(toobit_client, "_has_open_position"):
                opened, position_result = toobit_client._has_open_position(symbol, direction, quantity)
                if opened:
                    return {"ok": True, "position_result": position_result}
                last_error = str((position_result or {}).get("error") or "")[:200]
            else:
                result = get_toobit_open_positions_normalized(symbol)
                if result.get("ok"):
                    for ex in result.get("positions") or []:
                        if str(ex.get("symbol") or "").upper() == str(symbol or "").upper() and str(ex.get("direction") or "").upper() == str(direction or "").upper():
                            return {"ok": True, "position_result": result}
                else:
                    last_error = str(result.get("error") or "")[:200]
        except Exception as e:
            last_error = str(e)[:200]
        time.sleep(PENDING_ORDER_POLL_INTERVAL)
    return {"ok": False, "error": last_error or "position not visible after polling"}


def _register_real_open_position(state: Dict[str, Any], signal: Dict[str, Any], signal_id: str, quantity: float, order_result: Dict[str, Any], execution_info: Dict[str, Any], recovered_note: str = "") -> Dict[str, Any]:
    symbol = signal.get("symbol")
    direction = signal.get("direction")
    entry = float(signal.get("entry", 0) or 0)
    tp1 = signal.get("tp1")
    sl = signal.get("sl") or signal.get("stop_loss")
    state.setdefault("open_positions", {})
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
        "execution_info": execution_info,
        "recovered_note": recovered_note,
    }
    save_real_trade_state(state)
    return state["open_positions"][signal_id]


def is_real_trade_ready() -> tuple[bool, str]:
    state = load_real_trade_state()
    sync_result = sync_real_positions_with_toobit(state, save=True)
    if sync_result.get("ok"):
        state = sync_result.get("state") or state

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

    try:
        bal_info = _extract_toobit_usdt_balance(toobit_client.get_account_balance())
        if bal_info.get("ok"):
            available = float(bal_info.get("available_balance") or 0)
            needed = float(state.get("position_size_usd", 0) or 0)
            if available < needed:
                return False, f"بالانس قابل استفاده توبیت کافی نیست ({available}$ < {needed}$)"
    except Exception:
        pass

    _maybe_apply_daily_lock(state)
    if state.get("daily_loss_locked_until", 0) > _now():
        save_real_trade_state(state)
        return False, "قفل ضرر روزانه فعال شد"
    return True, "آماده ترید واقعی"


def open_real_position_from_signal(signal: Dict[str, Any]) -> Dict[str, Any]:
    ready, reason = is_real_trade_ready()
    if not ready:
        return {"ok": False, "blocked": True, "error": reason}

    symbol = str(signal.get("symbol") or "").upper().strip()
    direction = str(signal.get("direction") or "").upper().strip()
    entry = float(signal.get("entry", 0) or signal.get("price", 0) or 0)
    tp1 = signal.get("tp1")
    sl = signal.get("sl") or signal.get("stop_loss")
    signal_id = str(signal.get("signal_id") or signal.get("id") or f"{symbol}_{direction}_{_now()}")

    if not symbol or direction not in {"LONG", "SHORT"} or entry <= 0:
        return {"ok": False, "error": "اطلاعات سیگنال ناقص است"}

    state = load_real_trade_state()
    sync_result = sync_real_positions_with_toobit(state, save=True)
    if sync_result.get("ok"):
        state = sync_result.get("state") or state

    if signal_id in state.get("open_positions", {}):
        return {"ok": False, "blocked": True, "error": "این سیگنال قبلاً پوزیشن باز دارد"}
    for pos in state.get("open_positions", {}).values():
        if isinstance(pos, dict) and str(pos.get("symbol") or "").upper() == symbol and str(pos.get("direction") or "").upper() == direction:
            return {"ok": False, "blocked": True, "error": f"برای {symbol} {direction} از قبل پوزیشن واقعی باز است"}

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
        err = str(order_result.get("error") or order_result.get("data") or "")
        if "quantity too small" in err.lower() or "qty too small" in err.lower():
            order_result["user_hint"] = "حجم سفارش برای این نماد کم است؛ حجم دلاری یا لوریج را بیشتر کن یا این نماد را از ترید واقعی حذف کن."
        return order_result

    confirmed_position, execution_info = _order_is_confirmed_position(order_result)
    recovered_note = ""

    if not confirmed_position:
        waited = _wait_for_exchange_position(symbol, direction, quantity)
        if waited.get("ok"):
            confirmed_position = True
            execution_info["status"] = execution_info.get("status") or "EXCHANGE_POSITION_FOUND_AFTER_PENDING"
            execution_info["recovered_by_polling"] = True
            recovered_note = "سفارش ابتدا Pending بود، سپس با چک پوزیشن توبیت تایید و وارد اسلات شد."
        else:
            # Final sync before declaring failure, in case polling response shape differed.
            sync_after = sync_real_positions_with_toobit(load_real_trade_state(), save=True)
            if sync_after.get("ok"):
                for pos in (sync_after.get("state") or {}).get("open_positions", {}).values():
                    if isinstance(pos, dict) and str(pos.get("symbol") or "").upper() == symbol and str(pos.get("direction") or "").upper() == direction:
                        return {
                            "ok": True,
                            "signal_id": pos.get("signal_id"),
                            "symbol": symbol,
                            "direction": direction,
                            "quantity": pos.get("quantity"),
                            "order": order_result.get("data"),
                            "execution_info": execution_info,
                            "warning": "پوزیشن از طریق sync صرافی پیدا شد.",
                        }
            return {
                "ok": False,
                "blocked": True,
                "error": (
                    "سفارش توسط توبیت پذیرفته شد اما بعد از چند بار چک، پوزیشن واقعی دیده نشد؛ "
                    "برای جلوگیری از اسلات اشتباه وارد لیست پوزیشن‌های باز نشد."
                ),
                "order_status": execution_info.get("status"),
                "executed_qty": execution_info.get("executed_qty"),
                "orig_qty": execution_info.get("orig_qty"),
                "order_id": execution_info.get("order_id"),
                "client_order_id": execution_info.get("client_order_id"),
                "exchange_result": order_result.get("data"),
            }

    state = load_real_trade_state()
    if signal_id in state.get("open_positions", {}):
        return {"ok": False, "blocked": True, "error": "این سیگنال قبلاً پوزیشن باز دارد"}

    pos = _register_real_open_position(state, signal, signal_id, quantity, order_result, execution_info, recovered_note)
    return {
        "ok": True,
        "signal_id": signal_id,
        "symbol": symbol,
        "direction": direction,
        "quantity": quantity,
        "order": order_result.get("data"),
        "execution_info": execution_info,
        "position": pos,
        "warning": recovered_note or order_result.get("warning"),
    }


def get_real_trade_status_text() -> str:
    state = load_real_trade_state()
    sync_result = sync_real_positions_with_toobit(state, save=True)
    sync_line = ""
    exchange_count = 0
    if sync_result.get("ok"):
        state = sync_result.get("state") or state
        exchange_count = len(sync_result.get("exchange_positions") or [])
        if sync_result.get("added") or sync_result.get("removed"):
            sync_line = f"\nهمگام‌سازی صرافی: +{sync_result.get('added', 0)} / -{sync_result.get('removed', 0)}"
    else:
        sync_line = f"\n⚠️ همگام‌سازی پوزیشن‌های توبیت ناموفق: {str(sync_result.get('error'))[:120]}"

    open_positions = state.get("open_positions", {}) if isinstance(state.get("open_positions"), dict) else {}
    open_count = len(open_positions)
    max_positions = int(float(state.get("max_positions", 0) or 0))
    free_slots = max(max_positions - open_count, 0)

    position_size = _round_usd(state.get("position_size_usd", 0))
    leverage = _round_usd(state.get("leverage", 0))
    approx_position = _round_usd(position_size * leverage, 4)
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

    ready, reason = is_real_trade_ready()
    readiness = "آماده سفارش واقعی" if ready else reason

    leverage_warn = ""
    try:
        actual_levs = sorted({float(p.get("leverage") or 0) for p in (sync_result.get("exchange_positions") or []) if float(p.get("leverage") or 0) > 0})
        if actual_levs and leverage > 0 and any(abs(x - leverage) > 0.01 for x in actual_levs):
            leverage_warn = f"\n⚠️ لوریج واقعی پوزیشن در توبیت با تنظیم ربات فرق دارد: {actual_levs}"
    except Exception:
        pass

    return (
        "🤖 وضعیت ترید واقعی توبیت\n"
        f"وضعیت: {status}\n"
        f"حالت: REAL\n"
        f"صرافی: TOOBIT\n"
        f"توقف اضطراری: {emergency}\n"
        f"آمادگی: {readiness}\n"
        f"پوزیشن واقعی در توبیت: {exchange_count}\n"
        f"{sync_line}{leverage_warn}\n\n"
        f"{toobit_balance_line}\n\n"
        f"حجم هر پوزیشن: {position_size}$\n"
        f"لوریج تنظیمی ربات: {leverage}x\n"
        f"حجم تقریبی پوزیشن: {approx_position}$\n\n"
        f"پوزیشن باز داخلی/اسلات: {open_count}/{max_positions}\n"
        f"اسلات خالی: {free_slots}\n\n"
        f"سود/ضرر امروز: {round(float(state.get('today_realized_pnl', 0)), 4)}$\n"
        f"سود/ضرر کل: {round(float(state.get('total_realized_pnl', 0)), 4)}$\n"
        f"حد ضرر روزانه: {state.get('daily_loss_limit_usd', 0)}$\n"
        f"زمان قفل: {state.get('daily_lock_duration_hours', DEFAULT_REAL_LOCK_DURATION_HOURS)} ساعت\n"
        f"قفل ضرر روزانه: {lock_line}"
    )
