# -*- coding: utf-8 -*-
"""
Simple Classic Balanced Soft Engine + Smart TP/SL

هدف این نسخه:
- برگشت به ربات ساده کلاسیک اولیه
- بدون Setup / Watchlist / Pending
- بدون Power2/Power3/Power6 و بدون تاییدهای کندلی سنگین
- تحلیل فقط با EMA / RSI / MACD / MACD Histogram / ADX / VWAP؛ فقط ADX زیر 20 و score زیر 85 رد قطعی هستند
- ورود مستقیم، ساده، قابل دیباگ و نرم‌تر از نسخه خشک
- TP/SL هوشمند با سطوح 5M + 15M + 30M، Strength Score، ATR و پروفایل نوسان هر کوین
- حداقل فاصله SL همیشه ATR × 1.25 است
"""

import math
from typing import Dict, List, Optional, Tuple

import ccxt
import pandas as pd
import ta

from config import MIN_DIRECT_SCORE, MIN_MANUAL_CONFIRMATIONS, MIN_ADX_FOR_TREND


exchange = ccxt.okx({
    "enableRateLimit": True,
    "timeout": 20000,
    "options": {"defaultType": "swap"},
})

# --- Core thresholds ---
AUTO_DIRECT_SCORE_MIN = 82
ADX_HARD_MIN = max(float(MIN_ADX_FOR_TREND), 20.0)

# --- Smart TP/SL constants ---
MIN_SL_ATR_MULTIPLIER = 1.25       # حداقل SL؛ هیچ‌وقت کمتر از این نمی‌شود
TP1_FALLBACK_ATR = 0.75
TP2_FALLBACK_ATR = 1.40
MAX_REASONABLE_SL_ATR = 2.40       # اگر سطح خیلی دور بود، fallback استفاده می‌شود
MIN_TP1_ATR = 0.55                 # TP خیلی نزدیک به نویز نباشد
LEVEL_BUFFER_ATR = 0.14            # TP کمی قبل از سطح
SL_BUFFER_ATR = 0.25               # SL پشت سطح برای جلوگیری از stop hunt

# --- S/R strength weights ---
TF_LEVEL_WEIGHTS = {
    "5M": 1.0,
    "15M": 1.6,
    "30M": 2.2,
}
LEVEL_LOOKBACK = 160
SWING_WINDOW = 3


def to_okx_symbol(symbol: str) -> str:
    coin = str(symbol).upper().replace("USDT", "")
    return f"{coin}/USDT:USDT"


def safe_round(value, digits: int = 8):
    try:
        if value is None:
            return None
        return round(float(value), digits)
    except Exception:
        return None


def cap_score(value) -> int:
    try:
        return max(0, min(100, int(round(float(value)))))
    except Exception:
        return 0


def get_klines(symbol: str, interval: str = "15m", limit: int = 260, include_current: bool = False) -> pd.DataFrame:
    data = exchange.fetch_ohlcv(to_okx_symbol(symbol), timeframe=interval, limit=limit)
    if not data or len(data) < 220:
        raise Exception(f"داده کافی برای {symbol} در تایم {interval} دریافت نشد")

    df = pd.DataFrame(data, columns=["time", "open", "high", "low", "close", "volume"])
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna()
    if not include_current:
        df = df.iloc[:-1]

    if len(df) < 210:
        raise Exception(f"داده کندل کافی برای {symbol} در تایم {interval} کامل نیست")

    return df


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["ema20"] = ta.trend.ema_indicator(df["close"], window=20)
    df["ema50"] = ta.trend.ema_indicator(df["close"], window=50)
    df["ema200"] = ta.trend.ema_indicator(df["close"], window=200)
    df["rsi"] = ta.momentum.rsi(df["close"], window=14)

    macd = ta.trend.MACD(df["close"])
    df["macd"] = macd.macd()
    df["macd_signal"] = macd.macd_signal()
    df["macd_hist"] = macd.macd_diff()

    df["atr"] = ta.volatility.average_true_range(df["high"], df["low"], df["close"], window=14)
    adx = ta.trend.ADXIndicator(df["high"], df["low"], df["close"], window=14)
    df["adx"] = adx.adx()

    typical = (df["high"] + df["low"] + df["close"]) / 3
    volume_sum = df["volume"].cumsum().replace(0, pd.NA)
    df["vwap"] = (typical * df["volume"]).cumsum() / volume_sum
    df["volume_ma20"] = df["volume"].rolling(20).mean()
    df["volume_ratio"] = df["volume"] / df["volume_ma20"].replace(0, pd.NA)

    df = df.dropna()
    if len(df) < 60:
        raise Exception("اندیکاتورها کامل محاسبه نشدند")
    return df


