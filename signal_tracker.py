# -*- coding: utf-8 -*-
"""Signal tracker for AI Classic Direct bot.

Tracks ACTIVE signals until TP1 or SL, records stats, updates AI learning/risk,
closes slot and returns Telegram reply events.
"""

import json
import os
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import ccxt

try:
    from coin_learning import record_signal, update_signal_result
except Exception:
    record_signal = None
    update_signal_result = None

try:
    from coin_learning import register_dynamic_profit_exit
except Exception:
    register_dynamic_profit_exit = None

try:
    from coin_learning import get_similarity_adjustment, find_similar_patterns
except Exception:
    get_similarity_adjustment = None
    find_similar_patterns = None

try:
    from coin_risk import register_result, register_real_result, register_ghost_result
except Exception:
    register_result = None
    register_real_result = None
    register_ghost_result = None

try:
    from slot_manager import add_position, close_position
except Exception:
    add_position = None
    close_position = None

try:
    from real_trade_manager import (
        load_real_trade_state,
        save_real_trade_state,
        record_realized_pnl,
        get_real_pnl_for_closed_position,
        check_dynamic_profit_protection,
        close_real_position,
    )
except Exception:
    load_real_trade_state = None
    save_real_trade_state = None
    record_realized_pnl = None
    get_real_pnl_for_closed_position = None
    check_dynamic_profit_protection = None
    close_real_position = None

try:
    from tobit_client import toobit_client
except Exception:
    toobit_client = None

ACTIVE_SIGNALS_FILE = "active_signals.json"
SIGNAL_STATS_FILE = "signal_stats.json"

TRACKER_OHLCV_TIMEFRAME = "1m"
TRACKER_LOOKBACK_BUFFER_SECONDS = 90
TRACKER_MAX_OHLCV_LIMIT = 180
SAME_CANDLE_HIT_POLICY = "SL_FIRST"

# Real Toobit PnL can appear in history a little late after TP/SL closes.
# Do not send/record an exchange PnL immediately as "real" unless a valid
# non-suspicious realized PnL row is found.
REAL_PNL_WAIT_SECONDS = int(os.getenv("REAL_PNL_WAIT_SECONDS", "45") or "45")
REAL_PNL_RETRY_INTERVAL_SECONDS = int(os.getenv("REAL_PNL_RETRY_INTERVAL_SECONDS", "5") or "5")
REAL_PNL_ACCEPT_ZERO_WHEN_MOVE_BELOW_PCT = float(os.getenv("REAL_PNL_ACCEPT_ZERO_WHEN_MOVE_BELOW_PCT", "0.01") or "0.01")

# Special Trade Management AI / Dynamic Profit Protection.
# Important behavior:
# - Only exits profitable trades. It never touches losing/breakeven positions.
# - It does not move SL or TP. It only closes when continuation quality fails.
# - Shock Exit is intentionally fast for ADA-style one-candle profit collapse.
DYNAMIC_PROFIT_EVENT = "AI_DYNAMIC_PROFIT_EXIT"
DYNAMIC_PROFIT_MIN_PROFIT_PCT = float(os.getenv("TRACKER_DYNAMIC_MIN_PROFIT_PCT", "0.005") or "0.005")
DYNAMIC_PROFIT_SHOCK_RETRACE_PCT = float(os.getenv("TRACKER_DYNAMIC_SHOCK_RETRACE_PCT", "0.035") or "0.035")
DYNAMIC_PROFIT_COLLAPSE_RATIO = float(os.getenv("TRACKER_DYNAMIC_COLLAPSE_RATIO", "0.45") or "0.45")
DYNAMIC_PROFIT_VOLUME_SPIKE_MULT = float(os.getenv("TRACKER_DYNAMIC_VOLUME_SPIKE_MULT", "1.8") or "1.8")
DYNAMIC_PROFIT_BODY_SPIKE_MULT = float(os.getenv("TRACKER_DYNAMIC_BODY_SPIKE_MULT", "1.35") or "1.35")

# Historical Similarity / Pattern Memory layer for Dynamic Profit AI.
# This is a soft layer: it can strengthen/soften an exit decision, but it
# never closes a losing trade and never bypasses the real Toobit close verify.
DYNAMIC_SIMILARITY_MIN_SAMPLES = int(os.getenv("TRACKER_DYNAMIC_SIM_MIN_SAMPLES", "6") or "6")
DYNAMIC_SIMILARITY_BAD_WR = float(os.getenv("TRACKER_DYNAMIC_SIM_BAD_WR", "46") or "46")
DYNAMIC_SIMILARITY_GOOD_WR = float(os.getenv("TRACKER_DYNAMIC_SIM_GOOD_WR", "66") or "66")
DYNAMIC_SIMILARITY_MAX_RISK_POINTS = float(os.getenv("TRACKER_DYNAMIC_SIM_MAX_RISK_POINTS", "3.5") or "3.5")
DYNAMIC_SIMILARITY_MAX_CONTINUE_POINTS = float(os.getenv("TRACKER_DYNAMIC_SIM_MAX_CONTINUE_POINTS", "2.2") or "2.2")

# Anti-spam / delayed retry for REAL dynamic exits.
# Toobit may reject repeated close orders if sent back-to-back. The tracker
# sends one close command, waits a few seconds, then retries only after the
# cooldown. It sends at most one pending/failure notification and one final
# close notification.
DYNAMIC_CLOSE_RETRY_SECONDS = int(os.getenv("TRACKER_DYNAMIC_CLOSE_RETRY_SECONDS", "5") or "5")
DYNAMIC_CLOSE_MAX_ATTEMPTS = int(os.getenv("TRACKER_DYNAMIC_CLOSE_MAX_ATTEMPTS", "6") or "6")
DYNAMIC_CLOSE_LONG_RETRY_SECONDS = int(os.getenv("TRACKER_DYNAMIC_CLOSE_LONG_RETRY_SECONDS", "60") or "60")

exchange = ccxt.okx({
    "enableRateLimit": True,
    "timeout": 20000,
    "options": {"defaultType": "swap"},
})


def to_okx_symbol(symbol: str) -> str:
    coin = str(symbol).upper().replace("USDT", "")
    return f"{coin}/USDT:USDT"


def now_ts() -> int:
    return int(time.time())


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load_json(path: str, default: Any):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, type(default)) else default
    except Exception:
        return default


def save_json(path: str, data: Any) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def get_active_signals() -> List[Dict[str, Any]]:
    return load_json(ACTIVE_SIGNALS_FILE, [])


def save_active_signals(signals: List[Dict[str, Any]]) -> None:
    save_json(ACTIVE_SIGNALS_FILE, signals)


def get_signal_stats() -> List[Dict[str, Any]]:
    return load_json(SIGNAL_STATS_FILE, [])


def save_signal_stats(stats: List[Dict[str, Any]]) -> None:
    save_json(SIGNAL_STATS_FILE, stats)


def reset_signal_stats() -> bool:
    try:
        save_signal_stats([])
        return True
    except Exception:
        return False


# backward compatible name
reset_stats = reset_signal_stats


def fa_direction(direction: str) -> str:
    if direction == "LONG":
        return "لانگ"
    if direction == "SHORT":
        return "شورت"
    return str(direction)


def _signal_id(signal: Dict[str, Any]) -> str:
    return str(
        signal.get("signal_id")
        or signal.get("id")
        or f"{signal.get('symbol')}_{signal.get('message_id')}_{signal.get('created_at')}"
    )


def has_active_symbol(active: List[Dict[str, Any]], user_id: int, symbol: str, direction: Optional[str] = None) -> bool:
    for item in active:
        if int(item.get("user_id", 0) or 0) != int(user_id):
            continue
        if item.get("symbol") != symbol:
            continue
        if item.get("status") != "ACTIVE":
            continue
        if direction and item.get("direction") != direction:
            continue
        return True
    return False


def can_add_automatic_signal(user_id: int, symbol: str, direction: Optional[str] = None) -> Tuple[bool, str]:
    active = get_active_signals()
    if has_active_symbol(active, user_id, symbol, direction):
        return False, "duplicate"
    return True, "ok"


def record_stat_event(signal: Dict[str, Any], event_type: str, exit_price: Optional[float] = None, move_percent: Optional[float] = None) -> None:
    stats = get_signal_stats()
    item = dict(signal)
    item["signal_id"] = _signal_id(signal)
    item["event_type"] = event_type
    item["status"] = event_type
    item["event_at"] = now_ts()
    item["event_at_text"] = now_text()
    if exit_price is not None:
        item["exit_price"] = exit_price
    if move_percent is not None:
        item["move_percent"] = move_percent
    stats.append(item)
    save_signal_stats(stats)


def ai_record_signal(signal: Dict[str, Any]) -> None:
    if not record_signal:
        return
    try:
        record_signal(signal, signal_type="REAL")
    except TypeError:
        try:
            record_signal(signal)
        except Exception:
            pass
    except Exception:
        pass


