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

try:
    from coin_learning import register_dynamic_profit_exit, register_tp_sl_v2_result
except Exception:
    register_dynamic_profit_exit = None
    register_tp_sl_v2_result = None

try:
    from coin_learning import get_similarity_adjustment, find_similar_patterns
except Exception:
    get_similarity_adjustment = None
    find_similar_patterns = None

try:
    from tobit_client import normalize_bot_plain_symbol
except Exception:
    def normalize_bot_plain_symbol(symbol: str) -> str:
        raw = str(symbol or "").upper().strip()
        if not raw:
            return ""
        raw = raw.replace("/", "").replace("_", "-")
        if raw.endswith("-SWAP-USDT"):
            raw = raw.replace("-SWAP-USDT", "USDT")
        elif raw.endswith("-SWAP-USDC"):
            raw = raw.replace("-SWAP-USDC", "USDC")
        raw = raw.replace("-", "").replace("SWAP", "")
        for prefix in ("1000000", "10000", "1000"):
            if raw.startswith(prefix) and raw.endswith("USDT"):
                return raw[len(prefix):]
        return raw


REAL_TRADE_FILE = "real_trade_state.json"

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

    "daily_loss_protection_enabled": True,
    "daily_loss_limit_usd": DEFAULT_REAL_DAILY_LOSS_LIMIT_USD,
    "daily_lock_duration_hours": DEFAULT_REAL_LOCK_DURATION_HOURS,
    "daily_loss_locked_until": 0,
    "daily_lock_reason": "",
    "daily_lock_loss_value": 0.0,
    "daily_lock_auto_reenable": False,

    # Special high-priority AI trade-management update.
    # Applies only to profitable TP-side management for both LONG and SHORT;
    # it never moves/changes SL placement.
    # Goal: if an open trade is already in profit and evidence shows the trade
    # probably cannot continue to TP, close immediately and preserve whatever
    # profit exists. This is NOT a trailing stop and never acts on losing trades.
    "dynamic_profit_protection_enabled": True,
    "dynamic_profit_min_profit_pct": 0.01,
    "dynamic_profit_min_tp_progress": 0.0,
    "dynamic_profit_retrace_trigger_pct": 0.06,
    "dynamic_profit_shock_retrace_pct": 0.05,
    "dynamic_profit_score_exit_threshold": 6.0,
    "dynamic_profit_last_check": 0,

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


def _calc_move_percent(direction: str, entry: float, price: float) -> float:
    """Signed move percentage in trade direction. Positive means the position is in profit."""
    e = _safe_float_any(entry, 0.0)
    p2 = _safe_float_any(price, 0.0)
    if e <= 0 or p2 <= 0:
        return 0.0
    d = str(direction or "").upper().strip()
    if d == "SHORT":
        return ((e - p2) / e) * 100.0
    return ((p2 - e) / e) * 100.0


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
        is_pending_real = str(pos.get("real_status") or "").upper() == PENDING_REAL_CONFIRM_STATUS
        if (
            (not confirmed)
            and status in {"PENDING_NEW", "NEW", "PENDING", "CREATED", "ACCEPTED", PENDING_REAL_CONFIRM_STATUS}
            and executed_qty <= 0
            and age >= PENDING_SLOT_GRACE_SECONDS
            and not is_pending_real
        ):
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
        state["daily_lock_loss_value"] = 0.0
        state["daily_lock_auto_reenable"] = False


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

    # Keep daily loss protection truly disabled when the user sets it to 0/off.
    # Older state files may not have this flag; default is enabled for backward compatibility.
    if "daily_loss_protection_enabled" not in state:
        state["daily_loss_protection_enabled"] = True
        changed = True

    if bool(state.get("daily_loss_protection_enabled", True)):
        if _round_usd(state.get("daily_loss_limit_usd", 0)) <= 0:
            state["daily_loss_limit_usd"] = DEFAULT_REAL_DAILY_LOSS_LIMIT_USD
            changed = True
        if int(state.get("daily_lock_duration_hours", 0) or 0) <= 0:
            state["daily_lock_duration_hours"] = DEFAULT_REAL_LOCK_DURATION_HOURS
            changed = True
    else:
        # Disabled means no current lock, no timer, and no automatic re-lock from old loss.
        if _round_usd(state.get("daily_loss_limit_usd", 0)) != 0:
            state["daily_loss_limit_usd"] = 0.0
            changed = True
        if int(state.get("daily_lock_duration_hours", 0) or 0) != 0:
            state["daily_lock_duration_hours"] = 0
            changed = True
        if int(state.get("daily_loss_locked_until", 0) or 0) != 0:
            state["daily_loss_locked_until"] = 0
            changed = True
        if state.get("daily_lock_reason"):
            state["daily_lock_reason"] = ""
            changed = True
        if _round_usd(state.get("daily_lock_loss_value", 0)) != 0:
            state["daily_lock_loss_value"] = 0.0
            changed = True
        if bool(state.get("daily_lock_auto_reenable")):
            state["daily_lock_auto_reenable"] = False
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

    if _refresh_daily_lock_state(state):
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


def _refresh_daily_lock_state(state: Dict[str, Any]) -> bool:
    """
    Clear an expired daily loss lock and optionally re-enable real trading.

    Behavior:
    - When cooldown is finished, clear daily_loss_locked_until.
    - If trading was enabled when the lock was created, enable it again after
      expiry, unless emergency_stop is active or basic trade settings are missing.
    - Do not create a fresh lock from the same already-handled loss. A new lock
      is allowed only if loss_from_protected increases above daily_lock_loss_value.
    """
    changed = False
    locked_until = int(state.get("daily_loss_locked_until", 0) or 0)

    if locked_until and locked_until <= _now():
        state["daily_loss_locked_until"] = 0
        state["daily_lock_reason"] = ""
        changed = True

        if bool(state.get("daily_lock_auto_reenable")) and not bool(state.get("emergency_stop")):
            try:
                configured = (
                    float(state.get("initial_capital", 0) or 0) > 0
                    and float(state.get("position_size_usd", 0) or 0) > 0
                    and float(state.get("leverage", 0) or 0) > 0
                    and int(float(state.get("max_positions", 0) or 0)) > 0
                )
            except Exception:
                configured = False

            if configured:
                state["enabled"] = True

        state["daily_lock_auto_reenable"] = False

    if not bool(state.get("daily_loss_protection_enabled", True)):
        if _round_usd(state.get("daily_loss_limit_usd", 0)) != 0:
            state["daily_loss_limit_usd"] = 0.0
            changed = True
        if int(state.get("daily_lock_duration_hours", 0) or 0) != 0:
            state["daily_lock_duration_hours"] = 0
            changed = True
        if int(state.get("daily_loss_locked_until", 0) or 0) != 0:
            state["daily_loss_locked_until"] = 0
            changed = True
        if state.get("daily_lock_reason"):
            state["daily_lock_reason"] = ""
            changed = True
        if _round_usd(state.get("daily_lock_loss_value", 0)) != 0:
            state["daily_lock_loss_value"] = 0.0
            changed = True
        if bool(state.get("daily_lock_auto_reenable")):
            state["daily_lock_auto_reenable"] = False
            changed = True
        return changed

    max_loss = _round_usd(state.get("daily_loss_limit_usd", DEFAULT_REAL_DAILY_LOSS_LIMIT_USD))
    current_loss = get_real_loss_from_protected(state)

    # If loss recovered below the configured limit, reset the remembered lock
    # loss so future losses can trigger normally again.
    if max_loss > 0 and current_loss < max_loss:
        if _round_usd(state.get("daily_lock_loss_value", 0)) != 0:
            state["daily_lock_loss_value"] = 0.0
            changed = True

    return changed


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
    _refresh_daily_lock_state(state)

    if not bool(state.get("daily_loss_protection_enabled", True)):
        state["daily_loss_limit_usd"] = 0.0
        state["daily_lock_duration_hours"] = 0
        state["daily_loss_locked_until"] = 0
        state["daily_lock_reason"] = ""
        state["daily_lock_loss_value"] = 0.0
        state["daily_lock_auto_reenable"] = False
        return False

    if int(state.get("daily_loss_locked_until", 0) or 0) > _now():
        return True

    max_loss = _round_usd(state.get("daily_loss_limit_usd", DEFAULT_REAL_DAILY_LOSS_LIMIT_USD))
    if max_loss <= 0:
        return False

    loss_from_protected = get_real_loss_from_protected(state)
    last_lock_loss = _round_usd(state.get("daily_lock_loss_value", 0))

    # Do not relock from the same already-handled loss after cooldown expiry.
    # A new lock is allowed only if the loss increases after the previous lock.
    if loss_from_protected >= max_loss and loss_from_protected > last_lock_loss + 0.000001:
        hours = int(state.get("daily_lock_duration_hours", DEFAULT_REAL_LOCK_DURATION_HOURS) or DEFAULT_REAL_LOCK_DURATION_HOURS)
        hours = max(1, min(hours, 168))
        was_enabled = bool(state.get("enabled"))

        state["enabled"] = False
        state["daily_loss_locked_until"] = _now() + hours * 3600
        state["daily_lock_reason"] = (
            f"افت {round(loss_from_protected, 4)}$ از سرمایه محافظت‌شده "
            f"(حد مجاز: {round(max_loss, 4)}$)"
        )
        state["daily_lock_loss_value"] = _round_usd(loss_from_protected)
        state["daily_lock_auto_reenable"] = was_enabled
        return True

    return False


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

    if bool(state.get("daily_loss_protection_enabled", True)) and float(state.get("daily_loss_limit_usd", 0)) <= 0:
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

    if leverage < 1 or leverage > 50:
        return "❌ لوریج باید بین 1 تا 50 باشد."

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
        was_locked = int(state.get("daily_loss_locked_until", 0) or 0) > _now()
        state["daily_loss_protection_enabled"] = False
        state["daily_loss_limit_usd"] = 0.0
        state["daily_lock_duration_hours"] = 0
        state["daily_loss_locked_until"] = 0
        state["daily_lock_reason"] = ""
        state["daily_lock_loss_value"] = 0.0
        state["daily_lock_auto_reenable"] = False

        # If trading was only blocked by the daily-loss lock, allow trading again immediately.
        try:
            configured = (
                float(state.get("initial_capital", 0) or 0) > 0
                and float(state.get("position_size_usd", 0) or 0) > 0
                and float(state.get("leverage", 0) or 0) > 0
                and int(float(state.get("max_positions", 0) or 0)) > 0
            )
        except Exception:
            configured = False
        if was_locked and configured and not bool(state.get("emergency_stop")):
            state["enabled"] = True

        save_real_trade_state(state)
        return "✅ قفل/حد ضرر روزانه واقعی خاموش شد و تایمر قفل صفر شد."

    state["daily_loss_protection_enabled"] = True
    state["daily_loss_limit_usd"] = round(amount, 4)
    if int(state.get("daily_lock_duration_hours", 0) or 0) <= 0:
        state["daily_lock_duration_hours"] = DEFAULT_REAL_LOCK_DURATION_HOURS
    # When re-enabled, start clean so the previous handled loss does not instantly re-lock
    # unless a new/larger loss is recorded later.
    if get_real_loss_from_protected(state) < round(amount, 4):
        state["daily_lock_loss_value"] = 0.0
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
    entry: Optional[float] = None,
    stop_loss: Optional[float] = None,
    tp1: Optional[float] = None,
    tp2: Optional[float] = None,
    snapshot: Optional[Dict[str, Any]] = None,
    max_favorable: Optional[float] = None,
    max_adverse: Optional[float] = None,
    sr_event: Optional[str] = None,
    fake_breakout: Optional[bool] = None,
    **extra: Any,
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
        "entry": entry,
        "exit_price": exit_price,
        "stop_loss": stop_loss,
        "tp1": tp1,
        "tp2": tp2,
        "pnl_usd": pnl_usd,
        "protected_added": protected_added,
        "balance_after": state.get("balance"),
        "protected_balance_after": state.get("protected_balance"),
        "closed_at": _now(),
    }
    if isinstance(snapshot, dict) and snapshot:
        closed_record["snapshot"] = snapshot
    if max_favorable is not None:
        closed_record["max_favorable"] = max_favorable
    if max_adverse is not None:
        closed_record["max_adverse"] = max_adverse
    if sr_event is not None:
        closed_record["sr_event"] = sr_event
    if fake_breakout is not None:
        closed_record["fake_breakout"] = fake_breakout
    if extra:
        closed_record["extra"] = extra
    state.setdefault("closed_positions", []).append(closed_record)
    state["closed_positions"] = state["closed_positions"][-2000:]

    _maybe_apply_daily_lock(state)
    save_real_trade_state(state)

    # Learning hook: keep TP/SL v2 memory aligned with real closed trades.
    # This is best-effort and never blocks accounting or real-trade state.
    try:
        if register_tp_sl_v2_result and symbol and direction:
            register_tp_sl_v2_result(
                symbol=symbol,
                direction=direction,
                result=result,
                entry=entry,
                stop_loss=stop_loss,
                tp1=tp1,
                tp2=tp2,
                snapshot=snapshot if isinstance(snapshot, dict) else {},
                source="REAL",
                max_favorable=max_favorable,
                max_adverse=max_adverse,
                sr_event=sr_event,
                fake_breakout=fake_breakout,
                signal_id=signal_id,
                exit_price=exit_price,
                move_percent=extra.get("move_percent") if isinstance(extra, dict) else None,
            )
    except Exception:
        pass

    return {
        "ok": True,
        "pnl_usd": pnl_usd,
        "protected_added": protected_added,
        "balance": state.get("balance"),
        "protected_balance": state.get("protected_balance"),
        "daily_locked": int(state.get("daily_loss_locked_until", 0) or 0) > _now(),
        "loss_from_protected": get_real_loss_from_protected(state),
    }


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