# ---------- Simple classic technical layer ----------
def ema_direction(df: pd.DataFrame) -> str:
    last = df.iloc[-1]
    if last["ema50"] > last["ema200"]:
        return "bullish"
    if last["ema50"] < last["ema200"]:
        return "bearish"
    return "range"




def trend_direction(df: pd.DataFrame) -> str:
    """Compatibility helper for market_scanner.py.
    Keeps the old public function name while the classic engine uses EMA50/EMA200 direction.
    Returns bullish / bearish / range, and weak_* states when price agrees with EMA50/EMA200 but EMA50/EMA200 are not fully aligned.
    """
    last = df.iloc[-1]
    close = float(last["close"])

    if last["ema50"] > last["ema200"]:
        return "bullish" if close > last["ema50"] else "weak_bullish"
    if last["ema50"] < last["ema200"]:
        return "bearish" if close < last["ema50"] else "weak_bearish"
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
    price = float(last["close"])
    atr = max(float(last["atr"]), price * 0.0015)
    return abs(price - float(last["ema20"])) / atr


def volume_quality(df: pd.DataFrame) -> Tuple[str, float]:
    last = df.iloc[-1]
    try:
        ratio = float(last["volume_ratio"])
    except Exception:
        ratio = 1.0

    if ratio >= 1.35:
        return "high_volume", ratio
    if ratio >= 0.90:
        return "normal_volume", ratio
    if ratio <= 0.65:
        return "weak_volume", ratio
    return "neutral_volume", ratio


def buy_sell_power(df: pd.DataFrame, candles: int = 20) -> Tuple[float, float]:
    """فقط برای نمایش خروجی؛ در ورود دخالت داده نمی‌شود."""
    recent = df.tail(candles)
    green = recent[recent["close"] > recent["open"]]["volume"].sum()
    red = recent[recent["close"] < recent["open"]]["volume"].sum()
    total = green + red
    if total <= 0:
        return 50.0, 50.0
    return round((green / total) * 100, 1), round((red / total) * 100, 1)


