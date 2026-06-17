# real_trade_manager.py
# Safe real trading manager for Toobit Futures
#
# Source-of-truth rules:
# - Trading settings come from this bot state: position_size_usd, leverage, max_positions.
# - Before opening any position, Toobit leverage MUST be set to the bot leverage.
# - Position size means MARGIN USDT. Notional = margin * leverage.
# - Open slots are synced from live Toobit futures positions, not only local JSON.
# - Balance / open PnL shown in status come from Toobit whenever available.
# - TP/SL are sent immediately with the open order; fallback TP/SL placement is attempted if available.

import time
from typing import Dict, Any, Optional, List, Tuple

from data_store import load_json, save_json
from tobit_client import toobit_client


REAL_TRADE_FILE = "data/real_trade_state.json"

DEFAULT_REAL_LOCK_DURATION_HOURS = 1
DEFAULT_REAL_DAILY_LOSS_LIMIT_USD = 7.0

# Tolerance for comparing exchange margin/leverage against bot settings.
MARGIN_TOLERANCE_USD = 0.75
LEVERAGE_TOLERANCE = 0.05
POSITION_POLL_SECONDS = 10.0
POSITION_POLL_INTERVAL = 1.0


DEFAULT_REAL_TRADE_STATE = {
    "enabled": False,
    "exchange": "TOOBIT",
    "mode": "REAL",
    "emergency_stop": False,

    "initial_capital": 0.0,
    "balance": 0.0,
    "protected_balance": 0.0,
    "profit_carry_remainder": 0.0,

    # User settings.
    # position_size_usd = margin per position, not full notional size.
    "position_size_usd": 0.0,
    "leverage": 0.0,
    "max_positions": 0,
    "margin_type": "CROSS",

    # Local mirror only. Live Toobit positions are the source of truth.
    "open_positions": {},
    "closed_positions": [],
    "orphaned_internal_positions": [],

    "total_realized_pnl": 0.0,
    "today_realized_pnl": 0.0,

    "daily_loss_limit_usd": DEFAULT_REAL_DAILY_LOSS_LIMIT_USD,
    "daily_lock_duration_hours": DEFAULT_REAL_LOCK_DURATION_HOURS,
    "daily_loss_locked_until": 0,
    "daily_lock_reason": "",

    "last_exchange_sync_ok": False,
    "last_exchange_sync_error": "",
    "last_exchange_balance": None,
    "last_exchange_available_balance": None,
    "last_exchange_unrealized_pnl": 0.0,
    "last_exchange_account_pnl": 0.0,

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


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or str(value).strip() == "":
            return default
        return float(value)
    except Exception:
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or str(value).strip() == "":
            return default
        return int(float(value))
    except Exception:
        return default


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


def _flatten_dicts(value: Any) -> list:
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


def _plain_symbol(symbol: Any) -> str:
    raw = str(symbol or "").upper().strip()
    if not raw:
        return ""
    if hasattr(toobit_client, "plain_symbol"):
        try:
            return str(toobit_client.plain_symbol(raw)).upper()
        except Exception:
            pass
    raw = raw.replace("/", "").replace("_", "-")
    if raw.endswith("-SWAP-USDT"):
        return raw.replace("-SWAP-USDT", "USDT")
    if raw.endswith("-SWAP-USDC"):
        return raw.replace("-SWAP-USDC", "USDC")
    return raw.replace("-", "")


def _extract_toobit_usdt_balance(balance_result: Dict[str, Any]) -> Dict[str, Any]:
    out = {"ok": False, "balance": "0", "available_balance": "0", "error": ""}

    try:
        if not isinstance(balance_result, dict) or not balance_result.get("ok"):
            out["error"] = str((balance_result or {}).get("error") or (balance_result or {}).get("data") or "unknown")
            return out

        data = balance_result.get("data")
        items = []
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            if isinstance(data.get("data"), list):
                items = data.get("data")
            elif isinstance(data.get("result"), list):
                items = data.get("result")
            elif isinstance(data.get("list"), list):
                items = data.get("list")
            else:
                items = [data]

        selected = None
        for item in items:
            if isinstance(item, dict) and str(item.get("asset") or item.get("coin") or item.get("currency") or "").upper() == "USDT":
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
            or selected.get("accountEquity")
            or "0"
        )
        available = (
            selected.get("availableBalance")
            or selected.get("available")
            or selected.get("free")
            or selected.get("availableMargin")
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


def _get_exchange_balance_info() -> Dict[str, Any]:
    try:
        result = toobit_client.get_account_balance()
        info = _extract_toobit_usdt_balance(result)
        info["raw"] = result
        return info
    except Exception as e:
        return {"ok": False, "balance": "0", "available_balance": "0", "error": str(e)[:250]}


def _get_exchange_positions() -> Dict[str, Any]:
    """
    Use the normalized method from the updated tobit_client if available.
    Fallback to local best-effort parser if an older client is still installed.
    """
    try:
        if hasattr(toobit_client, "get_open_positions_normalized"):
            result = toobit_client.get_open_positions_normalized()
            if isinstance(result, dict):
                return result

        raw = toobit_client.get_positions()
        if not isinstance(raw, dict) or not raw.get("ok"):
            return {"ok": False, "error": str((raw or {}).get("error") or (raw or {}).get("data") or "position fetch failed"), "positions": [], "raw": raw}

        items = []
        if hasattr(toobit_client, "_flatten_position_items"):
            items = toobit_client._flatten_position_items(raw)
        else:
            items = _flatten_dicts(raw.get("data"))

        positions = []
        for item in items:
            if not isinstance(item, dict):
                continue
            qty = 0.0
            for key in ("positionAmt", "positionSize", "size", "qty", "quantity", "positionQuantity", "availablePosition", "holdAmount"):
                qty = _safe_float(item.get(key), 0)
                if qty != 0:
                    break
            if abs(qty) <= 0:
                continue

            sym = ""
            for key in ("symbol", "symbolId", "contractCode", "instrument", "instId", "pair"):
                if item.get(key):
                    sym = _plain_symbol(item.get(key))
                    break
            if not sym:
                continue

            side_text = " ".join(str(item.get(k, "")) for k in ("side", "positionSide", "direction", "positionType", "holdSide", "tradeSide")).upper()
            direction = "SHORT" if ("SHORT" in side_text or "SELL" in side_text or qty < 0) else "LONG"

            entry = 0.0
            for key in ("entryPrice", "avgPrice", "openPrice", "positionAvgPrice", "averagePrice", "holdAvgPrice"):
                entry = _safe_float(item.get(key), 0)
                if entry > 0:
                    break

            mark = 0.0
            for key in ("markPrice", "marketPrice", "lastPrice", "indexPrice"):
                mark = _safe_float(item.get(key), 0)
                if mark > 0:
                    break

            leverage = 0.0
            for key in ("leverage", "lever", "leverageValue"):
                leverage = _safe_float(item.get(key), 0)
                if leverage > 0:
                    break

            margin = 0.0
            for key in ("margin", "positionMargin", "initialMargin", "isolatedMargin", "marginAmount", "usedMargin"):
                margin = _safe_float(item.get(key), 0)
                if margin > 0:
                    break

            notional = 0.0
            for key in ("positionValue", "notional", "value", "sizeUSDT", "amount"):
                notional = _safe_float(item.get(key), 0)
                if notional > 0:
                    break
            if notional <= 0 and abs(qty) > 0 and (mark or entry):
                notional = abs(qty) * (mark or entry)
            if margin <= 0 and notional > 0 and leverage > 0:
                margin = notional / leverage

            upnl = 0.0
            for key in ("unrealizedPnl", "unRealizedPnl", "unrealizedProfit", "pnl", "profit", "positionPnl"):
                if item.get(key) is not None:
                    upnl = _safe_float(item.get(key), 0)
                    break

            positions.append({
                "symbol": sym,
                "exchange_symbol": sym,
                "direction": direction,
                "quantity": abs(qty),
                "entry": entry,
                "mark_price": mark,
                "leverage": leverage,
                "margin": margin,
                "notional": notional,
                "unrealized_pnl": upnl,
                "raw": item,
            })

        return {"ok": True, "positions": positions, "raw": raw}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300], "positions": []}


