# -*- coding: utf-8 -*-
"""
Paper Trade engine for Crypto AI bot.

Scope:
- Does not change AI/scanner/analysis architecture.
- Opens a paper position for every real Telegram signal.
- Closes it on TP1/TP2/SL from signal_tracker.
- Calculates PnL as if the trade was real:
    pnl_usdt = margin_usdt * leverage * move_percent / 100
- Maintains paper balance, daily PnL, open positions, closed history.
- Protects realized full-dollar profits by moving each complete 1$ profit
  into protected_balance immediately after a position closes.
- Applies a trading lock when the paper balance drops by daily_max_loss_usdt
  from protected_balance. Default lock duration is 1 hour.
"""

import json
import os
import time
import uuid
import math
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any, Tuple

try:
    from data_store import load_json as _ds_load_json, save_json as _ds_save_json
except Exception:
    _ds_load_json = None
    _ds_save_json = None

try:
    from auto_trade_config import (
        DEFAULT_START_BALANCE_USDT,
        DEFAULT_TRADE_MARGIN_USDT,
        DEFAULT_LEVERAGE,
        DEFAULT_MAX_OPEN_POSITIONS,
        DEFAULT_DAILY_MAX_LOSS_USDT,
        MIN_TRADE_MARGIN_USDT,
        MAX_TRADE_MARGIN_USDT,
        MIN_LEVERAGE,
        MAX_LEVERAGE,
        MIN_MAX_OPEN_POSITIONS,
        MAX_MAX_OPEN_POSITIONS,
    )
except Exception:
    DEFAULT_START_BALANCE_USDT = 1000.0
    DEFAULT_TRADE_MARGIN_USDT = 20.0
    DEFAULT_LEVERAGE = 5
    DEFAULT_MAX_OPEN_POSITIONS = 5
    DEFAULT_DAILY_MAX_LOSS_USDT = 7.0
    MIN_TRADE_MARGIN_USDT = 1.0
    MAX_TRADE_MARGIN_USDT = 1_000_000.0
    MIN_LEVERAGE = 1
    MAX_LEVERAGE = 50
    MIN_MAX_OPEN_POSITIONS = 1
    MAX_MAX_OPEN_POSITIONS = 50

# User-preferred default: daily loss lock should last 1 hour, not 12 hours.
PAPER_DEFAULT_LOCK_HOURS = 1
PAPER_PROFIT_PROTECTION_UNIT_USDT = 1.0

PAPER_FILE = "paper_trades.json"
TRADE_SETTINGS_FILE = os.path.join("data", "trade_settings.json")


def now_ts() -> int:
    return int(time.time())


def _today_key(ts: Optional[int] = None) -> str:
    return datetime.fromtimestamp(ts or now_ts(), tz=timezone.utc).strftime("%Y-%m-%d")


def _local_load_json(path: str, default: Any) -> Any:
    # Prefer project data_store for the paper file so old data path stays compatible.
    if _ds_load_json and path == PAPER_FILE:
        try:
            data = _ds_load_json(path, default)
            return data if isinstance(data, type(default)) else default
        except Exception:
            pass

    try:
        if not os.path.exists(path):
            return default
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, type(default)) else default
    except Exception:
        return default


def _local_save_json(path: str, data: Any) -> None:
    if _ds_save_json and path == PAPER_FILE:
        try:
            _ds_save_json(path, data)
            return
        except Exception:
            pass

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _read_trade_settings() -> Dict[str, Any]:
    data = _local_load_json(TRADE_SETTINGS_FILE, {})
    if not isinstance(data, dict):
        data = {}
    return {
        "capital_usd": float(data.get("capital_usd", DEFAULT_START_BALANCE_USDT) or DEFAULT_START_BALANCE_USDT),
        "trade_margin_usd": float(data.get("trade_margin_usd", DEFAULT_TRADE_MARGIN_USDT) or DEFAULT_TRADE_MARGIN_USDT),
        "leverage": float(data.get("leverage", DEFAULT_LEVERAGE) or DEFAULT_LEVERAGE),
        "max_positions": int(data.get("max_positions", DEFAULT_MAX_OPEN_POSITIONS) or DEFAULT_MAX_OPEN_POSITIONS),
    }