def simple_classic_score(symbol: str, df_4h: pd.DataFrame, df_1h: pd.DataFrame, df_30m: pd.DataFrame, df_15m: pd.DataFrame, df_5m: pd.DataFrame) -> Dict:
    long_score = 0
    short_score = 0
    long_reasons: List[str] = []
    short_reasons: List[str] = []
    confirmations_long = 0
    confirmations_short = 0

    trends = {
        "4H": ema_direction(df_4h),
        "1H": ema_direction(df_1h),
        "30M": ema_direction(df_30m),
        "15M": ema_direction(df_15m),
        "5M": ema_direction(df_5m),  # نمایشی/کمکی؛ موتور ورود نیست
    }

    last_4h = df_4h.iloc[-1]
    last_1h = df_1h.iloc[-1]
    last_30 = df_30m.iloc[-1]
    last_15 = df_15m.iloc[-1]
    prev_15 = df_15m.iloc[-2]
    last_5 = df_5m.iloc[-1]

    adx_15 = float(last_15["adx"])
    dist_15 = distance_from_ema20_atr(df_15m)
    vol_status, vol_ratio = volume_quality(df_15m)

    # 1) Direction: EMA50/EMA200 on 4H and 1H
    if trends["4H"] == "bullish":
        long_score += 12
        confirmations_long += 1
        long_reasons.append("4H: EMA50 بالای EMA200؛ جهت کلی صعودی")
    elif trends["4H"] == "bearish":
        short_score += 12
        confirmations_short += 1
        short_reasons.append("4H: EMA50 پایین EMA200؛ جهت کلی نزولی")

    if trends["1H"] == "bullish":
        long_score += 20
        confirmations_long += 1
        long_reasons.append("1H: EMA50 بالای EMA200؛ جهت اصلی لانگ")
    elif trends["1H"] == "bearish":
        short_score += 20
        confirmations_short += 1
        short_reasons.append("1H: EMA50 پایین EMA200؛ جهت اصلی شورت")

    # 30M as medium confirmation
    if trends["30M"] == "bullish":
        long_score += 10
        confirmations_long += 1
        long_reasons.append("30M: جهت میان‌مدت لانگ را تایید می‌کند")
    elif trends["30M"] == "bearish":
        short_score += 10
        confirmations_short += 1
        short_reasons.append("30M: جهت میان‌مدت شورت را تایید می‌کند")

    # 2) Momentum: RSI 15M and 30M
    if 52 <= float(last_15["rsi"]) <= 68:
        long_score += 12
        confirmations_long += 1
        long_reasons.append("15M: RSI مومنتوم لانگ مناسب دارد")
    elif 32 <= float(last_15["rsi"]) <= 48:
        short_score += 12
        confirmations_short += 1
        short_reasons.append("15M: RSI مومنتوم شورت مناسب دارد")

    if float(last_15["rsi"]) > float(prev_15["rsi"]):
        long_score += 4
        long_reasons.append("15M: شیب RSI رو به بالا است")
    elif float(last_15["rsi"]) < float(prev_15["rsi"]):
        short_score += 4
        short_reasons.append("15M: شیب RSI رو به پایین است")

    if float(last_30["rsi"]) >= 50:
        long_score += 4
        long_reasons.append("30M: RSI بالای 50 است")
    elif float(last_30["rsi"]) <= 50:
        short_score += 4
        short_reasons.append("30M: RSI پایین 50 است")

    # 3) Trend confirmation: MACD + Histogram on 15M / 30M
    if last_15["macd"] > last_15["macd_signal"]:
        long_score += 14
        confirmations_long += 1
        long_reasons.append("15M: MACD لانگ را تایید می‌کند")
    elif last_15["macd"] < last_15["macd_signal"]:
        short_score += 14
        confirmations_short += 1
        short_reasons.append("15M: MACD شورت را تایید می‌کند")

    if last_15["macd_hist"] > prev_15["macd_hist"]:
        long_score += 8
        confirmations_long += 1
        long_reasons.append("15M: MACD Histogram در حال تقویت صعودی است")
    elif last_15["macd_hist"] < prev_15["macd_hist"]:
        short_score += 8
        confirmations_short += 1
        short_reasons.append("15M: MACD Histogram در حال تقویت نزولی است")

    if last_30["macd"] > last_30["macd_signal"]:
        long_score += 7
        long_reasons.append("30M: MACD با لانگ هم‌جهت است")
    elif last_30["macd"] < last_30["macd_signal"]:
        short_score += 7
        short_reasons.append("30M: MACD با شورت هم‌جهت است")

    # 4) Entry position: EMA20 + distance from EMA20
    if last_15["close"] > last_15["ema20"]:
        long_score += 12
        confirmations_long += 1
        long_reasons.append("15M: قیمت بالای EMA20؛ موقعیت ورود لانگ")
    elif last_15["close"] < last_15["ema20"]:
        short_score += 12
        confirmations_short += 1
        short_reasons.append("15M: قیمت پایین EMA20؛ موقعیت ورود شورت")

    # جلوگیری از ورود دیر: نرم و امتیازی، نه فیلتر خشک
    if dist_15 <= 0.85:
        long_score += 6
        short_score += 6
        long_reasons.append(f"فاصله از EMA20 مناسب است: {round(dist_15, 2)} ATR")
        short_reasons.append(f"فاصله از EMA20 مناسب است: {round(dist_15, 2)} ATR")
    elif dist_15 <= 1.25:
        long_score -= 3.6
        short_score -= 3.6
        long_reasons.append(f"فاصله از EMA20 کمی زیاد است: {round(dist_15, 2)} ATR")
        short_reasons.append(f"فاصله از EMA20 کمی زیاد است: {round(dist_15, 2)} ATR")
    elif dist_15 <= 1.60:
        long_score -= 9
        short_score -= 9
        long_reasons.append(f"فاصله از EMA20 زیاد است؛ جریمه ورود دیر: {round(dist_15, 2)} ATR")
        short_reasons.append(f"فاصله از EMA20 زیاد است؛ جریمه ورود دیر: {round(dist_15, 2)} ATR")
    else:
        long_score -= 16.2
        short_score -= 16.2
        long_reasons.append(f"فاصله از EMA20 خیلی زیاد است؛ جریمه سنگین: {round(dist_15, 2)} ATR")
        short_reasons.append(f"فاصله از EMA20 خیلی زیاد است؛ جریمه سنگین: {round(dist_15, 2)} ATR")

    # 5) Final confirmation: ADX + VWAP
    # ADX تنها فیلتر سخت این لایه است؛ بالای 20 به صورت پله‌ای امتیاز می‌دهد.
    if adx_15 >= 35:
        long_score += 15
        short_score += 15
        long_reasons.append("ADX 15M بالای 35؛ قدرت روند قوی")
        short_reasons.append("ADX 15M بالای 35؛ قدرت روند قوی")
    elif adx_15 >= 25:
        long_score += 10
        short_score += 10
        long_reasons.append("ADX 15M بالای 25؛ قدرت روند خوب")
        short_reasons.append("ADX 15M بالای 25؛ قدرت روند خوب")
    elif adx_15 >= ADX_HARD_MIN:
        long_score += 5
        short_score += 5
        long_reasons.append("ADX 15M بالای 20؛ قدرت روند قابل قبول")
        short_reasons.append("ADX 15M بالای 20؛ قدرت روند قابل قبول")
    else:
        long_score = min(long_score, 69)
        short_score = min(short_score, 69)
        long_reasons.append("رد: ADX 15M زیر 20 است")
        short_reasons.append("رد: ADX 15M زیر 20 است")

    if last_15["close"] > last_15["vwap"]:
        long_score += 8
        confirmations_long += 1
        long_reasons.append("15M: قیمت بالای VWAP؛ تایید نهایی لانگ")
        short_score -= 2.7
    elif last_15["close"] < last_15["vwap"]:
        short_score += 8
        confirmations_short += 1
        short_reasons.append("15M: قیمت پایین VWAP؛ تایید نهایی شورت")
        long_score -= 2.7

    # Volume is soft and simple
    if vol_status == "high_volume":
        long_score += 4
        short_score += 4
        long_reasons.append(f"حجم 15M قوی است؛ نسبت حجم {round(vol_ratio, 2)}")
        short_reasons.append(f"حجم 15M قوی است؛ نسبت حجم {round(vol_ratio, 2)}")
    elif vol_status == "weak_volume":
        long_score -= 2.7
        short_score -= 2.7
        long_reasons.append("حجم 15M ضعیف است؛ امتیاز محافظه‌کار شد")
        short_reasons.append("حجم 15M ضعیف است؛ امتیاز محافظه‌کار شد")

    # Validity: خشک نیست. فقط ADX زیر 20 رد کامل می‌شود؛ بقیه لایه‌ها با امتیاز تصمیم می‌گیرند.
    long_valid = adx_15 >= ADX_HARD_MIN
    short_valid = adx_15 >= ADX_HARD_MIN

    buy2, sell2 = buy_sell_power(df_5m, 2)
    buy3, sell3 = buy_sell_power(df_5m, 3)
    buy20, sell20 = buy_sell_power(df_5m, 20)

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
    }