def _base_symbol(symbol: str) -> str:
    """Extract base coin from symbols like BTCUSDT or BTC-SWAP-USDT."""
    raw = str(symbol or "").upper().strip()
    raw = raw.replace("/", "").replace("_", "").replace("-", "")
    for quote in ("USDT", "USDC", "USD"):
        if raw.endswith(quote):
            raw = raw[: -len(quote)]
            break
    raw = raw.replace("SWAP", "")
    return raw


# Conservative local minimum-quantity estimates.
# This prevents Toobit -1202 "quantity too small" before sending an order.
# It does NOT increase order size; small orders are blocked safely.
MIN_QTY_BY_BASE_SYMBOL = {
    "BTC": 0.001,
    "ETH": 0.01,
    "BNB": 0.01,
    "BCH": 0.01,
    "SOL": 0.1,
    "LTC": 0.01,
    "AAVE": 0.01,
    "MKR": 0.001,
    "XMR": 0.01,
    "DASH": 0.01,
    "AVAX": 0.1,
    "LINK": 0.1,
    "ETC": 0.1,
    "UNI": 0.1,
}


def _estimated_min_quantity(symbol: str, entry_price: float) -> float:
    """Best-effort min quantity until exchange symbol filters are available."""
    base = _base_symbol(symbol)
    if base in MIN_QTY_BY_BASE_SYMBOL:
        return float(MIN_QTY_BY_BASE_SYMBOL[base])

    price = float(entry_price or 0)
    if price >= 50000:
        return 0.001
    if price >= 500:
        return 0.01
    if price >= 10:
        return 0.1
    if price >= 1:
        return 1.0
    if price >= 0.1:
        return 10.0
    return 100.0



def _toobit_normalized_symbol(symbol: str) -> str:
    """Return the exact Toobit futures symbol used for signed order routes."""
    try:
        return str(toobit_client.normalize_futures_symbol(symbol)).upper().strip()
    except Exception:
        raw = str(symbol or "").upper().strip().replace("/", "").replace("_", "").replace("-", "")
        if raw.endswith("USDT"):
            return f"{raw[:-4]}-SWAP-USDT"
        if raw.endswith("USDC"):
            return f"{raw[:-4]}-SWAP-USDC"
        return raw


def _toobit_contract_multiplier(symbol: str) -> float:
    """
    Detect Toobit's multiplier contracts such as 1000SHIB-SWAP-USDT.

    The bot/analysis works with normal symbols and normal prices, e.g. SHIBUSDT
    at 0.000012. Toobit may trade 1000SHIB-SWAP-USDT at 0.012. For those pairs:
      - Toobit order prices/TP/SL must be multiplied by the multiplier.
      - Toobit order quantity must be divided by the multiplier.
    """
    bot_plain = normalize_bot_plain_symbol(symbol)
    if not bot_plain.endswith("USDT") and not bot_plain.endswith("USDC"):
        return 1.0

    bot_base = bot_plain
    for quote in ("USDT", "USDC"):
        if bot_base.endswith(quote):
            bot_base = bot_base[: -len(quote)]
            break

    normalized = _toobit_normalized_symbol(symbol)
    contract = normalized.split("-SWAP-")[0].replace("-", "")
    digits = ""
    for ch in contract:
        if ch.isdigit():
            digits += ch
        else:
            break

    if digits and contract[len(digits):] == bot_base:
        try:
            mult = float(digits)
            return mult if mult > 1 else 1.0
        except Exception:
            return 1.0
    return 1.0


def _scale_price_for_toobit(symbol: str, price: Optional[float]) -> Optional[float]:
    if price is None:
        return None
    try:
        value = float(price)
    except Exception:
        return None
    if value <= 0:
        return None
    return _round_usd(value * _toobit_contract_multiplier(symbol), 12)


def _scale_quantity_for_toobit(symbol: str, quantity: float) -> float:
    try:
        qty = float(quantity or 0)
    except Exception:
        return 0.0
    mult = _toobit_contract_multiplier(symbol)
    if mult <= 1:
        return qty
    scaled = qty / mult
    if scaled >= 1:
        return round(scaled, 6)
    return round(scaled, 8)


def _prepare_toobit_order_values(symbol: str, entry: float, tp1: Optional[float], sl: Optional[float], quantity: float) -> Dict[str, Any]:
    """Build exchange-safe values without changing the bot's internal signal values."""
    mult = _toobit_contract_multiplier(symbol)
    return {
        "bot_symbol": normalize_bot_plain_symbol(symbol),
        "toobit_symbol": _toobit_normalized_symbol(symbol),
        "toobit_multiplier": mult,
        "bot_entry": float(entry or 0),
        "bot_tp1": None if tp1 is None else float(tp1),
        "bot_sl": None if sl is None else float(sl),
        "bot_quantity": float(quantity or 0),
        "toobit_entry": _scale_price_for_toobit(symbol, entry),
        "toobit_tp1": _scale_price_for_toobit(symbol, tp1),
        "toobit_sl": _scale_price_for_toobit(symbol, sl),
        "toobit_quantity": _scale_quantity_for_toobit(symbol, quantity),
    }

def calculate_order_quantity(entry_price: float, symbol: Optional[str] = None) -> float:
    state = load_real_trade_state()
    position_usd = float(state.get("position_size_usd", 0))
    leverage = float(state.get("leverage", 0))

    if entry_price <= 0 or position_usd <= 0 or leverage <= 0:
        return 0.0

    notional = position_usd * leverage
    quantity = notional / float(entry_price)

    # Keep enough precision for high-price coins. Do not round up because that
    # can exceed the user's configured real-money risk.
    if quantity >= 1:
        return round(quantity, 6)
    return round(quantity, 8)


def _safe_get_toobit_symbol_rules(symbol: str, reference_price: float, quantity: float) -> Dict[str, Any]:
    """Read Toobit symbol rules when the client supports it; never raise."""
    getter = getattr(toobit_client, "get_symbol_trading_rules", None)
    if not callable(getter):
        return {"source": "real_trade_manager_estimate_only"}
    try:
        rules = getter(symbol, reference_price=reference_price, quantity=quantity)
        return rules if isinstance(rules, dict) else {"source": "invalid_rules_response", "raw": rules}
    except Exception as e:
        return {"source": "rules_read_failed", "error": str(e)[:250]}


def _decimal_rule_value(rules: Dict[str, Any], key: str, default: float = 0.0) -> float:
    try:
        value = (rules or {}).get(key)
        if value is None or str(value).strip() == "":
            return float(default)
        return float(value)
    except Exception:
        return float(default)


def _round_quantity_to_client_step(quantity: float, qty_step: float) -> float:
    """Mirror the client-side down-rounding when possible, for pre-check only."""
    try:
        if qty_step and qty_step > 0 and hasattr(toobit_client, "_round_to_step"):
            return float(toobit_client._round_to_step(quantity, qty_step, mode="down"))
    except Exception:
        pass
    try:
        return float(quantity)
    except Exception:
        return 0.0


def validate_order_quantity(symbol: str, entry_price: float, quantity: float) -> Dict[str, Any]:
    """Pre-check quantity before sending to Toobit.

    This does not increase the user's order size. It only blocks orders that
    would likely fail with Toobit -1202 (quantity too small), using the same
    Toobit symbol rules that tobit_client.py uses when available.
    """
    state = load_real_trade_state()
    position_usd = float(state.get("position_size_usd", 0) or 0)
    leverage = float(state.get("leverage", 0) or 0)
    configured_notional = position_usd * leverage

    try:
        price = float(entry_price or 0)
        qty = float(quantity or 0)
    except Exception:
        return {"ok": False, "error": "محاسبه حجم سفارش نامعتبر است", "blocked_reason": "INVALID_QUANTITY"}

    if price <= 0 or qty <= 0:
        return {"ok": False, "error": "محاسبه حجم سفارش نامعتبر است", "blocked_reason": "INVALID_QUANTITY"}

    rules = _safe_get_toobit_symbol_rules(symbol, reference_price=price, quantity=qty)
    qty_step = _decimal_rule_value(rules, "qty_step", 0.0)
    exchange_min_qty = _decimal_rule_value(rules, "min_qty", 0.0)
    exchange_min_notional = _decimal_rule_value(rules, "min_notional", 0.0)

    # Fallback only when Toobit rules do not expose a usable minQty. Keep this
    # conservative and local; do not use it to increase order size automatically.
    estimated_min_qty = _estimated_min_quantity(symbol, price)
    effective_min_qty = exchange_min_qty if exchange_min_qty > 0 else float(estimated_min_qty or 0)

    # If minNotional is available, derive a min quantity from the exchange value.
    derived_min_qty = 0.0
    if exchange_min_notional > 0 and price > 0:
        derived_min_qty = exchange_min_notional / price
        if qty_step > 0 and hasattr(toobit_client, "_round_to_step"):
            try:
                derived_min_qty = float(toobit_client._round_to_step(derived_min_qty, qty_step, mode="up"))
            except Exception:
                pass
        effective_min_qty = max(effective_min_qty, derived_min_qty)

    normalized_qty = _round_quantity_to_client_step(qty, qty_step)
    min_notional_usd = effective_min_qty * price if effective_min_qty > 0 else exchange_min_notional

    if effective_min_qty > 0 and normalized_qty < effective_min_qty:
        return {
            "ok": False,
            "blocked": True,
            "blocked_reason": "QUANTITY_BELOW_TOOBIT_MIN",
            "error": (
                f"حجم سفارش برای {symbol} کمتر از حداقل مجاز توبیت است "
                f"({normalized_qty} < {effective_min_qty})."
            ),
            "user_hint": (
                f"برای این نماد حداقل حجم تقریبی/صرافی حدود {round(min_notional_usd, 4)}$ است. "
                f"تنظیم فعلی تو {round(configured_notional, 4)}$ است؛ حجم دلاری/لوریج را بیشتر کن یا این نماد را رد کن."
            ),
            "quantity": round(qty, 10),
            "normalized_quantity": round(normalized_qty, 10),
            "min_quantity": round(effective_min_qty, 10),
            "min_notional": round(min_notional_usd, 6),
            "configured_notional": round(configured_notional, 6),
            "rules": rules,
        }

    return {
        "ok": True,
        "quantity": round(qty, 10),
        "normalized_quantity": round(normalized_qty, 10),
        "min_quantity": round(effective_min_qty, 10),
        "min_notional": round(min_notional_usd, 6),
        "configured_notional": round(configured_notional, 6),
        "rules": rules,
    }



def _save_real_close_pending_state(
    signal_id: str,
    pos: Dict[str, Any],
    close_result: Dict[str, Any],
    verify_result: Dict[str, Any],
) -> Dict[str, Any]:
    """Mark a real position as pending close without freeing slot/accounting.

    This state prevents repeated Telegram/error spam and prevents sending close
    orders back-to-back. The tracker/manager can retry after
    next_close_retry_at. The position remains in open_positions until Toobit
    confirms it is actually gone.
    """
    state = load_real_trade_state()
    open_positions = state.setdefault("open_positions", {})
    current = open_positions.get(signal_id)
    if not isinstance(current, dict):
        current = dict(pos or {})
        open_positions[signal_id] = current

    attempts = int(current.get("close_attempts", 0) or 0) + 1
    current["real_status"] = PENDING_REAL_CLOSE_STATUS
    current["close_pending"] = True
    current["close_order_sent"] = True
    current["close_attempts"] = attempts
    current["last_close_attempt_at"] = _now()
    current["next_close_retry_at"] = _now() + int(REAL_CLOSE_RETRY_DELAY_SECONDS)
    current["last_close_result"] = close_result
    current["last_close_verify_result"] = verify_result
    current["last_close_error"] = "سفارش خروج ارسال شد ولی بسته‌شدن پوزیشن در توبیت هنوز تایید نشد"
    save_real_trade_state(state)
    return current


