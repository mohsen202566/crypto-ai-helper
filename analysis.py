# -*- coding: utf-8 -*-
"""
AI Classic Direct Analysis Engine

هدف:
- سیگنال مستقیم ACTIVE، بدون Setup/Watchlist
- سازگار با AI Learning / Coin Risk / Coin Rotation / Ghost Learning
- خروجی ساده برای تلگرام
- تصمیم‌گیری هوشمند پشت صحنه
- LONG کمی سخت‌تر از SHORT
- SHORT فعلاً نرم‌تر و نزدیک به منطق قبلی
- Smart TP/SL با ATR + S/R + AI TP Memory
"""

import math
from typing import Dict, List, Optional, Tuple, Any

import ccxt
import pandas as pd
import ta


try:
    from config import (
        MIN_DIRECT_SCORE,
        MIN_ADX_FOR_TREND,
        MIN_MANUAL_CONFIRMATIONS,
    )
except Exception:
    MIN_DIRECT_SCORE = 82
    MIN_ADX_FOR_TREND = 20
    MIN_MANUAL_CONFIRMATIONS = 4


try:
    from ai_memory import get_ai_settings, update_ai_summary
except Exception:
    get_ai_settings = None
    update_ai_summary = None


try:
    from coin_learning import (
        build_signal_snapshot,
        get_smart_tp_suggestion,
        should_require_extra_strength,
    )
except Exception:
    build_signal_snapshot = None
    get_smart_tp_suggestion = None
    should_require_extra_strength = None


try:
    from coin_risk import get_direction_risk_state
except Exception:
    get_direction_risk_state = None


try:
    from coin_rotation import get_coin_rotation_score
except Exception:
    get_coin_rotation_score = None


exchange = ccxt.okx({
    "enableRateLimit": True,
    "timeout": 20000,
    "options": {"defaultType": "swap"},
})


# ============================================================
# Core thresholds
# ============================================================

AUTO_DIRECT_SCORE_MIN = 82
ADX_HARD_MIN = max(float(MIN_ADX_FOR_TREND), 20.0)

LONG_DIRECT_SCORE_BONUS_REQUIREMENT = 3
LONG_MIN_1H_STRICT = True
LONG_BLOCK_IF_AGAINST_VWAP = True

MIN_SL_ATR_MULTIPLIER = 1.30
TP1_FALLBACK_ATR = 0.75
TP2_FALLBACK_ATR = 1.40
MAX_REASONABLE_SL_ATR = 2.40
MIN_TP1_ATR = 0.55
LEVEL_BUFFER_ATR = 0.14
SL_BUFFER_ATR = 0.25

TF_LEVEL_WEIGHTS = {
    "5M": 1.0,
    "15M": 1.6,
    "30M": 2.2,
}

LEVEL_LOOKBACK = 160
SWING_WINDOW = 3


# ============================================================
# Basic helpers
# ============================================================

def to_okx_symbol(symbol: str) -> str:
    coin = str(symbol).upper().replace("USDT", "")
    return f"{coin}/USDT:USDT"


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default

        v = float(value)

        if math.isnan(v) or math.isinf(v):
            return default

        return v

    except Exception:
        return default


def safe_round(value: Any, digits: int = 8):
    try:
        if value is None:
            return None

        return round(float(value), digits)

    except Exception:
        return None


def cap_score(value: Any) -> int:
    try:
        return max(0, min(100, int(round(float(value)))))

    except Exception:
        return 0


def direction_fa(direction: str) -> str:
    if direction == "LONG":
        return "لانگ"

    if direction == "SHORT":
        return "شورت"

    return "بدون سیگنال"


def get_klines(
    symbol: str,
    interval: str = "15m",
    limit: int = 260,
    include_current: bool = False,
) -> pd.DataFrame:
    data = exchange.fetch_ohlcv(
        to_okx_symbol(symbol),
        timeframe=interval,
        limit=limit,
    )

    if not data or len(data) < 220:
        raise Exception(
            f"داده کافی برای {symbol} در تایم {interval} دریافت نشد"
        )

    df = pd.DataFrame(
        data,
        columns=[
            "time",
            "open",
            "high",
            "low",
            "close",
            "volume",
        ],
    )

    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna()

    if not include_current:
        df = df.iloc[:-1]

    if len(df) < 210:
        raise Exception(
            f"داده کندل کافی برای {symbol} در تایم {interval} کامل نیست"
        )

    return df

def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    df["ema20"] = ta.trend.ema_indicator(
        df["close"],
        window=20,
    )

    df["ema50"] = ta.trend.ema_indicator(
        df["close"],
        window=50,
    )

    df["ema200"] = ta.trend.ema_indicator(
        df["close"],
        window=200,
    )

    df["rsi"] = ta.momentum.rsi(
        df["close"],
        window=14,
    )

    macd = ta.trend.MACD(df["close"])

    df["macd"] = macd.macd()
    df["macd_signal"] = macd.macd_signal()
    df["macd_hist"] = macd.macd_diff()

    df["atr"] = ta.volatility.average_true_range(
        df["high"],
        df["low"],
        df["close"],
        window=14,
    )

    adx = ta.trend.ADXIndicator(
        df["high"],
        df["low"],
        df["close"],
        window=14,
    )

    df["adx"] = adx.adx()

    typical = (
        df["high"]
        + df["low"]
        + df["close"]
    ) / 3

    volume_sum = df["volume"].cumsum().replace(0, pd.NA)

    df["vwap"] = (
        typical
        * df["volume"]
    ).cumsum() / volume_sum

    df["volume_ma20"] = df["volume"].rolling(20).mean()

    df["volume_ratio"] = (
        df["volume"]
        / df["volume_ma20"].replace(0, pd.NA)
    )

    df = df.dropna()

    if len(df) < 60:
        raise Exception("اندیکاتورها کامل محاسبه نشدند")

    return df


# ============================================================
# Technical helpers
# ============================================================

def ema_direction(df: pd.DataFrame) -> str:
    last = df.iloc[-1]

    if last["ema50"] > last["ema200"]:
        return "bullish"

    if last["ema50"] < last["ema200"]:
        return "bearish"

    return "range"


def trend_direction(df: pd.DataFrame) -> str:
    """
    Compatibility helper for scanner / market scanner.
    """

    last = df.iloc[-1]
    close = safe_float(last["close"])

    if last["ema50"] > last["ema200"]:
        if close > last["ema50"]:
            return "bullish"
        return "weak_bullish"

    if last["ema50"] < last["ema200"]:
        if close < last["ema50"]:
            return "bearish"
        return "weak_bearish"

    return "range"