# ---------- Smart TP/SL ----------
def find_swing_levels(df: pd.DataFrame, timeframe: str, lookback: int = LEVEL_LOOKBACK, window: int = SWING_WINDOW) -> List[Dict]:
    recent = df.tail(lookback).copy()
    if len(recent) < window * 2 + 10:
        return []

    levels: List[Dict] = []
    tf_weight = TF_LEVEL_WEIGHTS.get(timeframe, 1.0)

    for i in range(window, len(recent) - window):
        row = recent.iloc[i]
        left = recent.iloc[i - window:i]
        right = recent.iloc[i + 1:i + 1 + window]

        is_low = row["low"] <= left["low"].min() and row["low"] <= right["low"].min()
        is_high = row["high"] >= left["high"].max() and row["high"] >= right["high"].max()

        recency_score = 1.0 + (i / max(len(recent), 1)) * 0.8
        if is_low:
            levels.append({
                "price": float(row["low"]),
                "kind": "support",
                "timeframe": timeframe,
                "strength": tf_weight * recency_score,
            })
        if is_high:
            levels.append({
                "price": float(row["high"]),
                "kind": "resistance",
                "timeframe": timeframe,
                "strength": tf_weight * recency_score,
            })

    return levels


def cluster_levels(raw_levels: List[Dict], price: float, atr: float) -> List[Dict]:
    if not raw_levels:
        return []

    merge_distance = max(atr * 0.25, price * 0.0010)
    raw_levels = sorted(raw_levels, key=lambda x: x["price"])
    clusters: List[List[Dict]] = []

    for level in raw_levels:
        if not clusters or abs(level["price"] - clusters[-1][-1]["price"]) > merge_distance:
            clusters.append([level])
        else:
            clusters[-1].append(level)

    merged = []
    for group in clusters:
        total_strength = sum(float(x["strength"]) for x in group)
        weighted_price = sum(float(x["price"]) * float(x["strength"]) for x in group) / max(total_strength, 1e-9)
        timeframes = sorted(set(x["timeframe"] for x in group))
        kind_counts = {"support": 0, "resistance": 0}
        for x in group:
            kind_counts[x["kind"]] += 1
        kind = "support" if kind_counts["support"] >= kind_counts["resistance"] else "resistance"
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