def _position_key(symbol: str, direction: str) -> str:
    return f"{_plain_symbol(symbol)}_{str(direction or '').upper()}"


def _expected_notional(state: Dict[str, Any]) -> float:
    return _round_usd(_safe_float(state.get("position_size_usd")) * _safe_float(state.get("leverage")), 8)


def _position_margin(pos: Dict[str, Any]) -> float:
    margin = _safe_float(pos.get("margin"), 0)
    if margin > 0:
        return margin
    notional = _safe_float(pos.get("notional"), 0)
    leverage = _safe_float(pos.get("leverage"), 0)
    if notional > 0 and leverage > 0:
        return notional / leverage
    return 0.0


def _compare_position_to_bot_settings(pos: Dict[str, Any], state: Dict[str, Any]) -> List[str]:
    warnings = []
    bot_margin = _safe_float(state.get("position_size_usd"), 0)
    bot_leverage = _safe_float(state.get("leverage"), 0)

    ex_margin = _position_margin(pos)
    ex_leverage = _safe_float(pos.get("leverage"), 0)

    if bot_leverage > 0 and ex_leverage > 0 and abs(ex_leverage - bot_leverage) > LEVERAGE_TOLERANCE:
        warnings.append(f"لوریج توبیت {ex_leverage}x با ربات {bot_leverage}x یکی نیست")

    if bot_margin > 0 and ex_margin > 0 and abs(ex_margin - bot_margin) > MARGIN_TOLERANCE_USD:
        warnings.append(f"مارجین توبیت حدود {round(ex_margin, 4)}$ با حجم ربات {bot_margin}$ یکی نیست")

    return warnings