def _learning_snapshot(signal: Dict[str, Any], exit_price: Optional[float] = None, move_percent: Optional[float] = None) -> Dict[str, Any]:
    """Build compact snapshot for AI learning/risk memory.

    The analysis engine already stores a rich `snapshot`; this function keeps it
    and adds tracker/result context so coin_risk and coin_learning can learn from
    the exact real signal outcome.
    """
    snap = signal.get("snapshot") if isinstance(signal.get("snapshot"), dict) else {}
    out = dict(snap)
    for key in [
        "symbol", "direction", "entry", "price", "score", "risk_level",
        "risk_reward", "confirmations", "freshness", "rsi", "adx", "macd",
        "macd_signal", "macd_hist", "power2_buy", "power2_sell",
        "power3_buy", "power3_sell", "buy_power", "sell_power", "atr",
        "market_mode", "coin_behavior", "btc_bias", "support", "resistance",
        "entry_mode", "market_regime", "vwap_status", "ai_scanner",
        "similarity_learning", "similarity_score", "similarity_winrate",
        "similarity_samples", "similarity_adjustment",
    ]:
        if key not in out and signal.get(key) is not None:
            out[key] = signal.get(key)
    if exit_price is not None:
        out["exit_price"] = exit_price
    if move_percent is not None:
        out["move_percent"] = move_percent
    out["result_source"] = "REAL"
    out["result_recorded_at"] = now_ts()
    return out


def ai_record_result(signal: Dict[str, Any], hit_type: str, exit_price: float, pct: float) -> None:
    """Send REAL TP1/SL outcome to AI learning and coin-risk memory.

    TP2 is intentionally not tracked here per current architecture preference;
    TP1 vs SL remains the main win-rate and risk-learning signal.
    """
    signal_id = signal.get("signal_id") or signal.get("id")
    snapshot = _learning_snapshot(signal, exit_price=exit_price, move_percent=pct)

    if update_signal_result:
        try:
            update_signal_result(signal_id, hit_type, exit_price=exit_price, move_percent=pct, snapshot=snapshot, source="REAL")
        except TypeError:
            try:
                update_signal_result(signal_id, hit_type, exit_price=exit_price, move_percent=pct)
            except TypeError:
                try:
                    update_signal_result(signal_id, hit_type)
                except Exception:
                    pass
            except Exception:
                pass
        except Exception:
            pass

    # New coin_risk.py supports persistent daily + long-term memory, source,
    # and snapshot. Prefer the explicit REAL function, then fall back safely for
    # older deployments.
    try:
        if register_real_result:
            register_real_result(signal.get("symbol"), signal.get("direction"), hit_type, snapshot=snapshot)
            return
    except Exception:
        pass

    if register_result:
        try:
            register_result(signal.get("symbol"), signal.get("direction"), hit_type, source="REAL", snapshot=snapshot, is_ghost=False)
        except TypeError:
            try:
                register_result(signal.get("symbol"), signal.get("direction"), hit_type)
            except Exception:
                pass
        except Exception:
            pass


def ai_open_slot(signal: Dict[str, Any]) -> None:
    if not add_position:
        return
    try:
        add_position(signal.get("signal_id") or signal.get("id"), signal.get("symbol"), signal.get("direction"), score=signal.get("score"))
    except TypeError:
        try:
            add_position(signal)
        except Exception:
            pass
    except Exception:
        pass


def ai_close_slot(signal: Dict[str, Any]) -> None:
    if not close_position:
        return
    try:
        close_position(signal.get("signal_id") or signal.get("id"))
    except Exception:
        pass


def _extract_args_for_tracking(*args, **kwargs) -> Tuple[int, int, int, Dict[str, Any]]:
    """Supports both old and new call styles."""
    if args and isinstance(args[0], dict):
        result = dict(args[0])
        chat_id = int(kwargs.get("chat_id") or result.get("chat_id") or 0)
        message_id = int(kwargs.get("telegram_message_id") or kwargs.get("message_id") or result.get("telegram_message_id") or result.get("message_id") or 0)
        user_id = int(kwargs.get("user_id") or result.get("user_id") or 0)
        return user_id, chat_id, message_id, result

    if len(args) >= 4:
        user_id = int(args[0])
        chat_id = int(args[1])
        message_id = int(args[2])
        result = dict(args[3])
        return user_id, chat_id, message_id, result

    result = dict(kwargs.get("result") or kwargs.get("signal") or {})
    user_id = int(kwargs.get("user_id") or result.get("user_id") or 0)
    chat_id = int(kwargs.get("chat_id") or result.get("chat_id") or 0)
    message_id = int(kwargs.get("message_id") or kwargs.get("telegram_message_id") or result.get("message_id") or result.get("telegram_message_id") or 0)
    return user_id, chat_id, message_id, result


def add_signal_to_tracking(*args, **kwargs) -> Tuple[bool, str]:
    user_id, chat_id, message_id, result = _extract_args_for_tracking(*args, **kwargs)

    if result.get("direction") not in ["LONG", "SHORT"]:
        return False, "این تحلیل سیگنال قابل پیگیری ندارد."
    if result.get("stop_loss") is None:
        return False, "برای این سیگنال حد ضرر وجود ندارد."
    if result.get("tp1") is None:
        return False, "برای این سیگنال TP1 وجود ندارد."

    symbol = str(result.get("symbol", "")).upper().strip()
    direction = result.get("direction")
    active = get_active_signals()

    if has_active_symbol(active, user_id, symbol, direction):
        return False, f"⚠️ {symbol} {fa_direction(direction)} از قبل زیر نظر است."

    signal_uid = str(result.get("signal_id") or result.get("id") or f"{symbol}_{message_id}_{now_ts()}")
    entry = float(result.get("entry") or result.get("price"))
    price = float(result.get("price") or result.get("entry"))

    signal = {
        "id": signal_uid,
        "signal_id": signal_uid,
        "user_id": int(user_id or 0),
        "chat_id": int(chat_id or 0),
        "message_id": int(message_id or 0),
        "reply_to_message_id": int(message_id or 0),
        "symbol": symbol,
        "direction": direction,
        "status": "ACTIVE",
        "entry": entry,
        "price": price,
        "stop_loss": float(result["stop_loss"]),
        "tp1": float(result["tp1"]),
        "tp2": None if result.get("tp2") is None else float(result["tp2"]),
        "score": result.get("score"),
        "risk_level": result.get("risk_level"),
        "risk_reward": result.get("risk_reward"),
        "real_order": result.get("real_order"),
        "position_size_usd": result.get("position_size_usd"),
        "leverage": result.get("leverage"),
        "entry_mode": result.get("entry_mode") or "AI_CLASSIC_DIRECT",
        "confirmations": result.get("confirmations"),
        "freshness": result.get("freshness"),
        "rsi": result.get("rsi"),
        "adx": result.get("adx"),
        "macd": result.get("macd"),
        "macd_signal": result.get("macd_signal"),
        "macd_hist": result.get("macd_hist"),
        "power2_buy": result.get("power2_buy"),
        "power2_sell": result.get("power2_sell"),
        "power3_buy": result.get("power3_buy"),
        "power3_sell": result.get("power3_sell"),
        "buy_power": result.get("buy_power"),
        "sell_power": result.get("sell_power"),
        "atr": result.get("atr"),
        "market_mode": result.get("market_mode") or result.get("market_regime"),
        "coin_behavior": result.get("coin_behavior"),
        "btc_bias": result.get("btc_bias"),
        "support": result.get("support"),
        "resistance": result.get("resistance"),
        "vwap_status": result.get("vwap_status"),
        "min_score": result.get("min_score"),
        "required_confirmations": result.get("required_confirmations"),
        "ai_decision": result.get("ai_decision", {}),
        "coin_risk": result.get("coin_risk", {}),
        "rotation": result.get("rotation", {}),
        "snapshot": result.get("snapshot", {}),
        "ai_scanner": result.get("ai_scanner", {}),
        "similarity_learning": result.get("similarity_learning") or result.get("similarity") or ((result.get("snapshot") or {}).get("similarity_learning") if isinstance(result.get("snapshot"), dict) else None),
        "similarity_score": result.get("similarity_score"),
        "similarity_winrate": result.get("similarity_winrate"),
        "similarity_samples": result.get("similarity_samples"),
        "similarity_adjustment": result.get("similarity_adjustment"),
        "reasons": result.get("reasons", []),
        "created_at": now_ts(),
        "created_at_text": now_text(),
        "last_checked_at": now_ts(),
    }

    active.append(signal)
    save_active_signals(active)
    record_stat_event(signal, "SIGNAL_CREATED")
    ai_record_signal(signal)
    ai_open_slot(signal)

    msg = (
        f"✅ سیگنال زیر نظر گرفته شد\n\n"
        f"ارز: {signal['symbol']}\n"
        f"جهت: {fa_direction(signal['direction'])}\n"
        f"ورود: {signal['entry']}\n"
        f"TP1: {signal['tp1']}\n"
        f"SL: {signal['stop_loss']}"
    )
    if signal.get("score") is not None:
        msg += f"\nامتیاز: {signal.get('score')}"
    return True, msg


def get_recent_1m_candles_since(symbol: str, since_ts: int) -> List[List[Any]]:
    since_ts = int(since_ts or now_ts() - 5 * 60)
    since_ms = max(0, (since_ts - TRACKER_LOOKBACK_BUFFER_SECONDS) * 1000)
    minutes = max(5, int((now_ts() - since_ts) / 60) + 4)
    limit = min(TRACKER_MAX_OHLCV_LIMIT, max(10, minutes))
    return exchange.fetch_ohlcv(to_okx_symbol(symbol), timeframe=TRACKER_OHLCV_TIMEFRAME, since=since_ms, limit=limit) or []