def get_strong_levels(df_5m: pd.DataFrame, df_15m: pd.DataFrame, df_30m: pd.DataFrame, price: float, atr: float) -> Dict:
    raw = []
    raw.extend(find_swing_levels(df_5m, "5M"))
    raw.extend(find_swing_levels(df_15m, "15M"))
    raw.extend(find_swing_levels(df_30m, "30M"))

    clustered = cluster_levels(raw, price, atr)
    supports = [x for x in clustered if x["price"] < price]
    resistances = [x for x in clustered if x["price"] > price]

    # قوی‌ترین سطح معتبر نزدیک، نه صرفاً نزدیک‌ترین نویز 5M
    supports = sorted(supports, key=lambda x: (x["strength"], -abs(price - x["price"])), reverse=True)
    resistances = sorted(resistances, key=lambda x: (x["strength"], -abs(x["price"] - price)), reverse=True)

    nearest_support = max([x["price"] for x in supports], default=price - atr * MIN_SL_ATR_MULTIPLIER)
    nearest_resistance = min([x["price"] for x in resistances], default=price + atr * TP1_FALLBACK_ATR)

    return {
        "supports": supports,
        "resistances": resistances,
        "nearest_support": nearest_support,
        "nearest_resistance": nearest_resistance,
    }


def coin_volatility_factor(df_15m: pd.DataFrame, price: float) -> float:
    """پروفایل ساده نوسان خود کوین؛ فقط برای TP/SL، نه ورود."""
    try:
        atr_pct = float(df_15m.iloc[-1]["atr"]) / max(float(price), 1e-12)
        recent = df_15m.tail(96)
        avg_range_pct = ((recent["high"] - recent["low"]) / recent["close"].replace(0, pd.NA)).mean()
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


def select_level_for_sl(direction: str, price: float, atr: float, levels: List[Dict], base_distance: float) -> Optional[float]:
    max_distance = atr * MAX_REASONABLE_SL_ATR
    valid = []
    for level in levels:
        distance = abs(price - float(level["price"]))
        if base_distance * 0.45 <= distance <= max_distance:
            valid.append(level)
    if not valid:
        return None
    valid.sort(key=lambda x: x["strength"], reverse=True)
    return float(valid[0]["price"])


def select_level_for_tp(direction: str, price: float, atr: float, levels: List[Dict], fallback_mult: float, buffer: float) -> float:
    min_distance = atr * MIN_TP1_ATR
    max_distance = atr * 3.0
    candidates = []

    for level in levels:
        level_price = float(level["price"])
        target = level_price - buffer if direction == "LONG" else level_price + buffer
        distance = abs(target - price)
        if direction == "LONG" and target <= price:
            continue
        if direction == "SHORT" and target >= price:
            continue
        if min_distance <= distance <= max_distance:
            candidates.append((level["strength"], -distance, target))

    if candidates:
        candidates.sort(reverse=True)
        return float(candidates[0][2])

    return price + atr * fallback_mult if direction == "LONG" else price - atr * fallback_mult