def load_real_trade_state() -> Dict[str, Any]:
    state = load_json(REAL_TRADE_FILE, DEFAULT_REAL_TRADE_STATE.copy())
    if not isinstance(state, dict):
        state = DEFAULT_REAL_TRADE_STATE.copy()

    changed = False
    for k, v in DEFAULT_REAL_TRADE_STATE.items():
        if k not in state:
            state[k] = v
            changed = True

    if not isinstance(state.get("open_positions"), dict):
        state["open_positions"] = {}
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
    state.setdefault("orphaned_internal_positions", [])
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


def sync_real_positions_with_toobit(state: Optional[Dict[str, Any]] = None, *, save: bool = True) -> Dict[str, Any]:
    """
    Make local open_positions a mirror of live Toobit positions.
    The exchange is the source of truth for occupied slots.
    """
    state = state or load_real_trade_state()
    state.setdefault("open_positions", {})
    if not isinstance(state.get("open_positions"), dict):
        state["open_positions"] = {}

    old_positions = state.get("open_positions", {})
    old_by_key = {}
    for sid, old in old_positions.items():
        if isinstance(old, dict):
            old_by_key[_position_key(old.get("symbol"), old.get("direction"))] = (sid, old)

    pos_result = _get_exchange_positions()
    bal_info = _get_exchange_balance_info()

    if not pos_result.get("ok"):
        state["last_exchange_sync_ok"] = False
        state["last_exchange_sync_error"] = str(pos_result.get("error") or "position sync failed")[:250]
        if save:
            save_real_trade_state(state)
        return {
            "ok": False,
            "error": state["last_exchange_sync_error"],
            "state": state,
            "exchange_positions": [],
            "added": 0,
            "removed": 0,
        }

    exchange_positions = pos_result.get("positions") or []
    new_open = {}
    added = 0
    kept = 0
    warnings = []

    now = _now()
    for pos in exchange_positions:
        if not isinstance(pos, dict):
            continue

        key = _position_key(pos.get("symbol"), pos.get("direction"))
        old_sid, old = old_by_key.get(key, (None, {}))
        sid = old_sid or f"EXCHANGE_{key}_{now}_{added}"

        if old_sid:
            kept += 1
        else:
            added += 1

        merged = dict(old) if isinstance(old, dict) else {}
        merged.update({
            "signal_id": merged.get("signal_id") or sid,
            "symbol": _plain_symbol(pos.get("symbol")),
            "direction": str(pos.get("direction") or "").upper(),
            "entry": _safe_float(pos.get("entry"), _safe_float(merged.get("entry"), 0)),
            "mark_price": _safe_float(pos.get("mark_price"), 0),
            "tp1": merged.get("tp1"),
            "tp2": merged.get("tp2"),
            "sl": merged.get("sl"),
            "quantity": _safe_float(pos.get("quantity"), 0),
            "position_size_usd": _position_margin(pos) or merged.get("position_size_usd") or state.get("position_size_usd", 0),
            "bot_configured_margin_usd": state.get("position_size_usd", 0),
            "leverage": _safe_float(pos.get("leverage"), 0) or merged.get("leverage") or state.get("leverage", 0),
            "bot_configured_leverage": state.get("leverage", 0),
            "notional": _safe_float(pos.get("notional"), 0),
            "unrealized_pnl": _safe_float(pos.get("unrealized_pnl"), 0),
            "opened_at": int(merged.get("opened_at") or now),
            "source": "TOOBIT",
            "exchange_position": pos.get("raw") or pos,
            "settings_warnings": _compare_position_to_bot_settings(pos, state),
            "last_synced_at": now,
        })

        if not old_sid:
            merged["recovered_from_exchange"] = True
            merged["warning"] = "این پوزیشن از روی فیوچرز توبیت داخل اسلات ربات sync شد."

        if merged.get("settings_warnings"):
            warnings.extend([f"{merged.get('symbol')} {merged.get('direction')}: {w}" for w in merged["settings_warnings"]])

        new_open[sid] = merged

    removed = max(0, len(old_positions) - kept)

    if old_positions:
        live_keys = {_position_key(p.get("symbol"), p.get("direction")) for p in exchange_positions if isinstance(p, dict)}
        for sid, old in old_positions.items():
            if not isinstance(old, dict):
                continue
            if _position_key(old.get("symbol"), old.get("direction")) not in live_keys:
                old = dict(old)
                old["closed_or_missing_at"] = now
                state.setdefault("orphaned_internal_positions", []).append(old)

    state["orphaned_internal_positions"] = state.get("orphaned_internal_positions", [])[-200:]
    state["open_positions"] = new_open
    state["last_exchange_sync_ok"] = True
    state["last_exchange_sync_error"] = ""
    state["last_exchange_unrealized_pnl"] = _round_usd(sum(_safe_float(p.get("unrealized_pnl"), 0) for p in exchange_positions), 6)

    if bal_info.get("ok"):
        balance = _safe_float(bal_info.get("balance"), 0)
        available = _safe_float(bal_info.get("available_balance"), 0)
        state["last_exchange_balance"] = balance
        state["last_exchange_available_balance"] = available
        if balance > 0:
            state["balance"] = _round_usd(balance)
            initial = _safe_float(state.get("initial_capital"), 0)
            state["last_exchange_account_pnl"] = _round_usd(balance - initial, 6) if initial > 0 else 0.0

    if save:
        save_real_trade_state(state)

    return {
        "ok": True,
        "added": added,
        "removed": removed,
        "kept": kept,
        "state": state,
        "exchange_positions": exchange_positions,
        "balance": bal_info,
        "warnings": warnings,
    }


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
        state["protected_balance"] = _round_usd(_round_usd(state.get("protected_balance", state.get("initial_capital", 0))) + whole_dollars)
        total_remainder -= whole_dollars

    state["profit_carry_remainder"] = _round_usd(max(0.0, total_remainder))
    return whole_dollars