def price_position_ema20(df: pd.DataFrame) -> str:
    last = df.iloc[-1]

    if last["close"] > last["ema20"]:
        return "above_ema20"

    if last["close"] < last["ema20"]:
        return "below_ema20"

    return "near_ema20"


def vwap_status(df: pd.DataFrame) -> str:
    last = df.iloc[-1]

    if last["close"] > last["vwap"]:
        return "above_vwap"

    if last["close"] < last["vwap"]:
        return "below_vwap"

    return "near_vwap"


def distance_from_ema20_atr(df: pd.DataFrame) -> float:
    last = df.iloc[-1]

    price = safe_float(last["close"])
    atr = max(
        safe_float(last["atr"]),
        price * 0.0015,
    )

    return abs(
        price - safe_float(last["ema20"])
    ) / atr


def volume_quality(df: pd.DataFrame) -> Tuple[str, float]:
    last = df.iloc[-1]

    ratio = safe_float(
        last.get("volume_ratio", 1.0),
        1.0,
    )

    if ratio >= 1.35:
        return "high_volume", ratio

    if ratio >= 0.90:
        return "normal_volume", ratio

    if ratio <= 0.65:
        return "weak_volume", ratio

    return "neutral_volume", ratio


def buy_sell_power(
    df: pd.DataFrame,
    candles: int = 20,
) -> Tuple[float, float]:
    recent = df.tail(candles)

    green = recent[
        recent["close"] > recent["open"]
    ]["volume"].sum()

    red = recent[
        recent["close"] < recent["open"]
    ]["volume"].sum()

    total = green + red

    if total <= 0:
        return 50.0, 50.0

    return (
        round((green / total) * 100, 1),
        round((red / total) * 100, 1),
    )

# ============================================================
# AI / Risk helper layer
# ============================================================

def get_ai_mode_settings() -> Dict:
    default = {
        "enabled": True,
        "learning_enabled": True,
        "soft_mode": True,
    }

    if get_ai_settings is None:
        return default

    try:
        settings = get_ai_settings()
        if isinstance(settings, dict):
            default.update(settings)
    except Exception:
        pass

    return default


def get_coin_risk(symbol: str, direction: str) -> Dict:
    default = {
        "sl_count": 0,
        "tp_count": 0,
        "strictness_level": 0,
        "risk_score": 0,
        "bad_day": False,
        "recommend_reduce": False,
    }

    if get_direction_risk_state is None:
        return default

    try:
        state = get_direction_risk_state(symbol, direction)
        if isinstance(state, dict):
            default.update(state)
    except Exception:
        pass

    return default


def get_rotation_context(symbol: str) -> Dict:
    default = {
        "rotation_score": 50,
        "priority_score": 50,
        "risk_score": 0,
        "status": "NORMAL",
    }

    if get_coin_rotation_score is None:
        return default

    try:
        state = get_coin_rotation_score(symbol)
        if isinstance(state, dict):
            default.update(state)
    except Exception:
        pass

    return default


def ai_extra_strength_required(symbol: str, direction: str, snapshot: Dict) -> Dict:
    """
    خروجی نرم برای سخت‌تر کردن سیگنال‌های تکراری بد.
    اینجا سیگنال را مستقیم نابود نمی‌کنیم؛ فقط تایید/امتیاز بیشتر می‌خواهیم.
    """

    default = {
        "required": False,
        "extra_score": 0,
        "extra_confirmations": 0,
        "reason": None,
    }

    if should_require_extra_strength is None:
        return default

    try:
        result = should_require_extra_strength(symbol, direction, snapshot)
        if isinstance(result, dict):
            default.update(result)
        elif result is True:
            default["required"] = True
            default["extra_score"] = 3
            default["extra_confirmations"] = 1
            default["reason"] = "AI Learning برای این شرایط تایید بیشتر می‌خواهد"
    except Exception:
        pass

    return default


def build_local_snapshot(
    symbol: str,
    direction: str,
    df_4h: pd.DataFrame,
    df_1h: pd.DataFrame,
    df_30m: pd.DataFrame,
    df_15m: pd.DataFrame,
    df_5m: pd.DataFrame,
    score_pack: Dict,
    market_context: Dict,
) -> Dict:
    """
    Snapshot کامل برای AI Learning.
    اگر coin_learning.build_signal_snapshot موجود باشد از آن استفاده می‌کنیم،
    وگرنه snapshot داخلی می‌سازیم تا تحلیل قطع نشود.
    """

    last_15 = df_15m.iloc[-1]
    last_5 = df_5m.iloc[-1]

    buy2, sell2 = buy_sell_power(df_5m, 2)
    buy3, sell3 = buy_sell_power(df_5m, 3)
    buy20, sell20 = buy_sell_power(df_5m, 20)

    base_snapshot = {
        "symbol": symbol,
        "direction": direction,
        "price": safe_float(last_15["close"]),
        "entry": safe_float(last_15["close"]),
        "rsi": safe_float(last_15["rsi"]),
        "rsi_5m": safe_float(last_5["rsi"]),
        "macd": safe_float(last_15["macd"]),
        "macd_signal": safe_float(last_15["macd_signal"]),
        "macd_hist": safe_float(last_15["macd_hist"]),
        "macd_5m": safe_float(last_5["macd"]),
        "macd_signal_5m": safe_float(last_5["macd_signal"]),
        "macd_hist_5m": safe_float(last_5["macd_hist"]),
        "adx": safe_float(last_15["adx"]),
        "atr": safe_float(last_15["atr"]),
        "ema20": safe_float(last_15["ema20"]),
        "ema50": safe_float(last_15["ema50"]),
        "ema200": safe_float(last_15["ema200"]),
        "vwap": safe_float(last_15["vwap"]),

"vwap_status": vwap_status(df_15m),
        "price_above_ema20": safe_float(last_15["close"]) > safe_float(last_15["ema20"]),
        "price_above_vwap": safe_float(last_15["close"]) > safe_float(last_15["vwap"]),
        "power2_buy": buy2,
        "power2_sell": sell2,
        "power3_buy": buy3,
        "power3_sell": sell3,
        "buy_power": buy20,
        "sell_power": sell20,
        "trends": score_pack.get("trends", {}),
        "long_score": score_pack.get("long_score", 0),
        "short_score": score_pack.get("short_score", 0),
        "score": max(score_pack.get("long_score", 0), score_pack.get("short_score", 0)),
        "market_regime": market_context.get("market_regime", "neutral"),
        "btc_bias": market_context.get("btc_bias", "neutral"),
        "coin_behavior": market_context.get("coin_behavior", "unknown"),
        "timeframe": "15M/30M/1H + 5M trigger",
    }

    if build_signal_snapshot is None:
        return base_snapshot

    try:
        ai_snapshot = build_signal_snapshot(
            symbol=symbol,
            direction=direction,
            technical_snapshot=base_snapshot,
            market_context=market_context,
        )
        if isinstance(ai_snapshot, dict):
            base_snapshot.update(ai_snapshot)
    except Exception:
        pass

    return base_snapshot