def build_trade_levels(direction: str, price: float, atr: float, df_5m: pd.DataFrame, df_15m: pd.DataFrame, df_30m: pd.DataFrame) -> Tuple[float, float, float, float]:
    price = float(price)
    atr = max(float(atr or 0), price * 0.0015)
    vol_factor = coin_volatility_factor(df_15m, price)

    min_sl_distance = atr * MIN_SL_ATR_MULTIPLIER * vol_factor
    tp1_fallback = TP1_FALLBACK_ATR * vol_factor
    tp2_fallback = TP2_FALLBACK_ATR * vol_factor

    buffer_tp = max(atr * LEVEL_BUFFER_ATR * vol_factor, price * 0.0007)
    buffer_sl = max(atr * SL_BUFFER_ATR * vol_factor, price * 0.0010)

    level_pack = get_strong_levels(df_5m, df_15m, df_30m, price, atr)
    supports = level_pack["supports"]
    resistances = level_pack["resistances"]

    if direction == "LONG":
        classic_sl = price - min_sl_distance
        support_price = select_level_for_sl("LONG", price, atr, supports, min_sl_distance)
        if support_price is not None:
            sr_sl = support_price - buffer_sl
            # SL هرگز کمتر از 1.25 ATR فاصله نمی‌گیرد؛ اگر سطح دورتر بود از سطح استفاده می‌شود
            sl = min(sr_sl, classic_sl)
            if abs(price - sl) > atr * MAX_REASONABLE_SL_ATR * vol_factor:
                sl = classic_sl
        else:
            sl = classic_sl

        tp1 = select_level_for_tp("LONG", price, atr, resistances, tp1_fallback, buffer_tp)
        remaining = [x for x in resistances if float(x["price"]) > tp1]
        tp2 = select_level_for_tp("LONG", price, atr, remaining, tp2_fallback, buffer_tp)
        if tp2 <= tp1:
            tp2 = price + atr * tp2_fallback

    else:
        classic_sl = price + min_sl_distance
        resistance_price = select_level_for_sl("SHORT", price, atr, resistances, min_sl_distance)
        if resistance_price is not None:
            sr_sl = resistance_price + buffer_sl
            # SL هرگز کمتر از 1.25 ATR فاصله نمی‌گیرد؛ اگر سطح دورتر بود از سطح استفاده می‌شود
            sl = max(sr_sl, classic_sl)
            if abs(price - sl) > atr * MAX_REASONABLE_SL_ATR * vol_factor:
                sl = classic_sl
        else:
            sl = classic_sl

        tp1 = select_level_for_tp("SHORT", price, atr, supports, tp1_fallback, buffer_tp)
        remaining = [x for x in supports if float(x["price"]) < tp1]
        tp2 = select_level_for_tp("SHORT", price, atr, remaining, tp2_fallback, buffer_tp)
        if tp2 >= tp1:
            tp2 = price - atr * tp2_fallback

    risk = abs(price - sl)
    reward = abs(tp1 - price)
    rr = round(reward / risk, 2) if risk > 0 else 0
    return safe_round(sl), safe_round(tp1), safe_round(tp2), rr