def _sync_protected_balance_from_exchange_profit(state: Dict[str, Any]) -> None:
    """
    If exchange balance is above protected balance by full dollars, protect that profit.
    This follows user's requirement: realized profit should increase protected capital.
    """
    balance = _safe_float(state.get("balance"), 0)
    protected = _safe_float(state.get("protected_balance"), 0)
    initial = _safe_float(state.get("initial_capital"), 0)

    if protected <= 0 and initial > 0:
        state["protected_balance"] = initial
        protected = initial

    if balance > protected:
        full_profit = int(balance - protected)
        if full_profit > 0:
            state["protected_balance"] = _round_usd(protected + full_profit)


def _maybe_apply_daily_lock(state: Dict[str, Any]) -> bool:
    if int(state.get("daily_loss_locked_until", 0) or 0) > _now():
        return True

    max_loss = _round_usd(state.get("daily_loss_limit_usd", DEFAULT_REAL_DAILY_LOSS_LIMIT_USD))
    if max_loss <= 0:
        return False

    _sync_protected_balance_from_exchange_profit(state)
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


def is_real_trade_ready() -> Tuple[bool, str]:
    state = load_real_trade_state()
    sync_result = sync_real_positions_with_toobit(state, save=True)
    if sync_result.get("ok"):
        state = sync_result.get("state") or state

    if not state.get("enabled"):
        return False, "ترید واقعی خاموش است"
    if state.get("emergency_stop"):
        return False, "توقف اضطراری فعال است"
    if int(state.get("daily_loss_locked_until", 0) or 0) > _now():
        remaining = round((int(state.get("daily_loss_locked_until", 0)) - _now()) / 3600, 2)
        return False, f"قفل ضرر روزانه فعال است؛ حدود {remaining} ساعت باقی مانده"
    if _safe_float(state.get("initial_capital"), 0) <= 0:
        return False, "سرمایه ترید تنظیم نشده است"
    if _safe_float(state.get("position_size_usd"), 0) <= 0:
        return False, "حجم هر پوزیشن تنظیم نشده است"
    if _safe_float(state.get("leverage"), 0) <= 0:
        return False, "لوریج تنظیم نشده است"
    if _safe_int(state.get("max_positions"), 0) <= 0:
        return False, "حداکثر پوزیشن تنظیم نشده است"

    open_count = len(state.get("open_positions", {}) if isinstance(state.get("open_positions"), dict) else {})
    if open_count >= _safe_int(state.get("max_positions"), 0):
        return False, "ظرفیت پوزیشن‌ها پر است"

    available = _safe_float(state.get("last_exchange_available_balance"), 0)
    needed = _safe_float(state.get("position_size_usd"), 0)
    if available > 0 and needed > 0 and available < needed:
        return False, f"بالانس قابل استفاده توبیت کافی نیست ({available}$ < {needed}$)"

    _maybe_apply_daily_lock(state)
    if int(state.get("daily_loss_locked_until", 0) or 0) > _now():
        save_real_trade_state(state)
        return False, "قفل ضرر روزانه فعال شد"

    save_real_trade_state(state)
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
    return f"✅ لوریج ترید واقعی تنظیم شد: {leverage}x\nاین لوریج قبل از سفارش بعدی روی توبیت ست و verify می‌شود."


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
    missing = []
    for key, label in [
        ("initial_capital", "سرمایه ترید"),
        ("position_size_usd", "حجم هر پوزیشن"),
        ("leverage", "لوریج"),
        ("max_positions", "حداکثر پوزیشن"),
    ]:
        if _safe_float(state.get(key), 0) <= 0:
            missing.append(label)

    if missing:
        return "❌ ترید واقعی فعال نشد.\nاول این موارد را تنظیم کن:\n" + "\n".join(f"• {x}" for x in missing)

    sync = sync_real_positions_with_toobit(state, save=True)
    if sync.get("ok"):
        state = sync.get("state") or state

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


