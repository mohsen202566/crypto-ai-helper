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
    )
except Exception:
    load_real_trade_state = None
    save_real_trade_state = None
    record_realized_pnl = None
    get_real_pnl_for_closed_position = None

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
        "entry_mode",
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


def _fetch_real_closed_pnl_from_exchange(signal: Dict[str, Any], pos: Optional[Dict[str, Any]], hit_type: str, exit_price: float) -> Dict[str, Any]:
    """Read REAL closed-position PnL from Toobit when available.

    This function is fail-safe. It never guesses as 'real'. If Toobit/manager
    cannot return a confirmed closed PnL, caller can fall back to approximate
    calculation and label it as approximate.
    """
    symbol = str((pos or {}).get("symbol") or signal.get("symbol") or "").upper()
    direction = str((pos or {}).get("direction") or signal.get("direction") or "").upper()
    signal_id = str(signal.get("signal_id") or signal.get("id") or (pos or {}).get("signal_id") or "")
    opened_at = int((pos or {}).get("opened_at") or signal.get("created_at") or 0)
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
                return {"ok": True, "pnl_usd": float(res.get("pnl_usd")), "source": res.get("source") or "TOOBIT", "raw": res}
        except Exception as e:
            last_error = str(e)[:250]
        else:
            last_error = "manager returned no confirmed pnl"
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
            method = getattr(toobit_client, name, None)
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
                        return {"ok": True, "pnl_usd": float(pnl), "source": f"TOOBIT:{name}", "raw": res}
                except TypeError:
                    continue
                except Exception as e:
                    last_error = str(e)[:250]
                    break

    return {"ok": False, "error": last_error or "real closed pnl not available"}

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

        exchange_pnl = _fetch_real_closed_pnl_from_exchange(signal, pos, hit_type, exit_price)
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
            candles = get_recent_1m_candles_since(signal["symbol"], signal.get("last_checked_at") or signal.get("created_at"))
            for candle in candles:
                hit_type, exit_price = candle_path_hit(signal, candle)
                if hit_type:
                    break

            signal["last_checked_at"] = now_ts()
            if hit_type:
                pct = move_percent(signal, exit_price)
                record_stat_event(signal, hit_type, exit_price, pct)
                ai_record_result(signal, hit_type, exit_price, pct)
                real_trade_accounting = record_real_trade_result_for_signal(signal, hit_type, exit_price, pct)
                ai_close_slot(signal)
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
                    if source == "TOOBIT_REAL":
                        pnl_label = "PnL واقعی توبیت"
                    else:
                        pnl_label = "PnL تقریبی"
                    text += (
                        f"\n\n{pnl_label}: {sign}{pnl}$"
                        f"\nمارجین: {real_trade_accounting.get('margin')}$"
                        f"\nلوریج: {real_trade_accounting.get('leverage')}x"
                        f"\nحجم تقریبی: {real_trade_accounting.get('notional')}$"
                        f"\nبالانس داخلی: {acc.get('balance')}$"
                        f"\nسرمایه محافظت‌شده: {acc.get('protected_balance')}$"
                    )
                    if source != "TOOBIT_REAL" and real_trade_accounting.get("pnl_error"):
                        text += f"\n⚠️ سود واقعی از توبیت خوانده نشد؛ عدد بالا تخمینی است."
                    if acc.get("daily_locked"):
                        text += "\n🚨 قفل ضرر روزانه فعال شد."
                elif isinstance(real_trade_accounting, dict) and real_trade_accounting.get("error"):
                    text += f"\n\n⚠️ ثبت PnL واقعی انجام نشد: {real_trade_accounting.get('error')}"
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
    sl = [s for s in data if s.get("event_type") == "SL"]
    total = len(tp1) + len(sl)
    win_rate = round((len(tp1) / total) * 100, 1) if total else 0
    active_count = len(get_active_signals())
    longs = [s for s in tp1 + sl if s.get("direction") == "LONG"]
    shorts = [s for s in tp1 + sl if s.get("direction") == "SHORT"]
    long_tp = len([s for s in longs if s.get("event_type") == "TP1"])
    short_tp = len([s for s in shorts if s.get("event_type") == "TP1"])
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
    closed = [s for s in data if s.get("event_type") in ["TP1", "SL"] and s.get("symbol")]

    if not closed:
        return f"📊 آمار ارزها ({_format_days_label(days)})\n\nهنوز نتیجه TP1/SL ثبت نشده است."

    by_symbol: Dict[str, Dict[str, Any]] = {}
    sl_reason_counts: Dict[str, int] = {}

    for item in closed:
        symbol = str(item.get("symbol") or "UNKNOWN")
        row = by_symbol.setdefault(symbol, {"symbol": symbol, "tp1": 0, "sl": 0, "total": 0, "move_sum": 0.0, "long": 0, "short": 0})
        event = item.get("event_type")
        row["total"] += 1
        if event == "TP1":
            row["tp1"] += 1
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
        row["win_rate"] = round((row["tp1"] / row["total"]) * 100, 1) if row["total"] else 0
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
    total_sl = len([x for x in closed if x.get("event_type") == "SL"])
    total_closed = total_tp1 + total_sl
    total_wr = round((total_tp1 / total_closed) * 100, 1) if total_closed else 0
    total_move = round(sum(_safe_float(x.get("move_percent"), 0) for x in closed), 4)

    lines = [
        title,
        "",
        f"سیگنال‌های ثبت‌شده: {len(created)}",
        f"نتایج بسته‌شده: {total_closed} | TP1: {total_tp1} | SL: {total_sl}",
        f"Win Rate کلی: {total_wr}%",
        f"جمع درصد حرکت: {total_move}%",
        "--------------------",
    ]

    max_rows = 35 if mode == "all" else 15
    for i, row in enumerate(selected[:max_rows], start=1):
        sign = "+" if row["move_sum"] > 0 else ""
        lines.append(
            f"{i}) {row['symbol']} | معاملات: {row['total']} | TP1: {row['tp1']} | SL: {row['sl']} | "
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