# ============================================================
# Main classic score engine
# ============================================================

def simple_classic_score(
    symbol: str,
    df_4h: pd.DataFrame,
    df_1h: pd.DataFrame,
    df_30m: pd.DataFrame,
    df_15m: pd.DataFrame,
    df_5m: pd.DataFrame,
    market_context: Optional[Dict] = None,
) -> Dict:
    long_score = 0.0
    short_score = 0.0

    long_reasons: List[str] = []
    short_reasons: List[str] = []

    confirmations_long = 0
    confirmations_short = 0

    market_context = market_context or {}

    trends = {
        "4H": ema_direction(df_4h),
        "1H": ema_direction(df_1h),
        "30M": ema_direction(df_30m),
        "15M": ema_direction(df_15m),
        "5M": ema_direction(df_5m),
    }

    last_4h = df_4h.iloc[-1]
    last_1h = df_1h.iloc[-1]
    last_30 = df_30m.iloc[-1]
    last_15 = df_15m.iloc[-1]
    prev_15 = df_15m.iloc[-2]
    last_5 = df_5m.iloc[-1]
    prev_5 = df_5m.iloc[-2]

    adx_15 = safe_float(last_15["adx"])
    dist_15 = distance_from_ema20_atr(df_15m)
    vol_status, vol_ratio = volume_quality(df_15m)

    # --------------------------------------------------------
    # 1) EMA multi-timeframe direction
    # --------------------------------------------------------

    ema_tf_weights = {
        "4H": 8,
        "1H": 22,
        "30M": 12,
        "15M": 18,
        "5M": 10,
    }

    for tf, trend in trends.items():
        w = ema_tf_weights.get(tf, 0)

        if trend == "bullish":
            long_score += w
            if tf in ["1H", "30M", "15M"]:
                confirmations_long += 1
            long_reasons.append(f"{tf}: EMA50 بالای EMA200؛ تایید لانگ")

        elif trend == "bearish":
            short_score += w
            if tf in ["1H", "30M", "15M"]:
                confirmations_short += 1
            short_reasons.append(f"{tf}: EMA50 پایین EMA200؛ تایید شورت")

    if last_15["ema20"] > last_15["ema50"] > last_15["ema200"]:
        long_score += 10
        confirmations_long += 1
        long_reasons.append("15M: چینش EMA کاملاً صعودی است")

    elif last_15["ema20"] < last_15["ema50"] < last_15["ema200"]:
        short_score += 10
        confirmations_short += 1
        short_reasons.append("15M: چینش EMA کاملاً نزولی است")

    if last_1h["ema20"] > last_1h["ema50"] > last_1h["ema200"]:
        long_score += 7
        confirmations_long += 1
        long_reasons.append("1H: چینش EMAها برای لانگ قوی است")

    elif last_1h["ema20"] < last_1h["ema50"] < last_1h["ema200"]:
        short_score += 6
        short_reasons.append("1H: چینش EMAها برای شورت مناسب است")

if last_15["close"] > last_15["ema20"]:
        long_score += 10
        confirmations_long += 1
        long_reasons.append("15M: قیمت بالای EMA20 است")

    elif last_15["close"] < last_15["ema20"]:
        short_score += 10
        confirmations_short += 1
        short_reasons.append("15M: قیمت پایین EMA20 است")

    if last_5["close"] > last_5["ema20"]:
        long_score += 6
        long_reasons.append("5M: قیمت بالای EMA20 است")

    elif last_5["close"] < last_5["ema20"]:
        short_score += 6
        short_reasons.append("5M: قیمت پایین EMA20 است")

    # --------------------------------------------------------
    # 2) RSI
    # --------------------------------------------------------

    rsi_15 = safe_float(last_15["rsi"])
    rsi_15_prev = safe_float(prev_15["rsi"])
    rsi_30 = safe_float(last_30["rsi"])
    rsi_5 = safe_float(last_5["rsi"])
    rsi_5_prev = safe_float(prev_5["rsi"])

    if 52 <= rsi_15 <= 66:
        long_score += 12
        confirmations_long += 1
        long_reasons.append("15M: RSI در محدوده سالم لانگ است")

    elif 32 <= rsi_15 <= 50:
        short_score += 12
        confirmations_short += 1
        short_reasons.append("15M: RSI در محدوده سالم شورت است")

    else:
        if rsi_15 > 72:
            long_score -= 6
            long_reasons.append("15M: RSI برای لانگ بیش‌ازحد بالا است")

        if rsi_15 < 28:
            short_score -= 5
            short_reasons.append("15M: RSI برای شورت بیش‌ازحد پایین است")

    if rsi_15 > rsi_15_prev:
        long_score += 4
        long_reasons.append("15M: شیب RSI صعودی است")

    elif rsi_15 < rsi_15_prev:
        short_score += 4
        short_reasons.append("15M: شیب RSI نزولی است")

    if rsi_30 >= 50:
        long_score += 4
        long_reasons.append("30M: RSI بالای 50 است")

    else:
        short_score += 4
        short_reasons.append("30M: RSI پایین 50 است")

    if rsi_5 >= 50:
        long_score += 3
        long_reasons.append("5M: RSI بالای 50 است")

    else:
        short_score += 3
        short_reasons.append("5M: RSI زیر 50 است")

    if rsi_5 > rsi_5_prev:
        long_score += 2
        long_reasons.append("5M: شیب RSI صعودی است")

    elif rsi_5 < rsi_5_prev:
        short_score += 2
        short_reasons.append("5M: شیب RSI نزولی است")

    # --------------------------------------------------------
    # 3) MACD full + histogram
    # --------------------------------------------------------

    if last_15["macd"] > last_15["macd_signal"]:
        long_score += 15
        confirmations_long += 1
        long_reasons.append("15M: MACD بالای Signal است")

    elif last_15["macd"] < last_15["macd_signal"]:
        short_score += 15
        confirmations_short += 1
        short_reasons.append("15M: MACD پایین Signal است")

    if last_15["macd_hist"] > 0:
        long_score += 5
        long_reasons.append("15M: Histogram مثبت است")

    elif last_15["macd_hist"] < 0:
        short_score += 5
        short_reasons.append("15M: Histogram منفی است")

    if last_15["macd_hist"] > prev_15["macd_hist"]:
        long_score += 5
        long_reasons.append("15M: Histogram در حال تقویت صعودی است")

    elif last_15["macd_hist"] < prev_15["macd_hist"]:
        short_score += 5
        short_reasons.append("15M: Histogram در حال تقویت نزولی است")

    if last_30["macd"] > last_30["macd_signal"]:
        long_score += 7
        long_reasons.append("30M: MACD با لانگ هم‌جهت است")

    elif last_30["macd"] < last_30["macd_signal"]:
        short_score += 7
        short_reasons.append("30M: MACD با شورت هم‌جهت است")

    if last_5["macd"] > last_5["macd_signal"]:
        long_score += 6
        long_reasons.append("5M: MACD سریع لانگ را تایید می‌کند")

    elif last_5["macd"] < last_5["macd_signal"]:
        short_score += 6
        short_reasons.append("5M: MACD سریع شورت را تایید می‌کند")

    # --------------------------------------------------------
    # 4) ADX hard minimum
    # --------------------------------------------------------