def calculate_order_quantity(entry_price: float) -> float:
    state = load_real_trade_state()
    notional = _expected_notional(state)
    if entry_price <= 0 or notional <= 0:
        return 0.0
    return round(notional / float(entry_price), 8)


def _wait_for_position(symbol: str, direction: str, timeout: float = POSITION_POLL_SECONDS) -> Dict[str, Any]:
    end = time.time() + timeout
    symbol_plain = _plain_symbol(symbol)
    direction = str(direction or "").upper()

    last = {}
    while time.time() <= end:
        result = _get_exchange_positions()
        last = result
        if result.get("ok"):
            for pos in result.get("positions") or []:
                if _plain_symbol(pos.get("symbol")) == symbol_plain and str(pos.get("direction") or "").upper() == direction:
                    return {"ok": True, "position": pos, "all": result}
        time.sleep(POSITION_POLL_INTERVAL)

    return {"ok": False, "error": str(last.get("error") or "position not visible after order")[:250], "last": last}


def _verify_opened_position_against_request(position: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, Any]:
    warnings = _compare_position_to_bot_settings(position, state)
    return {"ok": len(warnings) == 0, "warnings": warnings}


def _ensure_tpsl_after_open(symbol: str, direction: str, tp1: Any, sl: Any, quantity: float = 0) -> Dict[str, Any]:
    """
    First order already includes TP/SL. This fallback attempts separate TP/SL creation
    if the client supports it. We do not claim verification unless the client can verify.
    """
    out = {"ok": True, "attempted": False, "fallback_result": None, "verification": None, "warning": ""}

    if not hasattr(toobit_client, "place_position_tpsl"):
        out["warning"] = "تابع ثبت جداگانه TP/SL در tobit_client موجود نیست."
        return out

    try:
        res = toobit_client.place_position_tpsl(
            symbol=symbol,
            direction=direction,
            take_profit=tp1,
            stop_loss=sl,
            quantity=quantity or 0,
        )
        out["attempted"] = True
        out["fallback_result"] = res
        if not res.get("ok"):
            out["ok"] = False
            out["warning"] = f"ثبت جداگانه TP/SL کامل نشد: {str(res.get('errors') or res.get('error') or res)[:180]}"
    except Exception as e:
        out["ok"] = False
        out["attempted"] = True
        out["warning"] = f"خطا در ثبت جداگانه TP/SL: {str(e)[:180]}"

    if hasattr(toobit_client, "verify_position_has_tpsl"):
        try:
            out["verification"] = toobit_client.verify_position_has_tpsl(symbol, direction)
        except Exception as e:
            out["verification"] = {"ok": False, "error": str(e)[:180]}

    return out