def _default_state() -> Dict[str, Any]:
    cfg = _read_trade_settings()
    capital = float(cfg["capital_usd"])
    return {
        "account": {
            "mode": "PAPER",
            "start_balance": capital,
            "balance": capital,
            "protected_balance": capital,
            "profit_carry_remainder": 0.0,
            "daily_max_loss_usdt": float(DEFAULT_DAILY_MAX_LOSS_USDT),
            "cooldown_hours": PAPER_DEFAULT_LOCK_HOURS,
            "daily_lock_until": 0,
            "daily_lock_reason": "",
            "created_at": now_ts(),
            "updated_at": now_ts(),
        },
        "open_positions": {},
        "closed_positions": [],
        "stats": {"total": 0, "tp1": 0, "tp2": 0, "sl": 0, "manual_closed": 0},
    }


def _migrate_protected_balance(acc: Dict[str, Any]) -> None:
    """Add protected-balance fields to old paper files without reducing old data."""
    start = float(acc.get("start_balance", 0) or 0)
    balance = float(acc.get("balance", start) or start)

    if "protected_balance" not in acc:
        full_profit_units = math.floor(max(0.0, balance - start) + 1e-9)
        acc["protected_balance"] = round(start + full_profit_units, 6)

    protected = float(acc.get("protected_balance", start) or start)
    acc["protected_balance"] = round(max(protected, start), 6)

    if "profit_carry_remainder" not in acc:
        acc["profit_carry_remainder"] = round(max(0.0, balance - float(acc["protected_balance"])), 6)

    # Any old 12h default should become 1h unless the user later changes it.
    if int(acc.get("cooldown_hours", 0) or 0) <= 0:
        acc["cooldown_hours"] = PAPER_DEFAULT_LOCK_HOURS


def _state() -> Dict[str, Any]:
    s = _local_load_json(PAPER_FILE, _default_state())
    if not isinstance(s, dict):
        s = _default_state()

    s.setdefault("open_positions", {})
    s.setdefault("closed_positions", [])
    s.setdefault("stats", {"total": 0, "tp1": 0, "tp2": 0, "sl": 0, "manual_closed": 0})

    if "account" not in s or not isinstance(s.get("account"), dict):
        cfg = _read_trade_settings()
        # Migrate old file without balance.
        closed = s.get("closed_positions", []) if isinstance(s.get("closed_positions"), list) else []
        realized = sum(float(x.get("pnl_usdt", 0) or 0) for x in closed if isinstance(x, dict))
        start_balance = float(cfg["capital_usd"])
        balance = round(start_balance + realized, 6)
        s["account"] = {
            "mode": "PAPER",
            "start_balance": start_balance,
            "balance": balance,
            "daily_max_loss_usdt": float(DEFAULT_DAILY_MAX_LOSS_USDT),
            "cooldown_hours": PAPER_DEFAULT_LOCK_HOURS,
            "daily_lock_until": 0,
            "daily_lock_reason": "",
            "created_at": now_ts(),
            "updated_at": now_ts(),
        }

    acc = s["account"]
    acc.setdefault("mode", "PAPER")
    acc.setdefault("start_balance", float(_read_trade_settings()["capital_usd"]))
    acc.setdefault("balance", float(acc.get("start_balance", 0) or 0))
    acc.setdefault("daily_max_loss_usdt", float(DEFAULT_DAILY_MAX_LOSS_USDT))
    acc.setdefault("cooldown_hours", PAPER_DEFAULT_LOCK_HOURS)
    acc.setdefault("daily_lock_until", 0)
    acc.setdefault("daily_lock_reason", "")
    _migrate_protected_balance(acc)
    return s


def _save(s: Dict[str, Any]) -> None:
    s.setdefault("account", {})["updated_at"] = now_ts()
    _local_save_json(PAPER_FILE, s)


def normalize_direction(d: Any) -> str:
    d = str(d).upper().strip()
    if d in ["LONG", "BUY", "لانگ"]:
        return "LONG"
    if d in ["SHORT", "SELL", "شورت"]:
        return "SHORT"
    return d