if adx_15 >= 35:
        long_score += 8
        short_score += 8
        long_reasons.append("ADX 15M قوی است")
        short_reasons.append("ADX 15M قوی است")

    elif adx_15 >= 25:
        long_score += 5
        short_score += 5
        long_reasons.append("ADX 15M مناسب است")
        short_reasons.append("ADX 15M مناسب است")

    elif adx_15 >= ADX_HARD_MIN:
        long_score += 1
        short_score += 1
        long_reasons.append("ADX 15M قابل قبول است")
        short_reasons.append("ADX 15M قابل قبول است")

    else:
        long_score = min(long_score, 69)
        short_score = min(short_score, 69)
        long_reasons.append("رد: ADX زیر حداقل مجاز است")
        short_reasons.append("رد: ADX زیر حداقل مجاز است")

    # --------------------------------------------------------
    # 5) VWAP
    # --------------------------------------------------------

    if last_15["close"] > last_15["vwap"]:
        long_score += 4
        short_score -= 4
        long_reasons.append("15M: قیمت بالای VWAP است")
        short_reasons.append("15M: قیمت بالای VWAP؛ جریمه نرم شورت")

    elif last_15["close"] < last_15["vwap"]:
        short_score += 4
        long_score -= 4
        short_reasons.append("15M: قیمت پایین VWAP است")
        long_reasons.append("15M: قیمت پایین VWAP؛ جریمه لانگ")

    # --------------------------------------------------------
    # 6) Soft market regime
    # --------------------------------------------------------

    market_bias = market_context.get("market_regime", "neutral")

    if market_bias == "bullish":
        long_score += 3
        short_score -= 3
        long_reasons.append("بازار کلی صعودی است")
        short_reasons.append("بازار کلی صعودی است؛ جریمه نرم شورت")

    elif market_bias == "bearish":
        short_score += 3
        long_score -= 3
        short_reasons.append("بازار کلی نزولی است")
        long_reasons.append("بازار کلی نزولی است؛ جریمه نرم لانگ")

    # --------------------------------------------------------
    # 7) Buy/Sell power فقط مکمل
    # --------------------------------------------------------

    buy2, sell2 = buy_sell_power(df_5m, 2)
    buy3, sell3 = buy_sell_power(df_5m, 3)
    buy20, sell20 = buy_sell_power(df_5m, 20)

    if buy3 >= 62:
        long_score += 3
        long_reasons.append("قدرت خرید کوتاه‌مدت مناسب است")

    if sell3 >= 62:
        short_score += 3
        short_reasons.append("قدرت فروش کوتاه‌مدت مناسب است")

    # --------------------------------------------------------
    # 8) Base validity
    # --------------------------------------------------------

    long_direction_ok = (
        trends["1H"] == "bullish"
        and trends["15M"] == "bullish"
    )

    short_direction_ok = (
        trends["1H"] == "bearish"
        or trends["15M"] == "bearish"
    )

    long_macd_ok = (
        last_15["macd"] > last_15["macd_signal"]
        and last_15["macd_hist"] > 0
    )

    short_macd_ok = (
        last_15["macd"] <= last_15["macd_signal"]
    )

    # LONG کمی سخت‌تر
    long_1h_strict_ok = True
    if LONG_MIN_1H_STRICT:
        long_1h_strict_ok = (
            trends["1H"] == "bullish"
            and last_1h["close"] > last_1h["ema20"]
            and last_1h["macd"] >= last_1h["macd_signal"]
        )

    long_vwap_ok = True
    if LONG_BLOCK_IF_AGAINST_VWAP:
        long_vwap_ok = last_15["close"] >= last_15["vwap"]

    if not long_direction_ok:
        long_reasons.append("رد لانگ: 1H و 15M همزمان صعودی نیستند")

    if not short_direction_ok:
        short_reasons.append("رد شورت: 1H یا 15M نزولی نیست")

    if not long_macd_ok:
        long_reasons.append("رد لانگ: MACD 15M برای لانگ کافی نیست")

    if not short_macd_ok:
        short_reasons.append("رد شورت: MACD 15M برای شورت کافی نیست")

    if not long_1h_strict_ok:
        long_reasons.append("رد لانگ: تایید 1H برای لانگ کافی نیست")

    if not long_vwap_ok:
        long_reasons.append("رد لانگ: لانگ خلاف VWAP است")

long_valid = (
        adx_15 >= ADX_HARD_MIN
        and long_direction_ok
        and long_macd_ok
        and long_1h_strict_ok
        and long_vwap_ok
    )

    short_valid = (
        adx_15 >= ADX_HARD_MIN
        and short_direction_ok
        and short_macd_ok
    )

    return {
        "long_score": cap_score(long_score),
        "short_score": cap_score(short_score),
        "long_reasons": long_reasons,
        "short_reasons": short_reasons,
        "confirmations_long": confirmations_long,
        "confirmations_short": confirmations_short,
        "trends": trends,
        "distance_ema20_atr": round(dist_15, 2),
        "volume_status": vol_status,
        "volume_ratio": round(vol_ratio, 2),
        "power2_buy": buy2,
        "power2_sell": sell2,
        "power3_buy": buy3,
        "power3_sell": sell3,
        "buy_power": buy20,
        "sell_power": sell20,
        "long_valid": long_valid,
        "short_valid": short_valid,
        "adx_15": adx_15,
        "market_regime": market_bias,
    }