def open_real_position_from_signal(signal: Dict[str, Any]) -> Dict[str, Any]:
    ready, reason = is_real_trade_ready()
    if not ready:
        return {"ok": False, "blocked": True, "error": reason}

    state = load_real_trade_state()
    sync = sync_real_positions_with_toobit(state, save=True)
    if sync.get("ok"):
        state = sync.get("state") or state

    symbol = _plain_symbol(signal.get("symbol"))
    direction = str(signal.get("direction") or "").upper().strip()
    entry = _safe_float(signal.get("entry") or signal.get("price"), 0)
    tp1 = signal.get("tp1")
    sl = signal.get("sl") or signal.get("stop_loss")
    signal_id = str(signal.get("signal_id") or signal.get("id") or f"{symbol}_{direction}_{_now()}")

    if not symbol or direction not in {"LONG", "SHORT"} or entry <= 0:
        return {"ok": False, "error": "اطلاعات سیگنال ناقص است"}
    if _safe_float(tp1, 0) <= 0 or _safe_float(sl, 0) <= 0:
        return {"ok": False, "error": "TP/SL سیگنال ناقص است؛ پوزیشن واقعی بدون حد سود/ضرر باز نمی‌شود."}

    for pos in state.get("open_positions", {}).values():
        if isinstance(pos, dict) and _plain_symbol(pos.get("symbol")) == symbol:
            return {"ok": False, "blocked": True, "error": f"برای {symbol} از قبل پوزیشن واقعی باز است"}

    margin = _safe_float(state.get("position_size_usd"), 0)
    leverage = _safe_float(state.get("leverage"), 0)
    margin_type = str(state.get("margin_type") or "CROSS").upper()
    expected_notional = margin * leverage

    if margin <= 0 or leverage <= 0:
        return {"ok": False, "error": "حجم پوزیشن یا لوریج تنظیم نشده است"}

    # Preferred exact-margin path from the updated tobit_client.py
    if hasattr(toobit_client, "place_market_order_by_margin"):
        order_result = toobit_client.place_market_order_by_margin(
            symbol=symbol,
            direction=direction,
            margin_usdt=margin,
            leverage=leverage,
            take_profit=tp1,
            stop_loss=sl,
            margin_type=margin_type,
        )
    else:
        quantity = calculate_order_quantity(entry)
        order_result = toobit_client.place_market_order(
            symbol=symbol,
            direction=direction,
            quantity=quantity,
            take_profit=tp1,
            stop_loss=sl,
            leverage=leverage if "leverage" in getattr(toobit_client.place_market_order, "__code__", type("", (), {"co_varnames": ()})()).co_varnames else None,
        )

    if not isinstance(order_result, dict) or not order_result.get("ok"):
        err = str((order_result or {}).get("error") or (order_result or {}).get("data") or order_result)
        if "quantity too small" in err.lower() or "qty too small" in err.lower():
            order_result = dict(order_result or {})
            order_result["user_hint"] = "حجم سفارش برای این نماد کم است؛ حجم دلاری یا لوریج را بیشتر کن یا این نماد را از ترید واقعی حذف کن."
        return order_result or {"ok": False, "error": "خطای نامشخص در ثبت سفارش"}

    waited = _wait_for_position(symbol, direction)
    if not waited.get("ok"):
        return {
            "ok": False,
            "blocked": True,
            "error": "سفارش ارسال شد اما پوزیشن واقعی در توبیت دیده نشد؛ برای جلوگیری از اسلات اشتباه ثبت نشد.",
            "order": order_result,
            "position_wait": waited,
        }

    opened_pos = waited.get("position") or {}
    verify = _verify_opened_position_against_request(opened_pos, state)
    if not verify.get("ok"):
        # Do not close automatically. But mark warning so user sees exact mismatch.
        order_result["settings_warning"] = verify.get("warnings")

    tpsl_status = {"ok": True, "note": "TP/SL همراه سفارش ارسال شد."}
    if hasattr(toobit_client, "verify_position_has_tpsl"):
        try:
            tpsl_status = toobit_client.verify_position_has_tpsl(symbol, direction)
        except Exception as e:
            tpsl_status = {"ok": False, "error": str(e)[:180]}

    # If verification is unavailable/negative, try fallback separate TP/SL placement.
    if not tpsl_status.get("verified"):
        fallback = _ensure_tpsl_after_open(symbol, direction, tp1, sl, quantity=_safe_float(opened_pos.get("quantity"), 0))
        order_result["tpsl_fallback"] = fallback
        if not fallback.get("ok"):
            order_result["tpsl_warning"] = fallback.get("warning")

    # Sync again so slots are source-of-truth from Toobit immediately.
    state = load_real_trade_state()
    sync_after = sync_real_positions_with_toobit(state, save=True)
    if sync_after.get("ok"):
        state = sync_after.get("state") or state
        key = _position_key(symbol, direction)
        for sid, pos in state.get("open_positions", {}).items():
            if _position_key(pos.get("symbol"), pos.get("direction")) == key:
                pos["signal_id"] = signal_id
                pos["tp1"] = tp1
                pos["tp2"] = signal.get("tp2")
                pos["sl"] = sl
                pos["bot_expected_margin"] = margin
                pos["bot_expected_leverage"] = leverage
                pos["bot_expected_notional"] = expected_notional
                pos["exchange_order"] = order_result.get("data")
                pos["order_requested_params"] = order_result.get("requested_params")
                pos["settings_result"] = order_result.get("settings")
                pos["tpsl_status"] = tpsl_status
                pos["opened_at"] = pos.get("opened_at") or _now()
                state["open_positions"][sid] = pos
                save_real_trade_state(state)
                break

    return {
        "ok": True,
        "signal_id": signal_id,
        "symbol": symbol,
        "direction": direction,
        "margin_usdt": margin,
        "leverage": leverage,
        "expected_notional": expected_notional,
        "exchange_position": opened_pos,
        "order": order_result.get("data"),
        "requested_params": order_result.get("requested_params"),
        "settings": order_result.get("settings"),
        "settings_warning": order_result.get("settings_warning"),
        "tpsl_status": tpsl_status,
        "tpsl_warning": order_result.get("tpsl_warning"),
    }