def calculate_pnl_percent(direction: str, entry: float, exit_price: float) -> float:
    direction = normalize_direction(direction)
    entry = float(entry)
    exit_price = float(exit_price)
    if entry <= 0:
        return 0.0
    if direction == "LONG":
        return round(((exit_price - entry) / entry) * 100, 4)
    return round(((entry - exit_price) / entry) * 100, 4)


def calculate_pnl_usdt(direction: str, entry: float, exit_price: float, margin_usdt: float, leverage: float) -> float:
    pct = calculate_pnl_percent(direction, entry, exit_price)
    return round(float(margin_usdt) * float(leverage) * pct / 100.0, 6)


def _closed_today(s: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    s = s or _state()
    today = _today_key()
    out = []
    for p in s.get("closed_positions", []):
        if not isinstance(p, dict):
            continue
        if _today_key(int(p.get("closed_at", 0) or 0)) == today:
            out.append(p)
    return out


def get_daily_pnl_usdt(s: Optional[Dict[str, Any]] = None) -> float:
    return round(sum(float(p.get("pnl_usdt", 0) or 0) for p in _closed_today(s)), 6)


def get_total_pnl_usdt(s: Optional[Dict[str, Any]] = None) -> float:
    s = s or _state()
    return round(sum(float(p.get("pnl_usdt", 0) or 0) for p in s.get("closed_positions", []) if isinstance(p, dict)), 6)


def get_loss_from_initial(s: Optional[Dict[str, Any]] = None) -> float:
    s = s or _state()
    acc = s.get("account", {})
    start = float(acc.get("start_balance", 0) or 0)
    balance = float(acc.get("balance", start) or start)
    return round(max(0.0, start - balance), 6)


def get_loss_from_protected(s: Optional[Dict[str, Any]] = None) -> float:
    s = s or _state()
    acc = s.get("account", {})
    protected = float(acc.get("protected_balance", acc.get("start_balance", 0)) or 0)
    balance = float(acc.get("balance", protected) or protected)
    return round(max(0.0, protected - balance), 6)


def _sync_profit_protection(s: Dict[str, Any]) -> float:
    """
    Move every complete 1 USDT of realized balance above protected_balance
    into protected_balance. Fractional profit remains as profit_carry_remainder.
    """
    acc = s.setdefault("account", {})
    start = float(acc.get("start_balance", 0) or 0)
    balance = float(acc.get("balance", start) or start)
    protected = float(acc.get("protected_balance", start) or start)

    if protected < start:
        protected = start

    full_units = math.floor(max(0.0, balance - protected) / PAPER_PROFIT_PROTECTION_UNIT_USDT + 1e-9)
    if full_units > 0:
        protected = round(protected + full_units * PAPER_PROFIT_PROTECTION_UNIT_USDT, 6)

    acc["protected_balance"] = protected
    acc["profit_carry_remainder"] = round(max(0.0, balance - protected), 6)
    return protected


def is_daily_locked() -> bool:
    s = _state()
    until = int(s.get("account", {}).get("daily_lock_until", 0) or 0)
    return until > now_ts()


def get_lock_remaining_seconds() -> int:
    s = _state()
    return max(0, int(s.get("account", {}).get("daily_lock_until", 0) or 0) - now_ts())


def _maybe_apply_daily_lock(s: Dict[str, Any]) -> bool:
    acc = s.setdefault("account", {})
    if int(acc.get("daily_lock_until", 0) or 0) > now_ts():
        return True

    max_loss = float(acc.get("daily_max_loss_usdt", DEFAULT_DAILY_MAX_LOSS_USDT) or DEFAULT_DAILY_MAX_LOSS_USDT)
    loss_from_protected = get_loss_from_protected(s)

    if loss_from_protected >= max_loss > 0:
        hours = int(acc.get("cooldown_hours", PAPER_DEFAULT_LOCK_HOURS) or PAPER_DEFAULT_LOCK_HOURS)
        hours = max(1, hours)
        acc["daily_lock_until"] = now_ts() + hours * 3600
        acc["daily_lock_reason"] = f"رسیدن افت از سرمایه محافظت‌شده به {round(loss_from_protected, 4)}$"
        return True
    return False


def clear_daily_lock() -> None:
    s = _state()
    s["account"]["daily_lock_until"] = 0
    s["account"]["daily_lock_reason"] = ""
    _save(s)


def configure_paper_account(
    capital_usd: Optional[float] = None,
    daily_max_loss_usdt: Optional[float] = None,
    cooldown_hours: Optional[int] = None,
    reset_balance: bool = False,
) -> Dict[str, Any]:
    s = _state()
    acc = s.setdefault("account", {})
    if capital_usd is not None:
        capital_usd = float(capital_usd)
        acc["start_balance"] = capital_usd
        if reset_balance or len(s.get("closed_positions", [])) == 0:
            acc["balance"] = capital_usd
            acc["protected_balance"] = capital_usd
            acc["profit_carry_remainder"] = 0.0
        else:
            _sync_profit_protection(s)
    if daily_max_loss_usdt is not None:
        acc["daily_max_loss_usdt"] = max(0.0, float(daily_max_loss_usdt))
    if cooldown_hours is not None:
        acc["cooldown_hours"] = max(1, int(cooldown_hours))
    _save(s)
    return s


def configure_daily_loss_limit(amount_usdt: float) -> str:
    amount = float(amount_usdt)
    if amount <= 0:
        return "❌ حد ضرر روزانه باید بیشتر از صفر باشد. مثال: حد ضرر روزانه 5"
    configure_paper_account(daily_max_loss_usdt=amount)
    return f"✅ حد ضرر روزانه روی {round(amount, 4)}$ تنظیم شد."


def configure_daily_lock_hours(hours: int) -> str:
    hours = int(hours)
    if hours < 1 or hours > 168:
        return "❌ زمان قفل باید بین 1 تا 168 ساعت باشد. مثال: قفل ضرر 1 ساعت"
    configure_paper_account(cooldown_hours=hours)
    return f"✅ زمان قفل ضرر روی {hours} ساعت تنظیم شد."


def has_open_position(symbol: str, direction: Optional[str] = None) -> bool:
    direction = normalize_direction(direction) if direction else None
    for p in _state().get("open_positions", {}).values():
        if p.get("symbol") == str(symbol).upper() and (direction is None or p.get("direction") == direction):
            return True
    return False


def can_open_paper_position(signal: Optional[Dict[str, Any]] = None) -> Tuple[bool, str]:
    if is_daily_locked():
        remaining = round(get_lock_remaining_seconds() / 3600, 2)
        return False, f"قفل ضرر روزانه فعال است؛ حدود {remaining} ساعت باقی مانده."

    cfg = _read_trade_settings()
    s = _state()
    max_pos = max(MIN_MAX_OPEN_POSITIONS, min(int(cfg["max_positions"]), MAX_MAX_OPEN_POSITIONS))
    if len(s.get("open_positions", {})) >= max_pos:
        return False, "ظرفیت پوزیشن‌های Paper پر است."

    if signal:
        symbol = str(signal.get("symbol", "")).upper()
        direction = normalize_direction(signal.get("direction"))
        if symbol and direction in ["LONG", "SHORT"] and has_open_position(symbol, direction):
            return False, "این پوزیشن Paper از قبل باز است."
    return True, "ok"


def open_paper_position(signal: Dict[str, Any], telegram_message_id=None, chat_id=None) -> Optional[Dict[str, Any]]:
    cfg = _read_trade_settings()
    allowed, _reason = can_open_paper_position(signal)
    if not allowed:
        return None

    symbol = str(signal.get("symbol", "")).upper().strip()
    direction = normalize_direction(signal.get("direction"))
    if not symbol or direction not in ["LONG", "SHORT"]:
        return None

    entry = signal.get("entry") or signal.get("price")
    sl = signal.get("stop_loss")
    tp1 = signal.get("tp1")
    if entry is None or sl is None or tp1 is None:
        return None

    margin = max(MIN_TRADE_MARGIN_USDT, min(float(cfg["trade_margin_usd"]), MAX_TRADE_MARGIN_USDT))
    leverage = max(MIN_LEVERAGE, min(float(cfg["leverage"]), MAX_LEVERAGE))
    s = _state()
    pid = f"paper_{symbol}_{direction}_{now_ts()}_{uuid.uuid4().hex[:6]}"

    p = {
        "position_id": pid,
        "signal_id": signal.get("signal_id") or signal.get("id"),
        "symbol": symbol,
        "direction": direction,
        "entry": float(entry),
        "stop_loss": float(sl),
        "tp1": float(tp1),
        "tp2": None if signal.get("tp2") is None else float(signal.get("tp2")),
        "margin_usdt": float(margin),
        "leverage": float(leverage),
        "position_size_usdt": round(float(margin) * float(leverage), 6),
        "score": signal.get("score"),
        "risk_level": signal.get("risk_level"),
        "risk_reward": signal.get("risk_reward"),
        "status": "OPEN",
        "opened_at": now_ts(),
        "telegram_message_id": telegram_message_id,
        "chat_id": chat_id,
        "snapshot": signal.get("snapshot", {}),
        "source": signal.get("source", "auto_signal"),
    }
    s["open_positions"][pid] = p
    _save(s)
    return p


def open_paper_trade(signal: Dict[str, Any]):
    allowed, reason = can_open_paper_position(signal)
    if not allowed:
        return False, f"Paper Trade باز نشد: {reason}"
    p = open_paper_position(signal)
    return (bool(p), "✅ Paper Trade باز شد." if p else "Paper Trade باز نشد یا تکراری بود.")


def close_paper_position(symbol: str, direction: str, exit_price: float, result: str, signal_id: str = None) -> Optional[Dict[str, Any]]:
    s = _state()
    target_id = None
    target = None
    direction = normalize_direction(direction)

    for pid, p in s.get("open_positions", {}).items():
        if signal_id and p.get("signal_id") == signal_id:
            target_id = pid
            target = p
            break
        if p.get("symbol") == str(symbol).upper() and p.get("direction") == direction:
            target_id = pid
            target = p
            break

    if not target_id or not target:
        return None

    exit_price = float(exit_price)
    margin = float(target.get("margin_usdt", _read_trade_settings()["trade_margin_usd"]) or 0)
    leverage = float(target.get("leverage", _read_trade_settings()["leverage"]) or 1)
    pnl_percent = calculate_pnl_percent(direction, target.get("entry"), exit_price)
    pnl_usdt = calculate_pnl_usdt(direction, target.get("entry"), exit_price, margin, leverage)

    closed = dict(target)
    closed.update({
        "status": "CLOSED",
        "result": result,
        "exit_price": exit_price,
        "pnl_percent": pnl_percent,
        "pnl_usdt": pnl_usdt,
        "closed_at": now_ts(),
    })

    del s["open_positions"][target_id]
    s["closed_positions"].append(closed)
    s["closed_positions"] = s["closed_positions"][-2000:]

    acc = s.setdefault("account", {})
    acc["balance"] = round(float(acc.get("balance", acc.get("start_balance", 0)) or 0) + pnl_usdt, 6)
    _sync_profit_protection(s)

    stats = s.setdefault("stats", {"total": 0, "tp1": 0, "tp2": 0, "sl": 0, "manual_closed": 0})
    stats["total"] = int(stats.get("total", 0)) + 1
    rk = str(result).lower()
    key = "tp1" if rk in ["tp", "tp1"] else "tp2" if rk == "tp2" else "sl" if rk == "sl" else "manual_closed"
    stats[key] = int(stats.get(key, 0)) + 1

    _maybe_apply_daily_lock(s)
    _save(s)
    return closed


def close_paper_position_by_signal_id(signal_id, exit_price, result):
    for p in _state().get("open_positions", {}).values():
        if p.get("signal_id") == signal_id:
            return close_paper_position(p.get("symbol"), p.get("direction"), exit_price, result, signal_id)
    return None


def close_paper_trade_by_signal(signal, result_type, exit_price):
    c = close_paper_position(
        signal.get("symbol"),
        signal.get("direction"),
        exit_price,
        result_type,
        signal.get("signal_id") or signal.get("id"),
    )
    if not c:
        return False, "پوزیشن Paper مربوط به این سیگنال پیدا نشد."
    return True, format_closed_trade_line(c)


def get_open_positions() -> List[Dict[str, Any]]:
    return list(_state().get("open_positions", {}).values())


def get_closed_positions() -> List[Dict[str, Any]]:
    return list(_state().get("closed_positions", []))


def get_paper_stats() -> Dict[str, Any]:
    s = _state()
    st = s.get("stats", {})
    total = int(st.get("total", 0) or 0)
    wins = int(st.get("tp1", 0) or 0) + int(st.get("tp2", 0) or 0)
    closed = s.get("closed_positions", [])
    acc = s.get("account", {})
    daily_pnl = get_daily_pnl_usdt(s)
    total_pnl = get_total_pnl_usdt(s)
    balance = float(acc.get("balance", acc.get("start_balance", 0)) or 0)
    protected_balance = float(acc.get("protected_balance", acc.get("start_balance", balance)) or balance)

    best = max([float(p.get("pnl_usdt", 0) or 0) for p in closed], default=0.0)
    worst = min([float(p.get("pnl_usdt", 0) or 0) for p in closed], default=0.0)

    return {
        "total": total,
        "tp1": int(st.get("tp1", 0) or 0),
        "tp2": int(st.get("tp2", 0) or 0),
        "sl": int(st.get("sl", 0) or 0),
        "manual_closed": int(st.get("manual_closed", 0) or 0),
        "win_rate": round(wins / max(total, 1) * 100, 2) if total else 0,
        "open_positions": len(s.get("open_positions", {})),
        "balance": round(balance, 6),
        "start_balance": round(float(acc.get("start_balance", balance) or balance), 6),
        "protected_balance": round(protected_balance, 6),
        "profit_carry_remainder": round(float(acc.get("profit_carry_remainder", 0) or 0), 6),
        "daily_pnl": round(daily_pnl, 6),
        "total_pnl": round(total_pnl, 6),
        "loss_from_initial": get_loss_from_initial(s),
        "loss_from_protected": get_loss_from_protected(s),
        "daily_max_loss": round(float(acc.get("daily_max_loss_usdt", DEFAULT_DAILY_MAX_LOSS_USDT) or DEFAULT_DAILY_MAX_LOSS_USDT), 6),
        "daily_lock": is_daily_locked(),
        "lock_remaining_seconds": get_lock_remaining_seconds(),
        "cooldown_hours": int(acc.get("cooldown_hours", PAPER_DEFAULT_LOCK_HOURS) or PAPER_DEFAULT_LOCK_HOURS),
        "daily_lock_reason": str(acc.get("daily_lock_reason", "") or ""),
        "best_trade": round(best, 6),
        "worst_trade": round(worst, 6),
    }


def _money(v: Any) -> str:
    try:
        x = float(v)
    except Exception:
        x = 0.0
    sign = "+" if x > 0 else ""
    return f"{sign}{round(x, 4)}$"


def format_closed_trade_line(c: Dict[str, Any]) -> str:
    st = get_paper_stats()
    return (
        f"Paper Trade بسته شد\n"
        f"PnL: {_money(c.get('pnl_usdt', 0))}\n"
        f"درصد: {c.get('pnl_percent')}٪\n"
        f"بالانس Paper: {_money(st.get('balance', 0)).replace('+','')}\n"
        f"سرمایه محافظت‌شده: {st.get('protected_balance', 0)}$"
    )


def format_paper_stats() -> str:
    st = get_paper_stats()
    return (
        "📊 Paper Trade\n"
        f"کل: {st['total']}\n"
        f"TP1: {st['tp1']} | TP2: {st['tp2']} | SL: {st['sl']}\n"
        f"WinRate: {st['win_rate']}%\n"
        f"باز: {st['open_positions']}\n\n"
        f"بالانس Paper: {st['balance']}$\n"
        f"سرمایه محافظت‌شده: {st['protected_balance']}$\n"
        f"سود کامل منتقل‌نشده: {st['profit_carry_remainder']}$\n"
        f"سود/ضرر امروز: {_money(st['daily_pnl'])}\n"
        f"سود/ضرر کل: {_money(st['total_pnl'])}\n"
        f"ضرر از سرمایه محافظت‌شده: {st['loss_from_protected']}$\n"
        f"حد ضرر روزانه: {st['daily_max_loss']}$\n"
        f"زمان قفل: {st['cooldown_hours']} ساعت\n"
        f"قفل ضرر: {'فعال' if st['daily_lock'] else 'غیرفعال'}"
    )


def format_paper_trade_status() -> str:
    cfg = _read_trade_settings()
    st = get_paper_stats()
    max_positions = int(cfg["max_positions"])
    free = max(0, max_positions - int(st["open_positions"]))
    position_size = round(float(cfg["trade_margin_usd"]) * float(cfg["leverage"]), 4)
    risk_pct = round((float(cfg["trade_margin_usd"]) / max(float(st["protected_balance"]), 1e-9)) * 100, 2)

    lock_line = "غیرفعال"
    if st["daily_lock"]:
        hours = round(st["lock_remaining_seconds"] / 3600, 2)
        reason = st.get("daily_lock_reason") or "رسیدن به حد ضرر روزانه"
        lock_line = f"فعال، حدود {hours} ساعت باقی مانده\nدلیل قفل: {reason}"

    return (
        "🤖 وضعیت ترید\n\n"
        "وضعیت: ✅ فعال\n"
        "حالت: PAPER\n"
        f"توقف اضطراری: {'فعال' if st['daily_lock'] else 'غیرفعال'}\n\n"
        f"سرمایه اولیه: {st['start_balance']}$\n"
        f"بالانس Paper: {st['balance']}$\n"
        f"سرمایه محافظت‌شده: {st['protected_balance']}$\n"
        f"سود کامل منتقل‌نشده: {st['profit_carry_remainder']}$\n"
        f"حجم هر پوزیشن: {cfg['trade_margin_usd']}$\n"
        f"لوریج: {cfg['leverage']}x\n"
        f"حجم پوزیشن تقریبی: {position_size}$\n"
        f"ریسک هر ترید نسبت به سرمایه محافظت‌شده: {risk_pct}%\n\n"
        f"پوزیشن باز: {st['open_positions']}/{max_positions}\n"
        f"اسلات خالی: {free}\n\n"
        f"سود/ضرر امروز: {_money(st['daily_pnl'])}\n"
        f"سود/ضرر کل: {_money(st['total_pnl'])}\n"
        f"ضرر از سرمایه اولیه: {st['loss_from_initial']}$\n"
        f"ضرر از سرمایه محافظت‌شده: {st['loss_from_protected']}$\n"
        f"حد ضرر روزانه: {st['daily_max_loss']}$\n"
        f"زمان قفل ضرر: {st['cooldown_hours']} ساعت\n"
        f"قفل ضرر روزانه: {lock_line}"
    )


def format_open_positions() -> str:
    ps = get_open_positions()
    if not ps:
        return "پوزیشن Paper بازی وجود ندارد."
    lines = ["📌 پوزیشن‌های Paper باز:"]
    for p in ps:
        lines.append(
            f"\n{p.get('symbol')} {p.get('direction')}\n"
            f"Entry: {p.get('entry')} | SL: {p.get('stop_loss')} | TP1: {p.get('tp1')}\n"
            f"Margin: {p.get('margin_usdt')}$ | Lev: {p.get('leverage')}x | Size: {p.get('position_size_usdt')}$"
        )
    return "\n".join(lines)


def reset_paper_trades(capital_usd: Optional[float] = None) -> bool:
    cfg = _read_trade_settings()
    capital = float(capital_usd if capital_usd is not None else cfg["capital_usd"])
    state = _default_state()
    state["account"]["start_balance"] = capital
    state["account"]["balance"] = capital
    state["account"]["protected_balance"] = capital
    state["account"]["profit_carry_remainder"] = 0.0
    state["account"]["cooldown_hours"] = PAPER_DEFAULT_LOCK_HOURS
    _save(state)
    return True