# ============================================================
# Smart Support / Resistance
# ============================================================

def find_swing_levels(
    df: pd.DataFrame,
    timeframe: str,
    lookback: int = LEVEL_LOOKBACK,
    window: int = SWING_WINDOW,
) -> List[Dict]:
    recent = df.tail(lookback).copy()

    if len(recent) < window * 2 + 10:
        return []

    levels: List[Dict] = []
    tf_weight = TF_LEVEL_WEIGHTS.get(timeframe, 1.0)

    for i in range(window, len(recent) - window):
        row = recent.iloc[i]
        left = recent.iloc[i - window:i]
        right = recent.iloc[i + 1:i + 1 + window]

        is_low = (
            row["low"] <= left["low"].min()
            and row["low"] <= right["low"].min()
        )

        is_high = (
            row["high"] >= left["high"].max()
            and row["high"] >= right["high"].max()
        )

        recency_score = 1.0 + (i / max(len(recent), 1)) * 0.8

        if is_low:
            levels.append({
                "price": safe_float(row["low"]),
                "kind": "support",
                "timeframe": timeframe,
                "strength": tf_weight * recency_score,
            })

        if is_high:
            levels.append({
                "price": safe_float(row["high"]),
                "kind": "resistance",
                "timeframe": timeframe,
                "strength": tf_weight * recency_score,
            })

    return levels


def cluster_levels(
    raw_levels: List[Dict],
    price: float,
    atr: float,
) -> List[Dict]:
    if not raw_levels:
        return []

    merge_distance = max(atr * 0.25, price * 0.0010)
    raw_levels = sorted(raw_levels, key=lambda x: x["price"])

    clusters: List[List[Dict]] = []

    for level in raw_levels:
        if not clusters:
            clusters.append([level])
            continue

        if abs(level["price"] - clusters[-1][-1]["price"]) > merge_distance:
            clusters.append([level])
        else:
            clusters[-1].append(level)

    merged = []

    for group in clusters:
        total_strength = sum(safe_float(x.get("strength", 0)) for x in group)
        weighted_price = (
            sum(safe_float(x["price"]) * safe_float(x["strength"]) for x in group)
            / max(total_strength, 1e-9)
        )

        timeframes = sorted(set(x.get("timeframe", "?") for x in group))

        kind_counts = {
            "support": 0,
            "resistance": 0,
        }

        for x in group:
            kind_counts[x.get("kind", "support")] += 1

        kind = (
            "support"
            if kind_counts["support"] >= kind_counts["resistance"]
            else "resistance"
        )

        touch_bonus = min(len(group), 5) * 0.9
        mtf_bonus = len(timeframes) * 0.8

        merged.append({
            "price": weighted_price,
            "kind": kind,
            "strength": round(total_strength + touch_bonus + mtf_bonus, 2),
            "touches": len(group),
            "timeframes": timeframes,
        })

    return merged


def get_strong_levels(
    df_5m: pd.DataFrame,
    df_15m: pd.DataFrame,
    df_30m: pd.DataFrame,
    price: float,
    atr: float,
) -> Dict:
    raw = []

    raw.extend(find_swing_levels(df_5m, "5M"))
    raw.extend(find_swing_levels(df_15m, "15M"))
    raw.extend(find_swing_levels(df_30m, "30M"))

    clustered = cluster_levels(raw, price, atr)

    supports = [x for x in clustered if safe_float(x["price"]) < price]
    resistances = [x for x in clustered if safe_float(x["price"]) > price]

    supports = sorted(
        supports,
        key=lambda x: (x["strength"], -abs(price - x["price"])),
        reverse=True,
    )

    resistances = sorted(
        resistances,
        key=lambda x: (x["strength"], -abs(x["price"] - price)),
        reverse=True,
    )

    nearest_support = max(
        [x["price"] for x in supports],
        default=price - atr * MIN_SL_ATR_MULTIPLIER,
    )

nearest_resistance = min(
        [x["price"] for x in resistances],
        default=price + atr * TP1_FALLBACK_ATR,
    )

    return {
        "supports": supports,
        "resistances": resistances,
        "nearest_support": nearest_support,
        "nearest_resistance": nearest_resistance,
    }


# ============================================================
# TP / SL helpers
# ============================================================

def coin_volatility_factor(df_15m: pd.DataFrame, price: float) -> float:
    try:
        atr_pct = safe_float(df_15m.iloc[-1]["atr"]) / max(price, 1e-12)
        recent = df_15m.tail(96)

        avg_range_pct = (
            ((recent["high"] - recent["low"]) / recent["close"].replace(0, pd.NA))
            .mean()
        )

        raw = max(float(atr_pct), float(avg_range_pct))

    except Exception:
        raw = 0.004

    if raw >= 0.012:
        return 1.25

    if raw >= 0.008:
        return 1.15

    if raw <= 0.003:
        return 0.95

    return 1.0


def select_level_for_sl(
    direction: str,
    price: float,
    atr: float,
    levels: List[Dict],
    base_distance: float,
) -> Optional[float]:
    max_distance = atr * MAX_REASONABLE_SL_ATR
    valid = []

    for level in levels:
        distance = abs(price - safe_float(level["price"]))

        if base_distance * 0.45 <= distance <= max_distance:
            valid.append(level)

    if not valid:
        return None

    valid.sort(key=lambda x: x["strength"], reverse=True)

    return safe_float(valid[0]["price"])


def select_level_for_tp(
    direction: str,
    price: float,
    atr: float,
    levels: List[Dict],
    fallback_mult: float,
    buffer: float,
) -> float:
    min_distance = atr * MIN_TP1_ATR
    max_distance = atr * 3.0

    candidates = []

    for level in levels:
        level_price = safe_float(level["price"])

        target = (
            level_price - buffer
            if direction == "LONG"
            else level_price + buffer
        )

        distance = abs(target - price)

        if direction == "LONG" and target <= price:
            continue

        if direction == "SHORT" and target >= price:
            continue

        if min_distance <= distance <= max_distance:
            candidates.append((level["strength"], -distance, target))

    if candidates:
        candidates.sort(reverse=True)
        return safe_float(candidates[0][2])

    if direction == "LONG":
        return price + atr * fallback_mult

    return price - atr * fallback_mult