def _close_retry_not_allowed_yet(pos: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return a throttling response if a pending close retry is too early."""
    if not isinstance(pos, dict):
        return None
    if str(pos.get("real_status") or "").upper() != PENDING_REAL_CLOSE_STATUS:
        return None
    next_retry = int(pos.get("next_close_retry_at", 0) or 0)
    if next_retry > _now():
        return {
            "ok": False,
            "closed": False,
            "close_pending": True,
            "retry_allowed": False,
            "next_close_retry_at": next_retry,
            "seconds_until_retry": max(0, next_retry - _now()),
            "close_attempts": int(pos.get("close_attempts", 0) or 0),
            "error": "خروج قبلاً ارسال شده؛ برای جلوگیری از رد شدن توسط توبیت هنوز زمان تلاش بعدی نرسیده است.",
        }
    return None


def close_real_position(
    signal_id: str,
    pnl_usd: Optional[float] = None,
    exit_price: Optional[float] = None,
    result_type: str = "MANUAL_CLOSE",
    require_exchange_confirm: bool = True,
) -> Dict[str, Any]:
    """Close a REAL Toobit position safely with pending/retry protection.

    Behavior:
    - Sends one close order only when retry is allowed.
    - Verifies that the matching Toobit position disappeared.
    - If still visible, keeps the internal position open as PENDING_CLOSE and
      sets next_close_retry_at instead of freeing the slot or recording PnL.
    - A later call can retry after REAL_CLOSE_RETRY_DELAY_SECONDS.
    """
    state = load_real_trade_state()
    pos = state.get("open_positions", {}).get(signal_id)

    if not isinstance(pos, dict):
        return {"ok": False, "closed": False, "error": "پوزیشن پیدا نشد"}

    symbol = str(pos.get("symbol") or "")
    direction = str(pos.get("direction") or "")
    quantity = _safe_float_any(pos.get("quantity"), 0.0)

    if not symbol or not direction or quantity <= 0:
        return {
            "ok": False,
            "closed": False,
            "error": "اطلاعات پوزیشن برای بستن کامل نیست",
            "symbol": symbol,
            "direction": direction,
            "quantity": quantity,
        }

    # If a close order was already sent and Toobit still had not confirmed the
    # close, do not hammer the exchange with back-to-back close requests.
    throttled = _close_retry_not_allowed_yet(pos)
    if throttled is not None:
        throttled["signal_id"] = signal_id
        return throttled

    attempts_so_far = int(pos.get("close_attempts", 0) or 0)
    if attempts_so_far >= int(REAL_CLOSE_MAX_ATTEMPTS):
        return {
            "ok": False,
            "closed": False,
            "close_pending": True,
            "max_attempts_reached": True,
            "signal_id": signal_id,
            "close_attempts": attempts_so_far,
            "error": "حداکثر تلاش برای بستن پوزیشن انجام شده؛ نیاز به بررسی دستی توبیت دارد.",
        }

    result = toobit_client.close_market_position(
        symbol=symbol,
        direction=direction,
        quantity=float(quantity),
    )

    if not isinstance(result, dict) or not result.get("ok"):
        # Keep retry timing even on API rejection, because immediate repeated
        # requests can make Toobit reject again.
        verify_result = {"ok": False, "closed_confirmed": False, "error": "close order rejected/not ok"}
        _save_real_close_pending_state(signal_id, pos, result if isinstance(result, dict) else {"raw": result}, verify_result)
        return {
            "ok": False,
            "closed": False,
            "close_pending": True,
            "signal_id": signal_id,
            "error": (result or {}).get("error") if isinstance(result, dict) else str(result),
            "exchange_result": result.get("data") if isinstance(result, dict) else result,
            "next_close_retry_at": _now() + int(REAL_CLOSE_RETRY_DELAY_SECONDS),
        }

    verify_result = {"ok": True, "closed_confirmed": True, "skipped": True}
    if require_exchange_confirm:
        verify_result = _verify_exchange_position_closed(symbol=symbol, direction=direction)
        if not verify_result.get("ok"):
            pending_pos = _save_real_close_pending_state(signal_id, pos, result, verify_result)
            return {
                "ok": False,
                "closed": False,
                "close_pending": True,
                "close_order_sent": True,
                "retry_allowed": False,
                "signal_id": signal_id,
                "close_attempts": int(pending_pos.get("close_attempts", 0) or 0),
                "next_close_retry_at": pending_pos.get("next_close_retry_at"),
                "error": "سفارش خروج ارسال شد ولی بسته‌شدن پوزیشن در توبیت هنوز تایید نشد",
                "exchange_result": result.get("data"),
                "verify_result": verify_result,
            }

    # Reload state after verification to avoid overwriting concurrent sync/state
    # changes, then remove only the confirmed closed position.
    state = load_real_trade_state()
    pos = state.get("open_positions", {}).get(signal_id, pos)
    state.setdefault("open_positions", {}).pop(signal_id, None)
    save_real_trade_state(state)

    accounting = None
    move_pct = None
    if exit_price is not None:
        move_pct = _calc_move_percent(
            str(pos.get("direction") or ""),
            _safe_float_any(pos.get("entry"), 0.0),
            _safe_float_any(exit_price, 0.0),
        )

    if pnl_usd is None and exit_price is not None:
        try:
            entry = _safe_float_any(pos.get("entry"), 0.0)
            margin = _safe_float_any(pos.get("position_size_usd") or state.get("position_size_usd"), 0.0)
            leverage = _safe_float_any(pos.get("leverage") or state.get("leverage"), 0.0)
            if entry > 0 and margin > 0 and leverage > 0:
                pnl_usd = _round_usd(margin * leverage * float(move_pct or 0.0) / 100.0)
        except Exception:
            pnl_usd = None

    if pnl_usd is not None:
        accounting = record_realized_pnl(
            pnl_usd=float(pnl_usd),
            signal_id=signal_id,
            symbol=pos.get("symbol"),
            direction=pos.get("direction"),
            result=result_type,
            exit_price=exit_price,
            entry=pos.get("entry"),
            stop_loss=pos.get("sl") or pos.get("stop_loss"),
            tp1=pos.get("tp1"),
            tp2=pos.get("tp2"),
            snapshot=_position_learning_snapshot(pos, {
                "exit_price": exit_price,
                "move_percent": move_pct,
                "result_type": result_type,
                "exchange_close_confirmed": True,
            }),
            max_favorable=pos.get("max_favorable_percent"),
            max_adverse=pos.get("max_adverse_percent"),
            pnl_source="APPROX_FROM_EXIT_PRICE" if result_type == "AI_DYNAMIC_PROFIT_EXIT" else "APPROX_OR_MANUAL",
            move_percent=move_pct,
            exchange_close_confirmed=True,
        )

    return {
        "ok": True,
        "closed": True,
        "close_order_sent": True,
        "close_confirmed": True,
        "close_pending": False,
        "signal_id": signal_id,
        "exchange_result": result.get("data"),
        "verify_result": verify_result,
        "accounting": accounting,
    }


# ---------------------------------------------------------------------------
# Robust Toobit synchronization layer
# Added to fix accepted/pending orders that later become real positions and to
# keep internal slots aligned with actual exchange positions.
# ---------------------------------------------------------------------------

PENDING_ORDER_POLL_SECONDS = 60.0
PENDING_ORDER_POLL_INTERVAL = 2.0
PENDING_SLOT_GRACE_SECONDS = 75

PENDING_REAL_CONFIRM_STATUS = "PENDING_REAL_CONFIRM"

# After sending a dynamic/manual close order, do not mark a real trade as closed
# until Toobit no longer reports the matching live position. This prevents the
# bot from freeing slots or recording profit while the exchange position is
# still open.
REAL_CLOSE_VERIFY_SECONDS = 12.0
REAL_CLOSE_VERIFY_INTERVAL = 1.5

# Anti-spam close retry state. If Toobit accepts a close order but the
# position is still visible, keep the internal position as PENDING_CLOSE and
# do not send another close order until this delay has passed. This avoids
# back-to-back close requests that Toobit may reject, while still retrying
# automatically later.
PENDING_REAL_CLOSE_STATUS = "PENDING_REAL_CLOSE"
REAL_CLOSE_RETRY_DELAY_SECONDS = 5
REAL_CLOSE_MAX_ATTEMPTS = 6


def _verify_exchange_position_closed(symbol: str, direction: str, timeout: float = REAL_CLOSE_VERIFY_SECONDS) -> Dict[str, Any]:
    """Poll Toobit briefly and confirm the matching futures position is gone.

    Returns ok=True only when the exchange position is not visible anymore.
    If Toobit position fetch fails, the function is conservative and returns
    ok=False so the tracker keeps monitoring instead of assuming a close.
    """
    end = time.time() + float(timeout)
    last_error = ""
    last_positions = []
    plain_symbol = normalize_bot_plain_symbol(str(symbol or ""))
    direct = str(direction or "").upper()

    while time.time() <= end:
        result = get_toobit_open_positions_normalized(symbol)
        if not isinstance(result, dict) or not result.get("ok"):
            last_error = str((result or {}).get("error") or "position fetch failed")[:250]
            time.sleep(float(REAL_CLOSE_VERIFY_INTERVAL))
            continue

        positions = result.get("positions") or []
        last_positions = positions
        still_open = False
        for ex in positions:
            if not isinstance(ex, dict):
                continue
            ex_symbol = normalize_bot_plain_symbol(str(ex.get("symbol") or ""))
            ex_direction = str(ex.get("direction") or "").upper()
            if ex_symbol == plain_symbol and ex_direction == direct:
                still_open = True
                break

        if not still_open:
            return {"ok": True, "closed_confirmed": True, "positions": positions}

        time.sleep(float(REAL_CLOSE_VERIFY_INTERVAL))

    return {
        "ok": False,
        "closed_confirmed": False,
        "error": last_error or "position still visible after close order",
        "positions": last_positions,
    }


def _plain_symbol_from_toobit(value: Any) -> str:
    """Convert Toobit/futures symbols to the bot plain symbol, e.g. 1000SHIB -> SHIBUSDT."""
    try:
        return normalize_bot_plain_symbol(str(value or ""))
    except Exception:
        raw = str(value or "").upper().strip()
        if not raw:
            return ""
        raw = raw.replace("/", "").replace("_", "-")
        if "-SWAP-USDT" in raw:
            raw = raw.replace("-SWAP-USDT", "USDT")
        elif "-SWAP-USDC" in raw:
            raw = raw.replace("-SWAP-USDC", "USDC")
        raw = raw.replace("-", "").replace("SWAP", "")
        for prefix in ("1000000", "10000", "1000"):
            if raw.startswith(prefix) and raw.endswith("USDT"):
                return raw[len(prefix):]
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
    pos_symbol = normalize_bot_plain_symbol(str(pos.get("symbol") or ""))
    ex_symbol = normalize_bot_plain_symbol(str(ex.get("symbol") or ""))
    return (
        pos_symbol == ex_symbol
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
            "margin_mode": "UNKNOWN_RECOVERED",
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
        is_pending_real = str(pos.get("real_status") or "").upper() == PENDING_REAL_CONFIRM_STATUS
        is_exchange_pending = status in {"PENDING_NEW", "NEW", "PENDING", "CREATED", "ACCEPTED", PENDING_REAL_CONFIRM_STATUS}

        # Keep newly sent real-order slots reserved long enough for Toobit to
        # expose the futures position through the API. Do not free Pending slots
        # immediately just because the order status is NEW/PENDING.
        if (is_pending_real or is_exchange_pending) and age < PENDING_SLOT_GRACE_SECONDS:
            continue

        if age >= PENDING_SLOT_GRACE_SECONDS:
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
                        if normalize_bot_plain_symbol(str(ex.get("symbol") or "")) == normalize_bot_plain_symbol(str(symbol or "")) and str(ex.get("direction") or "").upper() == str(direction or "").upper():
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
        "bot_quantity": (signal.get("toobit_order") or {}).get("bot_quantity"),
        "toobit_quantity": quantity,
        "toobit_symbol": (signal.get("toobit_order") or {}).get("toobit_symbol"),
        "toobit_multiplier": (signal.get("toobit_order") or {}).get("toobit_multiplier", 1.0),
        "toobit_entry": (signal.get("toobit_order") or {}).get("toobit_entry"),
        "toobit_tp1": (signal.get("toobit_order") or {}).get("toobit_tp1"),
        "toobit_sl": (signal.get("toobit_order") or {}).get("toobit_sl"),
        "position_size_usd": state.get("position_size_usd", 0),
        "leverage": state.get("leverage", 0),
        "margin_mode": "ISOLATED",
        "opened_at": _now(),
        "exchange_order": order_result.get("data"),
        "execution_info": execution_info,
        "recovered_note": recovered_note,
        "snapshot": signal.get("snapshot") if isinstance(signal.get("snapshot"), dict) else {},
        "max_favorable_percent": 0.0,
        "max_adverse_percent": 0.0,
        "dynamic_profit_protection": {"enabled": True, "applies_to": "LONG_AND_SHORT"},
    }
    save_real_trade_state(state)
    return state["open_positions"][signal_id]


def _register_pending_real_position(
    state: Dict[str, Any],
    signal: Dict[str, Any],
    signal_id: str,
    quantity: float,
    order_result: Dict[str, Any],
    execution_info: Dict[str, Any],
) -> Dict[str, Any]:
    """Reserve a real-trade slot while Toobit position confirmation is pending."""
    symbol = signal.get("symbol")
    direction = signal.get("direction")
    entry = float(signal.get("entry", 0) or signal.get("price", 0) or 0)
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
        "bot_quantity": (signal.get("toobit_order") or {}).get("bot_quantity"),
        "toobit_quantity": quantity,
        "toobit_symbol": (signal.get("toobit_order") or {}).get("toobit_symbol"),
        "toobit_multiplier": (signal.get("toobit_order") or {}).get("toobit_multiplier", 1.0),
        "toobit_entry": (signal.get("toobit_order") or {}).get("toobit_entry"),
        "toobit_tp1": (signal.get("toobit_order") or {}).get("toobit_tp1"),
        "toobit_sl": (signal.get("toobit_order") or {}).get("toobit_sl"),
        "position_size_usd": state.get("position_size_usd", 0),
        "leverage": state.get("leverage", 0),
        "margin_mode": "ISOLATED",
        "opened_at": _now(),
        "pending_started_at": _now(),
        "real_status": PENDING_REAL_CONFIRM_STATUS,
        "exchange_order": order_result.get("data"),
        "execution_info": execution_info,
        "warning": "سفارش ارسال شده و اسلات موقتاً رزرو است؛ در حال تایید پوزیشن واقعی در توبیت.",
        "snapshot": signal.get("snapshot") if isinstance(signal.get("snapshot"), dict) else {},
        "max_favorable_percent": 0.0,
        "max_adverse_percent": 0.0,
        "dynamic_profit_protection": {"enabled": True, "applies_to": "LONG_AND_SHORT"},
    }
    save_real_trade_state(state)
    return state["open_positions"][signal_id]


def _confirm_pending_real_position(
    signal_id: str,
    execution_info: Dict[str, Any],
    recovered_note: str = "",
) -> Optional[Dict[str, Any]]:
    """Mark a pending reserved slot as an active real position."""
    state = load_real_trade_state()
    pos = (state.get("open_positions") or {}).get(signal_id)
    if not isinstance(pos, dict):
        return None

    pos["real_status"] = "ACTIVE_REAL"
    pos["confirmed_at"] = _now()
    pos["execution_info"] = execution_info
    pos["recovered_note"] = recovered_note
    if recovered_note:
        pos["warning"] = recovered_note
    save_real_trade_state(state)
    return pos


def _release_pending_real_position(signal_id: str, reason: str = "") -> None:
    """Release a pending reserved slot after the confirmation grace period."""
    state = load_real_trade_state()
    pos = (state.get("open_positions") or {}).get(signal_id)
    if isinstance(pos, dict):
        pos["released_at"] = _now()
        pos["release_reason"] = reason
        state.setdefault("orphaned_internal_positions", []).append(pos)
        state["orphaned_internal_positions"] = state["orphaned_internal_positions"][-200:]
        state.get("open_positions", {}).pop(signal_id, None)
        save_real_trade_state(state)


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




def _call_client_method(method_names: tuple[str, ...], *args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Call the first available Toobit client method from a candidate list."""
    for method_name in method_names:
        method = getattr(toobit_client, method_name, None)
        if not callable(method):
            continue
        try:
            result = method(*args, **kwargs)
            if isinstance(result, dict):
                return result
            return {"ok": True, "data": result, "method": method_name}
        except TypeError:
            # Try again without keyword arguments for older client versions.
            try:
                result = method(*args)
                if isinstance(result, dict):
                    return result
                return {"ok": True, "data": result, "method": method_name}
            except Exception as e:
                return {"ok": False, "error": str(e), "method": method_name}
        except Exception as e:
            return {"ok": False, "error": str(e), "method": method_name}
    return {"ok": False, "error": "Toobit client leverage method not found"}


def _extract_leverage_from_result(result: Dict[str, Any]) -> float:
    if not isinstance(result, dict):
        return 0.0
    candidates = []
    for item in _flatten_dicts(result.get("data", result)):
        if isinstance(item, dict):
            for key in ("leverage", "lever", "leverageValue"):
                if key in item:
                    candidates.append(_safe_float_any(item.get(key)))
    for value in candidates:
        if value > 0:
            return value
    return 0.0


def _ensure_toobit_leverage(symbol: str, desired_leverage: float) -> Dict[str, Any]:
    """
    Safety gate before a real order.

    The order is allowed only if the configured leverage can be set/read back
    through the available Toobit client methods. If the current client does not
    expose leverage verification, we block instead of opening an unsafe trade.
    """
    desired = float(desired_leverage or 0)
    if desired <= 0:
        return {"ok": False, "error": "لوریج تنظیمی نامعتبر است"}

    # 1) Try to set/change leverage. Candidate names keep compatibility with
    # different tobit_client.py versions.
    set_result = _call_client_method(
        ("set_leverage", "set_symbol_leverage", "change_leverage", "change_symbol_leverage", "set_futures_leverage"),
        symbol,
        desired,
    )
    if not set_result.get("ok"):
        return {
            "ok": False,
            "error": "تنظیم لوریج در توبیت ناموفق بود؛ سفارش واقعی ارسال نشد.",
            "details": set_result.get("error"),
        }

    # 2) Read/verify leverage. If the client cannot read it back, block safely.
    read_result = _call_client_method(
        ("get_symbol_leverage", "get_leverage", "get_futures_leverage", "get_position_mode_leverage"),
        symbol,
    )
    if not read_result.get("ok"):
        return {
            "ok": False,
            "error": "تایید لوریج از توبیت ممکن نشد؛ برای امنیت سفارش واقعی ارسال نشد.",
            "details": read_result.get("error"),
            "set_result": set_result,
        }

    actual = _extract_leverage_from_result(read_result)
    if actual <= 0:
        # Some clients return only success on set but no numeric readback. For
        # real money we keep this conservative.
        return {
            "ok": False,
            "error": "عدد لوریج از پاسخ توبیت قابل تشخیص نبود؛ سفارش واقعی ارسال نشد.",
            "set_result": set_result,
            "read_result": read_result,
        }

    if abs(actual - desired) > 0.01:
        return {
            "ok": False,
            "error": f"لوریج توبیت با تنظیم ربات یکی نیست ({actual}x != {desired}x)؛ سفارش واقعی ارسال نشد.",
            "actual_leverage": actual,
            "desired_leverage": desired,
            "set_result": set_result,
            "read_result": read_result,
        }

    return {"ok": True, "actual_leverage": actual, "desired_leverage": desired}


def _extract_margin_mode_from_result(result: Dict[str, Any]) -> str:
    """Best-effort extractor for margin mode from Toobit client results."""
    if not isinstance(result, dict):
        return ""

    # Prefer tobit_client helper if available.
    extractor = getattr(toobit_client, "_extract_margin_mode_value", None)
    if callable(extractor):
        try:
            mode = extractor(result)
            if mode:
                return str(mode).upper()
        except Exception:
            pass
        try:
            mode = extractor(result.get("data"))
            if mode:
                return str(mode).upper()
        except Exception:
            pass

    for item in _flatten_dicts(result.get("data", result)):
        if not isinstance(item, dict):
            continue

        for key in (
            "marginMode", "margin_mode", "marginType", "margin_type",
            "tradeMode", "trade_mode", "positionMode", "position_mode"
        ):
            value = item.get(key)
            text_value = str(value or "").upper()
            if "ISOL" in text_value:
                return "ISOLATED"
            if "CROSS" in text_value:
                return "CROSS"

        for key in ("isolated", "isIsolated"):
            if key in item:
                return "ISOLATED" if bool(item.get(key)) else "CROSS"
        for key in ("cross", "isCross"):
            if key in item:
                return "CROSS" if bool(item.get(key)) else "ISOLATED"

    return ""


def _ensure_toobit_isolated_margin(symbol: str) -> Dict[str, Any]:
    """
    Safety gate before a real order.

    Required rule for the user's real account:
    - Every Toobit futures position opened by the bot must be ISOLATED.
    - CROSS is never allowed.
    - If ISOLATED cannot be set/read/confirmed, block the real order.
    """
    # Best path: use tobit_client.ensure_isolated_margin() from the updated client.
    ensure_method = getattr(toobit_client, "ensure_isolated_margin", None)
    if callable(ensure_method):
        try:
            result = ensure_method(symbol)
            if not isinstance(result, dict):
                return {"ok": False, "error": "پاسخ تایید مارجین ایزوله نامعتبر بود؛ سفارش واقعی ارسال نشد."}
            if not result.get("ok"):
                return {
                    "ok": False,
                    "error": result.get("error") or "مارجین ISOLATED تایید نشد؛ سفارش واقعی ارسال نشد.",
                    "details": result,
                }

            mode = _extract_margin_mode_from_result(result) or str(result.get("actual_margin_mode") or "").upper()
            if mode and mode != "ISOLATED":
                return {
                    "ok": False,
                    "error": f"Margin Mode توبیت ISOLATED نیست ({mode})؛ سفارش واقعی ارسال نشد.",
                    "details": result,
                }

            return {"ok": True, "actual_margin_mode": "ISOLATED", "details": result}
        except Exception as e:
            return {"ok": False, "error": f"خطا در تایید مارجین ایزوله: {str(e)[:200]}"}

    # Compatibility fallback for older/newer client method names.
    set_result = _call_client_method(
        (
            "set_margin_mode", "set_symbol_margin_mode", "change_margin_mode",
            "change_symbol_margin_mode", "set_futures_margin_mode"
        ),
        symbol,
        "ISOLATED",
    )
    if not set_result.get("ok"):
        return {
            "ok": False,
            "error": "تنظیم Margin Mode روی ISOLATED در توبیت ناموفق بود؛ سفارش واقعی ارسال نشد.",
            "details": set_result.get("error"),
            "set_result": set_result,
        }

    read_result = _call_client_method(
        ("get_margin_mode", "get_symbol_margin_mode", "get_futures_margin_mode"),
        symbol,
    )
    if not read_result.get("ok"):
        return {
            "ok": False,
            "error": "تایید Margin Mode ایزوله از توبیت ممکن نشد؛ سفارش واقعی ارسال نشد.",
            "set_result": set_result,
            "read_result": read_result,
        }

    mode = _extract_margin_mode_from_result(read_result)
    if mode != "ISOLATED":
        return {
            "ok": False,
            "error": f"Margin Mode توبیت ISOLATED نیست ({mode or 'UNKNOWN'})؛ سفارش واقعی ارسال نشد.",
            "set_result": set_result,
            "read_result": read_result,
        }

    return {
        "ok": True,
        "actual_margin_mode": "ISOLATED",
        "set_result": set_result,
        "read_result": read_result,
    }

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

    bot_quantity = calculate_order_quantity(entry, symbol=symbol)
    toobit_order = _prepare_toobit_order_values(symbol, entry, tp1, sl, bot_quantity)
    toobit_quantity = float(toobit_order.get("toobit_quantity") or 0)
    toobit_entry = float(toobit_order.get("toobit_entry") or entry)

    quantity_check = validate_order_quantity(symbol, toobit_entry, toobit_quantity)
    if not quantity_check.get("ok"):
        quantity_check["toobit_order"] = toobit_order
        return quantity_check

    signal = dict(signal)
    signal["toobit_order"] = toobit_order

    leverage_check = _ensure_toobit_leverage(symbol, float(state.get("leverage", 0) or 0))
    if not leverage_check.get("ok"):
        return {"ok": False, "blocked": True, "error": leverage_check.get("error"), "leverage_check": leverage_check}

    isolated_check = _ensure_toobit_isolated_margin(symbol)
    if not isolated_check.get("ok"):
        return {
            "ok": False,
            "blocked": True,
            "error": isolated_check.get("error") or "مارجین ISOLATED تایید نشد؛ سفارش واقعی ارسال نشد.",
            "isolated_check": isolated_check,
        }

    order_result = toobit_client.place_market_order(
        symbol=symbol,
        direction=direction,
        quantity=toobit_quantity,
        take_profit=toobit_order.get("toobit_tp1"),
        stop_loss=toobit_order.get("toobit_sl"),
    )

    if not order_result.get("ok"):
        err = str(order_result.get("error") or order_result.get("data") or "")
        order_result.setdefault("toobit_order", toobit_order)
        order_result.setdefault("quantity_check", quantity_check)
        if (
            "quantity too small" in err.lower()
            or "qty too small" in err.lower()
            or "-1202" in err
            or order_result.get("blocked_reason") in {"TOOBIT_QUANTITY_TOO_SMALL", "QUANTITY_BELOW_EXCHANGE_MIN"}
        ):
            order_result["blocked"] = True
            order_result["blocked_reason"] = order_result.get("blocked_reason") or "TOOBIT_QUANTITY_TOO_SMALL"
            order_result["user_hint"] = (
                "حجم سفارش برای این نماد کم است؛ حجم دلاری یا لوریج را بیشتر کن "
                "یا این نماد را برای ترید واقعی رد کن."
            )
        return order_result

    confirmed_position, execution_info = _order_is_confirmed_position(order_result)
    recovered_note = ""

    if not confirmed_position:
        # Reserve the slot immediately and keep it reserved while Toobit is
        # checked for up to 60 seconds. This prevents the bot from freeing the
        # slot too quickly while the exchange is still making the futures
        # position visible through the API.
        state = load_real_trade_state()
        pending_pos = _register_pending_real_position(state, signal, signal_id, toobit_quantity, order_result, execution_info)

        waited = _wait_for_exchange_position(symbol, direction, toobit_quantity, timeout=PENDING_ORDER_POLL_SECONDS)
        if waited.get("ok"):
            execution_info["status"] = execution_info.get("status") or "EXCHANGE_POSITION_FOUND_AFTER_PENDING"
            execution_info["recovered_by_polling"] = True
            recovered_note = "سفارش ابتدا Pending بود، سپس با چک پوزیشن توبیت تایید و وارد اسلات شد."
            pos = _confirm_pending_real_position(signal_id, execution_info, recovered_note) or pending_pos
            return {
                "ok": True,
                "signal_id": signal_id,
                "symbol": symbol,
                "direction": direction,
                "quantity": toobit_quantity,
                "bot_quantity": bot_quantity,
                "toobit_order": toobit_order,
                "order": order_result.get("data"),
                "execution_info": execution_info,
                "position": pos,
                "warning": recovered_note,
            }

        # Final sync before declaring failure. Only real exchange_positions can
        # confirm success here; the internal pending slot alone is not enough.
        sync_after = sync_real_positions_with_toobit(load_real_trade_state(), save=True)
        if sync_after.get("ok"):
            for ex in sync_after.get("exchange_positions") or []:
                if normalize_bot_plain_symbol(str(ex.get("symbol") or "")) == normalize_bot_plain_symbol(symbol) and str(ex.get("direction") or "").upper() == direction:
                    pos = _confirm_pending_real_position(signal_id, execution_info, "پوزیشن از طریق sync صرافی پیدا شد.")
                    if pos is None:
                        state_after = load_real_trade_state()
                        pos = (state_after.get("open_positions") or {}).get(signal_id) or {}
                    return {
                        "ok": True,
                        "signal_id": signal_id,
                        "symbol": symbol,
                        "direction": direction,
                        "quantity": toobit_quantity,
                        "bot_quantity": bot_quantity,
                        "toobit_order": toobit_order,
                        "order": order_result.get("data"),
                        "execution_info": execution_info,
                        "position": pos,
                        "warning": "پوزیشن از طریق sync صرافی پیدا شد.",
                    }

        _release_pending_real_position(signal_id, "No real Toobit position visible after pending confirmation window.")
        return {
            "ok": False,
            "blocked": True,
            "error": (
                f"سفارش توسط توبیت پذیرفته شد اما بعد از حدود {int(PENDING_ORDER_POLL_SECONDS)} ثانیه چک، "
                "پوزیشن واقعی دیده نشد؛ اسلات موقت آزاد شد."
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

    pos = _register_real_open_position(state, signal, signal_id, toobit_quantity, order_result, execution_info, recovered_note)
    pos["real_status"] = "ACTIVE_REAL"
    pos["confirmed_at"] = _now()
    save_real_trade_state(state)
    return {
        "ok": True,
        "signal_id": signal_id,
        "symbol": symbol,
        "direction": direction,
        "quantity": toobit_quantity,
        "bot_quantity": bot_quantity,
        "toobit_order": toobit_order,
        "order": order_result.get("data"),
        "execution_info": execution_info,
        "position": pos,
        "warning": recovered_note or order_result.get("warning"),
    }



# ---------------------------------------------------------------------------
# Special high-priority AI update: Dynamic Profit Protection
# ---------------------------------------------------------------------------
# This layer is intentionally TP-side only.  It never changes SL placement.
# It can close either LONG or SHORT early only after the position is already
# profitable and continuation probability drops while reversal risk rises.

def _normalize_live_price_for_bot(pos: Dict[str, Any], price: float) -> float:
    """Convert Toobit multiplier-contract price back to the bot's plain-symbol price."""
    value = _safe_float_any(price, 0.0)
    if value <= 0 or not isinstance(pos, dict):
        return value
    entry = _safe_float_any(pos.get("entry"), 0.0)
    mult = _safe_float_any(pos.get("toobit_multiplier"), 1.0)
    if mult <= 1:
        try:
            mult = _toobit_contract_multiplier(pos.get("symbol"))
        except Exception:
            mult = 1.0
    if mult > 1 and entry > 0 and value > entry * 10:
        return value / mult
    return value


def _get_live_price_from_position(pos: Dict[str, Any]) -> float:
    """Best-effort live price getter for a Toobit futures position."""
    if not isinstance(pos, dict):
        return 0.0

    # First try values already returned by Toobit position sync.
    raw = pos.get("exchange_position") or pos.get("raw") or {}
    if isinstance(raw, dict):
        for key in ("markPrice", "lastPrice", "indexPrice", "price", "close", "fairPrice"):
            v = _safe_float_any(raw.get(key), 0.0)
            if v > 0:
                return _normalize_live_price_for_bot(pos, v)

    symbol = pos.get("symbol")
    method_names = (
        "get_last_price", "get_mark_price", "get_symbol_price", "get_ticker", "fetch_ticker", "ticker"
    )
    for method_name in method_names:
        method = getattr(toobit_client, method_name, None)
        if not callable(method):
            continue
        try:
            res = method(symbol)
            if isinstance(res, dict):
                for item in _flatten_dicts(res):
                    if not isinstance(item, dict):
                        continue
                    for key in ("last", "lastPrice", "markPrice", "price", "close", "indexPrice"):
                        v = _safe_float_any(item.get(key), 0.0)
                        if v > 0:
                            return _normalize_live_price_for_bot(pos, v)
            else:
                v = _safe_float_any(res, 0.0)
                if v > 0:
                    return _normalize_live_price_for_bot(pos, v)
        except Exception:
            continue
    return 0.0


def _tp_progress_percent(pos: Dict[str, Any], current_price: float) -> float:
    entry = _safe_float_any(pos.get("entry"), 0.0)
    direction = str(pos.get("direction") or "").upper()
    tp1 = _safe_float_any(pos.get("tp1"), 0.0)
    if entry <= 0 or current_price <= 0 or tp1 <= 0:
        return 0.0
    target_move = abs(_calc_move_percent(direction, entry, tp1))
    if target_move <= 0:
        return 0.0
    return max(0.0, _calc_move_percent(direction, entry, current_price) / target_move)


def _live_analysis_pack_for_dynamic_exit(symbol: str, direction: str) -> Dict[str, Any]:
    """Optional live AI context; never raises and never blocks monitoring."""
    try:
        from analysis import analyze_symbol
        sig = analyze_symbol(symbol)
        if not isinstance(sig, dict):
            return {}
        snap = sig.get("snapshot") if isinstance(sig.get("snapshot"), dict) else {}
        pred = snap.get("prediction_layer") if isinstance(snap.get("prediction_layer"), dict) else {}
        state = snap.get("state_awareness") if isinstance(snap.get("state_awareness"), dict) else pred.get("state", {}) if isinstance(pred.get("state"), dict) else {}
        liq = snap.get("liquidity_trap") if isinstance(snap.get("liquidity_trap"), dict) else pred.get("liquidity_trap", {}) if isinstance(pred.get("liquidity_trap"), dict) else {}
        out = {
            "direction_now": str(sig.get("direction") or "").upper(),
            "status_now": sig.get("status"),
            "score_now": sig.get("score"),
            "prediction_score": pred.get("prediction_score") or snap.get("prediction_score"),
            "reversal_risk_score": pred.get("reversal_risk_score") or state.get("reversal_risk_score") or snap.get("reversal_risk_score"),
            "move_state": state.get("move_state") or snap.get("move_state"),
            "trap_risk": liq.get("trap_risk") or snap.get("trap_risk"),
            "vwap_status": snap.get("vwap_status"),
            "rsi_slope_15m": snap.get("rsi_slope_15m"),
            "adx_slope_15m": snap.get("adx_slope_15m"),
            "macd_hist_accel_15m": snap.get("macd_hist_accel_15m"),
        }
        # Direction no longer agrees with the open trade.
        out["direction_disagreement"] = bool(out.get("direction_now") in {"LONG", "SHORT"} and out.get("direction_now") != str(direction).upper())
        return out
    except Exception as e:
        return {"error": str(e)[:160]}



def _read_similarity_number(data: Any, *keys: str, default: float = 0.0) -> float:
    """Read a numeric Similarity Engine value across several compatible shapes."""
    if not isinstance(data, dict):
        return float(default)
    for key in keys:
        if data.get(key) is not None:
            return _safe_float_any(data.get(key), default)
    nested = data.get("similarity") if isinstance(data.get("similarity"), dict) else {}
    if isinstance(nested, dict):
        for key in keys:
            if nested.get(key) is not None:
                return _safe_float_any(nested.get(key), default)
    return float(default)


def _position_learning_snapshot(pos: Dict[str, Any], extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Build a safe learning snapshot for REAL trade manager events.

    It preserves the original analysis snapshot and adds real-time management
    fields such as current profit, MFE/MAE and Similarity/Dynamic decision data.
    """
    base = pos.get("snapshot") if isinstance(pos.get("snapshot"), dict) else {}
    snap = dict(base)
    for key in [
        "signal_id", "symbol", "direction", "entry", "tp1", "tp2", "sl",
        "stop_loss", "quantity", "position_size_usd", "leverage",
        "max_favorable_percent", "max_adverse_percent", "ai_final_rank",
        "ai_final_score", "score", "market_mode", "market_regime", "btc_bias",
    ]:
        if key not in snap and pos.get(key) is not None:
            snap[key] = pos.get(key)

    decision = pos.get("last_dynamic_profit_decision")
    if isinstance(decision, dict):
        snap["dynamic_profit_decision"] = decision
        if isinstance(decision.get("similarity_learning"), dict):
            snap["similarity_learning"] = decision.get("similarity_learning")

    if isinstance(pos.get("similarity_learning"), dict):
        snap["similarity_learning"] = pos.get("similarity_learning")

    if isinstance(extra, dict):
        for k, v in extra.items():
            if v is not None:
                snap[k] = v

    snap["result_source"] = "REAL"
    snap["trade_management_source"] = "real_trade_manager"
    return snap


def _dynamic_similarity_snapshot(
    pos: Dict[str, Any],
    current_price: float,
    profit_pct: float,
    peak_profit: float,
    retrace_from_peak: float,
    tp_progress: float,
    live_ai: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build the current-state snapshot used by the Historical Similarity layer."""
    snap = _position_learning_snapshot(pos)
    live_ai = live_ai if isinstance(live_ai, dict) else {}

    snap.update({
        "symbol": normalize_bot_plain_symbol(str(pos.get("symbol") or "")) or str(pos.get("symbol") or "").upper(),
        "direction": str(pos.get("direction") or "").upper(),
        "entry": pos.get("entry"),
        "price": current_price,
        "current_price": current_price,
        "profit_pct": round(float(profit_pct or 0.0), 6),
        "tp_progress": round(float(tp_progress or 0.0), 6),
        "peak_profit_pct": round(float(peak_profit or 0.0), 6),
        "retrace_from_peak_pct": round(float(retrace_from_peak or 0.0), 6),
        "max_favorable_percent": pos.get("max_favorable_percent"),
        "max_adverse_percent": pos.get("max_adverse_percent"),
        "mode": "dynamic_exit",
        "snapshot_at": _now(),
    })

    # Promote live analysis values into the snapshot so Similarity can compare
    # current post-entry state with past TP/SL/profit-exit states.
    for key in [
        "prediction_score", "reversal_risk_score", "move_state", "trap_risk",
        "vwap_status", "rsi_slope_15m", "adx_slope_15m",
        "macd_hist_accel_15m", "direction_now", "score_now", "status_now",
    ]:
        if live_ai.get(key) is not None:
            snap[key] = live_ai.get(key)
    if live_ai:
        snap["live_ai"] = live_ai
    return snap


def _dynamic_similarity_context(pos: Dict[str, Any], snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """Return normalized Historical Similarity data for REAL dynamic exits.

    Missing/old coin_learning.py is safe: it simply returns available=False.
    The output is used as a soft score only and never closes losing trades.
    """
    symbol = str(snapshot.get("symbol") or pos.get("symbol") or "").upper()
    direction = str(snapshot.get("direction") or pos.get("direction") or "").upper()
    if not symbol or not direction:
        return {"available": False, "reason": "NO_SYMBOL_DIRECTION"}

    raw: Dict[str, Any] = {}
    if callable(get_similarity_adjustment):
        try:
            try:
                raw = get_similarity_adjustment(symbol, direction, snapshot=snapshot, mode="dynamic_exit") or {}
            except TypeError:
                raw = get_similarity_adjustment(symbol, direction, snapshot) or {}
        except Exception as e:
            return {"available": False, "reason": str(e)[:160]}
    elif callable(find_similar_patterns):
        try:
            try:
                raw = find_similar_patterns(symbol, direction, snapshot=snapshot, limit=40, mode="dynamic_exit") or {}
            except TypeError:
                raw = find_similar_patterns(symbol, direction, snapshot) or {}
        except Exception as e:
            return {"available": False, "reason": str(e)[:160]}
    else:
        return {"available": False, "reason": "SIMILARITY_ENGINE_MISSING"}

    if not isinstance(raw, dict):
        return {"available": False, "reason": "BAD_SIMILARITY_RESULT"}

    samples = int(_read_similarity_number(raw, "samples", "similar_samples", "match_count", "count", default=0))
    winrate = _read_similarity_number(raw, "win_rate", "winrate", "similar_winrate", "tp_rate", default=50.0)
    rank_adj = _read_similarity_number(raw, "rank_adjustment", "similarity_adjustment", "adjustment", default=0.0)
    avg_move = _read_similarity_number(raw, "avg_move", "average_move", "avg_move_percent", default=0.0)
    avg_mfe = _read_similarity_number(raw, "avg_mfe", "avg_max_favorable", "avg_max_favorable_pct", default=0.0)
    avg_mae = _read_similarity_number(raw, "avg_mae", "avg_max_adverse", "avg_max_adverse_pct", default=0.0)
    confidence = _read_similarity_number(raw, "confidence", "similarity_confidence", default=min(100.0, samples * 8.0))

    if samples <= 0:
        return {"available": False, "reason": "NO_SIMILAR_MATCHES", "raw": raw}

    weak = bool(samples >= 3 and (winrate <= 42.0 or rank_adj <= -3.0))
    strong = bool(samples >= 3 and (winrate >= 65.0 or rank_adj >= 3.0))

    return {
        "available": True,
        "samples": samples,
        "winrate": round(winrate, 3),
        "rank_adjustment": round(rank_adj, 4),
        "avg_move": round(avg_move, 6),
        "avg_mfe": round(avg_mfe, 6),
        "avg_mae": round(avg_mae, 6),
        "confidence": round(confidence, 3),
        "weak_similarity": weak,
        "strong_similarity": strong,
        "raw": raw,
    }



def _dynamic_profit_exit_decision(pos: Dict[str, Any], current_price: float, state: Dict[str, Any]) -> Dict[str, Any]:
    """Decide whether an already-profitable REAL position should be closed early.

    Design rules from the current bot architecture:
    - Only act when the trade is in profit. Never touch losing/breakeven trades.
    - Do not move SL and do not change TP. This is a pure profit-protection exit.
    - Do not wait for multiple candles when a clear shock reversal appears.
    - Do not exit from one weak indicator alone; combine RSI/MACD/Histogram/ADX,
      trend/EMA/VWAP, power/momentum, candle shock, trap/liquidity and AI direction.
    """
    if not isinstance(pos, dict):
        return {"exit": False, "reason": "NO_POSITION"}
    if not bool(state.get("dynamic_profit_protection_enabled", True)):
        return {"exit": False, "reason": "DYNAMIC_PROFIT_DISABLED"}

    direction = str(pos.get("direction") or "").upper()
    entry = _safe_float_any(pos.get("entry"), 0.0)
    if direction not in {"LONG", "SHORT"} or entry <= 0 or current_price <= 0:
        return {"exit": False, "reason": "INVALID_DYNAMIC_INPUT"}

    profit_pct = _calc_move_percent(direction, entry, current_price)

    # Hard rule: never interfere in loss or breakeven. Let SL/TP logic handle it.
    if profit_pct <= 0:
        if profit_pct < 0:
            pos["max_adverse_percent"] = min(_safe_float_any(pos.get("max_adverse_percent"), 0.0), round(profit_pct, 6))
        return {"exit": False, "reason": "NOT_IN_PROFIT", "profit_pct": round(profit_pct, 4)}

    progress = _tp_progress_percent(pos, current_price)

    # Keep MFE/MAE memory. MFE is needed for shock-reversal detection.
    prev_max = _safe_float_any(pos.get("max_favorable_percent"), 0.0)
    if profit_pct > prev_max:
        pos["max_favorable_percent"] = round(profit_pct, 6)
        prev_max = profit_pct
    peak_profit = _safe_float_any(pos.get("max_favorable_percent"), 0.0)
    retrace_from_peak = max(0.0, peak_profit - profit_pct)

    # Existing state values are backward-compatible, but the new special update
    # must not require 45% TP progress or 0.18% profit before acting. Cap the old
    # thresholds so old saved JSON state cannot make the feature too slow.
    configured_min_profit = _safe_float_any(state.get("dynamic_profit_min_profit_pct"), 0.01)
    min_profit_for_normal_exit = min(max(configured_min_profit, 0.0), 0.03)
    shock_retrace = _safe_float_any(state.get("dynamic_profit_shock_retrace_pct"), 0.05)
    shock_retrace = min(max(shock_retrace, 0.02), 0.12)
    score_exit_threshold = _safe_float_any(state.get("dynamic_profit_score_exit_threshold"), 6.0)
    score_exit_threshold = min(max(score_exit_threshold, 4.0), 9.0)

    live_ai = _live_analysis_pack_for_dynamic_exit(str(pos.get("symbol") or ""), direction)

    reversal_score = 0.0
    continuation_score = 0.0
    reasons = []
    debug = {
        "profit_pct": round(profit_pct, 4),
        "tp_progress": round(progress, 4),
        "peak_profit_pct": round(peak_profit, 4),
        "retrace_from_peak_pct": round(retrace_from_peak, 4),
        "signals": [],
    }

    def add_risk(points: float, reason: str, code: str) -> None:
        nonlocal reversal_score
        reversal_score += float(points)
        reasons.append(reason)
        debug["signals"].append({"type": "risk", "code": code, "points": points, "reason": reason})

    def add_continue(points: float, code: str) -> None:
        nonlocal continuation_score
        continuation_score += float(points)
        debug["signals"].append({"type": "continue", "code": code, "points": points})

    # Historical Similarity Engine: compare the current in-profit trade state
    # with prior real/ghost TP/SL/profit-exit snapshots. This is a soft layer:
    # weak similar history increases exit pressure; strong similar continuation
    # reduces premature exits. It never acts before the profit check above.
    similarity_snapshot = _dynamic_similarity_snapshot(
        pos,
        current_price=current_price,
        profit_pct=profit_pct,
        peak_profit=peak_profit,
        retrace_from_peak=retrace_from_peak,
        tp_progress=progress,
        live_ai=live_ai,
    )
    similarity_ctx = _dynamic_similarity_context(pos, similarity_snapshot)
    debug["similarity_learning"] = similarity_ctx
    pos["last_similarity_learning"] = similarity_ctx

    if similarity_ctx.get("available") and int(similarity_ctx.get("samples", 0) or 0) >= 3:
        sim_wr = _safe_float_any(similarity_ctx.get("winrate"), 50.0)
        sim_adj = _safe_float_any(similarity_ctx.get("rank_adjustment"), 0.0)
        sim_samples = int(similarity_ctx.get("samples", 0) or 0)
        sim_avg_mfe = _safe_float_any(similarity_ctx.get("avg_mfe"), 0.0)

        if similarity_ctx.get("weak_similarity"):
            points = max(1.2, min(3.8, abs(sim_adj) * 0.55 + max(0.0, 50.0 - sim_wr) / 12.0))
            add_risk(points, f"شباهت به الگوهای ضعیف گذشته ({sim_samples} نمونه، WR {round(sim_wr, 1)}%)", "SIMILARITY_WEAK")
        elif similarity_ctx.get("strong_similarity"):
            points = max(1.0, min(3.0, abs(sim_adj) * 0.40 + max(0.0, sim_wr - 55.0) / 18.0))
            add_continue(points, "SIMILARITY_CONTINUATION")

        # If history says similar setups usually had limited favorable movement
        # and the current trade is already near/above that learned MFE, protect profit.
        if sim_avg_mfe > 0 and peak_profit >= sim_avg_mfe * 0.80 and sim_wr < 58.0:
            add_risk(1.6, "سود فعلی نزدیک محدوده معمول الگوهای مشابه است", "SIMILARITY_MFE_LIMIT")

    # 1) SHOCK EXIT: this is the fast ADA-style path. If the position was in a
    # decent profit and a single move quickly takes back a meaningful part of
    # the floating profit, exit without waiting for RSI/MACD confirmation.
    if peak_profit >= max(0.04, min_profit_for_normal_exit) and retrace_from_peak >= shock_retrace:
        add_risk(4.5, f"بازگشت سریع از اوج سود ({round(retrace_from_peak, 3)}%)", "SHOCK_RETRACE")
    if peak_profit >= 0.08 and retrace_from_peak >= max(0.04, peak_profit * 0.45):
        add_risk(5.0, "پس‌گرفتن بخش بزرگی از سود شناور", "FLOATING_PROFIT_COLLAPSE")

    # 2) Optional live AI/analysis layer. A single weak RSI is not enough; it
    # contributes points. Several independent weaknesses together can close.
    reversal = _safe_float_any(live_ai.get("reversal_risk_score"), 0.0)
    pred = _safe_float_any(live_ai.get("prediction_score"), 0.0)
    move_state = str(live_ai.get("move_state") or "").upper()
    trap_risk = str(live_ai.get("trap_risk") or "").upper()
    vwap_status = str(live_ai.get("vwap_status") or "").upper()

    if live_ai.get("direction_disagreement"):
        add_risk(4.0, "تغییر جهت تحلیل AI", "AI_DIRECTION_FLIP")
    if reversal >= 75:
        add_risk(3.5, "ریسک برگشت خیلی بالا", "REVERSAL_RISK_HIGH")
    elif reversal >= 62:
        add_risk(2.0, "افزایش ریسک برگشت", "REVERSAL_RISK_MEDIUM")
    if pred and pred <= 38:
        add_risk(3.0, "افت شدید احتمال ادامه حرکت", "PREDICTION_COLLAPSE")
    elif pred and pred <= 48:
        add_risk(1.5, "افت احتمال ادامه حرکت", "PREDICTION_WEAK")
    elif pred and pred >= 65:
        add_continue(2.0, "PREDICTION_STILL_STRONG")
    if move_state in {"LATE_OR_EXHAUSTION", "EXHAUSTION", "REVERSAL", "REVERSAL_PHASE"}:
        add_risk(2.5, "خستگی/تغییر فاز حرکت", "MOVE_STATE_EXHAUSTION")
    if trap_risk == "HIGH":
        add_risk(3.0, "ریسک Trap/Liquidity بالا", "TRAP_RISK_HIGH")
    elif trap_risk == "MEDIUM":
        add_risk(1.0, "ریسک Trap/Liquidity متوسط", "TRAP_RISK_MEDIUM")

    # VWAP/EMA/trend status if provided by analysis snapshot. Do not require it,
    # but use it as a strong immediate reversal clue when against the position.
    if direction == "LONG" and any(x in vwap_status for x in ("BELOW", "LOST", "BEAR", "SELL")):
        add_risk(2.5, "از دست رفتن VWAP/روند کوتاه", "VWAP_AGAINST_LONG")
    if direction == "SHORT" and any(x in vwap_status for x in ("ABOVE", "RECLAIM", "BULL", "BUY")):
        add_risk(2.5, "پس‌گرفتن VWAP/روند کوتاه", "VWAP_AGAINST_SHORT")

    # Direction-specific momentum fade. RSI alone only adds small risk; MACD
    # histogram/ADX/power/trend together make the exit decisive.
    rsi_slope = _safe_float_any(live_ai.get("rsi_slope_15m"), 0.0)
    hist_accel = _safe_float_any(live_ai.get("macd_hist_accel_15m"), 0.0)
    adx_slope = _safe_float_any(live_ai.get("adx_slope_15m"), 0.0)

    if direction == "LONG":
        if rsi_slope < 0:
            add_risk(1.0, "ضعف RSI", "RSI_FADE_LONG")
        if hist_accel < 0:
            add_risk(2.0, "ضعف MACD Histogram", "HIST_FADE_LONG")
        if adx_slope < 0:
            add_risk(1.5, "افت قدرت روند", "ADX_FADE_LONG")
        if rsi_slope > 0 and hist_accel > 0:
            add_continue(2.0, "MOMENTUM_STILL_LONG")
    else:  # SHORT
        if rsi_slope > 0:
            add_risk(1.0, "ضعف RSI به نفع برگشت", "RSI_FADE_SHORT")
        if hist_accel > 0:
            add_risk(2.0, "ضعف MACD Histogram به ضرر شورت", "HIST_FADE_SHORT")
        if adx_slope < 0:
            add_risk(1.5, "افت قدرت روند", "ADX_FADE_SHORT")
        if rsi_slope < 0 and hist_accel < 0:
            add_continue(2.0, "MOMENTUM_STILL_SHORT")

    # If the trade is barely positive, do not close on soft/ambiguous evidence;
    # only a real shock/direction flip should close it. This prevents fee/noise exits.
    has_shock = any(x.get("code") in {"SHOCK_RETRACE", "FLOATING_PROFIT_COLLAPSE"} for x in debug["signals"])
    has_hard_flip = any(x.get("code") in {"AI_DIRECTION_FLIP", "VWAP_AGAINST_LONG", "VWAP_AGAINST_SHORT"} for x in debug["signals"])

    net_exit_score = reversal_score - continuation_score
    can_normal_exit = profit_pct >= min_profit_for_normal_exit
    should_exit = False

    if has_shock and profit_pct > 0:
        should_exit = True
    elif has_hard_flip and net_exit_score >= 4.0 and profit_pct > 0:
        should_exit = True
    elif can_normal_exit and net_exit_score >= score_exit_threshold:
        should_exit = True

    # One weak indicator alone must never close the trade.
    if should_exit and len(reasons) == 1 and not (has_shock or has_hard_flip):
        should_exit = False

    return {
        "exit": bool(should_exit),
        "reason": "، ".join(reasons[:4]) if should_exit and reasons else "HOLD_PROFIT",
        "profit_pct": round(profit_pct, 4),
        "tp_progress": round(progress, 4),
        "peak_profit_pct": round(peak_profit, 4),
        "retrace_from_peak_pct": round(retrace_from_peak, 4),
        "reversal_score": round(reversal_score, 3),
        "continuation_score": round(continuation_score, 3),
        "net_exit_score": round(net_exit_score, 3),
        "live_ai": live_ai,
        "similarity_learning": similarity_ctx if 'similarity_ctx' in locals() else {"available": False},
        "debug": debug,
        "current_price": current_price,
    }

def check_dynamic_profit_protection(signal_id: Optional[str] = None) -> Dict[str, Any]:
    """Monitor open positions and close profitable trades when momentum fades.

    This function is safe to call from bot/scanner/tracker loops. It only acts
    when a position is already in profit and the TP-side continuation quality
    deteriorates. It applies to both LONG and SHORT.
    """
    state = load_real_trade_state()
    sync_result = sync_real_positions_with_toobit(state, save=True)
    if sync_result.get("ok"):
        state = sync_result.get("state") or state
    open_positions = state.get("open_positions") or {}
    if not isinstance(open_positions, dict) or not open_positions:
        return {"ok": True, "checked": 0, "closed": 0, "results": []}

    results = []
    checked = 0
    closed = 0
    for sid, pos in list(open_positions.items()):
        if signal_id and str(sid) != str(signal_id):
            continue
        if not isinstance(pos, dict):
            continue
        if str(pos.get("real_status") or "ACTIVE_REAL").upper() == PENDING_REAL_CONFIRM_STATUS:
            continue
        checked += 1
        current_price = _get_live_price_from_position(pos)
        decision = _dynamic_profit_exit_decision(pos, current_price, state)
        # Save updated max favorable/adverse even when no close happens.
        state.setdefault("open_positions", {}).setdefault(sid, {}).update({
            "max_favorable_percent": pos.get("max_favorable_percent", 0.0),
            "max_adverse_percent": pos.get("max_adverse_percent", 0.0),
            "last_dynamic_profit_check": _now(),
            "last_dynamic_profit_decision": decision,
            "similarity_learning": decision.get("similarity_learning") if isinstance(decision, dict) else None,
        })
        save_real_trade_state(state)

        if not decision.get("exit"):
            results.append({"signal_id": sid, "symbol": pos.get("symbol"), "direction": pos.get("direction"), "closed": False, "decision": decision})
            continue

        close_result = close_real_position(signal_id=sid, exit_price=current_price, result_type="AI_DYNAMIC_PROFIT_EXIT")
        ok_close = bool(close_result.get("ok"))
        if ok_close:
            closed += 1
            try:
                if register_dynamic_profit_exit:
                    register_dynamic_profit_exit(
                        symbol=pos.get("symbol"),
                        direction=pos.get("direction"),
                        entry=pos.get("entry"),
                        exit_price=current_price,
                        snapshot=_position_learning_snapshot(pos, {
                            "exit_price": current_price,
                            "move_percent": decision.get("profit_pct"),
                            "dynamic_profit_decision": decision,
                            "similarity_learning": decision.get("similarity_learning") if isinstance(decision, dict) else None,
                        }),
                        reason=decision.get("reason"),
                        source="REAL",
                        signal_id=sid,
                        max_favorable=pos.get("max_favorable_percent"),
                        max_adverse=pos.get("max_adverse_percent"),
                        decision=decision,
                    )
            except Exception:
                pass
        results.append({
            "signal_id": sid,
            "symbol": pos.get("symbol"),
            "direction": pos.get("direction"),
            "closed": ok_close,
            "exit_price": current_price,
            "reason": decision.get("reason"),
            "profit_pct": decision.get("profit_pct"),
            "close_result": close_result,
        })

    state = load_real_trade_state()
    state["dynamic_profit_last_check"] = _now()
    save_real_trade_state(state)
    return {"ok": True, "checked": checked, "closed": closed, "results": results}


def dynamic_profit_protection_text() -> str:
    result = check_dynamic_profit_protection()
    if not result.get("ok"):
        return f"❌ بررسی خروج سود AI ناموفق بود: {result.get('error')}"
    if int(result.get("checked", 0) or 0) <= 0:
        return "ℹ️ پوزیشن بازی برای بررسی خروج سود AI وجود ندارد."
    closed_rows = [r for r in result.get("results", []) if r.get("closed")]
    if not closed_rows:
        return f"✅ بررسی خروج سود AI انجام شد. پوزیشن بررسی‌شده: {result.get('checked')} | خروج لازم نبود."
    lines = [f"✅ خروج سود AI انجام شد. تعداد خروج: {len(closed_rows)}"]
    for r in closed_rows[:10]:
        lines.append(f"• {r.get('symbol')} {r.get('direction')} | قیمت خروج: {r.get('exit_price')} | سود تقریبی حرکت: {r.get('profit_pct')}% | علت: {r.get('reason')}")
    return "\n".join(lines)

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
        f"حد ضرر روزانه: {('خاموش' if not bool(state.get('daily_loss_protection_enabled', True)) else str(state.get('daily_loss_limit_usd', 0)) + '$')}\n"
        f"زمان قفل: {(0 if not bool(state.get('daily_loss_protection_enabled', True)) else state.get('daily_lock_duration_hours', DEFAULT_REAL_LOCK_DURATION_HOURS))} ساعت\n"
        f"قفل ضرر روزانه: {('خاموش' if not bool(state.get('daily_loss_protection_enabled', True)) else lock_line)}\n"
        f"محافظ سود AI: {('روشن' if bool(state.get('dynamic_profit_protection_enabled', True)) else 'خاموش')}"
    )



# ---------------------------------------------------------------------------
# Extra bot.py compatibility functions
# bot.py imports these names directly. Keep them top-level.
# ---------------------------------------------------------------------------

def close_real_position_by_symbol(symbol: str, direction: Optional[str] = None) -> Dict[str, Any]:
    """
    Close an internal real position by symbol (and optional direction).

    This function exists for bot.py compatibility. It syncs with Toobit first,
    searches the internal open_positions list, then closes the matching position
    using close_real_position(signal_id).
    """
    sym = str(symbol or "").upper().strip()
    dir_filter = str(direction or "").upper().strip()

    if not sym:
        return {"ok": False, "error": "نماد برای بستن پوزیشن مشخص نیست"}

    state = load_real_trade_state()
    sync_result = sync_real_positions_with_toobit(state, save=True)
    if sync_result.get("ok"):
        state = sync_result.get("state") or state

    matches = []
    for signal_id, pos in (state.get("open_positions") or {}).items():
        if not isinstance(pos, dict):
            continue
        if str(pos.get("symbol") or "").upper() != sym:
            continue
        if dir_filter and str(pos.get("direction") or "").upper() != dir_filter:
            continue
        matches.append((signal_id, pos))

    if not matches:
        return {"ok": False, "error": f"پوزیشن باز برای {sym} پیدا نشد"}

    if len(matches) > 1 and not dir_filter:
        dirs = sorted({str(p.get("direction") or "").upper() for _, p in matches})
        return {
            "ok": False,
            "error": f"برای {sym} چند پوزیشن/جهت پیدا شد؛ جهت را مشخص کن: {', '.join(dirs)}",
            "matches": [{"signal_id": sid, "direction": p.get("direction")} for sid, p in matches],
        }

    signal_id, pos = matches[0]
    result = close_real_position(signal_id=signal_id, result_type="MANUAL_CLOSE_BY_SYMBOL")
    if result.get("ok"):
        result["symbol"] = sym
        result["direction"] = pos.get("direction")
    return result


def close_all_real_positions() -> Dict[str, Any]:
    """
    Close all internal real positions known by the bot.

    Safety note:
    - This only uses positions that are visible after syncing with Toobit.
    - Each close is executed through close_real_position().
    - It does not guess PnL; accounting is updated later when real closed PnL
      is available from the exchange/tracker.
    """
    state = load_real_trade_state()
    sync_result = sync_real_positions_with_toobit(state, save=True)
    if sync_result.get("ok"):
        state = sync_result.get("state") or state

    open_positions = state.get("open_positions") or {}
    if not isinstance(open_positions, dict) or not open_positions:
        return {"ok": True, "closed_count": 0, "results": [], "message": "پوزیشن بازی برای بستن وجود ندارد."}

    results = []
    closed_count = 0
    failed_count = 0

    for signal_id, pos in list(open_positions.items()):
        if not isinstance(pos, dict):
            continue
        result = close_real_position(signal_id=signal_id, result_type="MANUAL_CLOSE_ALL")
        results.append({
            "signal_id": signal_id,
            "symbol": pos.get("symbol"),
            "direction": pos.get("direction"),
            "ok": bool(result.get("ok")),
            "error": result.get("error"),
            "result": result,
        })
        if result.get("ok"):
            closed_count += 1
        else:
            failed_count += 1

    return {
        "ok": failed_count == 0,
        "closed_count": closed_count,
        "failed_count": failed_count,
        "results": results,
        "message": f"بستن همه پوزیشن‌ها انجام شد. موفق: {closed_count} | ناموفق: {failed_count}",
    }


def sync_real_positions_text() -> str:
    """
    Sync internal slots with live Toobit positions and return Telegram-ready text.
    """
    state = load_real_trade_state()
    result = sync_real_positions_with_toobit(state, save=True)

    if not result.get("ok"):
        return f"❌ همگام‌سازی پوزیشن‌های واقعی ناموفق بود:\n{result.get('error') or 'خطای نامشخص'}"

    state = result.get("state") or state
    exchange_positions = result.get("exchange_positions") or []
    open_positions = state.get("open_positions") or {}

    lines = [
        "✅ همگام‌سازی پوزیشن‌های واقعی انجام شد.",
        f"پوزیشن واقعی در توبیت: {len(exchange_positions)}",
        f"پوزیشن داخلی/اسلات ربات: {len(open_positions) if isinstance(open_positions, dict) else 0}",
        f"اضافه‌شده به اسلات: {result.get('added', 0)}",
        f"حذف‌شده از اسلات: {result.get('removed', 0)}",
    ]

    if exchange_positions:
        lines.append("")
        lines.append("پوزیشن‌های دیده‌شده در توبیت:")
        for pos in exchange_positions[:10]:
            lines.append(
                f"• {pos.get('symbol')} {pos.get('direction')} | Qty: {pos.get('quantity')} | Lev: {pos.get('leverage') or 0}x"
            )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Real Toobit closed-position PnL lookup
# ---------------------------------------------------------------------------
# This section is intentionally read-only. It never opens/closes orders and it
# never uses the old percent formula. It only tries to find a confirmed realized
# PnL row from Toobit history/income endpoints so signal_tracker.py can label it
# as "PnL واقعی توبیت" only when the exchange really returned a usable value.

_REAL_PNL_KEYS = (
    "netPnl", "netPNL", "netProfit", "realizedPnl", "realizedPNL", "realized_pnl",
    "closedPnl", "closedPNL", "closePnl", "closeProfit", "realizedProfit",
    "profit", "pnl", "PNL", "tradePnl",
)
_REAL_PNL_INCOME_KEYS = ("income", "amount")
_REAL_PNL_TIME_KEYS = (
    "time", "timestamp", "ts", "createdTime", "createTime", "updatedTime",
    "closeTime", "closedTime", "transactTime", "incomeTime",
)
_REAL_PNL_SYMBOL_KEYS = (
    "symbol", "contractCode", "instrument", "instId", "pair", "symbolName",
    "contract", "contractName",
)
_REAL_PNL_DIRECTION_KEYS = (
    "side", "positionSide", "direction", "positionType", "holdSide",
    "tradeSide", "sideType", "positionDirection",
)
_REAL_PNL_INCOME_TYPE_KEYS = ("incomeType", "type", "transactionType", "bizType", "assetType")


def _real_pnl_timestamp_seconds(value: Any) -> int:
    """Normalize seconds/milliseconds timestamps to seconds."""
    try:
        if value is None or str(value).strip() == "":
            return 0
        ts = int(float(value))
        if ts > 10_000_000_000:  # milliseconds
            ts //= 1000
        return ts
    except Exception:
        return 0


def _real_pnl_time_from_item(item: Dict[str, Any]) -> int:
    if not isinstance(item, dict):
        return 0
    for key in _REAL_PNL_TIME_KEYS:
        ts = _real_pnl_timestamp_seconds(item.get(key))
        if ts > 0:
            return ts
    return 0


def _real_pnl_symbol_from_item(item: Dict[str, Any]) -> str:
    if not isinstance(item, dict):
        return ""
    for key in _REAL_PNL_SYMBOL_KEYS:
        value = item.get(key)
        if value is not None and str(value).strip():
            return normalize_bot_plain_symbol(str(value))
    return ""


def _real_pnl_direction_from_item(item: Dict[str, Any]) -> str:
    if not isinstance(item, dict):
        return ""
    text = " ".join(str(item.get(k, "")) for k in _REAL_PNL_DIRECTION_KEYS).upper()
    if "SHORT" in text or "SELL" in text or "BUY_CLOSE" in text or "CLOSE_SHORT" in text:
        return "SHORT"
    if "LONG" in text or "BUY" in text or "SELL_CLOSE" in text or "CLOSE_LONG" in text:
        return "LONG"
    return ""


def _real_pnl_income_type(item: Dict[str, Any]) -> str:
    if not isinstance(item, dict):
        return ""
    for key in _REAL_PNL_INCOME_TYPE_KEYS:
        value = item.get(key)
        if value is not None and str(value).strip():
            return str(value).upper()
    return ""


def _real_pnl_value_from_item(item: Dict[str, Any]) -> tuple[Optional[float], str]:
    """Extract a realized/closed PnL number and the source key.

    Generic income/amount rows are accepted only when their type looks like
    realized/closed profit. This prevents funding/fee/zero placeholder rows from
    being treated as real closed-position PnL.
    """
    if not isinstance(item, dict):
        return None, ""

    for key in _REAL_PNL_KEYS:
        if key not in item:
            continue
        try:
            value = item.get(key)
            if value is not None and str(value).strip() != "":
                return float(value), key
        except Exception:
            pass

    income_type = _real_pnl_income_type(item)
    if income_type and any(x in income_type for x in ("REALIZED", "PNL", "PROFIT", "CLOSED", "CLOSE")):
        for key in _REAL_PNL_INCOME_KEYS:
            if key not in item:
                continue
            try:
                value = item.get(key)
                if value is not None and str(value).strip() != "":
                    return float(value), key
            except Exception:
                pass

    return None, ""


def _real_pnl_records_from_response(value: Any) -> list:
    """Flatten a Toobit response into rows that contain possible closed PnL."""
    rows = []

    def walk(v: Any):
        if isinstance(v, dict):
            pnl, pnl_key = _real_pnl_value_from_item(v)
            if pnl is not None:
                rows.append({
                    "pnl_usd": float(pnl),
                    "pnl_key": pnl_key,
                    "symbol": _real_pnl_symbol_from_item(v),
                    "direction": _real_pnl_direction_from_item(v),
                    "ts": _real_pnl_time_from_item(v),
                    "income_type": _real_pnl_income_type(v),
                    "raw": v,
                })
            for child in v.values():
                if isinstance(child, (dict, list)):
                    walk(child)
        elif isinstance(v, list):
            for item in v:
                walk(item)

    if isinstance(value, dict) and "data" in value:
        walk(value.get("data"))
    else:
        walk(value)
    return rows


def _real_pnl_score_candidate(row: Dict[str, Any], symbol: str, direction: str, opened_at: int, closed_at: int) -> int:
    score = 0
    row_symbol = normalize_bot_plain_symbol(str(row.get("symbol") or ""))
    wanted = normalize_bot_plain_symbol(str(symbol or ""))
    if row_symbol and wanted and row_symbol == wanted:
        score += 4
    elif not row_symbol:
        # Account-income endpoints sometimes omit symbol when symbol filter was accepted.
        score += 1

    row_direction = str(row.get("direction") or "").upper()
    direct = str(direction or "").upper()
    if row_direction and direct and row_direction == direct:
        score += 1
    elif not row_direction:
        score += 1

    ts = int(row.get("ts") or 0)
    opened = int(opened_at or 0)
    closed = int(closed_at or _now())
    if opened and opened < 10_000_000_000:
        pass
    elif opened:
        opened //= 1000
    if closed and closed > 10_000_000_000:
        closed //= 1000
    if ts:
        if max(0, opened - 10 * 60) <= ts <= closed + 10 * 60:
            score += 3
        elif max(0, opened - 30 * 60) <= ts <= closed + 30 * 60:
            score += 1
    else:
        score += 1

    income_type = str(row.get("income_type") or "").upper()
    if income_type and any(x in income_type for x in ("REALIZED", "PNL", "PROFIT", "CLOSED", "CLOSE")):
        score += 1

    if row.get("pnl_usd") is not None:
        score += 1
    return score


def _call_toobit_pnl_method(method_name: str, *, symbol: str, direction: str, signal_id: str, opened_at: int, closed_at: int) -> list:
    """Call a Toobit client history method using compatible signatures."""
    method = getattr(toobit_client, method_name, None)
    if not callable(method):
        return []

    opened = int(opened_at or 0)
    closed = int(closed_at or _now())
    if opened and opened < 10_000_000_000:
        start_ms = opened * 1000
    else:
        start_ms = opened
    if closed and closed < 10_000_000_000:
        end_ms = closed * 1000
    else:
        end_ms = closed

    call_styles = [
        lambda: method(symbol=symbol, direction=direction, signal_id=signal_id, opened_at=opened, closed_at=closed),
        lambda: method(symbol=symbol, direction=direction, opened_at=opened, closed_at=closed),
        lambda: method(symbol=symbol, startTime=start_ms, endTime=end_ms, limit=100),
        lambda: method(symbol=symbol, startTime=max(0, start_ms - 180000), endTime=end_ms + 180000, limit=100),
        lambda: method(symbol, direction, opened, closed),
        lambda: method(symbol=symbol),
        lambda: method(symbol),
    ]

    results = []
    seen = set()
    for call in call_styles:
        try:
            response = call()
            key = repr(response)[:500]
            if key in seen:
                continue
            seen.add(key)
            results.append({"response": response})
        except TypeError:
            continue
        except Exception as e:
            results.append({"error": str(e)[:250]})
    return results


def _real_pnl_move_is_nonzero(position: Dict[str, Any], signal: Dict[str, Any], direction: str, exit_price: Optional[float]) -> bool:
    try:
        entry = float(
            (position or {}).get("entry")
            or (position or {}).get("price")
            or (signal or {}).get("entry")
            or (signal or {}).get("price")
            or 0
        )
        ex = float(exit_price or 0)
        if entry <= 0 or ex <= 0:
            return False
        move = abs(_calc_move_percent(direction, entry, ex))
        return move >= 0.01
    except Exception:
        return False


def get_real_pnl_for_closed_position(
    *,
    signal_id: Optional[str] = None,
    symbol: Optional[str] = None,
    direction: Optional[str] = None,
    opened_at: Optional[int] = None,
    closed_at: Optional[int] = None,
    exit_price: Optional[float] = None,
    result: Optional[str] = None,
    position: Optional[Dict[str, Any]] = None,
    signal: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Return confirmed REAL Toobit PnL for a recently closed futures position.

    It returns ok=True only when a Toobit history/income row reasonably matches
    the symbol/time/direction. Suspicious 0.0 rows are rejected when price moved,
    so signal_tracker.py will keep retrying and, if needed, label fallback as
    approximate instead of fake "real".
    """
    pos = position if isinstance(position, dict) else {}
    sig = signal if isinstance(signal, dict) else {}
    sym = normalize_bot_plain_symbol(str(symbol or pos.get("symbol") or sig.get("symbol") or ""))
    direct = str(direction or pos.get("direction") or sig.get("direction") or "").upper()
    sid = str(signal_id or pos.get("signal_id") or sig.get("signal_id") or sig.get("id") or "")
    opened = int(opened_at or pos.get("opened_at") or sig.get("created_at") or 0)
    closed = int(closed_at or _now())

    if not sym:
        return {"ok": False, "error": "symbol missing for real pnl lookup"}

    methods = (
        "get_closed_position_pnl",
        "get_realized_pnl_for_position",
        "get_recent_closed_pnl",
        "get_position_realized_pnl",
        "get_closed_position_history",
        "get_closed_positions",
        "get_futures_closed_positions",
        "get_income_history",
        "get_trade_income_history",
        "get_futures_income_history",
        "get_account_income_history",
        "get_order_history",
        "get_closed_orders",
    )

    all_rows = []
    errors = []
    for method_name in methods:
        calls = _call_toobit_pnl_method(
            method_name,
            symbol=sym,
            direction=direct,
            signal_id=sid,
            opened_at=opened,
            closed_at=closed,
        )
        for call_result in calls:
            if call_result.get("error"):
                errors.append(f"{method_name}: {call_result.get('error')}")
                continue
            raw = call_result.get("response")
            for row in _real_pnl_records_from_response(raw):
                row["method"] = method_name
                row["score"] = _real_pnl_score_candidate(row, sym, direct, opened, closed)
                all_rows.append(row)
        strong = [r for r in all_rows if r.get("method") == method_name and int(r.get("score") or 0) >= 7]
        if strong and method_name not in {"get_income_history", "get_trade_income_history", "get_futures_income_history", "get_account_income_history"}:
            break

    if not all_rows:
        return {
            "ok": False,
            "error": "confirmed Toobit closed PnL not found",
            "methods_checked": list(methods),
            "errors": errors[-5:],
        }

    candidates = [r for r in all_rows if int(r.get("score") or 0) >= 5]
    if not candidates:
        return {
            "ok": False,
            "error": "Toobit PnL rows found but none matched this symbol/time",
            "candidate_count": len(all_rows),
            "best_score": max(int(r.get("score") or 0) for r in all_rows),
        }

    nonzero_move = _real_pnl_move_is_nonzero(pos, sig, direct, exit_price)
    usable = []
    zero_suspicious = []
    for row in candidates:
        pnl = _round_usd(row.get("pnl_usd"), 8)
        if abs(float(pnl)) <= 0.00000001 and nonzero_move:
            zero_suspicious.append(row)
            continue
        usable.append(row)

    if not usable:
        return {
            "ok": False,
            "error": "Toobit returned only suspicious 0.0 PnL rows; waiting for finalized realized PnL",
            "candidate_count": len(candidates),
            "zero_suspicious_count": len(zero_suspicious),
            "best_score": max(int(r.get("score") or 0) for r in candidates),
        }

    # Income history can split one close into multiple realized-PnL rows.
    income_methods = {"get_income_history", "get_trade_income_history", "get_futures_income_history", "get_account_income_history"}
    income_rows = [
        r for r in usable
        if str(r.get("method") or "") in income_methods
        and any(x in str(r.get("income_type") or "").upper() for x in ("REALIZED", "PNL", "PROFIT", "CLOSED", "CLOSE"))
    ]
    if income_rows:
        dedup = {}
        for r in income_rows:
            key = (
                r.get("method"),
                r.get("symbol"),
                r.get("direction"),
                r.get("ts"),
                r.get("income_type"),
                r.get("pnl_key"),
                round(float(r.get("pnl_usd") or 0), 8),
            )
            dedup[key] = r
        rows = sorted(dedup.values(), key=lambda x: (int(x.get("score") or 0), int(x.get("ts") or 0)), reverse=True)
        pnl_sum = _round_usd(sum(float(r.get("pnl_usd") or 0) for r in rows), 8)
        if abs(float(pnl_sum)) <= 0.00000001 and nonzero_move:
            return {"ok": False, "error": "summed income PnL is suspicious 0.0; waiting for Toobit final PnL", "rows": rows[:5]}
        return {
            "ok": True,
            "pnl_usd": pnl_sum,
            "source": "TOOBIT_INCOME_HISTORY",
            "rows_used": rows[:10],
            "method": rows[0].get("method") if rows else "income_history",
            "matched_rows": len(rows),
        }

    best = sorted(usable, key=lambda x: (int(x.get("score") or 0), int(x.get("ts") or 0)), reverse=True)[0]
    return {
        "ok": True,
        "pnl_usd": _round_usd(best.get("pnl_usd"), 8),
        "source": f"TOOBIT:{best.get('method')}",
        "row": best,
        "score": best.get("score"),
    }

# ---------------------------------------------------------------------------
# Bot-facing helper functions
# These three functions are intentionally small/stable so bot.py can import
# them without duplicating parsing/order-result logic.
# ---------------------------------------------------------------------------

def handle_real_trade_command(text: str) -> Optional[str]:
    """Parse Persian real-trade commands and return a Telegram-ready response."""
    msg = str(text or "").strip()
    if not msg:
        return None

    parts = msg.replace("x", " ").replace("X", " ").split()
    head = parts[0] if parts else ""

    def _num(default: Optional[float] = None) -> Optional[float]:
        for token in parts[1:]:
            token = token.replace("$", "").replace(",", "").strip()
            try:
                return float(token)
            except Exception:
                continue
        return default

    if msg in {"ترید", "وضعیت ترید", "ترید واقعی", "وضعیت ترید واقعی"}:
        return get_real_trade_status_text()
    if msg in {"بالانس توبیت", "موجودی توبیت"}:
        return get_toobit_balance_text()
    if msg in {"ترید فعال", "فعال کردن ترید", "ترید روشن", "روشن کردن ترید"}:
        return enable_real_trading()
    if msg in {"ترید خاموش", "غیرفعال کردن ترید", "ترید غیرفعال"}:
        return disable_real_trading()
    if msg in {"توقف اضطراری", "استاپ اضطراری", "قطع اضطراری"}:
        return activate_real_emergency_stop()
    if msg in {"ریست ترید", "ریست ترید واقعی"}:
        return reset_real_trade_state()

    if msg.startswith("سرمایه ترید"):
        value = _num()
        return set_real_initial_capital(value) if value is not None else "❌ مقدار سرمایه را وارد کن. مثال: سرمایه ترید 50"
    if msg.startswith("ترید دلار") or msg.startswith("حجم پوزیشن"):
        value = _num()
        return set_real_position_size(value) if value is not None else "❌ مقدار دلار هر پوزیشن را وارد کن. مثال: ترید دلار 5"
    if msg.startswith("ترید لوریج") or msg.startswith("لوریج ترید"):
        value = _num()
        return set_real_leverage(value) if value is not None else "❌ مقدار لوریج را وارد کن. مثال: ترید لوریج 10"
    if msg.startswith("حداکثر پوزیشن") or msg.startswith("حداکثر پوزیشن‌ها"):
        value = _num()
        return set_real_max_positions(int(value)) if value is not None else "❌ تعداد پوزیشن را وارد کن. مثال: حداکثر پوزیشن 10"
    if msg.startswith("حد ضرر روزانه") or msg.startswith("ضرر روزانه"):
        value = _num()
        return set_real_daily_loss_limit(value) if value is not None else "❌ مقدار حد ضرر روزانه را وارد کن. مثال: حد ضرر روزانه 5"
    if msg.startswith("زمان قفل") or msg.startswith("مدت قفل"):
        value = _num()
        return set_real_lock_duration_hours(int(value)) if value is not None else "❌ مدت قفل را به ساعت وارد کن. مثال: زمان قفل 1"
    if msg in {"بررسی خروج سود", "خروج سود AI", "محافظ سود", "محافظ سود AI"}:
        return dynamic_profit_protection_text()

    return None


def format_real_trade_open_result(result: Dict[str, Any]) -> str:
    """Convert open_real_position_from_signal result into a short Persian message."""
    if not isinstance(result, dict):
        return "❌ نتیجه سفارش واقعی نامعتبر بود."
    if result.get("ok"):
        warning = result.get("warning")
        msg = (
            "✅ سفارش واقعی ثبت و تایید شد.\n"
            f"نماد: {result.get('symbol')}\n"
            f"جهت: {result.get('direction')}\n"
            f"حجم: {result.get('quantity')}"
        )
        if warning:
            msg += f"\n⚠️ {warning}"
        return msg

    hint = result.get("user_hint")
    err = result.get("error") or result.get("msg") or result.get("data") or "نامشخص"
    msg = f"❌ سفارش واقعی ثبت نشد.\nعلت: {err}"
    if hint:
        msg += f"\nراهنما: {hint}"
    return msg


def try_open_real_trade_from_signal(signal: Dict[str, Any]) -> Dict[str, Any]:
    """Bot.py wrapper: open a real trade and include a ready-to-send message."""
    result = open_real_position_from_signal(signal)
    result["message"] = format_real_trade_open_result(result)
    return result

