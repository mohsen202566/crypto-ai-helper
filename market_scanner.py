# -*- coding: utf-8 -*-
"""
market_scanner.py

Market breadth / movement-context helper for the crypto futures bot.

Architecture note:
- This file is NOT a signal engine.
- It does NOT open trades, block trades, or approve entries.
- It only gives the AI Movement Hunter a soft market-context snapshot:
    market breadth, fresh pump/dump pressure, range/compression pressure,
    exhaustion/range-after-move pressure, and a short Persian status report.

Compatibility:
- Keeps old public functions:
    get_market_breadth
    get_market_status_text
- Keeps/extends:
    get_market_breadth_profile

Real trading and Telegram commands are handled elsewhere and are preserved.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

try:
    from analysis import get_klines, add_indicators, trend_direction  # type: ignore
except Exception:
    get_klines = None
    add_indicators = None
    trend_direction = None

try:
    from coins_fa import COINS_FA  # type: ignore
except Exception:
    COINS_FA = {}

MARKET_STATUS_CACHE = {
    "time": 0,
    "text": None,
    "data": None,
}

CACHE_SECONDS = 300
MAX_MARKET_SCAN_SYMBOLS = 100
MIN_CHECKED_FOR_STRONG_BIAS = 8


# ---------------------------------------------------------------------------
# Safe helpers
# ---------------------------------------------------------------------------
def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except Exception:
        return int(default)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return float(default)
        v = float(value)
        if v != v:  # NaN
            return float(default)
        return v
    except Exception:
        return float(default)


def _safe_pct(part: int, total: int) -> int:
    if total <= 0:
        return 0
    return int(round((part / total) * 100))


def _pct_distance(a: Any, b: Any) -> float:
    av = _safe_float(a)
    bv = _safe_float(b)
    return ((av - bv) / max(abs(bv), 1e-12)) * 100.0


def _slope(values: Any, periods: int = 3) -> float:
    try:
        s = list(values.dropna().tail(max(2, periods + 1)))
        if len(s) < 2:
            return 0.0
        return _safe_float(s[-1]) - _safe_float(s[0])
    except Exception:
        return 0.0


def _unique_symbols() -> List[str]:
    values: List[Any] = []
    try:
        if isinstance(COINS_FA, dict):
            values = list(COINS_FA.values())
        elif isinstance(COINS_FA, (list, tuple, set)):
            values = list(COINS_FA)
    except Exception:
        values = []

    out: List[str] = []
    seen = set()
    for item in values:
        sym = str(item or "").upper().replace("/", "").replace("-", "").strip()
        if not sym or not sym.endswith("USDT"):
            continue
        if sym not in seen:
            seen.add(sym)
            out.append(sym)
    return sorted(out)[:MAX_MARKET_SCAN_SYMBOLS]


def _normalize_trend(value: Any) -> str:
    t = str(value or "").lower().strip()
    if t in {"bullish", "strong_bullish", "weak_bullish", "uptrend", "long"}:
        return "bullish" if t != "weak_bullish" else "weak_bullish"
    if t in {"bearish", "strong_bearish", "weak_bearish", "downtrend", "short"}:
        return "bearish" if t != "weak_bearish" else "weak_bearish"
    return "range"


def _market_label(trend: str) -> str:
    if trend in ["bullish", "weak_bullish"]:
        return "صعودی"
    if trend in ["bearish", "weak_bearish"]:
        return "نزولی"
    return "رنج"


# ---------------------------------------------------------------------------
# Movement Hunter market sensors
# ---------------------------------------------------------------------------
def _buy_sell_power(df: Any, candles: int = 3) -> Dict[str, float]:
    try:
        r = df.tail(max(1, candles))
        green = _safe_float(r[r["close"] > r["open"]]["volume"].sum())
        red = _safe_float(r[r["close"] < r["open"]]["volume"].sum())
        total = green + red
        if total <= 0:
            return {"buy": 50.0, "sell": 50.0}
        return {"buy": round(green / total * 100.0, 1), "sell": round(red / total * 100.0, 1)}
    except Exception:
        return {"buy": 50.0, "sell": 50.0}


def _compression_pack(df: Any, lookback: int = 24) -> Dict[str, Any]:
    try:
        recent = df.tail(max(lookback, 12))
        last = recent.iloc[-1]
        atr_now = _safe_float(last.get("atr"), 0.0)
        atr_avg = _safe_float(recent["atr"].tail(lookback).mean(), atr_now)
        rng_now = _safe_float(last["high"]) - _safe_float(last["low"])
        rng_avg = _safe_float((recent["high"] - recent["low"]).tail(lookback).mean(), rng_now)
        volume_ratio = _safe_float(last.get("volume_ratio"), 1.0)
        compression = atr_now <= atr_avg * 0.84 or rng_now <= rng_avg * 0.76
        expansion = rng_now >= rng_avg * 1.22 and volume_ratio >= 1.12
        return {
            "compression": bool(compression),
            "expansion": bool(expansion),
            "range_to_avg": round(rng_now / max(rng_avg, 1e-12), 3),
            "atr_to_avg": round(atr_now / max(atr_avg, 1e-12), 3),
            "volume_ratio": round(volume_ratio, 3),
        }
    except Exception:
        return {"compression": False, "expansion": False, "range_to_avg": 1.0, "atr_to_avg": 1.0, "volume_ratio": 1.0}


def _movement_profile_symbol(symbol: str) -> Optional[Dict[str, Any]]:
    """Return soft movement context for one symbol. Never raises.

    This is a sensor/profile only. It does not issue signals. It is designed so
    AI can detect pre-move conditions across the market: compression, early
    expansion, power shift, momentum turn, and exhaustion/range-after-move.
    """
    if not get_klines or not add_indicators:
        return None

    try:
        df_30m = add_indicators(get_klines(symbol, "30m"))
        df_15m = add_indicators(get_klines(symbol, "15m"))
        df_5m = add_indicators(get_klines(symbol, "5m", include_current=True))

        if df_30m is None or df_15m is None or df_5m is None:
            return None
        if len(df_30m) < 40 or len(df_15m) < 40 or len(df_5m) < 40:
            return None

        l30 = df_30m.iloc[-1]
        l15 = df_15m.iloc[-1]
        l5 = df_5m.iloc[-1]
        p5 = df_5m.iloc[-2]
        price = _safe_float(l5["close"])
        atr5 = max(_safe_float(l5.get("atr"), 0.0), price * 0.0015, 1e-12)

        trend_30m = _normalize_trend(trend_direction(df_30m) if trend_direction else "range")
        trend_15m = _normalize_trend(trend_direction(df_15m) if trend_direction else "range")

        rsi_slope_5m = _slope(df_5m["rsi"], 3)
        hist_slope_5m = _slope(df_5m["macd_hist"], 3)
        adx_slope_5m = _slope(df_5m["adx"], 3)
        power3 = _buy_sell_power(df_5m, 3)
        power6 = _buy_sell_power(df_5m, 6)
        comp5 = _compression_pack(df_5m, 24)
        comp15 = _compression_pack(df_15m, 24)

        # Fresh movement pressure: prefer start/early signals, not late trends.
        long_pressure = 0
        short_pressure = 0
        if rsi_slope_5m > 0:
            long_pressure += 1
        elif rsi_slope_5m < 0:
            short_pressure += 1
        if hist_slope_5m > 0:
            long_pressure += 1
        elif hist_slope_5m < 0:
            short_pressure += 1
        if _safe_float(l5.get("macd_hist"), 0.0) > _safe_float(p5.get("macd_hist"), 0.0):
            long_pressure += 1
        else:
            short_pressure += 1
        if price > _safe_float(l5.get("vwap"), price):
            long_pressure += 1
        elif price < _safe_float(l5.get("vwap"), price):
            short_pressure += 1
        if power3["buy"] >= 58 or power6["buy"] >= 57:
            long_pressure += 1
        if power3["sell"] >= 58 or power6["sell"] >= 57:
            short_pressure += 1
        if comp5.get("expansion") or comp15.get("expansion"):
            if long_pressure >= short_pressure:
                long_pressure += 1
            else:
                short_pressure += 1

        recent = df_5m.tail(10)
        hi = _safe_float(recent["high"].max())
        lo = _safe_float(recent["low"].min())
        move_atr = abs(hi - lo) / atr5
        dist_from_high_atr = (hi - price) / atr5
        dist_from_low_atr = (price - lo) / atr5
        distance_ema20_atr = abs(price - _safe_float(l5.get("ema20"), price)) / atr5

        # Exhaustion/range-after-move: market already moved hard and is no
        # longer at the first/early stage. This is a warning for AI only.
        exhaustion = False
        range_after_move = False
        if move_atr >= 3.0 and min(dist_from_high_atr, dist_from_low_atr) <= 0.55:
            exhaustion = True
        if move_atr >= 2.4 and distance_ema20_atr <= 0.55 and not comp5.get("expansion"):
            range_after_move = True

        if long_pressure > short_pressure:
            movement_bias = "pump_pressure"
        elif short_pressure > long_pressure:
            movement_bias = "dump_pressure"
        else:
            movement_bias = "neutral"

        if comp5.get("compression") or comp15.get("compression"):
            setup_phase = "PRE_MOVE_COMPRESSION"
        elif comp5.get("expansion") or comp15.get("expansion"):
            setup_phase = "EARLY_EXPANSION"
        elif exhaustion:
            setup_phase = "EXHAUSTION"
        elif range_after_move:
            setup_phase = "RANGE_AFTER_MOVE"
        else:
            setup_phase = "NORMAL"

        return {
            "symbol": symbol,
            "trend_30m": trend_30m,
            "trend_15m": trend_15m,
            "status": "bullish" if trend_30m in ["bullish", "weak_bullish"] and trend_15m in ["bullish", "weak_bullish"] else "bearish" if trend_30m in ["bearish", "weak_bearish"] and trend_15m in ["bearish", "weak_bearish"] else "range",
            "movement_bias": movement_bias,
            "setup_phase": setup_phase,
            "long_pressure": int(long_pressure),
            "short_pressure": int(short_pressure),
            "rsi_slope_5m": round(rsi_slope_5m, 4),
            "macd_hist_slope_5m": round(hist_slope_5m, 8),
            "adx_slope_5m": round(adx_slope_5m, 4),
            "power3_buy": power3["buy"],
            "power3_sell": power3["sell"],
            "power6_buy": power6["buy"],
            "power6_sell": power6["sell"],
            "compression_5m": comp5,
            "compression_15m": comp15,
            "move_atr_5m_10bar": round(move_atr, 3),
            "distance_ema20_atr_5m": round(distance_ema20_atr, 3),
            "exhaustion_risk": bool(exhaustion),
            "range_after_move": bool(range_after_move),
            "vwap_side": "above" if price > _safe_float(l5.get("vwap"), price) else "below" if price < _safe_float(l5.get("vwap"), price) else "near",
            "price_change_5m_6bar_pct": round(_pct_distance(df_5m.iloc[-1]["close"], df_5m.tail(6).iloc[0]["close"]), 4),
            "price_change_15m_4bar_pct": round(_pct_distance(df_15m.iloc[-1]["close"], df_15m.tail(4).iloc[0]["close"]), 4),
        }
    except Exception:
        return None


def _analyze_symbol_market(symbol: str) -> Optional[str]:
    """Return bullish/bearish/range for old breadth compatibility."""
    profile = _movement_profile_symbol(symbol)
    if not profile:
        return None
    return str(profile.get("status") or "range")


# ---------------------------------------------------------------------------
# Breadth/profile aggregation
# ---------------------------------------------------------------------------
def _empty_breadth() -> Dict[str, Any]:
    return {
        "checked": 0,
        "bullish": 0,
        "bearish": 0,
        "range": 0,
        "bullish_pct": 0,
        "bearish_pct": 0,
        "range_pct": 0,
        "bias": "unknown",
        "bias_text": "نامشخص",
        "power": "ضعیف",
        "confidence": "LOW",
        "soft_score_long": 0,
        "soft_score_short": 0,
        "pump_pressure": 0,
        "dump_pressure": 0,
        "pump_pressure_pct": 0,
        "dump_pressure_pct": 0,
        "pre_move_compression": 0,
        "early_expansion": 0,
        "exhaustion": 0,
        "range_after_move": 0,
        "movement_bias": "unknown",
        "movement_bias_text": "نامشخص",
        "movement_confidence": "LOW",
        "top_movement_symbols": [],
        "is_soft_layer": True,
    }


def _classify_breadth(
    bullish: int,
    bearish: int,
    ranging: int,
    checked: int,
    pump_pressure: int = 0,
    dump_pressure: int = 0,
    pre_move_compression: int = 0,
    early_expansion: int = 0,
    exhaustion: int = 0,
    range_after_move: int = 0,
    top_symbols: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    bullish_pct = _safe_pct(bullish, checked)
    bearish_pct = _safe_pct(bearish, checked)
    range_pct = _safe_pct(ranging, checked)
    pump_pct = _safe_pct(pump_pressure, checked)
    dump_pct = _safe_pct(dump_pressure, checked)

    confidence = "LOW"
    if checked >= 30:
        confidence = "HIGH"
    elif checked >= MIN_CHECKED_FOR_STRONG_BIAS:
        confidence = "MEDIUM"

    bias = "neutral"
    bias_text = "خنثی"
    power = "ضعیف"
    soft_long = 0
    soft_short = 0

    # Old market-breadth output remains soft context only.
    if checked < MIN_CHECKED_FOR_STRONG_BIAS:
        bias = "unknown"
        bias_text = "نامشخص"
        power = "ضعیف"
    elif bullish_pct >= 65:
        bias = "bullish"
        bias_text = "صعودی"
        power = "قوی" if bullish_pct >= 75 else "متوسط"
        soft_long = 3 if bullish_pct >= 75 else 2
    elif bearish_pct >= 65:
        bias = "bearish"
        bias_text = "نزولی"
        power = "قوی" if bearish_pct >= 75 else "متوسط"
        soft_short = 3 if bearish_pct >= 75 else 2
    elif range_pct >= 55:
        bias = "range"
        bias_text = "رنج"
        power = "ضعیف"
    elif bullish_pct > bearish_pct:
        bias = "weak_bullish"
        bias_text = "رنج متمایل به صعود"
        power = "متوسط"
        soft_long = 1
    elif bearish_pct > bullish_pct:
        bias = "weak_bearish"
        bias_text = "رنج متمایل به نزول"
        power = "متوسط"
        soft_short = 1

    movement_bias = "neutral"
    movement_bias_text = "حرکت تازه مشخص نیست"
    if checked < MIN_CHECKED_FOR_STRONG_BIAS:
        movement_bias = "unknown"
        movement_bias_text = "نامشخص"
    elif pump_pressure >= dump_pressure + max(2, checked * 0.12):
        movement_bias = "pump_pressure"
        movement_bias_text = "فشار شروع پامپ بیشتر است"
    elif dump_pressure >= pump_pressure + max(2, checked * 0.12):
        movement_bias = "dump_pressure"
        movement_bias_text = "فشار شروع دامپ بیشتر است"
    elif pre_move_compression >= max(3, checked * 0.18):
        movement_bias = "compression"
        movement_bias_text = "بازار در فشردگی قبل از حرکت است"
    elif early_expansion >= max(3, checked * 0.16):
        movement_bias = "early_expansion"
        movement_bias_text = "چند حرکت تازه در بازار شروع شده"
    elif exhaustion + range_after_move >= max(4, checked * 0.20):
        movement_bias = "post_move_risk"
        movement_bias_text = "ریسک ورود بعد از حرکت زیاد است"

    movement_confidence = confidence
    return {
        "checked": checked,
        "bullish": bullish,
        "bearish": bearish,
        "range": ranging,
        "bullish_pct": bullish_pct,
        "bearish_pct": bearish_pct,
        "range_pct": range_pct,
        "bias": bias,
        "bias_text": bias_text,
        "power": power,
        "confidence": confidence,
        "soft_score_long": soft_long,
        "soft_score_short": soft_short,
        "pump_pressure": pump_pressure,
        "dump_pressure": dump_pressure,
        "pump_pressure_pct": pump_pct,
        "dump_pressure_pct": dump_pct,
        "pre_move_compression": pre_move_compression,
        "early_expansion": early_expansion,
        "exhaustion": exhaustion,
        "range_after_move": range_after_move,
        "movement_bias": movement_bias,
        "movement_bias_text": movement_bias_text,
        "movement_confidence": movement_confidence,
        "top_movement_symbols": (top_symbols or [])[:8],
        "is_soft_layer": True,
    }


def get_market_breadth() -> Dict[str, Any]:
    symbols = _unique_symbols()
    if not symbols:
        return _empty_breadth()

    bullish = 0
    bearish = 0
    ranging = 0
    checked = 0
    pump_pressure = 0
    dump_pressure = 0
    pre_move_compression = 0
    early_expansion = 0
    exhaustion = 0
    range_after_move = 0
    top: List[Dict[str, Any]] = []

    for symbol in symbols:
        profile = _movement_profile_symbol(symbol)
        if profile is None:
            continue

        checked += 1
        status = str(profile.get("status") or "range")
        if status == "bullish":
            bullish += 1
        elif status == "bearish":
            bearish += 1
        else:
            ranging += 1

        movement_bias = str(profile.get("movement_bias") or "neutral")
        if movement_bias == "pump_pressure":
            pump_pressure += 1
        elif movement_bias == "dump_pressure":
            dump_pressure += 1

        phase = str(profile.get("setup_phase") or "NORMAL")
        if phase == "PRE_MOVE_COMPRESSION":
            pre_move_compression += 1
        elif phase == "EARLY_EXPANSION":
            early_expansion += 1
        elif phase == "EXHAUSTION":
            exhaustion += 1
        elif phase == "RANGE_AFTER_MOVE":
            range_after_move += 1

        movement_strength = abs(_safe_int(profile.get("long_pressure"), 0) - _safe_int(profile.get("short_pressure"), 0))
        if phase in {"PRE_MOVE_COMPRESSION", "EARLY_EXPANSION"} or movement_strength >= 2:
            top.append({
                "symbol": profile.get("symbol"),
                "movement_bias": movement_bias,
                "setup_phase": phase,
                "long_pressure": profile.get("long_pressure"),
                "short_pressure": profile.get("short_pressure"),
                "move_atr_5m_10bar": profile.get("move_atr_5m_10bar"),
            })

    if checked == 0:
        return _empty_breadth()

    top.sort(key=lambda x: abs(_safe_int(x.get("long_pressure"), 0) - _safe_int(x.get("short_pressure"), 0)) + (2 if x.get("setup_phase") in {"PRE_MOVE_COMPRESSION", "EARLY_EXPANSION"} else 0), reverse=True)
    return _classify_breadth(
        bullish,
        bearish,
        ranging,
        checked,
        pump_pressure,
        dump_pressure,
        pre_move_compression,
        early_expansion,
        exhaustion,
        range_after_move,
        top,
    )


def get_market_breadth_profile() -> Dict[str, Any]:
    """Profile for AI Movement Hunter. All fields are soft context only."""
    data = get_market_breadth()
    return {
        **data,
        "market_breadth_bias": data.get("bias", "unknown"),
        "market_breadth_power": data.get("power", "ضعیف"),
        "market_breadth_confidence": data.get("confidence", "LOW"),
        "movement_market_bias": data.get("movement_bias", "unknown"),
        "movement_market_text": data.get("movement_bias_text", "نامشخص"),
        "movement_market_confidence": data.get("movement_confidence", "LOW"),
        "ai_movement_hunter_context": True,
        "is_soft_layer": True,
        "can_block_signal": False,
        "can_open_trade": False,
    }


def get_market_status_text() -> str:
    now = int(time.time())

    if MARKET_STATUS_CACHE.get("text") and now - _safe_int(MARKET_STATUS_CACHE.get("time"), 0) < CACHE_SECONDS:
        return str(MARKET_STATUS_CACHE["text"])

    data = get_market_breadth()
    MARKET_STATUS_CACHE["time"] = now
    MARKET_STATUS_CACHE["data"] = data

    if data.get("checked", 0) == 0:
        text = (
            "📊 وضعیت بازار\n\n"
            "داده کافی برای محاسبه وضعیت بازار دریافت نشد.\n"
            "چند دقیقه بعد دوباره امتحان کن."
        )
        MARKET_STATUS_CACHE["text"] = text
        return text

    top_symbols = data.get("top_movement_symbols") or []
    top_line = ""
    if top_symbols:
        names = []
        for item in top_symbols[:5]:
            sym = str(item.get("symbol") or "")
            phase = str(item.get("setup_phase") or "")
            bias = str(item.get("movement_bias") or "")
            if sym:
                names.append(f"{sym}({bias}/{phase})")
        if names:
            top_line = "\n🎯 کاندیداهای حرکتی: " + "، ".join(names) + "\n"

    text = (
        "📊 وضعیت بازار\n\n"
        f"تعداد ارزهای بررسی‌شده: {data['checked']}\n"
        f"🟢 صعودی: {data['bullish']} ارز | {data['bullish_pct']}٪\n"
        f"🔴 نزولی: {data['bearish']} ارز | {data['bearish_pct']}٪\n"
        f"⚪ رنج: {data['range']} ارز | {data['range_pct']}٪\n\n"
        f"نتیجه کلی: بازار {data['bias_text']} است.\n"
        f"قدرت وضعیت: {data['power']}\n"
        f"اعتماد گزارش: {data.get('confidence', 'LOW')}\n\n"
        "🧠 زمینه شکار حرکت\n"
        f"فشار پامپ تازه: {data.get('pump_pressure', 0)} ارز | {data.get('pump_pressure_pct', 0)}٪\n"
        f"فشار دامپ تازه: {data.get('dump_pressure', 0)} ارز | {data.get('dump_pressure_pct', 0)}٪\n"
        f"فشردگی قبل حرکت: {data.get('pre_move_compression', 0)} ارز\n"
        f"شروع گسترش حرکت: {data.get('early_expansion', 0)} ارز\n"
        f"ریسک بعد از حرکت/خستگی: {data.get('exhaustion', 0) + data.get('range_after_move', 0)} ارز\n"
        f"جمع‌بندی حرکتی: {data.get('movement_bias_text', 'نامشخص')}\n"
        f"{top_line}\n"
        "این گزارش فقط زمینه نرم AI است؛ به تنهایی سیگنال ورود یا مجوز ترید واقعی نیست."
    )

    MARKET_STATUS_CACHE["text"] = text
    return text


__all__ = [
    "get_market_breadth",
    "get_market_breadth_profile",
    "get_market_status_text",
]