def record_realized_pnl(
    pnl_usd: float,
    signal_id: Optional[str] = None,
    symbol: Optional[str] = None,
    direction: Optional[str] = None,
    result: str = "REALIZED",
    exit_price: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Kept for compatibility with signal_tracker.py.
    Status display prefers exchange balance/open PnL after sync.
    """
    state = load_real_trade_state()
    _new_day_if_needed(state)

    pnl_usd = _round_usd(pnl_usd)
    state["total_realized_pnl"] = _round_usd(_round_usd(state.get("total_realized_pnl", 0)) + pnl_usd)
    state["today_realized_pnl"] = _round_usd(_round_usd(state.get("today_realized_pnl", 0)) + pnl_usd)

    closed_record = {
        "signal_id": signal_id,
        "symbol": symbol,
        "direction": direction,
        "result": result,
        "exit_price": exit_price,
        "pnl_usd": pnl_usd,
        "closed_at": _now(),
    }
    state.setdefault("closed_positions", []).append(closed_record)
    state["closed_positions"] = state["closed_positions"][-2000:]

    # Sync exchange balance after close if possible.
    sync_real_positions_with_toobit(state, save=False)
    _maybe_apply_daily_lock(state)
    save_real_trade_state(state)

    return {
        "ok": True,
        "pnl_usd": pnl_usd,
        "balance": state.get("balance"),
        "protected_balance": state.get("protected_balance"),
        "daily_locked": int(state.get("daily_loss_locked_until", 0) or 0) > _now(),
        "loss_from_protected": get_real_loss_from_protected(state),
    }


def close_real_position(
    signal_id: str,
    pnl_usd: Optional[float] = None,
    exit_price: Optional[float] = None,
    result_type: str = "MANUAL_CLOSE",
) -> Dict[str, Any]:
    state = load_real_trade_state()
    sync = sync_real_positions_with_toobit(state, save=True)
    if sync.get("ok"):
        state = sync.get("state") or state

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

    time.sleep(1.0)
    sync_real_positions_with_toobit(load_real_trade_state(), save=True)

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

    return {"ok": True, "closed": True, "signal_id": signal_id, "exchange_result": result.get("data"), "accounting": accounting}


def get_toobit_balance_text() -> str:
    info = _get_exchange_balance_info()
    if info.get("ok"):
        return (
            "✅ بالانس توبیت\n"
            f"بالانس کل USDT: {info.get('balance')}$\n"
            f"بالانس قابل استفاده USDT: {info.get('available_balance')}$"
        )
    return f"❌ دریافت بالانس توبیت ناموفق بود:\n{info.get('error')}"


def _format_open_positions_lines(open_positions: Dict[str, Any]) -> List[str]:
    lines = []
    for _, p in list(open_positions.items())[:10]:
        if not isinstance(p, dict):
            continue
        sym = p.get("symbol")
        direction = p.get("direction")
        margin = _round_usd(_position_margin(p), 4)
        lev = _round_usd(p.get("leverage"), 4)
        upnl = _round_usd(p.get("unrealized_pnl"), 4)
        notional = _round_usd(p.get("notional"), 4)
        warn = " ⚠️" if p.get("settings_warnings") else ""
        lines.append(f"• {sym} {direction} | مارجین: {margin}$ | لوریج: {lev}x | حجم: {notional}$ | PnL باز: {upnl}${warn}")
    return lines


def get_real_trade_status_text() -> str:
    state = load_real_trade_state()
    sync_result = sync_real_positions_with_toobit(state, save=True)
    sync_line = ""
    warnings: List[str] = []
    exchange_count = 0

    if sync_result.get("ok"):
        state = sync_result.get("state") or state
        exchange_count = len(sync_result.get("exchange_positions") or [])
        if sync_result.get("added") or sync_result.get("removed"):
            sync_line = f"\nهمگام‌سازی صرافی: +{sync_result.get('added', 0)} / -{sync_result.get('removed', 0)}"
        warnings = sync_result.get("warnings") or []
    else:
        sync_line = f"\n⚠️ همگام‌سازی پوزیشن‌های توبیت ناموفق: {str(sync_result.get('error'))[:120]}"

    open_positions = state.get("open_positions", {}) if isinstance(state.get("open_positions"), dict) else {}
    open_count = exchange_count if sync_result.get("ok") else len(open_positions)
    max_positions = _safe_int(state.get("max_positions"), 0)
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

    ready, reason = is_real_trade_ready()
    readiness = "آماده سفارش واقعی" if ready else reason

    balance = state.get("last_exchange_balance")
    available = state.get("last_exchange_available_balance")
    if balance is None:
        balance_line = "بالانس واقعی توبیت: نامشخص"
    else:
        balance_line = f"بالانس واقعی توبیت: {balance}$\nبالانس قابل استفاده توبیت: {available}$"

    initial = _safe_float(state.get("initial_capital"), 0)
    account_pnl = _round_usd((_safe_float(balance, 0) - initial) if balance is not None and initial > 0 else state.get("last_exchange_account_pnl", 0), 4)
    open_pnl = _round_usd(state.get("last_exchange_unrealized_pnl", 0), 4)
    protected_loss = _round_usd(get_real_loss_from_protected(state), 4)

    pos_lines = _format_open_positions_lines(open_positions)
    positions_text = "\n".join(pos_lines) if pos_lines else "ندارد"

    warnings_text = ""
    if warnings:
        warnings_text = "\n\n⚠️ هشدار تنظیمات:\n" + "\n".join(f"• {w}" for w in warnings[:5])

    return (
        "🤖 وضعیت ترید واقعی توبیت\n"
        f"وضعیت: {status}\n"
        f"حالت: REAL\n"
        f"صرافی: TOOBIT\n"
        f"توقف اضطراری: {emergency}\n"
        f"آمادگی: {readiness}\n"
        f"پوزیشن واقعی در فیوچرز توبیت: {exchange_count}\n"
        f"{sync_line}\n\n"
        f"{balance_line}\n\n"
        f"حجم هر پوزیشن/مارجین ربات: {position_size}$\n"
        f"لوریج تنظیمی ربات: {leverage}x\n"
        f"حجم تقریبی هر پوزیشن: {approx_position}$\n\n"
        f"پوزیشن باز/اسلات: {open_count}/{max_positions}\n"
        f"اسلات خالی: {free_slots}\n\n"
        f"پوزیشن‌های باز:\n{positions_text}\n\n"
        f"سود/ضرر حساب توبیت نسبت به سرمایه تنظیمی: {account_pnl}$\n"
        f"سود/ضرر باز فعلی از توبیت: {open_pnl}$\n"
        f"سود/ضرر امروز ثبت داخلی: {round(float(state.get('today_realized_pnl', 0)), 4)}$\n"
        f"سود/ضرر کل ثبت داخلی: {round(float(state.get('total_realized_pnl', 0)), 4)}$\n"
        f"سرمایه محافظت‌شده: {state.get('protected_balance', 0)}$\n"
        f"ضرر از سرمایه محافظت‌شده: {protected_loss}$\n"
        f"حد ضرر روزانه: {state.get('daily_loss_limit_usd', 0)}$\n"
        f"زمان قفل: {state.get('daily_lock_duration_hours', DEFAULT_REAL_LOCK_DURATION_HOURS)} ساعت\n"
        f"قفل ضرر روزانه: {lock_line}"
        f"{warnings_text}"
    )