# ---------- Public analysis function ----------
def analyze_symbol(symbol: str) -> Dict:
    symbol = str(symbol).upper().strip()

    try:
        df_4h = add_indicators(get_klines(symbol, "4h"))
        df_1h = add_indicators(get_klines(symbol, "1h"))
        df_30m = add_indicators(get_klines(symbol, "30m"))
        df_15m = add_indicators(get_klines(symbol, "15m"))
        df_5m = add_indicators(get_klines(symbol, "5m"))

        score = simple_classic_score(symbol, df_4h, df_1h, df_30m, df_15m, df_5m)

        price = float(df_15m.iloc[-1]["close"])
        atr = float(df_15m.iloc[-1]["atr"])
        strong_levels = get_strong_levels(df_5m, df_15m, df_30m, price, atr)
        support = strong_levels["nearest_support"]
        resistance = strong_levels["nearest_resistance"]
        vwap = vwap_status(df_15m)

        long_score = score["long_score"]
        short_score = score["short_score"]
        edge = abs(long_score - short_score)

        if long_score >= short_score:
            direction = "LONG"
            final_score = long_score
            confirmations = score["confirmations_long"]
            reasons = score["long_reasons"]
            valid_direction = score.get("long_valid", False)
        else:
            direction = "SHORT"
            final_score = short_score
            confirmations = score["confirmations_short"]
            reasons = score["short_reasons"]
            valid_direction = score.get("short_valid", False)

        min_required_score = max(int(MIN_DIRECT_SCORE), AUTO_DIRECT_SCORE_MIN)

        if (
            not valid_direction
            or final_score < min_required_score
        ):
            direction = "NO TRADE"
            entry_confirmed = False
            entry_mode = "NO_ENTRY"
            stop_loss = tp1 = tp2 = None
            rr = 0
            risk_level = "نامشخص"
            freshness = "LOW"
        else:
            entry_confirmed = True
            entry_mode = "CLASSIC_TECHNICAL"
            stop_loss, tp1, tp2, rr = build_trade_levels(direction, price, atr, df_5m, df_15m, df_30m)

            if final_score >= 92 and confirmations >= 6:
                risk_level = "LOW"
            elif final_score >= 85 and confirmations >= 5:
                risk_level = "MEDIUM"
            else:
                risk_level = "HIGH"

            freshness = "HIGH" if confirmations >= 6 else "MEDIUM" if confirmations >= 5 else "LOW"

        return {
            "symbol": symbol,
            "direction": direction,
            "score": cap_score(final_score),
            "long_score": long_score,
            "short_score": short_score,
            "entry_mode": entry_mode,
            "entry_confirmed": entry_confirmed,
            "status": "ACTIVE" if entry_confirmed else "NO_TRADE",
            "price": safe_round(price),
            "entry": safe_round(price),
            "stop_loss": stop_loss,
            "tp1": tp1,
            "tp2": tp2,
            "atr": safe_round(atr),
            "risk_reward": rr,
            "risk_level": risk_level,
            "freshness": freshness,
            "confirmations": confirmations,
            "rsi": safe_round(df_15m.iloc[-1]["rsi"], 2),
            "macd": safe_round(df_15m.iloc[-1]["macd"], 6),
            "macd_signal": safe_round(df_15m.iloc[-1]["macd_signal"], 6),
            "macd_hist": safe_round(df_15m.iloc[-1]["macd_hist"], 6),
            "adx": safe_round(df_15m.iloc[-1]["adx"], 2),
            "vwap_status": vwap,
            "support": safe_round(support),
            "resistance": safe_round(resistance),
            "sr_timeframe": "5M/15M/30M Strength Score",
            "trends": score["trends"],
            "distance_ema20_atr": score["distance_ema20_atr"],
            "volume_status": score["volume_status"],
            "volume_ratio": score["volume_ratio"],
            # Power values are returned only for display/backward compatibility. They do not create entries.
            "power2_buy": score["power2_buy"],
            "power2_sell": score["power2_sell"],
            "power3_buy": score["power3_buy"],
            "power3_sell": score["power3_sell"],
            "buy_power": score["buy_power"],
            "sell_power": score["sell_power"],
            "reasons": reasons[:12],
            "signal_timeframe": "Simple Classic 15M/30M + Smart TP/SL",
            "validity": "15 تا 45 دقیقه" if entry_confirmed else "سیگنال معتبر نیست",
        }

    except Exception as e:
        return {
            "symbol": symbol,
            "direction": "NO TRADE",
            "score": 0,
            "entry_mode": "ERROR",
            "entry_confirmed": False,
            "status": "NO_TRADE",
            "price": None,
            "entry": None,
            "stop_loss": None,
            "tp1": None,
            "tp2": None,
            "risk_level": "نامشخص",
            "risk_reward": 0,
            "freshness": "LOW",
            "confirmations": 0,
            "rsi": None,
            "macd": None,
            "macd_signal": None,
            "macd_hist": None,
            "adx": None,
            "vwap_status": None,
            "support": None,
            "resistance": None,
            "trends": {},
            "power2_buy": None,
            "power2_sell": None,
            "power3_buy": None,
            "power3_sell": None,
            "buy_power": None,
            "sell_power": None,
            "reasons": [f"خطا در تحلیل: {str(e)[:160]}"],
            "signal_timeframe": "بدون تایم‌فریم ورود",
            "validity": "سیگنال معتبر نیست",
        }