def normalize_ai_tp_suggestion(
    direction: str,
    price: float,
    atr: float,
    suggestion: Optional[Dict],
) -> Dict:
    """
    AI TP Memory نباید TP را بیش از حد کوچک کند.
    این تابع پیشنهاد AI را سالم‌سازی می‌کند.
    """

    if not isinstance(suggestion, dict):
        return {}

    result = {}

    min_tp_distance = max(
        atr * MIN_TP1_ATR,
        price * 0.0015,
    )

    raw_tp1 = suggestion.get("tp1")
    raw_tp2 = suggestion.get("tp2")
    raw_tp1_distance = suggestion.get("tp1_distance")
    raw_tp2_distance = suggestion.get("tp2_distance")

    if raw_tp1 is not None:
        tp1 = safe_float(raw_tp1)
    elif raw_tp1_distance is not None:
        d = safe_float(raw_tp1_distance)
        tp1 = price + d if direction == "LONG" else price - d
    else:
        tp1 = None

    if raw_tp2 is not None:
        tp2 = safe_float(raw_tp2)
    elif raw_tp2_distance is not None:
        d = safe_float(raw_tp2_distance)
        tp2 = price + d if direction == "LONG" else price - d
    else:
        tp2 = None

    if tp1 is not None:
        if direction == "LONG" and tp1 > price + min_tp_distance:
            result["tp1"] = tp1
        elif direction == "SHORT" and tp1 < price - min_tp_distance:
            result["tp1"] = tp1

    if tp2 is not None:
        if direction == "LONG" and tp2 > price + min_tp_distance * 1.4:
            result["tp2"] = tp2
        elif direction == "SHORT" and tp2 < price - min_tp_distance * 1.4:
            result["tp2"] = tp2

    return result

def get_ai_tp_memory(
    symbol: str,
    direction: str,
    price: float,
    atr: float,
    snapshot: Dict,
) -> Dict:
    if get_smart_tp_suggestion is None:
        return {}

    try:
        suggestion = get_smart_tp_suggestion(
            symbol=symbol,
            direction=direction,
            snapshot=snapshot,
        )
        return normalize_ai_tp_suggestion(direction, price, atr, suggestion)
    except Exception:
        return {}


def merge_tp_with_ai_memory(
    direction: str,
    price: float,
    atr: float,
    sr_tp1: float,
    sr_tp2: float,
    ai_tp: Dict,
) -> Tuple[float, float]:
    """
    ترکیب TP کلاسیک با AI Memory.
    اگر AI سابقه کافی داشته باشد، TP را کمی هوشمندتر می‌کند.
    اما TP بیش‌ازحد کوچک یا غیرمنطقی نمی‌شود.
    """

    tp1 = sr_tp1
    tp2 = sr_tp2

    ai_tp1 = ai_tp.get("tp1")
    ai_tp2 = ai_tp.get("tp2")

    min_tp1_distance = max(atr * MIN_TP1_ATR, price * 0.0015)
    min_tp2_distance = min_tp1_distance * 1.35

    if ai_tp1 is not None:
        ai_tp1 = safe_float(ai_tp1)

        if direction == "LONG":
            if price + min_tp1_distance <= ai_tp1 <= price + atr * 2.5:
                # برای TP1، هدف سریع‌تر را ترجیح می‌دهیم
                tp1 = min(tp1, ai_tp1)

        else:
            if price - atr * 2.5 <= ai_tp1 <= price - min_tp1_distance:
                tp1 = max(tp1, ai_tp1)

    if ai_tp2 is not None:
        ai_tp2 = safe_float(ai_tp2)

        if direction == "LONG":
            if price + min_tp2_distance <= ai_tp2 <= price + atr * 4.0:
                tp2 = min(tp2, ai_tp2)

        else:
            if price - atr * 4.0 <= ai_tp2 <= price - min_tp2_distance:
                tp2 = max(tp2, ai_tp2)

    # ترتیب TPها را اصلاح می‌کنیم
    if direction == "LONG":
        if tp1 <= price + min_tp1_distance:
            tp1 = price + min_tp1_distance

        if tp2 <= tp1:
            tp2 = max(price + min_tp2_distance, tp1 + atr * 0.45)

    else:
        if tp1 >= price - min_tp1_distance:
            tp1 = price - min_tp1_distance

        if tp2 >= tp1:
            tp2 = min(price - min_tp2_distance, tp1 - atr * 0.45)

    return tp1, tp2


def build_trade_levels(
    direction: str,
    price: float,
    atr: float,
    df_5m: pd.DataFrame,
    df_15m: pd.DataFrame,
    df_30m: pd.DataFrame,
    snapshot: Optional[Dict] = None,
    symbol: Optional[str] = None,
) -> Tuple[float, float, float, float, Dict]:
    price = safe_float(price)
    atr = max(safe_float(atr), price * 0.0015)

    vol_factor = coin_volatility_factor(df_15m, price)

    min_sl_distance = atr * MIN_SL_ATR_MULTIPLIER * vol_factor
    tp1_fallback = TP1_FALLBACK_ATR * vol_factor
    tp2_fallback = TP2_FALLBACK_ATR * vol_factor

    buffer_tp = max(atr * LEVEL_BUFFER_ATR * vol_factor, price * 0.0007)
    buffer_sl = max(atr * SL_BUFFER_ATR * vol_factor, price * 0.0010)

    level_pack = get_strong_levels(df_5m, df_15m, df_30m, price, atr)

    supports = level_pack["supports"]
    resistances = level_pack["resistances"]

    ai_tp = {}
    if symbol and snapshot:
        ai_tp = get_ai_tp_memory(symbol, direction, price, atr, snapshot)

    if direction == "LONG":
        classic_sl = price - min_sl_distance

        support_price = select_level_for_sl(
            "LONG",
            price,
            atr,
            supports,
            min_sl_distance,
        )

        if support_price is not None:
            sr_sl = support_price - buffer_sl
            sl = min(sr_sl, classic_sl)

            if abs(price - sl) > atr * MAX_REASONABLE_SL_ATR * vol_factor:
                sl = classic_sl
        else:
            sl = classic_sl

        sr_tp1 = select_level_for_tp(
            "LONG",
            price,
            atr,
            resistances,
            tp1_fallback,
            buffer_tp,
        )

        remaining = [
            x for x in resistances
            if safe_float(x["price"]) > sr_tp1
        ]