def candle_path_hit(signal: Dict[str, Any], candle: List[Any]) -> Tuple[Optional[str], Optional[float]]:
    high = float(candle[2])
    low = float(candle[3])
    direction = signal.get("direction")
    tp1 = float(signal["tp1"])
    sl = float(signal["stop_loss"])

    if direction == "LONG":
        tp_hit = high >= tp1
        sl_hit = low <= sl
    elif direction == "SHORT":
        tp_hit = low <= tp1
        sl_hit = high >= sl
    else:
        return None, None

    if tp_hit and sl_hit:
        return ("SL", sl) if SAME_CANDLE_HIT_POLICY == "SL_FIRST" else ("TP1", tp1)
    if tp_hit:
        return "TP1", tp1
    if sl_hit:
        return "SL", sl
    return None, None


def move_percent(signal: Dict[str, Any], exit_price: float) -> float:
    entry = float(signal.get("entry") or 0)
    if entry <= 0:
        return 0.0
    if signal.get("direction") == "LONG":
        return round(((float(exit_price) - entry) / entry) * 100, 4)
    return round(((entry - float(exit_price)) / entry) * 100, 4)



def _extract_realized_pnl_from_any(value: Any) -> Optional[float]:
    """Best-effort extractor for realized PnL from Toobit-like responses."""
    pnl_keys = (
        "realizedPnl", "realizedPNL", "realized_pnl", "closedPnl", "closedPNL",
        "closePnl", "closeProfit", "profit", "pnl", "netPnl", "netProfit",
        "realizedProfit", "income", "amount",
    )
    def walk(v: Any):
        if isinstance(v, dict):
            for k in pnl_keys:
                if k in v:
                    try:
                        return float(v.get(k))
                    except Exception:
                        pass
            for child in v.values():
                got = walk(child)
                if got is not None:
                    return got
        elif isinstance(v, list):
            for item in v:
                got = walk(item)
                if got is not None:
                    return got
        return None
    return walk(value)



def _is_real_pnl_value_confirmed(pnl: Any, pct: Optional[float]) -> Tuple[bool, str]:
    """Reject suspicious 0.0 PnL when the signal clearly moved.

    Toobit history can temporarily return rows with amount/income=0 or no final
    realized PnL right after TP/SL. Treat those as NOT_READY instead of real.
    """
    try:
        value = float(pnl)
    except Exception:
        return False, "PNL_NOT_NUMERIC"

    try:
        movement = abs(float(pct or 0))
    except Exception:
        movement = 0.0

    if abs(value) <= 0.00000001 and movement >= REAL_PNL_ACCEPT_ZERO_WHEN_MOVE_BELOW_PCT:
        return False, "ZERO_PNL_SUSPICIOUS_WAITING_FOR_TOOBIT"

    return True, "OK"


def _fetch_real_closed_pnl_from_exchange(signal: Dict[str, Any], pos: Optional[Dict[str, Any]], hit_type: str, exit_price: float, pct: Optional[float] = None) -> Dict[str, Any]:
    """Read REAL closed-position PnL from Toobit with delayed retries.

    The tracker waits up to REAL_PNL_WAIT_SECONDS and retries every
    REAL_PNL_RETRY_INTERVAL_SECONDS. It never labels 0.0 as real when the signal
    movement is clearly non-zero, because Toobit sometimes exposes final history
    a few seconds after the position disappears from open positions.
    """
    symbol = str((pos or {}).get("symbol") or signal.get("symbol") or "").upper()
    direction = str((pos or {}).get("direction") or signal.get("direction") or "").upper()
    signal_id = str(signal.get("signal_id") or signal.get("id") or (pos or {}).get("signal_id") or "")
    opened_at = int((pos or {}).get("opened_at") or signal.get("created_at") or 0)
    last_error = "real closed pnl not available"
    checked_sources: List[Dict[str, Any]] = []

    wait_seconds = max(0, int(REAL_PNL_WAIT_SECONDS))
    interval = max(1, int(REAL_PNL_RETRY_INTERVAL_SECONDS))
    deadline = time.time() + wait_seconds
    attempt = 0

    while True:
        attempt += 1
        closed_at = now_ts()

        # Preferred project-level hook in real_trade_manager.py if it exists.
        if callable(get_real_pnl_for_closed_position):
            try:
                res = get_real_pnl_for_closed_position(
                    signal_id=signal_id,
                    symbol=symbol,
                    direction=direction,
                    opened_at=opened_at,
                    closed_at=closed_at,
                    exit_price=exit_price,
                    result=hit_type,
                    position=pos,
                    signal=signal,
                )
                if isinstance(res, dict) and res.get("ok") and res.get("pnl_usd") is not None:
                    pnl_value = float(res.get("pnl_usd"))
                    confirmed, reason = _is_real_pnl_value_confirmed(pnl_value, pct)
                    checked_sources.append({"attempt": attempt, "source": "manager", "confirmed": confirmed, "reason": reason, "pnl": pnl_value})
                    if confirmed:
                        return {"ok": True, "pnl_usd": pnl_value, "source": res.get("source") or "TOOBIT", "raw": res, "attempts": attempt}
                    last_error = reason
                else:
                    last_error = "manager returned no confirmed pnl"
                    checked_sources.append({"attempt": attempt, "source": "manager", "confirmed": False, "reason": last_error})
            except Exception as e:
                last_error = str(e)[:250]
                checked_sources.append({"attempt": attempt, "source": "manager", "confirmed": False, "error": last_error})
        else:
            last_error = "real_trade_manager pnl hook missing"

        # Direct tobit_client compatibility hooks if present.
        if toobit_client is not None:
            candidates = (
                "get_closed_position_pnl",
                "get_realized_pnl_for_position",
                "get_recent_closed_pnl",
                "get_position_realized_pnl",
                "get_closed_position_history",
                "get_income_history",
            )
            for name in candidates:
                method = getattr(tobit_client, name, None)
                if not callable(method):
                    continue
                call_styles = [
                    lambda m=method: m(symbol=symbol, direction=direction, signal_id=signal_id, opened_at=opened_at, closed_at=closed_at),
                    lambda m=method: m(symbol, direction, opened_at, closed_at),
                    lambda m=method: m(symbol=symbol),
                    lambda m=method: m(symbol),
                ]
                for call in call_styles:
                    try:
                        res = call()
                        pnl = _extract_realized_pnl_from_any(res)
                        ok = bool(res.get("ok", True)) if isinstance(res, dict) else True
                        if ok and pnl is not None:
                            confirmed, reason = _is_real_pnl_value_confirmed(pnl, pct)
                            checked_sources.append({"attempt": attempt, "source": name, "confirmed": confirmed, "reason": reason, "pnl": pnl})
                            if confirmed:
                                return {"ok": True, "pnl_usd": float(pnl), "source": f"TOOBIT:{name}", "raw": res, "attempts": attempt}
                            last_error = reason
                    except TypeError:
                        continue
                    except Exception as e:
                        last_error = str(e)[:250]
                        checked_sources.append({"attempt": attempt, "source": name, "confirmed": False, "error": last_error})
                        break

        if time.time() >= deadline:
            break
        time.sleep(interval)

    return {
        "ok": False,
        "error": last_error or "real closed pnl not available after retries",
        "attempts": attempt,
        "wait_seconds": wait_seconds,
        "retry_interval": interval,
        "sources_checked": checked_sources[-20:],
    }