sr_tp2 = select_level_for_tp(
            "LONG",
            price,
            atr,
            remaining,
            tp2_fallback,
            buffer_tp,
        )

        if sr_tp2 <= sr_tp1:
            sr_tp2 = price + atr * tp2_fallback

        tp1, tp2 = merge_tp_with_ai_memory(
            "LONG",
            price,
            atr,
            sr_tp1,
            sr_tp2,
            ai_tp,
        )

    else:
        classic_sl = price + min_sl_distance

        resistance_price = select_level_for_sl(
            "SHORT",
            price,
            atr,
            resistances,
            min_sl_distance,
        )

        if resistance_price is not None:
            sr_sl = resistance_price + buffer_sl
            sl = max(sr_sl, classic_sl)

            if abs(price - sl) > atr * MAX_REASONABLE_SL_ATR * vol_factor:
                sl = classic_sl
        else:
            sl = classic_sl

        sr_tp1 = select_level_for_tp(
            "SHORT",
            price,
            atr,
            supports,
            tp1_fallback,
            buffer_tp,
        )

        remaining = [
            x for x in supports
            if safe_float(x["price"]) < sr_tp1
        ]

        sr_tp2 = select_level_for_tp(
            "SHORT",
            price,
            atr,
            remaining,
            tp2_fallback,
            buffer_tp,
        )

        if sr_tp2 >= sr_tp1:
            sr_tp2 = price - atr * tp2_fallback

        tp1, tp2 = merge_tp_with_ai_memory(
            "SHORT",
            price,
            atr,
            sr_tp1,
            sr_tp2,
            ai_tp,
        )

    risk = abs(price - sl)
    reward = abs(tp1 - price)

    rr = round(reward / risk, 2) if risk > 0 else 0

    meta = {
        "volatility_factor": round(vol_factor, 3),
        "ai_tp_used": bool(ai_tp),
        "ai_tp": ai_tp,
        "nearest_support": level_pack.get("nearest_support"),
        "nearest_resistance": level_pack.get("nearest_resistance"),
    }

    return (
        safe_round(sl),
        safe_round(tp1),
        safe_round(tp2),
        rr,
        meta,
    )


# ============================================================
# Market context
# ============================================================

def get_soft_market_context() -> Dict:
    """
    فعلاً ساده و کم‌ریسک:
    BTC 1H/4H جهت کلی بازار را مشخص می‌کند.
    اثر آن سخت نیست؛ فقط در امتیازدهی و AI snapshot استفاده می‌شود.
    """

    try:
        btc_4h = add_indicators(get_klines("BTCUSDT", "4h"))
        btc_1h = add_indicators(get_klines("BTCUSDT", "1h"))
        btc_15m = add_indicators(get_klines("BTCUSDT", "15m"))

        t4 = ema_direction(btc_4h)
        t1 = ema_direction(btc_1h)
        t15 = ema_direction(btc_15m)

        btc_last = btc_15m.iloc[-1]
        btc_bias = "neutral"

        if (
            t4 == "bullish"
            and t1 == "bullish"
            and btc_last["macd"] >= btc_last["macd_signal"]
        ):
            btc_bias = "bullish"

        elif (
            t4 == "bearish"
            and t1 == "bearish"
            and btc_last["macd"] <= btc_last["macd_signal"]
        ):
            btc_bias = "bearish"

        if t4 == "bullish" and t1 == "bullish":
            regime = "bullish"
        elif t4 == "bearish" and t1 == "bearish":
            regime = "bearish"
        else:
            regime = "neutral"

        return {
            "market_regime": regime,
            "btc_bias": btc_bias,
            "btc_4h": t4,
            "btc_1h": t1,
            "btc_15m": t15,
        }

    except Exception:
        return {
            "market_regime": "neutral",
            "btc_bias": "neutral",
        }

# ============================================================
# Final public analysis function
# ============================================================

def analyze_symbol(symbol: str) -> Dict:
    symbol = str(symbol).upper().strip()

    try:
        df_4h = add_indicators(get_klines(symbol, "4h"))
        df_1h = add_indicators(get_klines(symbol, "1h"))
        df_30m = add_indicators(get_klines(symbol, "30m"))
        df_15m = add_indicators(get_klines(symbol, "15m"))
        df_5m = add_indicators(get_klines(symbol, "5m"))

        market_context = get_soft_market_context()

        score_pack = simple_classic_score(
            symbol=symbol,
            df_4h=df_4h,
            df_1h=df_1h,
            df_30m=df_30m,
            df_15m=df_15m,
            df_5m=df_5m,
            market_context=market_context,
        )

        price = safe_float(df_15m.iloc[-1]["close"])
        atr = safe_float(df_15m.iloc[-1]["atr"])

        long_score = int(score_pack.get("long_score", 0))
        short_score = int(score_pack.get("short_score", 0))

        if long_score >= short_score:
            direction = "LONG"
            final_score = long_score
            confirmations = int(score_pack.get("confirmations_long", 0))
            reasons = list(score_pack.get("long_reasons", []))
            valid_direction = bool(score_pack.get("long_valid", False))
        else:
            direction = "SHORT"
            final_score = short_score
            confirmations = int(score_pack.get("confirmations_short", 0))
            reasons = list(score_pack.get("short_reasons", []))
            valid_direction = bool(score_pack.get("short_valid", False))

        snapshot = build_local_snapshot(
            symbol=symbol,
            direction=direction,
            df_4h=df_4h,
            df_1h=df_1h,
            df_30m=df_30m,
            df_15m=df_15m,
            df_5m=df_5m,
            score_pack=score_pack,
            market_context=market_context,
        )

        risk_state = get_coin_risk(symbol, direction)
        strictness_level = int(risk_state.get("strictness_level", 0) or 0)

        if strictness_level >= 1:
            final_score -= strictness_level
            reasons.append(f"AI Risk: سختگیری فعال سطح {strictness_level}")

        rotation_state = get_rotation_context(symbol)
        rotation_score = safe_float(rotation_state.get("rotation_score", 50), 50)

        if rotation_score >= 75:
            final_score += 2
            reasons.append("AI Rotation: وضعیت کوین مناسب است")
        elif rotation_score <= 25:
            final_score -= 2
            reasons.append("AI Rotation: وضعیت کوین ضعیف است")

        extra_rule = ai_extra_strength_required(
            symbol=symbol,
            direction=direction,
            snapshot=snapshot,
        )

        extra_score_required = int(extra_rule.get("extra_score", 0) or 0)
        extra_confirmations_required = int(extra_rule.get("extra_confirmations", 0) or 0)

        if extra_rule.get("required", False):
            reasons.append(
                extra_rule.get("reason") or "AI Learning تایید بیشتر می‌خواهد"
            )

        min_required_score = max(int(MIN_DIRECT_SCORE), AUTO_DIRECT_SCORE_MIN)

        if direction == "LONG":
            min_required_score += LONG_DIRECT_SCORE_BONUS_REQUIREMENT

        min_required_score += extra_score_required

        required_confirmations = int(MIN_MANUAL_CONFIRMATIONS) + extra_confirmations_required

        entry_confirmed = (
            valid_direction
            and final_score >= min_required_score
            and confirmations >= required_confirmations
        )

        level_pack = get_strong_levels(
            df_5m=df_5m,
            df_15m=df_15m,
            df_30m=df_30m,
            price=price,
            atr=atr,
        )

        support = level_pack.get("nearest_support")
        resistance = level_pack.get("nearest_resistance")