def _real_position_still_open_on_toobit(signal: Dict[str, Any], pos: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Verify real Toobit position is no longer open before final TP/SL accounting.

    Tracker candles are from market-data feed, while real TP/SL execution happens
    on Toobit. If OKX candle touches TP/SL but Toobit still reports the futures
    position open, the bot must keep tracking and must not free slots or record
    a final result yet.
    """
    if toobit_client is None or not isinstance(pos, dict):
        return {"checked": False, "still_open": False, "reason": "NO_REAL_POSITION_OR_CLIENT"}

    symbol = str(pos.get("symbol") or signal.get("symbol") or "").upper()
    direction = str(pos.get("direction") or signal.get("direction") or "").upper()
    quantity = _safe_percent(pos.get("quantity"), 0.0)

    if not symbol or direction not in {"LONG", "SHORT"}:
        return {"checked": False, "still_open": False, "reason": "MISSING_SYMBOL_DIRECTION"}

    try:
        checker = getattr(toobit_client, "_has_open_position", None)
        if callable(checker):
            opened, position_result = checker(symbol, direction, quantity)
            return {
                "checked": True,
                "still_open": bool(opened),
                "source": "toobit_client._has_open_position",
                "position_result": position_result,
            }
    except Exception as e:
        return {"checked": True, "still_open": False, "error": str(e)[:250], "source": "toobit_client._has_open_position"}

    return {"checked": False, "still_open": False, "reason": "NO_POSITION_CHECKER"}

def record_real_trade_result_for_signal(signal: Dict[str, Any], hit_type: str, exit_price: float, pct: float) -> Optional[Dict[str, Any]]:
    """
    Update REAL trading accounting when tracker detects TP1/SL.

    Prefer REAL Toobit closed-position PnL.

    The old percent formula is kept only as a fallback and is clearly marked as
    approximate, because fees/funding/slippage/execution price can make the
    exchange PnL different from signal-price PnL.
    """
    if not (load_real_trade_state and save_real_trade_state and record_realized_pnl):
        return None

    try:
        state = load_real_trade_state()
        # Keep REAL internal slots aligned with Toobit before calculating PnL.
        # This covers cases where an order was initially PENDING/ACCEPTED and
        # became an open position seconds later, or a position was recovered from
        # the exchange after an API response mismatch.
        try:
            from real_trade_manager import sync_real_positions_with_toobit
            sync_result = sync_real_positions_with_toobit(state, save=True)
            if isinstance(sync_result, dict) and sync_result.get("ok"):
                state = sync_result.get("state") or state
        except Exception:
            pass

        open_positions = state.get("open_positions", {})
        if not isinstance(open_positions, dict):
            open_positions = {}

        sid = str(signal.get("signal_id") or signal.get("id") or "")
        symbol = str(signal.get("symbol") or "").upper()
        direction = signal.get("direction")

        pos_key = None
        pos = None

        if sid and sid in open_positions:
            pos_key = sid
            pos = open_positions.get(sid)

        if not isinstance(pos, dict):
            for k, v in open_positions.items():
                if not isinstance(v, dict):
                    continue
                if str(v.get("symbol") or "").upper() == symbol and v.get("direction") == direction:
                    pos_key = k
                    pos = v
                    break

        margin = 0.0
        leverage = 0.0

        if isinstance(pos, dict):
            margin = float(pos.get("position_size_usd") or 0)
            leverage = float(pos.get("leverage") or 0)

        if margin <= 0:
            margin = float(signal.get("position_size_usd") or state.get("position_size_usd") or 0)
        if leverage <= 0:
            leverage = float(signal.get("leverage") or state.get("leverage") or 0)

        if margin <= 0 or leverage <= 0:
            return {
                "ok": False,
                "error": "margin/leverage missing",
                "margin": margin,
                "leverage": leverage,
            }

        # Safety before finalizing TP/SL: real execution is Toobit, not OKX.
        # If Toobit still shows the matching futures position open, keep the
        # signal active and wait for exchange TP/SL/close confirmation.
        open_check = _real_position_still_open_on_toobit(signal, pos)
        if open_check.get("checked") and open_check.get("still_open"):
            return {
                "ok": False,
                "keep_open": True,
                "error": "OKX candle touched TP/SL but Toobit position is still open; waiting for real exchange close.",
                "position_check": open_check,
                "margin": margin,
                "leverage": leverage,
                "notional": round(margin * leverage, 6),
            }

        exchange_pnl = _fetch_real_closed_pnl_from_exchange(signal, pos, hit_type, exit_price, pct=pct)
        pnl_source = "TOOBIT_REAL" if exchange_pnl.get("ok") else "APPROX_FALLBACK"
        pnl_error = exchange_pnl.get("error")

        if exchange_pnl.get("ok"):
            pnl_usd = round(float(exchange_pnl.get("pnl_usd") or 0), 6)
        else:
            # Fallback only. Do not present this as exact real PnL.
            pnl_usd = round(float(margin) * float(leverage) * float(pct) / 100.0, 6)

        # Remove from REAL open positions before accounting so status/slots are not stale.
        if pos_key:
            state.setdefault("open_positions", {}).pop(pos_key, None)
            save_real_trade_state(state)

        accounting = record_realized_pnl(
            pnl_usd=pnl_usd,
            signal_id=sid or None,
            symbol=symbol,
            direction=direction,
            result=hit_type,
            exit_price=exit_price,
            entry=(pos or {}).get("entry") or signal.get("entry"),
            stop_loss=(pos or {}).get("sl") or (pos or {}).get("stop_loss") or signal.get("stop_loss"),
            tp1=(pos or {}).get("tp1") or signal.get("tp1"),
            tp2=(pos or {}).get("tp2") or signal.get("tp2"),
            snapshot=_learning_snapshot(signal, exit_price=exit_price, move_percent=pct),
        )

        return {
            "ok": True,
            "pnl_usd": pnl_usd,
            "pnl_source": pnl_source,
            "pnl_error": pnl_error,
            "exchange_pnl": exchange_pnl if exchange_pnl.get("ok") else None,
            "margin": margin,
            "leverage": leverage,
            "notional": round(margin * leverage, 6),
            "accounting": accounting,
            "removed_position": bool(pos_key),
        }

    except Exception as e:
        return {"ok": False, "error": str(e)[:250]}




def _safe_percent(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _dynamic_event_fa(event_type: str) -> str:
    if event_type == DYNAMIC_PROFIT_EVENT:
        return "خروج هوشمند سود"
    if event_type == "TP1":
        return "حد سود 1"
    if event_type == "SL":
        return "حد ضرر"
    return str(event_type)


def _is_profit_event(event_type: str) -> bool:
    return event_type in {"TP1", DYNAMIC_PROFIT_EVENT}


def _is_closed_event(event_type: str) -> bool:
    return event_type in {"TP1", "SL", DYNAMIC_PROFIT_EVENT}


def _candle_close(candle: List[Any]) -> float:
    return float(candle[4])


def _candle_open(candle: List[Any]) -> float:
    return float(candle[1])


def _candle_high(candle: List[Any]) -> float:
    return float(candle[2])


def _candle_low(candle: List[Any]) -> float:
    return float(candle[3])


def _candle_volume(candle: List[Any]) -> float:
    try:
        return float(candle[5])
    except Exception:
        return 0.0


def _body_pct(candle: List[Any]) -> float:
    close = _candle_close(candle)
    if close <= 0:
        return 0.0
    return abs(_candle_close(candle) - _candle_open(candle)) / close * 100.0


def _avg(values: List[float], default: float = 0.0) -> float:
    clean = [float(x) for x in values if x is not None]
    return sum(clean) / len(clean) if clean else default


def _is_against_trade_candle(signal: Dict[str, Any], candle: List[Any]) -> bool:
    direction = str(signal.get("direction") or "").upper()
    open_price = _candle_open(candle)
    close = _candle_close(candle)
    if direction == "LONG":
        return close < open_price
    if direction == "SHORT":
        return close > open_price
    return False


def _favorable_price_for_signal(signal: Dict[str, Any], candle: List[Any]) -> float:
    return _candle_high(candle) if signal.get("direction") == "LONG" else _candle_low(candle)


def _adverse_price_for_signal(signal: Dict[str, Any], candle: List[Any]) -> float:
    return _candle_low(candle) if signal.get("direction") == "LONG" else _candle_high(candle)


def _update_signal_mfe_mae(signal: Dict[str, Any], candle: List[Any]) -> None:
    """Keep max favorable/adverse movement for Dynamic Profit AI learning."""
    try:
        fav_pct = move_percent(signal, _favorable_price_for_signal(signal, candle))
        adv_pct = move_percent(signal, _adverse_price_for_signal(signal, candle))
        signal["max_favorable_percent"] = round(max(_safe_percent(signal.get("max_favorable_percent"), 0.0), fav_pct), 6)
        signal["max_adverse_percent"] = round(min(_safe_percent(signal.get("max_adverse_percent"), 0.0), adv_pct), 6)
    except Exception:
        pass



def _build_dynamic_similarity_snapshot(
    signal: Dict[str, Any],
    candle: List[Any],
    recent_candles: List[List[Any]],
    profit_pct: float,
    peak_profit: float,
    retrace: float,
    body: float,
    volume_spike: bool,
    body_spike: bool,
) -> Dict[str, Any]:
    """Build the current in-trade snapshot for Historical Similarity.

    It reuses the original signal snapshot and adds live trade-management
    features. The Similarity Engine can then compare this live state to past
    REAL/GHOST outcomes for the same coin+direction.
    """
    snap = _learning_snapshot(signal, exit_price=_candle_close(candle), move_percent=profit_pct)
    snap.update({
        "symbol": signal.get("symbol"),
        "direction": signal.get("direction"),
        "dynamic_profit_check": True,
        "current_profit_pct": round(float(profit_pct), 6),
        "peak_profit_pct": round(float(peak_profit), 6),
        "retrace_from_peak_pct": round(float(retrace), 6),
        "max_favorable_percent": signal.get("max_favorable_percent"),
        "max_adverse_percent": signal.get("max_adverse_percent"),
        "against_trade_candle": _is_against_trade_candle(signal, candle),
        "current_body_pct": round(float(body), 6),
        "body_spike": bool(body_spike),
        "volume_spike": bool(volume_spike),
        "recent_candle_count": len(recent_candles or []),
        "move_state": snap.get("move_state") or "IN_TRADE_PROFIT_MANAGEMENT",
    })
    return snap


def _read_similarity_number(data: Dict[str, Any], *keys: str, default: float = 0.0) -> float:
    if not isinstance(data, dict):
        return float(default)
    for key in keys:
        if data.get(key) is not None:
            return _safe_percent(data.get(key), default)
    return float(default)


def _dynamic_similarity_context(signal: Dict[str, Any], snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """Return a normalized Similarity Engine result for dynamic exits.

    Supports both new get_similarity_adjustment(...) and the lower-level
    find_similar_patterns(...). Missing/old coin_learning.py remains safe and
    simply returns unavailable=False.
    """
    symbol = str(signal.get("symbol") or snapshot.get("symbol") or "").upper()
    direction = str(signal.get("direction") or snapshot.get("direction") or "").upper()
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

    nested = raw.get("similarity") if isinstance(raw.get("similarity"), dict) else {}
    samples = int(_read_similarity_number(raw, "samples", "similar_samples", "match_count", "count", default=_read_similarity_number(nested, "samples", "similar_samples", "match_count", "count", default=0)))
    winrate = _read_similarity_number(raw, "win_rate", "winrate", "similar_winrate", "tp_rate", default=_read_similarity_number(nested, "win_rate", "winrate", "similar_winrate", "tp_rate", default=50.0))
    rank_adj = _read_similarity_number(raw, "rank_adjustment", "similarity_adjustment", "adjustment", default=_read_similarity_number(nested, "rank_adjustment", "similarity_adjustment", "adjustment", default=0.0))
    avg_move = _read_similarity_number(raw, "avg_move", "average_move", "avg_move_percent", default=_read_similarity_number(nested, "avg_move", "average_move", "avg_move_percent", default=0.0))
    avg_mfe = _read_similarity_number(raw, "avg_mfe", "avg_max_favorable", "avg_max_favorable_pct", default=_read_similarity_number(nested, "avg_mfe", "avg_max_favorable", "avg_max_favorable_pct", default=0.0))
    avg_mae = _read_similarity_number(raw, "avg_mae", "avg_max_adverse", "avg_max_adverse_pct", default=_read_similarity_number(nested, "avg_mae", "avg_max_adverse", "avg_max_adverse_pct", default=0.0))

    return {
        "available": samples >= max(1, DYNAMIC_SIMILARITY_MIN_SAMPLES),
        "samples": samples,
        "winrate": round(winrate, 2),
        "rank_adjustment": round(rank_adj, 4),
        "avg_move": round(avg_move, 6),
        "avg_mfe": round(avg_mfe, 6),
        "avg_mae": round(avg_mae, 6),
        "raw": raw,
    }

def _tracker_dynamic_profit_decision(signal: Dict[str, Any], candle: List[Any], recent_candles: List[List[Any]]) -> Dict[str, Any]:
    """Fast tracker-side Dynamic Profit decision.

    This is intentionally conservative about one weak indicator, but fast about
    shock reversal. It exits only when the trade is already profitable.
    It does not change SL/TP and does not close losing/breakeven trades.
    """
    close = _candle_close(candle)
    profit_pct = move_percent(signal, close)

    if profit_pct <= DYNAMIC_PROFIT_MIN_PROFIT_PCT:
        return {"exit": False, "reason": "NOT_IN_PROFIT", "profit_pct": profit_pct}

    peak_profit = _safe_percent(signal.get("max_favorable_percent"), 0.0)
    retrace = max(0.0, peak_profit - profit_pct)
    against = _is_against_trade_candle(signal, candle)

    prev = recent_candles[-8:-1] if len(recent_candles) > 1 else []
    avg_body = _avg([_body_pct(x) for x in prev], 0.0)
    avg_vol = _avg([_candle_volume(x) for x in prev], 0.0)
    body = _body_pct(candle)
    vol = _candle_volume(candle)
    body_spike = bool(avg_body > 0 and body >= avg_body * DYNAMIC_PROFIT_BODY_SPIKE_MULT)
    volume_spike = bool(avg_vol > 0 and vol >= avg_vol * DYNAMIC_PROFIT_VOLUME_SPIKE_MULT)

    similarity_snapshot = _build_dynamic_similarity_snapshot(
        signal, candle, recent_candles, profit_pct, peak_profit, retrace, body, volume_spike, body_spike
    )
    similarity = _dynamic_similarity_context(signal, similarity_snapshot)

    reasons: List[str] = []
    risk_score = 0.0
    continue_score = 0.0

    def add_risk(points: float, text: str) -> None:
        nonlocal risk_score
        risk_score += float(points)
        if text not in reasons:
            reasons.append(text)

    def add_continue(points: float) -> None:
        nonlocal continue_score
        continue_score += float(points)

    # 1) ADA-style shock path: one opposite candle suddenly takes back floating profit.
    if peak_profit > 0 and retrace >= DYNAMIC_PROFIT_SHOCK_RETRACE_PCT and against:
        add_risk(4.5, f"برگشت ناگهانی از اوج سود ({round(retrace, 3)}٪)")
    if peak_profit >= 0.08 and retrace >= max(0.03, peak_profit * DYNAMIC_PROFIT_COLLAPSE_RATIO):
        add_risk(5.0, "پس‌گرفتن بخش بزرگی از سود شناور")
    if against and body_spike:
        add_risk(2.0, "کندل مخالف قوی")
    if against and volume_spike:
        add_risk(1.5, "افزایش حجم روی کندل برگشتی")

    # 2) Simple candle-structure continuation guard.
    # If the candle extends the trade direction and closes favorably, do not exit from noise.
    direction = str(signal.get("direction") or "").upper()
    if direction == "LONG" and _candle_close(candle) > _candle_open(candle) and _candle_close(candle) >= (_candle_high(candle) + _candle_low(candle)) / 2:
        add_continue(1.5)
    elif direction == "SHORT" and _candle_close(candle) < _candle_open(candle) and _candle_close(candle) <= (_candle_high(candle) + _candle_low(candle)) / 2:
        add_continue(1.5)

    # 3) Historical Similarity / Pattern Memory layer.
    # If the live profitable state resembles past weak/failed states for this
    # coin+direction, strengthen the exit decision. If similar states usually
    # continued to TP, protect against premature exits. This is soft and only
    # runs after the trade is already profitable.
    if similarity.get("available"):
        sim_samples = int(similarity.get("samples") or 0)
        sim_wr = _safe_percent(similarity.get("winrate"), 50.0)
        sim_adj = _safe_percent(similarity.get("rank_adjustment"), 0.0)
        sim_avg_move = _safe_percent(similarity.get("avg_move"), 0.0)
        if sim_wr <= DYNAMIC_SIMILARITY_BAD_WR or sim_adj <= -2.0 or sim_avg_move < 0:
            risk_points = min(DYNAMIC_SIMILARITY_MAX_RISK_POINTS, max(1.0, (50.0 - sim_wr) / 8.0 + max(0.0, -sim_adj) * 0.22))
            add_risk(risk_points, f"شباهت به الگوهای برگشتی/ضعیف قبلی ({sim_samples} نمونه، WR {round(sim_wr,1)}٪)")
        elif sim_wr >= DYNAMIC_SIMILARITY_GOOD_WR and sim_adj >= 1.5:
            cont_points = min(DYNAMIC_SIMILARITY_MAX_CONTINUE_POINTS, max(0.8, (sim_wr - 55.0) / 10.0 + sim_adj * 0.12))
            add_continue(cont_points)

    net_score = risk_score - continue_score
    has_shock = any("ناگهانی" in r or "شناور" in r for r in reasons)

    # Fast rule: shock exits immediately if still profitable.
    # Normal rule: several risks must combine; one weak clue alone is not enough.
    should_exit = False
    if has_shock and profit_pct > 0:
        should_exit = True
    elif profit_pct > 0 and net_score >= 6.0 and len(reasons) >= 2:
        should_exit = True

    return {
        "exit": bool(should_exit),
        "reason": "، ".join(reasons[:4]) if should_exit else "HOLD_PROFIT",
        "profit_pct": round(profit_pct, 4),
        "peak_profit_pct": round(peak_profit, 4),
        "retrace_from_peak_pct": round(retrace, 4),
        "risk_score": round(risk_score, 3),
        "continuation_score": round(continue_score, 3),
        "net_exit_score": round(net_score, 3),
        "exit_price": close,
        "similarity_learning": {k: v for k, v in similarity.items() if k != "raw"},
        "similarity_snapshot": similarity_snapshot,
    }


def _normalize_dynamic_close_accounting(
    signal: Dict[str, Any],
    close_result: Optional[Dict[str, Any]],
    pct: float,
) -> Optional[Dict[str, Any]]:
    """Convert real_trade_manager.close_real_position() output to tracker message shape.

    Dynamic Profit accounting is performed by real_trade_manager after the
    exchange close is verified. The tracker must not record PnL a second time.
    """
    if not isinstance(close_result, dict) or not close_result.get("ok"):
        return close_result if isinstance(close_result, dict) else None

    accounting = close_result.get("accounting")
    if not isinstance(accounting, dict):
        accounting = {}

    try:
        margin = float(signal.get("position_size_usd") or 0)
    except Exception:
        margin = 0.0
    try:
        leverage = float(signal.get("leverage") or 0)
    except Exception:
        leverage = 0.0

    pnl = accounting.get("pnl_usd")
    if pnl is None:
        pnl = close_result.get("pnl_usd", 0)

    return {
        "ok": True,
        "pnl_usd": pnl,
        "pnl_source": "DYNAMIC_REAL_CLOSE",
        "pnl_error": None,
        "exchange_pnl": close_result.get("exchange_result"),
        "margin": margin,
        "leverage": leverage,
        "notional": round(margin * leverage, 6) if margin and leverage else None,
        "accounting": accounting,
        "removed_position": True,
        "close_verified": True,
        "close_result": close_result,
        "move_percent": pct,
    }


def _check_real_dynamic_profit_exit(signal: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Ask real_trade_manager to close a profitable REAL position if the special AI exit fires.

    This path is authoritative: if real_trade_manager says it closed, it has
    already sent the close order and recorded accounting. The tracker must only
    record stats/Telegram result and must not call record_real_trade_result_for_signal.
    """
    if not callable(check_dynamic_profit_protection):
        return None
    try:
        sid = str(signal.get("signal_id") or signal.get("id") or "")
        if not sid:
            return None
        result = check_dynamic_profit_protection(signal_id=sid)
        if not isinstance(result, dict) or not result.get("ok"):
            return None
        for row in result.get("results") or []:
            if str(row.get("signal_id") or "") != sid:
                continue
            if row.get("closed"):
                close_result = row.get("close_result") if isinstance(row.get("close_result"), dict) else {}
                return {
                    "exit": True,
                    "exit_price": float(row.get("exit_price") or 0),
                    "reason": row.get("reason") or "خروج هوشمند سود",
                    "profit_pct": _safe_percent(row.get("profit_pct"), 0.0),
                    "source": "REAL_TRADE_MANAGER",
                    "raw": row,
                    "close_result": close_result,
                    "already_accounted": True,
                }
            if row.get("exit") and not row.get("closed"):
                signal["last_dynamic_profit_error"] = str((row.get("close_result") or {}).get("error") or row)[:250]
        return None
    except Exception as e:
        signal["last_dynamic_profit_error"] = str(e)[:250]
        return None



def _dynamic_pending_reason(signal: Dict[str, Any]) -> str:
    reason = str(signal.get("dynamic_exit_reason") or signal.get("last_dynamic_profit_reason") or "خروج هوشمند سود")
    return reason[:500]


def _set_dynamic_close_pending(signal: Dict[str, Any], exit_price: float, decision: Optional[Dict[str, Any]], close_result: Optional[Dict[str, Any]]) -> None:
    """Store pending close state so the bot does not spam Telegram or Toobit."""
    attempts = int(signal.get("dynamic_exit_attempts", 0) or 0) + 1
    signal["dynamic_exit_pending"] = True
    signal["dynamic_exit_attempts"] = attempts
    signal["dynamic_exit_first_detected_at"] = int(signal.get("dynamic_exit_first_detected_at") or now_ts())
    signal["last_dynamic_exit_attempt_at"] = now_ts()
    signal["next_dynamic_exit_retry_at"] = now_ts() + max(3, int(DYNAMIC_CLOSE_RETRY_SECONDS))
    signal["dynamic_exit_price"] = float(exit_price or signal.get("dynamic_exit_price") or 0)
    if isinstance(decision, dict):
        signal["dynamic_exit_decision"] = decision
        signal["dynamic_exit_reason"] = str(decision.get("reason") or signal.get("dynamic_exit_reason") or "خروج هوشمند سود")[:500]
    err = ""
    if isinstance(close_result, dict):
        err = str(close_result.get("error") or close_result.get("message") or close_result)[:500]
    signal["last_dynamic_profit_error"] = err or str(close_result)[:500]


def _clear_dynamic_close_pending(signal: Dict[str, Any]) -> None:
    for key in [
        "dynamic_exit_pending", "dynamic_exit_attempts", "dynamic_exit_first_detected_at",
        "last_dynamic_exit_attempt_at", "next_dynamic_exit_retry_at", "dynamic_exit_price",
        "dynamic_exit_decision", "dynamic_exit_reason", "dynamic_exit_notified",
        "dynamic_exit_gave_up_notified", "last_dynamic_profit_error",
    ]:
        signal.pop(key, None)


def _build_dynamic_pending_message(signal: Dict[str, Any]) -> str:
    attempts = int(signal.get("dynamic_exit_attempts", 0) or 0)
    reason = _dynamic_pending_reason(signal)
    err = str(signal.get("last_dynamic_profit_error") or "")[:250]
    text = (
        f"⚠️ خروج هوشمند سود برای {signal.get('symbol')} تشخیص داده شد.\n"
        f"دستور بستن ارسال/تلاش شد اما بستن پوزیشن هنوز در توبیت تایید نشد.\n"
        f"ربات بدون اسپم، با فاصله چند ثانیه دوباره تلاش می‌کند.\n"
        f"تلاش: {attempts}/{DYNAMIC_CLOSE_MAX_ATTEMPTS}\n"
        f"دلیل: {reason}"
    )
    if err:
        text += f"\nخطا: {err}"
    return text


def _build_dynamic_giveup_message(signal: Dict[str, Any]) -> str:
    err = str(signal.get("last_dynamic_profit_error") or "")[:250]
    return (
        f"⚠️ خروج هوشمند سود برای {signal.get('symbol')} چند بار تلاش شد اما هنوز تایید بسته‌شدن نگرفت.\n"
        f"پوزیشن در لیست فعال نگه داشته شد و ربات با فاصله طولانی‌تر دوباره چک می‌کند.\n"
        f"لطفاً وضعیت پوزیشن را در توبیت بررسی کن."
        + (f"\nآخرین خطا: {err}" if err else "")
    )


def _should_retry_dynamic_close(signal: Dict[str, Any]) -> bool:
    if not bool(signal.get("dynamic_exit_pending")):
        return False
    return now_ts() >= int(signal.get("next_dynamic_exit_retry_at", 0) or 0)


def _execute_tracker_dynamic_real_close(signal: Dict[str, Any], exit_price: float, decision: Dict[str, Any]) -> Dict[str, Any]:
    """Close a REAL position for tracker-side Shock/Dynamic Exit.

    Tracker-side Dynamic Profit is allowed to become final only after
    real_trade_manager.close_real_position() returns ok=True. If the close fails,
    the active signal must remain open.
    """
    if not callable(close_real_position):
        return {"ok": False, "error": "real_trade_manager.close_real_position در دسترس نیست"}

    sid = str(signal.get("signal_id") or signal.get("id") or "")
    if not sid:
        return {"ok": False, "error": "signal_id برای بستن پوزیشن واقعی پیدا نشد"}

    try:
        return close_real_position(
            signal_id=sid,
            exit_price=float(exit_price),
            result_type=DYNAMIC_PROFIT_EVENT,
        )
    except Exception as e:
        return {"ok": False, "error": str(e)[:250]}


def _record_dynamic_learning(signal: Dict[str, Any], exit_price: float, pct: float, decision: Optional[Dict[str, Any]] = None) -> None:
    try:
        if callable(register_dynamic_profit_exit):
            register_dynamic_profit_exit(
                symbol=signal.get("symbol"),
                direction=signal.get("direction"),
                entry=signal.get("entry"),
                exit_price=exit_price,
                snapshot={**_learning_snapshot(signal, exit_price=exit_price, move_percent=pct), **({"similarity_learning": (decision or {}).get("similarity_learning"), "dynamic_similarity_snapshot": (decision or {}).get("similarity_snapshot")} if isinstance(decision, dict) else {})},
                reason=(decision or {}).get("reason"),
                source="TRACKER",
                signal_id=signal.get("signal_id") or signal.get("id"),
                max_favorable=signal.get("max_favorable_percent"),
                max_adverse=signal.get("max_adverse_percent"),
                decision=decision or {},
            )
    except Exception:
        pass


def _build_result_message(signal: Dict[str, Any], hit_type: str, exit_price: float, pct: float, real_trade_accounting: Optional[Dict[str, Any]], reason: str = "") -> str:
    if hit_type == DYNAMIC_PROFIT_EVENT:
        icon = "🟢"
        result_fa = "خروج زودتر با سود"
        text = (
            f"{icon} خروج هوشمند سود {signal.get('symbol')}\n"
            f"جهت: {fa_direction(signal.get('direction'))}\n"
            f"ورود: {signal.get('entry')}\n"
            f"قیمت خروج: {exit_price}\n"
            f"نتیجه: {result_fa}\n"
            f"درصد حرکت: {pct}٪"
        )
        if reason:
            text += f"\nدلیل خروج: {reason}"
    else:
        icon = "✅" if hit_type == "TP1" else "❌"
        result_fa = "حد سود 1" if hit_type == "TP1" else "حد ضرر"
        text = (
            f"{icon} نتیجه سیگنال {signal.get('symbol')}\n"
            f"جهت: {fa_direction(signal.get('direction'))}\n"
            f"ورود: {signal.get('entry')}\n"
            f"قیمت خروج: {exit_price}\n"
            f"نتیجه: {result_fa}\n"
            f"درصد حرکت: {pct}٪"
        )

    if isinstance(real_trade_accounting, dict) and real_trade_accounting.get("ok"):
        acc = real_trade_accounting.get("accounting") or {}
        pnl = real_trade_accounting.get("pnl_usd", 0)
        sign = "+" if float(pnl or 0) > 0 else ""
        source = real_trade_accounting.get("pnl_source")
        pnl_label = "PnL واقعی توبیت" if source == "TOOBIT_REAL" else "PnL تقریبی"
        text += (
            f"\n\n{pnl_label}: {sign}{pnl}$"
            f"\nمارجین: {real_trade_accounting.get('margin')}$"
            f"\nلوریج: {real_trade_accounting.get('leverage')}x"
            f"\nحجم تقریبی: {real_trade_accounting.get('notional')}$"
            f"\nبالانس داخلی: {acc.get('balance')}$"
            f"\nسرمایه محافظت‌شده: {acc.get('protected_balance')}$"
        )
        if source != "TOOBIT_REAL" and real_trade_accounting.get("pnl_error"):
            text += "\n⚠️ سود واقعی از توبیت بعد از چند تلاش خوانده نشد؛ عدد بالا تخمینی است."
        if acc.get("daily_locked"):
            text += "\n🚨 قفل ضرر روزانه فعال شد."
    elif isinstance(real_trade_accounting, dict) and real_trade_accounting.get("error"):
        text += f"\n\n⚠️ ثبت PnL واقعی انجام نشد: {real_trade_accounting.get('error')}"
    return text
def check_active_signals() -> List[Dict[str, Any]]:
    active = get_active_signals()
    remaining: List[Dict[str, Any]] = []
    messages: List[Dict[str, Any]] = []

    for signal in active:
        if signal.get("status") != "ACTIVE":
            continue
        try:
            hit_type = None
            exit_price = None
            dynamic_decision: Optional[Dict[str, Any]] = None
            real_trade_accounting: Optional[Dict[str, Any]] = None
            dynamic_close_verified = False

            # 0) Pending Dynamic Exit retry state.
            # If a close was already attempted, do NOT rescan/re-alert every loop.
            # Retry only after a cooldown so Toobit is not spammed with close orders.
            if bool(signal.get("dynamic_exit_pending")):
                if not _should_retry_dynamic_close(signal):
                    signal["last_checked_at"] = now_ts()
                    remaining.append(signal)
                    continue

                exit_price = float(signal.get("dynamic_exit_price") or signal.get("price") or signal.get("entry") or 0)
                dynamic_decision = signal.get("dynamic_exit_decision") if isinstance(signal.get("dynamic_exit_decision"), dict) else {"reason": _dynamic_pending_reason(signal)}

                close_result = _execute_tracker_dynamic_real_close(signal, exit_price, dynamic_decision or {})
                if isinstance(close_result, dict) and close_result.get("ok"):
                    hit_type = DYNAMIC_PROFIT_EVENT
                    dynamic_close_verified = True
                    pct_preview = move_percent(signal, exit_price)
                    real_trade_accounting = _normalize_dynamic_close_accounting(signal, close_result, pct_preview)
                    _clear_dynamic_close_pending(signal)
                else:
                    _set_dynamic_close_pending(signal, exit_price, dynamic_decision, close_result)
                    attempts = int(signal.get("dynamic_exit_attempts", 0) or 0)
                    # After several fast retries, slow down. Send only one final warning.
                    if attempts >= DYNAMIC_CLOSE_MAX_ATTEMPTS:
                        signal["next_dynamic_exit_retry_at"] = now_ts() + max(30, int(DYNAMIC_CLOSE_LONG_RETRY_SECONDS))
                        if not bool(signal.get("dynamic_exit_gave_up_notified")):
                            signal["dynamic_exit_gave_up_notified"] = True
                            messages.append({
                                "chat_id": signal.get("chat_id"),
                                "message": _build_dynamic_giveup_message(signal),
                                "reply_to_message_id": signal.get("message_id") or signal.get("reply_to_message_id"),
                            })
                    elif not bool(signal.get("dynamic_exit_notified")):
                        signal["dynamic_exit_notified"] = True
                        messages.append({
                            "chat_id": signal.get("chat_id"),
                            "message": _build_dynamic_pending_message(signal),
                            "reply_to_message_id": signal.get("message_id") or signal.get("reply_to_message_id"),
                        })
                    signal["last_checked_at"] = now_ts()
                    remaining.append(signal)
                    continue

            # 1) Special update first: let real_trade_manager close profitable
            # REAL positions immediately when continuation quality fails.
            # This never touches losing/breakeven positions by design.
            real_dynamic = _check_real_dynamic_profit_exit(signal)
            if real_dynamic and real_dynamic.get("exit"):
                hit_type = DYNAMIC_PROFIT_EVENT
                exit_price = float(real_dynamic.get("exit_price") or 0)
                dynamic_decision = real_dynamic
                dynamic_close_verified = True
                # real_trade_manager has already closed and recorded accounting.
                pct_preview = move_percent(signal, exit_price)
                real_trade_accounting = _normalize_dynamic_close_accounting(
                    signal,
                    real_dynamic.get("close_result"),
                    pct_preview,
                )

            candles = get_recent_1m_candles_since(signal["symbol"], signal.get("last_checked_at") or signal.get("created_at"))
            recent_seen: List[List[Any]] = []

            # 2) If REAL dynamic exit did not already close it, monitor candle path.
            # TP/SL logic is preserved. Tracker-side Dynamic Exit only acts while
            # the signal is in profit and is only final after real close succeeds.
            if not hit_type:
                for candle in candles:
                    recent_seen.append(candle)
                    _update_signal_mfe_mae(signal, candle)

                    path_hit, path_exit = candle_path_hit(signal, candle)
                    if path_hit:
                        hit_type, exit_price = path_hit, path_exit
                        break

                    decision = _tracker_dynamic_profit_decision(signal, candle, recent_seen)
                    if decision.get("exit"):
                        hit_type = DYNAMIC_PROFIT_EVENT
                        exit_price = float(decision.get("exit_price") or _candle_close(candle))
                        dynamic_decision = decision
                        break

            signal["last_checked_at"] = now_ts()
            if hit_type:
                pct = move_percent(signal, exit_price)

                # Final safety: Dynamic Profit must never close a non-profitable trade.
                if hit_type == DYNAMIC_PROFIT_EVENT and pct <= 0:
                    signal["last_dynamic_profit_skip"] = "NOT_IN_PROFIT_AFTER_RECHECK"
                    remaining.append(signal)
                    continue

                if hit_type == DYNAMIC_PROFIT_EVENT:
                    # If real_trade_manager did not already close it, the tracker
                    # must close through real_trade_manager now. Do not record stats,
                    # learning, slot close, or Telegram result unless exchange close
                    # is confirmed ok=True.
                    if not dynamic_close_verified:
                        close_result = _execute_tracker_dynamic_real_close(signal, exit_price, dynamic_decision or {})
                        if not isinstance(close_result, dict) or not close_result.get("ok"):
                            _set_dynamic_close_pending(signal, exit_price, dynamic_decision, close_result)
                            # Only one pending notification. Next message is only when closed,
                            # or after max attempts as a final warning.
                            if not bool(signal.get("dynamic_exit_notified")):
                                signal["dynamic_exit_notified"] = True
                                messages.append({
                                    "chat_id": signal.get("chat_id"),
                                    "message": _build_dynamic_pending_message(signal),
                                    "reply_to_message_id": signal.get("message_id") or signal.get("reply_to_message_id"),
                                })
                            remaining.append(signal)
                            continue
                        dynamic_close_verified = True
                        real_trade_accounting = _normalize_dynamic_close_accounting(signal, close_result, pct)

                    # Dynamic learning is recorded by real_trade_manager for the
                    # manager-side close. For tracker-side close, record once here.
                    if not (isinstance(dynamic_decision, dict) and dynamic_decision.get("source") == "REAL_TRADE_MANAGER"):
                        _record_dynamic_learning(signal, exit_price, pct, dynamic_decision)

                if hit_type != DYNAMIC_PROFIT_EVENT:
                    real_trade_accounting = record_real_trade_result_for_signal(signal, hit_type, exit_price, pct)
                    if isinstance(real_trade_accounting, dict) and real_trade_accounting.get("keep_open"):
                        signal["last_toobit_wait_reason"] = str(real_trade_accounting.get("error") or "waiting for Toobit close")[:250]
                        signal["last_checked_at"] = now_ts()
                        remaining.append(signal)
                        continue

                record_stat_event(signal, hit_type, exit_price, pct)
                ai_record_result(signal, hit_type, exit_price, pct)

                ai_close_slot(signal)

                reason = ""
                if isinstance(dynamic_decision, dict):
                    reason = str(dynamic_decision.get("reason") or "")
                text = _build_result_message(signal, hit_type, exit_price, pct, real_trade_accounting, reason=reason)
                messages.append({
                    "chat_id": signal.get("chat_id"),
                    "message": text,
                    "reply_to_message_id": signal.get("message_id") or signal.get("reply_to_message_id"),
                })
            else:
                remaining.append(signal)
        except Exception as e:
            signal["last_checked_at"] = now_ts()
            signal["last_error"] = str(e)[:250]
            remaining.append(signal)

    save_active_signals(remaining)
    return messages

def parse_days_from_text(text: str) -> int:
    m = re.search(r"(\d+)", text or "")
    if m:
        return int(m.group(1))
    if text and "کل" in text:
        return 3650
    return 7


parse_days_from_report_text = parse_days_from_text


def parse_profit_calc_text(text: str):
    if not text:
        return None
    nums = re.findall(r"\d+(?:\.\d+)?", text)
    if ("سود" in text or "محاسبه" in text or "درآمد" in text) and len(nums) >= 2:
        return float(nums[0]), float(nums[1])
    return None


def get_profit_for_signal_text(reply_text: str, margin: float, leverage: float):
    return None


def get_profit_simulation_report(margin: float, leverage: float, days: int = 7) -> str:
    stats = get_signal_stats()
    since = now_ts() - int(days) * 86400
    closed = [s for s in stats if int(s.get("event_at", 0) or 0) >= since and s.get("event_type") in ["TP1", "SL"]]
    wins = len([s for s in closed if s.get("event_type") == "TP1"])
    losses = len([s for s in closed if s.get("event_type") == "SL"])
    return f"📊 شبیه‌سازی سود {days} روز\nTP1: {wins}\nSL: {losses}\nمارجین: {margin}$\nلوریج: {leverage}x"


def _event_ts(item: Dict[str, Any]) -> int:
    try:
        return int(item.get("event_at", item.get("created_at", 0)) or 0)
    except Exception:
        return 0


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except Exception:
        return int(default)


def _format_days_label(days: int) -> str:
    try:
        d = int(days)
    except Exception:
        d = 7
    return "کل" if d >= 3650 else f"{d} روز اخیر"


def _infer_sl_reason(item: Dict[str, Any]) -> str:
    reasons = item.get("reasons") or []
    reason_text = " ".join([str(x) for x in reasons])
    if "EMA20" in reason_text and "زیاد" in reason_text:
        return "ورود دیر / فاصله زیاد از EMA20"
    if "حجم" in reason_text and "ضعیف" in reason_text:
        return "حجم ضعیف هنگام سیگنال"
    risk = str(item.get("risk_level") or "").upper()
    if risk in ["HIGH", "ریسک بالا", "بالا"]:
        return "ریسک سیگنال بالا بوده"
    adx = _safe_float(item.get("adx"), 0)
    if 0 < adx < 25:
        return "ADX پایین / قدرت روند کم"
    confirmations = _safe_int(item.get("confirmations"), 0)
    if confirmations and confirmations < 5:
        return "تاییدیه‌های کم"
    if str(item.get("freshness") or "").upper() == "LOW":
        return "تازگی حرکت ضعیف"
    return "نامشخص / نیاز به داده بیشتر"


def get_stats_report(days: int = 7) -> str:
    stats = get_signal_stats()
    since = now_ts() - int(days) * 86400
    data = [s for s in stats if _event_ts(s) >= since]
    created = [s for s in data if s.get("event_type") == "SIGNAL_CREATED"]
    tp1 = [s for s in data if s.get("event_type") == "TP1"]
    dynamic = [s for s in data if s.get("event_type") == DYNAMIC_PROFIT_EVENT]
    sl = [s for s in data if s.get("event_type") == "SL"]
    total = len(tp1) + len(dynamic) + len(sl)
    wins = len(tp1) + len(dynamic)
    win_rate = round((wins / total) * 100, 1) if total else 0
    active_count = len(get_active_signals())
    closed_rows = tp1 + dynamic + sl
    longs = [s for s in closed_rows if s.get("direction") == "LONG"]
    shorts = [s for s in closed_rows if s.get("direction") == "SHORT"]
    long_tp = len([s for s in longs if _is_profit_event(str(s.get("event_type")))])
    short_tp = len([s for s in shorts if _is_profit_event(str(s.get("event_type")))])
    return (
        f"📊 آمار {days} روز اخیر\n\n"
        f"سیگنال مستقیم صادرشده: {len(created)}\n"
        f"معاملات فعال باز: {active_count}\n"
        f"--------------------\n"
        f"TP1: {len(tp1)}\n"
        f"SL: {len(sl)}\n"
        f"Win Rate: {win_rate}%\n"
        f"--------------------\n"
        f"لانگ: {len(longs)} | TP1: {long_tp}\n"
        f"شورت: {len(shorts)} | TP1: {short_tp}\n"
        f"\nمعماری: AI_CLASSIC_DIRECT + AI_LEARNING"
    )


def format_signal_stats(days: int = 7) -> str:
    return get_stats_report(days=days)


def get_symbol_stats_report(days: int = 3650, mode: str = "all") -> str:
    try:
        days = int(days)
    except Exception:
        days = 3650
    since = 0 if days >= 3650 else now_ts() - days * 86400
    stats = get_signal_stats()
    data = [s for s in stats if _event_ts(s) >= since]
    created = [s for s in data if s.get("event_type") == "SIGNAL_CREATED"]
    closed = [s for s in data if _is_closed_event(str(s.get("event_type"))) and s.get("symbol")]

    if not closed:
        return f"📊 آمار ارزها ({_format_days_label(days)})\n\nهنوز نتیجه TP1/SL ثبت نشده است."

    by_symbol: Dict[str, Dict[str, Any]] = {}
    sl_reason_counts: Dict[str, int] = {}

    for item in closed:
        symbol = str(item.get("symbol") or "UNKNOWN")
        row = by_symbol.setdefault(symbol, {"symbol": symbol, "tp1": 0, "dynamic": 0, "sl": 0, "total": 0, "move_sum": 0.0, "long": 0, "short": 0})
        event = item.get("event_type")
        row["total"] += 1
        if event == "TP1":
            row["tp1"] += 1
        elif event == DYNAMIC_PROFIT_EVENT:
            row["dynamic"] += 1
        elif event == "SL":
            row["sl"] += 1
            reason = _infer_sl_reason(item)
            sl_reason_counts[reason] = sl_reason_counts.get(reason, 0) + 1
        if item.get("direction") == "LONG":
            row["long"] += 1
        elif item.get("direction") == "SHORT":
            row["short"] += 1
        row["move_sum"] += _safe_float(item.get("move_percent"), 0)

    rows: List[Dict[str, Any]] = []
    for row in by_symbol.values():
        row["win_rate"] = round(((row["tp1"] + row.get("dynamic", 0)) / row["total"]) * 100, 1) if row["total"] else 0
        row["move_sum"] = round(row["move_sum"], 4)
        rows.append(row)

    rows_by_best = sorted(rows, key=lambda x: (x["win_rate"], x["move_sum"], x["total"]), reverse=True)
    rows_by_worst = sorted(rows, key=lambda x: (x["win_rate"], x["move_sum"], -x["total"]))
    rows_all = sorted(rows, key=lambda x: (x["total"], x["win_rate"], x["move_sum"]), reverse=True)

    if mode == "best":
        selected = rows_by_best[:15]
        title = f"🏆 بهترین ارزها ({_format_days_label(days)})"
    elif mode == "worst":
        selected = rows_by_worst[:15]
        title = f"⚠️ ضعیف‌ترین ارزها ({_format_days_label(days)})"
    else:
        selected = rows_all
        title = f"📊 آمار کلی ارزها ({_format_days_label(days)})"

    total_tp1 = len([x for x in closed if x.get("event_type") == "TP1"])
    total_dynamic = len([x for x in closed if x.get("event_type") == DYNAMIC_PROFIT_EVENT])
    total_sl = len([x for x in closed if x.get("event_type") == "SL"])
    total_closed = total_tp1 + total_dynamic + total_sl
    total_wr = round(((total_tp1 + total_dynamic) / total_closed) * 100, 1) if total_closed else 0
    total_move = round(sum(_safe_float(x.get("move_percent"), 0) for x in closed), 4)

    lines = [
        title,
        "",
        f"سیگنال‌های ثبت‌شده: {len(created)}",
        f"نتایج بسته‌شده: {total_closed} | TP1: {total_tp1} | خروج هوشمند: {total_dynamic} | SL: {total_sl}",
        f"Win Rate کلی: {total_wr}%",
        f"جمع درصد حرکت: {total_move}%",
        "--------------------",
    ]

    max_rows = 35 if mode == "all" else 15
    for i, row in enumerate(selected[:max_rows], start=1):
        sign = "+" if row["move_sum"] > 0 else ""
        lines.append(
            f"{i}) {row['symbol']} | معاملات: {row['total']} | TP1: {row['tp1']} | خروج هوشمند: {row.get('dynamic', 0)} | SL: {row['sl']} | "
            f"WR: {row['win_rate']}% | حرکت: {sign}{row['move_sum']}% | L/S: {row['long']}/{row['short']}"
        )

    if mode == "all":
        lines.append("--------------------")
        lines.append("🏆 بهترین‌ها:")
        for row in rows_by_best[:5]:
            sign = "+" if row["move_sum"] > 0 else ""
            lines.append(f"{row['symbol']} → WR {row['win_rate']}% | {sign}{row['move_sum']}%")
        lines.append("⚠️ ضعیف‌ترین‌ها:")
        for row in rows_by_worst[:5]:
            sign = "+" if row["move_sum"] > 0 else ""
            lines.append(f"{row['symbol']} → WR {row['win_rate']}% | {sign}{row['move_sum']}%")
        if sl_reason_counts:
            lines.append("--------------------")
            lines.append("❌ علت‌های احتمالی SL:")
            for reason, count in sorted(sl_reason_counts.items(), key=lambda x: x[1], reverse=True)[:6]:
                lines.append(f"{reason}: {count}")

    text = "\n".join(lines)
    if len(text) > 3900:
        trimmed: List[str] = []
        total_len = 0
        for line in lines:
            if total_len + len(line) + 1 > 3800:
                break
            trimmed.append(line)
            total_len += len(line) + 1
        trimmed.append("\nگزارش طولانی بود؛ بخشی از ارزهای کم‌تعداد حذف شد.")
        text = "\n".join(trimmed)
    return text


def format_active_signals() -> str:
    active = get_active_signals()
    if not active:
        return "سیگنال فعالی وجود ندارد."
    lines = ["📌 سیگنال‌های فعال:"]
    for item in active[:30]:
        lines.append(
            f"\n{item.get('symbol')} | {fa_direction(item.get('direction'))}\n"
            f"Entry: {item.get('entry')}\nTP1: {item.get('tp1')}\nSL: {item.get('stop_loss')}"
        )
    return "\n".join(lines)