if not entry_confirmed:
            return {
                "symbol": symbol,
                "direction": "NO TRADE",
                "status": "NO_TRADE",
                "entry_confirmed": False,
                "entry_mode": "NO_ENTRY",

                "score": cap_score(final_score),
                "long_score": long_score,
                "short_score": short_score,

                "price": safe_round(price),
                "entry": None,
                "stop_loss": None,
                "tp1": None,
                "tp2": None,
                "atr": safe_round(atr),

                "risk_reward": 0,
                "risk_level": "UNKNOWN",

                "market_regime": market_context.get("market_regime", "neutral"),
                "btc_bias": market_context.get("btc_bias", "neutral"),

                "freshness": "LOW",
                "confirmations": confirmations,
                "required_confirmations": required_confirmations,

                "rsi": safe_round(df_15m.iloc[-1]["rsi"], 2),
                "macd": safe_round(df_15m.iloc[-1]["macd"], 6),
                "macd_signal": safe_round(df_15m.iloc[-1]["macd_signal"], 6),
                "macd_hist": safe_round(df_15m.iloc[-1]["macd_hist"], 6),
                "adx": safe_round(df_15m.iloc[-1]["adx"], 2),
                "vwap_status": vwap_status(df_15m),

                "support": safe_round(support),
                "resistance": safe_round(resistance),
                "trends": score_pack.get("trends", {}),

                "distance_ema20_atr": score_pack.get("distance_ema20_atr"),
                "volume_status": score_pack.get("volume_status"),
                "volume_ratio": score_pack.get("volume_ratio"),

                "buy_power": score_pack.get("buy_power"),
                "sell_power": score_pack.get("sell_power"),
                "power2_buy": score_pack.get("power2_buy"),
                "power2_sell": score_pack.get("power2_sell"),
                "power3_buy": score_pack.get("power3_buy"),
                "power3_sell": score_pack.get("power3_sell"),

                "snapshot": snapshot,
                "coin_risk": risk_state,
                "rotation": rotation_state,
                "tp_meta": {},

                "reasons": reasons[:20],
                "signal_timeframe": "AI Classic Direct",
                "validity": "سیگنال معتبر نیست",
            }

        stop_loss, tp1, tp2, rr, tp_meta = build_trade_levels(
            direction=direction,
            price=price,
            atr=atr,
            df_5m=df_5m,
            df_15m=df_15m,
            df_30m=df_30m,
            snapshot=snapshot,
            symbol=symbol,
        )

        if final_score >= 92 and confirmations >= 6:
            risk_level = "LOW"
        elif final_score >= 86 and confirmations >= 5:
            risk_level = "MEDIUM"
        else:
            risk_level = "HIGH"

        if confirmations >= 6:
            freshness = "HIGH"
        elif confirmations >= 5:
            freshness = "MEDIUM"
        else:
            freshness = "LOW"

        if update_ai_summary:
            try:
                update_ai_summary(total_signals=1)
            except Exception:
                pass

        return {
            "symbol": symbol,
            "direction": direction,
            "status": "ACTIVE",
            "entry_confirmed": True,
            "entry_mode": "AI_CLASSIC_DIRECT",

            "score": cap_score(final_score),
            "long_score": long_score,
            "short_score": short_score,

            "price": safe_round(price),
            "entry": safe_round(price),

            "stop_loss": stop_loss,
            "tp1": tp1,
            "tp2": tp2,
            "atr": safe_round(atr),

            "risk_reward": rr,
            "risk_level": risk_level,

            "market_regime": market_context.get("market_regime", "neutral"),
            "btc_bias": market_context.get("btc_bias", "neutral"),

"freshness": freshness,
            "confirmations": confirmations,
            "required_confirmations": required_confirmations,

            "rsi": safe_round(df_15m.iloc[-1]["rsi"], 2),
            "macd": safe_round(df_15m.iloc[-1]["macd"], 6),
            "macd_signal": safe_round(df_15m.iloc[-1]["macd_signal"], 6),
            "macd_hist": safe_round(df_15m.iloc[-1]["macd_hist"], 6),
            "adx": safe_round(df_15m.iloc[-1]["adx"], 2),
            "vwap_status": vwap_status(df_15m),

            "support": safe_round(support),
            "resistance": safe_round(resistance),
            "trends": score_pack.get("trends", {}),

            "distance_ema20_atr": score_pack.get("distance_ema20_atr"),
            "volume_status": score_pack.get("volume_status"),
            "volume_ratio": score_pack.get("volume_ratio"),

            "buy_power": score_pack.get("buy_power"),
            "sell_power": score_pack.get("sell_power"),
            "power2_buy": score_pack.get("power2_buy"),
            "power2_sell": score_pack.get("power2_sell"),
            "power3_buy": score_pack.get("power3_buy"),
            "power3_sell": score_pack.get("power3_sell"),

            "snapshot": snapshot,
            "coin_risk": risk_state,
            "rotation": rotation_state,
            "tp_meta": tp_meta,

            "reasons": reasons[:20],
            "signal_timeframe": "AI Classic Direct",
            "validity": "15 تا 45 دقیقه",
        }

    except Exception as e:
        return {
            "symbol": symbol,
            "direction": "NO TRADE",
            "status": "NO_TRADE",
            "entry_confirmed": False,
            "entry_mode": "ERROR",

            "score": 0,
            "long_score": 0,
            "short_score": 0,

            "price": None,
            "entry": None,
            "stop_loss": None,
            "tp1": None,
            "tp2": None,
            "atr": None,

            "risk_reward": 0,
            "risk_level": "UNKNOWN",

            "market_regime": "unknown",
            "btc_bias": "unknown",

            "freshness": "LOW",
            "confirmations": 0,
            "required_confirmations": 0,

            "rsi": None,
            "macd": None,
            "macd_signal": None,
            "macd_hist": None,
            "adx": None,
            "vwap_status": None,

            "support": None,
            "resistance": None,
            "trends": {},

            "snapshot": {},
            "coin_risk": {},
            "rotation": {},
            "tp_meta": {},

            "reasons": [f"Analysis Error: {str(e)[:200]}"],
            "signal_timeframe": "AI Classic Direct",
            "validity": "سیگنال معتبر نیست",
        }
