# -*- coding: utf-8 -*-
import time
import os
import ccxt
import pandas as pd
import ta

from market_sentiment import get_market_sentiment
from trend_analysis import detect_trendline, detect_breakout, trendline_score, breakout_score
from market_structure import detect_market_structure, structure_score

try:
    from config import (
        TECHNICAL_QUALITY_LATE_ENTRY_ATR,
        TECHNICAL_QUALITY_MIN_TP_SPACE_ATR,
        TECHNICAL_QUALITY_LOW_ATR_PCT,
        TECHNICAL_QUALITY_EXTREME_ATR_PCT,
        SR_ENTRY_NEAR_ATR,
        SR_ENTRY_REJECTION_WICK_RATIO,
        SR_ENTRY_MIN_SCORE_BLOCK,
        BOLLINGER_SQUEEZE_WIDTH_PCT,
        BOLLINGER_EXTENSION_ATR,
    )
except Exception:
    TECHNICAL_QUALITY_LATE_ENTRY_ATR = 1.65
    TECHNICAL_QUALITY_MIN_TP_SPACE_ATR = 0.95
    TECHNICAL_QUALITY_LOW_ATR_PCT = 0.08
    TECHNICAL_QUALITY_EXTREME_ATR_PCT = 3.5
    SR_ENTRY_NEAR_ATR = 0.85
    SR_ENTRY_REJECTION_WICK_RATIO = 1.45
    SR_ENTRY_MIN_SCORE_BLOCK = 88
    BOLLINGER_SQUEEZE_WIDTH_PCT = 1.2
    BOLLINGER_EXTENSION_ATR = 0.9


exchange = ccxt.okx({
    "enableRateLimit": True,
    "options": {"defaultType": "swap"}
})


def to_okx_symbol(symbol):
    coin = symbol.replace("USDT", "")
    return f"{coin}/USDT:USDT"


def cap_score(value):
    return max(0, min(int(value), 100))


def safe_round(value, digits=8):
    if value is None:
        return None
    try:
        return round(float(value), digits)
    except Exception:
        return None


def get_klines(symbol, interval="15m", limit=320):
    ohlcv = exchange.fetch_ohlcv(to_okx_symbol(symbol), timeframe=interval, limit=limit)

    if not ohlcv or len(ohlcv) < 220:
        raise Exception(f"داده کافی برای {symbol} در تایم {interval} دریافت نشد")

    df = pd.DataFrame(
        ohlcv,
        columns=["time", "open", "high", "low", "close", "volume"]
    )

    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna()
    df = df.iloc[:-1]

    return df


def add_indicators(df):
    df = df.copy()

    df["ema20"] = ta.trend.ema_indicator(df["close"], window=20)
    df["ema50"] = ta.trend.ema_indicator(df["close"], window=50)
    df["ema200"] = ta.trend.ema_indicator(df["close"], window=200)

    df["rsi"] = ta.momentum.rsi(df["close"], window=14)

    macd = ta.trend.MACD(df["close"])
    df["macd"] = macd.macd()
    df["macd_signal"] = macd.macd_signal()
    df["macd_hist"] = macd.macd_diff()

    df["atr"] = ta.volatility.average_true_range(
        df["high"], df["low"], df["close"], window=14
    )

    adx = ta.trend.ADXIndicator(
        high=df["high"],
        low=df["low"],
        close=df["close"],
        window=14
    )
    df["adx"] = adx.adx()

    df["volume_ma20"] = df["volume"].rolling(20).mean()
    df["atr_ma50"] = df["atr"].rolling(50).mean()

    bollinger = ta.volatility.BollingerBands(close=df["close"], window=20, window_dev=2)
    df["bb_high"] = bollinger.bollinger_hband()
    df["bb_low"] = bollinger.bollinger_lband()
    df["bb_mid"] = bollinger.bollinger_mavg()
    df["bb_width"] = ((df["bb_high"] - df["bb_low"]) / df["bb_mid"].replace(0, pd.NA)) * 100

    typical_price = (df["high"] + df["low"] + df["close"]) / 3
    df["vwap"] = (typical_price * df["volume"]).cumsum() / df["volume"].cumsum()

    df = df.dropna()

    if len(df) < 80:
        raise Exception("اندیکاتورها کامل محاسبه نشدند")

    return df


def get_funding_rate(symbol):
    try:
        data = exchange.fetch_funding_rate(to_okx_symbol(symbol))
        rate = data.get("fundingRate")
        if rate is None:
            return None
        return round(float(rate) * 100, 5)
    except Exception:
        return None


def get_open_interest(symbol):
    try:
        data = exchange.fetch_open_interest(to_okx_symbol(symbol))
        value = data.get("openInterestAmount") or data.get("openInterestValue")
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def get_spread_percent(symbol):
    try:
        orderbook = exchange.fetch_order_book(to_okx_symbol(symbol), limit=5)

        if not orderbook.get("bids") or not orderbook.get("asks"):
            return None

        bid = orderbook["bids"][0][0]
        ask = orderbook["asks"][0][0]

        if not bid or not ask:
            return None

        mid = (bid + ask) / 2
        return round(((ask - bid) / mid) * 100, 4)

    except Exception:
        return None


def trend_direction(df):
    last = df.iloc[-1]

    if last["close"] > last["ema20"] > last["ema50"] > last["ema200"]:
        return "bullish"

    if last["close"] < last["ema20"] < last["ema50"] < last["ema200"]:
        return "bearish"

    if last["close"] > last["ema200"]:
        return "weak_bullish"

    if last["close"] < last["ema200"]:
        return "weak_bearish"

    return "range"


def buy_sell_power(df):
    recent = df.tail(20)

    green_volume = recent[recent["close"] > recent["open"]]["volume"].sum()
    red_volume = recent[recent["close"] < recent["open"]]["volume"].sum()
    total = green_volume + red_volume

    if total == 0:
        return 50, 50

    buy_power = round((green_volume / total) * 100, 1)
    sell_power = round((red_volume / total) * 100, 1)

    return buy_power, sell_power


def support_resistance(df):
    return support_resistance_swing(df)


def is_near_resistance(price, resistance, atr):
    return (resistance - price) <= atr * 0.9


def is_near_support(price, support, atr):
    return (price - support) <= atr * 0.9


def is_middle_of_range(price, support, resistance):
    if resistance <= support:
        return False

    pos = (price - support) / (resistance - support)
    return 0.38 <= pos <= 0.62


def candle_pattern(df):
    last = df.iloc[-1]
    prev = df.iloc[-2]

    body = abs(last["close"] - last["open"])
    candle_range = last["high"] - last["low"]

    if candle_range == 0:
        return "weak"

    upper_wick = last["high"] - max(last["close"], last["open"])
    lower_wick = min(last["close"], last["open"]) - last["low"]

    if last["close"] > last["open"] and prev["close"] < prev["open"]:
        if last["close"] > prev["open"] and last["open"] < prev["close"]:
            return "bullish_engulfing"

    if last["close"] < last["open"] and prev["close"] > prev["open"]:
        if last["close"] < prev["open"] and last["open"] > prev["close"]:
            return "bearish_engulfing"

    if lower_wick > body * 2.2 and upper_wick < body * 1.2:
        return "bullish_pinbar"

    if upper_wick > body * 2.2 and lower_wick < body * 1.2:
        return "bearish_pinbar"

    if body / candle_range >= 0.6:
        if last["close"] > last["open"]:
            return "bullish_strong"
        return "bearish_strong"

    return "weak"


def multi_candle_confirmation(df):
    recent = df.tail(3)

    bullish = len(recent[recent["close"] > recent["open"]])
    bearish = len(recent[recent["close"] < recent["open"]])

    if bullish >= 2 and recent.iloc[-1]["close"] > recent.iloc[-2]["close"]:
        return "bullish"

    if bearish >= 2 and recent.iloc[-1]["close"] < recent.iloc[-2]["close"]:
        return "bearish"

    return "neutral"


def volume_spike(df):
    last = df.iloc[-1]

    if last["volume_ma20"] == 0:
        return False

    return last["volume"] > last["volume_ma20"] * 1.5


def atr_compression(df):
    last = df.iloc[-1]

    if last["atr_ma50"] == 0:
        return False

    return last["atr"] < last["atr_ma50"] * 0.65


def minimum_volatility_ok(df):
    last = df.iloc[-1]

    if last["close"] == 0:
        return False

    atr_percent = (last["atr"] / last["close"]) * 100
    return atr_percent >= 0.08


def market_is_choppy(df_15m, df_5m):
    last_15 = df_15m.iloc[-1]
    last_5 = df_5m.iloc[-1]

    ema_gap_15 = abs(last_15["ema20"] - last_15["ema50"]) / last_15["close"] * 100
    ema_gap_5 = abs(last_5["ema20"] - last_5["ema50"]) / last_5["close"] * 100

    if ema_gap_15 < 0.08 and ema_gap_5 < 0.08:
        return True

    if last_15["adx"] < 18 and last_5["adx"] < 18:
        return True

    if atr_compression(df_15m) and atr_compression(df_5m):
        return True

    return False


def detect_liquidity_grab(df):
    recent = df.tail(20)
    last = df.iloc[-1]

    prev_high = recent.iloc[:-1]["high"].max()
    prev_low = recent.iloc[:-1]["low"].min()

    if last["high"] > prev_high and last["close"] < prev_high:
        return "bearish_liquidity_grab"

    if last["low"] < prev_low and last["close"] > prev_low:
        return "bullish_liquidity_grab"

    return "none"


def detect_stop_hunt(df):
    recent = df.tail(30)
    last = df.iloc[-1]

    prev_high = recent.iloc[:-1]["high"].max()
    prev_low = recent.iloc[:-1]["low"].min()

    candle_range = last["high"] - last["low"]

    if candle_range == 0:
        return "none"

    upper_wick = last["high"] - max(last["open"], last["close"])
    lower_wick = min(last["open"], last["close"]) - last["low"]

    if last["high"] > prev_high and upper_wick / candle_range > 0.45:
        return "bearish_stop_hunt"

    if last["low"] < prev_low and lower_wick / candle_range > 0.45:
        return "bullish_stop_hunt"

    return "none"


def detect_fvg(df):
    if len(df) < 5:
        return "none"

    c1 = df.iloc[-3]
    c3 = df.iloc[-1]

    if c1["high"] < c3["low"]:
        return "bullish_fvg"

    if c1["low"] > c3["high"]:
        return "bearish_fvg"

    return "none"


def detect_order_block(df):
    recent = df.tail(16)

    for i in range(len(recent) - 3, 2, -1):
        candle = recent.iloc[i]
        next_candle = recent.iloc[i + 1]

        if candle["close"] < candle["open"] and next_candle["close"] > next_candle["open"]:
            if next_candle["close"] > candle["high"]:
                return "bullish_order_block"

        if candle["close"] > candle["open"] and next_candle["close"] < next_candle["open"]:
            if next_candle["close"] < candle["low"]:
                return "bearish_order_block"

    return "none"


def detect_rsi_divergence(df):
    recent = df.tail(35)

    lows = recent.nsmallest(2, "low").sort_index()
    highs = recent.nlargest(2, "high").sort_index()

    if len(lows) == 2:
        first = lows.iloc[0]
        second = lows.iloc[1]

        if second["low"] < first["low"] and second["rsi"] > first["rsi"]:
            return "bullish_rsi_divergence"

    if len(highs) == 2:
        first = highs.iloc[0]
        second = highs.iloc[1]

        if second["high"] > first["high"] and second["rsi"] < first["rsi"]:
            return "bearish_rsi_divergence"

    return "none"


def detect_macd_divergence(df):
    recent = df.tail(35)

    lows = recent.nsmallest(2, "low").sort_index()
    highs = recent.nlargest(2, "high").sort_index()

    if len(lows) == 2:
        first = lows.iloc[0]
        second = lows.iloc[1]

        if second["low"] < first["low"] and second["macd_hist"] > first["macd_hist"]:
            return "bullish_macd_divergence"

    if len(highs) == 2:
        first = highs.iloc[0]
        second = highs.iloc[1]

        if second["high"] > first["high"] and second["macd_hist"] < first["macd_hist"]:
            return "bearish_macd_divergence"

    return "none"


def detect_fake_breakout(df):
    recent = df.tail(25)
    last = df.iloc[-1]

    prev_high = recent.iloc[:-1]["high"].max()
    prev_low = recent.iloc[:-1]["low"].min()

    if last["high"] > prev_high and last["close"] < prev_high:
        return "fake_bullish_breakout"

    if last["low"] < prev_low and last["close"] > prev_low:
        return "fake_bearish_breakout"

    return "none"


def detect_trend_exhaustion(df):
    last = df.iloc[-1]
    prev = df.iloc[-2]

    if last["rsi"] > 74 and last["macd_hist"] < prev["macd_hist"]:
        return "bullish_exhaustion"

    if last["rsi"] < 26 and last["macd_hist"] > prev["macd_hist"]:
        return "bearish_exhaustion"

    return "none"


def calculate_vwap_status(df):
    last = df.iloc[-1]

    if last["close"] > last["vwap"]:
        return "above_vwap"

    if last["close"] < last["vwap"]:
        return "below_vwap"

    return "near_vwap"


def calculate_volume_profile(df):
    recent = df.tail(120).copy()

    try:
        recent["price_bin"] = pd.cut(recent["close"], bins=24)
        grouped = recent.groupby("price_bin", observed=False)["volume"].sum()

        if grouped.empty:
            return None, "unknown"

        poc_bin = grouped.idxmax()
        poc_price = (poc_bin.left + poc_bin.right) / 2

        last_price = float(recent.iloc[-1]["close"])

        if last_price > poc_price:
            status = "above_poc"
        elif last_price < poc_price:
            status = "below_poc"
        else:
            status = "near_poc"

        return float(poc_price), status

    except Exception:
        return None, "unknown"



def detect_market_regime(symbol, df_4h, df_1h, df_30m, df_15m, market=None):
    """
    تشخیص روند کلی بازار.
    خروجی:
    bullish / bearish / neutral

    این لایه فقط نمایشی نیست؛ روی امتیازدهی و فیلتر ورود اثر می‌گذارد.
    """
    score = 0
    reasons = []

    try:
        if symbol == "BTCUSDT":
            btc_4h = df_4h
            btc_1h = df_1h
            btc_30m = df_30m
            btc_15m = df_15m
        else:
            btc_4h = add_indicators(get_klines("BTCUSDT", "4h"))
            btc_1h = add_indicators(get_klines("BTCUSDT", "1h"))
            btc_30m = add_indicators(get_klines("BTCUSDT", "30m"))
            btc_15m = add_indicators(get_klines("BTCUSDT", "15m"))

        btc_trends = {
            "4H": trend_direction(btc_4h),
            "1H": trend_direction(btc_1h),
            "30M": trend_direction(btc_30m),
            "15M": trend_direction(btc_15m),
        }

        weights = {
            "4H": 3,
            "1H": 3,
            "30M": 2,
            "15M": 1,
        }

        for tf, trend in btc_trends.items():
            weight = weights[tf]

            if trend == "bullish":
                score += weight
                reasons.append(f"BTC {tf}: روند صعودی")
            elif trend == "weak_bullish":
                score += max(1, int(weight * 0.5))
                reasons.append(f"BTC {tf}: تمایل صعودی")
            elif trend == "bearish":
                score -= weight
                reasons.append(f"BTC {tf}: روند نزولی")
            elif trend == "weak_bearish":
                score -= max(1, int(weight * 0.5))
                reasons.append(f"BTC {tf}: تمایل نزولی")

        last_btc_1h = btc_1h.iloc[-1]
        last_btc_30m = btc_30m.iloc[-1]

        if last_btc_1h["close"] > last_btc_1h["ema200"]:
            score += 2
            reasons.append("BTC بالای EMA200 یک‌ساعته است")
        else:
            score -= 2
            reasons.append("BTC پایین EMA200 یک‌ساعته است")

        if last_btc_30m["close"] > last_btc_30m["vwap"]:
            score += 1
            reasons.append("BTC بالای VWAP سی‌دقیقه است")
        else:
            score -= 1
            reasons.append("BTC پایین VWAP سی‌دقیقه است")

        if market:
            fear_value = market.get("fear_value")
            btc_dominance = market.get("btc_dominance")

            if fear_value is not None:
                if fear_value <= 25:
                    score -= 1
                    reasons.append("Fear & Greed در محدوده ترس شدید است")
                elif fear_value >= 75:
                    score += 1
                    reasons.append("Fear & Greed در محدوده طمع شدید است")

            if symbol != "BTCUSDT" and btc_dominance is not None:
                try:
                    dominance = float(btc_dominance)
                    if dominance >= 55:
                        score -= 1
                        reasons.append("دامیننس بیتکوین بالا است و برای آلت‌ها فشار ایجاد می‌کند")
                    elif dominance <= 50:
                        score += 1
                        reasons.append("دامیننس بیتکوین پایین‌تر است و برای آلت‌ها بهتر است")
                except Exception:
                    pass

        if score >= 5:
            regime = "bullish"
            text_value = "صعودی"
        elif score <= -5:
            regime = "bearish"
            text_value = "نزولی"
        else:
            regime = "neutral"
            text_value = "خنثی"

        return regime, text_value, score, reasons[:8]

    except Exception as e:
        return "neutral", "نامشخص", 0, [f"تشخیص روند کلی بازار ناموفق بود: {str(e)}"]


def apply_market_regime_to_scores(long_score, short_score, market_regime, reasons_long, reasons_short):
    """
    اعمال روند کلی بازار روی امتیازها.
    """
    if market_regime == "bearish":
        short_score += 10
        long_score -= 15
        reasons_short.append("تقویت: روند کلی بازار نزولی است")
        reasons_long.append("جریمه: لانگ خلاف روند کلی نزولی بازار است")

    elif market_regime == "bullish":
        long_score += 10
        short_score -= 15
        reasons_long.append("تقویت: روند کلی بازار صعودی است")
        reasons_short.append("جریمه: شورت خلاف روند کلی صعودی بازار است")

    elif market_regime == "neutral":
        long_score -= 3
        short_score -= 3
        reasons_long.append("احتیاط: روند کلی بازار خنثی است")
        reasons_short.append("احتیاط: روند کلی بازار خنثی است")

    return max(0, long_score), max(0, short_score)


def find_swing_levels(df, lookback=140, window=3):
    try:
        recent = df.tail(lookback).copy()
        lows, highs = [], []
        for i in range(window, len(recent) - window):
            row = recent.iloc[i]
            left = recent.iloc[i - window:i]
            right = recent.iloc[i + 1:i + 1 + window]
            if row["low"] <= left["low"].min() and row["low"] <= right["low"].min():
                lows.append(float(row["low"]))
            if row["high"] >= left["high"].max() and row["high"] >= right["high"].max():
                highs.append(float(row["high"]))
        return lows[-8:], highs[-8:]
    except Exception:
        return [], []


def support_resistance_basic(df):
    recent = df.tail(80)
    return recent["low"].min(), recent["high"].max()


def support_resistance_swing(df):
    lows, highs = find_swing_levels(df)
    support, resistance = support_resistance_basic(df)
    try:
        price = float(df.iloc[-1]["close"])
        below = [x for x in lows if x < price]
        above = [x for x in highs if x > price]
        if below:
            support = max(below)
        if above:
            resistance = min(above)
    except Exception:
        pass
    return support, resistance


def detect_liquidity_pools(df):
    try:
        lows, highs = find_swing_levels(df, lookback=160, window=2)
        price = float(df.iloc[-1]["close"])
        atr = float(df.iloc[-1]["atr"])
        tolerance = max(atr * 0.25, price * 0.0015)
        equal_highs, equal_lows = [], []
        for level in highs:
            cluster = [h for h in highs if abs(h - level) <= tolerance]
            if len(cluster) >= 2:
                avg = round(sum(cluster) / len(cluster), 8)
                if avg not in equal_highs:
                    equal_highs.append(avg)
        for level in lows:
            cluster = [l for l in lows if abs(l - level) <= tolerance]
            if len(cluster) >= 2:
                avg = round(sum(cluster) / len(cluster), 8)
                if avg not in equal_lows:
                    equal_lows.append(avg)
        nearest_high = min([h for h in equal_highs if h > price], default=None)
        nearest_low = max([l for l in equal_lows if l < price], default=None)
        if nearest_high and nearest_low:
            status, label = "both_sides_liquidity", "نقدینگی در دو طرف بازار دیده می‌شود"
        elif nearest_high:
            status, label = "upside_liquidity", "نقدینگی بالای قیمت وجود دارد"
        elif nearest_low:
            status, label = "downside_liquidity", "نقدینگی پایین قیمت وجود دارد"
        else:
            status, label = "none", "Liquidity Pool واضحی دیده نشد"
        return {"status": status, "label": label, "upside_level": nearest_high, "downside_level": nearest_low}
    except Exception:
        return {"status": "unknown", "label": "Liquidity Pool نامشخص", "upside_level": None, "downside_level": None}


def volatility_state(df_15m, df_5m):
    try:
        last15 = df_15m.iloc[-1]
        last5 = df_5m.iloc[-1]
        atr_pct_15 = (float(last15["atr"]) / float(last15["close"])) * 100
        atr_pct_5 = (float(last5["atr"]) / float(last5["close"])) * 100
        if atr_pct_15 < TECHNICAL_QUALITY_LOW_ATR_PCT and atr_pct_5 < TECHNICAL_QUALITY_LOW_ATR_PCT:
            return "too_low", "نوسان بسیار کم است؛ احتمال نویز بالاست"
        if atr_pct_15 > TECHNICAL_QUALITY_EXTREME_ATR_PCT or atr_pct_5 > TECHNICAL_QUALITY_EXTREME_ATR_PCT:
            return "too_high", "نوسان بیش از حد بالاست؛ ریسک استاپ‌هانت زیاد است"
        return "normal", "نوسان قابل قبول است"
    except Exception:
        return "unknown", "نوسان نامشخص است"


def late_entry_status(direction, df_15m, df_5m):
    try:
        if direction not in ["LONG", "SHORT"]:
            return False, "بدون جهت"
        last = df_5m.iloc[-1]
        recent = df_5m.tail(4)
        price = float(last["close"])
        atr = float(df_15m.iloc[-1]["atr"])
        vwap = float(last["vwap"])
        move = abs(price - float(recent.iloc[0]["open"]))
        far_from_vwap = abs(price - vwap)
        if move >= atr * TECHNICAL_QUALITY_LATE_ENTRY_ATR and far_from_vwap >= atr * 0.75:
            if direction == "LONG" and price > vwap:
                return True, "احتمال ورود دیرهنگام بعد از حرکت صعودی بزرگ"
            if direction == "SHORT" and price < vwap:
                return True, "احتمال ورود دیرهنگام بعد از حرکت نزولی بزرگ"
        body = abs(float(last["close"]) - float(last["open"]))
        rng = float(last["high"]) - float(last["low"])
        if rng > 0 and body / rng >= 0.72 and body >= atr * 0.9:
            return True, "کندل ورود خیلی بزرگ است؛ احتمال ورود دیرهنگام وجود دارد"
        return False, "ورود دیرهنگام واضح نیست"
    except Exception:
        return False, "Late Entry نامشخص"


def tp_space_validation(direction, price, atr, support, resistance):
    try:
        price, atr = float(price), float(atr)
        if direction == "LONG":
            if resistance is None or resistance <= price:
                return True, "فضای TP برای لانگ قابل قبول است", None
            space_atr = (float(resistance) - price) / atr
            if space_atr < TECHNICAL_QUALITY_MIN_TP_SPACE_ATR:
                return False, "فضای کافی تا مقاومت/TP1 برای لانگ وجود ندارد", round(space_atr, 2)
            return True, "فضای TP برای لانگ قابل قبول است", round(space_atr, 2)
        if direction == "SHORT":
            if support is None or support >= price:
                return True, "فضای TP برای شورت قابل قبول است", None
            space_atr = (price - float(support)) / atr
            if space_atr < TECHNICAL_QUALITY_MIN_TP_SPACE_ATR:
                return False, "فضای کافی تا حمایت/TP1 برای شورت وجود ندارد", round(space_atr, 2)
            return True, "فضای TP برای شورت قابل قبول است", round(space_atr, 2)
        return True, "بدون جهت", None
    except Exception:
        return True, "TP Space نامشخص", None


def noise_filter_status(df_15m, df_5m):
    try:
        last15 = df_15m.iloc[-1]
        last5 = df_5m.iloc[-1]
        ema_gap_15 = abs(float(last15["ema20"]) - float(last15["ema50"])) / float(last15["close"]) * 100
        ema_gap_5 = abs(float(last5["ema20"]) - float(last5["ema50"])) / float(last5["close"]) * 100
        if last15["adx"] < 19 and last5["adx"] < 19 and ema_gap_15 < 0.10 and ema_gap_5 < 0.10:
            return "high_noise", "بازار نویزی/رنج است و سیگنال کیفیت پایین‌تری دارد"
        if last15["adx"] < 17 or ema_gap_15 < 0.06:
            return "medium_noise", "کمی نویز در بازار دیده می‌شود"
        return "clean", "نویز بازار قابل قبول است"
    except Exception:
        return "unknown", "نویز نامشخص است"


def mtf_structure_context(df_15m, df_30m, df_1h):
    try:
        structures = {"15M": detect_market_structure(df_15m), "30M": detect_market_structure(df_30m), "1H": detect_market_structure(df_1h)}
        bullish = sum(1 for v in structures.values() if v == "bullish_structure")
        bearish = sum(1 for v in structures.values() if v == "bearish_structure")
        if bullish >= 2:
            status, label = "bullish", "ساختار چندتایم‌فریم صعودی است"
        elif bearish >= 2:
            status, label = "bearish", "ساختار چندتایم‌فریم نزولی است"
        else:
            status, label = "mixed", "ساختار چندتایم‌فریم ترکیبی/نامشخص است"
        return {"status": status, "label": label, "structures": structures}
    except Exception:
        return {"status": "unknown", "label": "ساختار چندتایم‌فریم نامشخص است", "structures": {}}



def bollinger_context(direction, df_15m, df_5m):
    """تحلیل نرم Bollinger Bands برای تشخیص کشیدگی قیمت و فشردگی بازار."""
    try:
        last15 = df_15m.iloc[-1]
        last5 = df_5m.iloc[-1]
        price = float(last5["close"])
        atr = float(last15["atr"])
        bb_high = float(last5["bb_high"])
        bb_low = float(last5["bb_low"])
        bb_mid = float(last5["bb_mid"])
        bb_width = float(last5["bb_width"])

        status = "normal"
        label = "وضعیت Bollinger Bands عادی است"
        long_adj = 0
        short_adj = 0
        reasons_long = []
        reasons_short = []

        if bb_width <= BOLLINGER_SQUEEZE_WIDTH_PCT:
            status = "squeeze"
            label = "Bollinger Bands فشرده است؛ احتمال حرکت ناگهانی یا نویز وجود دارد"
            long_adj -= 1
            short_adj -= 1

        if price > bb_high:
            status = "above_upper"
            label = "قیمت بالای باند بالایی Bollinger است؛ لانگ می‌تواند دیرهنگام باشد"
            long_adj -= 4
            short_adj += 1
            reasons_long.append("Bollinger: قیمت بالای باند بالایی است و ریسک لانگ دیرهنگام دارد")
        elif price < bb_low:
            status = "below_lower"
            label = "قیمت پایین باند پایینی Bollinger است؛ شورت می‌تواند دیرهنگام باشد"
            short_adj -= 4
            long_adj += 1
            reasons_short.append("Bollinger: قیمت پایین باند پایینی است و ریسک شورت دیرهنگام دارد")

        if atr > 0:
            if direction == "LONG" and price > bb_mid and (price - bb_mid) >= atr * BOLLINGER_EXTENSION_ATR:
                long_adj -= 2
                reasons_long.append("Bollinger: فاصله قیمت از میانگین باند برای لانگ زیاد است")
            if direction == "SHORT" and price < bb_mid and (bb_mid - price) >= atr * BOLLINGER_EXTENSION_ATR:
                short_adj -= 2
                reasons_short.append("Bollinger: فاصله قیمت از میانگین باند برای شورت زیاد است")

        return long_adj, short_adj, reasons_long, reasons_short, {
            "bollinger_status": status,
            "bollinger_label": label,
            "bb_high": safe_round(bb_high, 8),
            "bb_mid": safe_round(bb_mid, 8),
            "bb_low": safe_round(bb_low, 8),
            "bb_width": safe_round(bb_width, 2),
        }
    except Exception:
        return 0, 0, [], [], {
            "bollinger_status": "unknown",
            "bollinger_label": "Bollinger Bands نامشخص است",
            "bb_high": None,
            "bb_mid": None,
            "bb_low": None,
            "bb_width": None,
        }


def support_resistance_entry_context(direction, price, atr, support, resistance, df_5m):
    """ورود را به واکنش قیمت به حمایت/مقاومت نزدیک می‌کند، اما برای حفظ تعداد سیگنال‌ها نرم و بالانس است."""
    try:
        if direction not in ["LONG", "SHORT"]:
            return 0, 0, [], [], {
                "sr_entry_status": "none",
                "sr_entry_label": "بدون جهت معتبر برای بررسی ورود بر اساس حمایت/مقاومت",
                "sr_entry_confirmed": False,
            }

        last = df_5m.iloc[-1]
        price = float(price)
        atr = float(atr)
        body = abs(float(last["close"]) - float(last["open"]))
        upper_wick = float(last["high"]) - max(float(last["open"]), float(last["close"]))
        lower_wick = min(float(last["open"]), float(last["close"])) - float(last["low"])
        near_atr = max(atr * SR_ENTRY_NEAR_ATR, price * 0.001)

        long_adj = short_adj = 0
        reasons_long, reasons_short = [], []
        status = "not_near_level"
        label = "قیمت هنوز واکنش واضحی به حمایت/مقاومت نداده است"
        confirmed = False

        if direction == "LONG":
            near_support = support is not None and (price - float(support)) <= near_atr and price >= float(support) - atr * 0.20
            rejected_support = near_support and lower_wick >= max(body * SR_ENTRY_REJECTION_WICK_RATIO, atr * 0.18) and float(last["close"]) >= float(last["open"])
            if rejected_support:
                long_adj += 5
                confirmed = True
                status = "support_rejection"
                label = "قیمت نزدیک حمایت واکنش مثبت نشان داده است"
                reasons_long.append("ورود بر اساس حمایت: واکنش مثبت/حفظ حمایت دیده شد")
            elif near_support:
                long_adj += 2
                status = "near_support"
                label = "قیمت نزدیک حمایت است اما تایید کامل هنوز ضعیف است"
                reasons_long.append("قیمت نزدیک حمایت است؛ برای ورود لانگ تایید کندلی مهم است")
            else:
                long_adj -= 3
                reasons_long.append("ورود بر اساس حمایت: قیمت هنوز به حمایت واکنش واضح نداده است")

        if direction == "SHORT":
            near_resistance = resistance is not None and (float(resistance) - price) <= near_atr and price <= float(resistance) + atr * 0.20
            rejected_resistance = near_resistance and upper_wick >= max(body * SR_ENTRY_REJECTION_WICK_RATIO, atr * 0.18) and float(last["close"]) <= float(last["open"])
            if rejected_resistance:
                short_adj += 5
                confirmed = True
                status = "resistance_rejection"
                label = "قیمت نزدیک مقاومت واکنش منفی نشان داده است"
                reasons_short.append("ورود بر اساس مقاومت: واکنش منفی/رد مقاومت دیده شد")
            elif near_resistance:
                short_adj += 2
                status = "near_resistance"
                label = "قیمت نزدیک مقاومت است اما تایید کامل هنوز ضعیف است"
                reasons_short.append("قیمت نزدیک مقاومت است؛ برای ورود شورت تایید کندلی مهم است")
            else:
                short_adj -= 3
                reasons_short.append("ورود بر اساس مقاومت: قیمت هنوز به مقاومت واکنش واضح نداده است")

        return long_adj, short_adj, reasons_long, reasons_short, {
            "sr_entry_status": status,
            "sr_entry_label": label,
            "sr_entry_confirmed": confirmed,
        }
    except Exception:
        return 0, 0, [], [], {
            "sr_entry_status": "unknown",
            "sr_entry_label": "بررسی ورود بر اساس حمایت/مقاومت نامشخص است",
            "sr_entry_confirmed": False,
        }

def technical_quality_context(raw_direction, price, atr, support, resistance, df_15m, df_5m, df_30m, df_1h):
    volatility, volatility_label = volatility_state(df_15m, df_5m)
    late_entry, late_entry_reason = late_entry_status(raw_direction, df_15m, df_5m)
    # VPS-safe unpack: older/duplicate tp_space_validation variants may return 2 values.
    tp_space_result = tp_space_validation(raw_direction, price, atr, support, resistance)
    if isinstance(tp_space_result, (list, tuple)):
        if len(tp_space_result) >= 3:
            tp_ok, tp_space_reason, tp_space_atr = tp_space_result[:3]
        elif len(tp_space_result) == 2:
            tp_ok, tp_space_reason = tp_space_result
            tp_space_atr = None
        else:
            tp_ok, tp_space_reason, tp_space_atr = True, "TP Space نامشخص", None
    else:
        tp_ok, tp_space_reason, tp_space_atr = True, "TP Space نامشخص", None
    noise_status, noise_label = noise_filter_status(df_15m, df_5m)
    liquidity = detect_liquidity_pools(df_15m)
    mtf = mtf_structure_context(df_15m, df_30m, df_1h)
    long_adj = short_adj = 0
    reasons_long, reasons_short = [], []
    if noise_status == "high_noise":
        long_adj -= 3; short_adj -= 3
        (reasons_long if raw_direction == "LONG" else reasons_short).append("فیلتر نویز: بازار رنج/نویزی است")
    elif noise_status == "medium_noise":
        long_adj -= 1; short_adj -= 1
    if volatility == "too_low":
        long_adj -= 3; short_adj -= 3
        (reasons_long if raw_direction == "LONG" else reasons_short).append("فیلتر نوسان: ATR خیلی کم است")
    elif volatility == "too_high":
        long_adj -= 2; short_adj -= 2
        (reasons_long if raw_direction == "LONG" else reasons_short).append("فیلتر نوسان: ATR خیلی زیاد است")
    if late_entry:
        if raw_direction == "LONG":
            long_adj -= 5; reasons_long.append(late_entry_reason)
        elif raw_direction == "SHORT":
            short_adj -= 5; reasons_short.append(late_entry_reason)
    if not tp_ok:
        if raw_direction == "LONG":
            long_adj -= 6; reasons_long.append(tp_space_reason)
        elif raw_direction == "SHORT":
            short_adj -= 6; reasons_short.append(tp_space_reason)
    if mtf["status"] == "bullish":
        long_adj += 3; short_adj -= 2; reasons_long.append("ساختار چندتایم‌فریم صعودی است")
    elif mtf["status"] == "bearish":
        short_adj += 3; long_adj -= 2; reasons_short.append("ساختار چندتایم‌فریم نزولی است")
    if liquidity["status"] == "upside_liquidity" and raw_direction == "SHORT":
        short_adj -= 1; reasons_short.append("نقدینگی بالای قیمت می‌تواند ریسک شورت باشد")
    if liquidity["status"] == "downside_liquidity" and raw_direction == "LONG":
        long_adj -= 1; reasons_long.append("نقدینگی پایین قیمت می‌تواند ریسک لانگ باشد")

    sr_l, sr_s, sr_rl, sr_rs, sr_context = support_resistance_entry_context(raw_direction, price, atr, support, resistance, df_5m)
    bb_l, bb_s, bb_rl, bb_rs, bb_context = bollinger_context(raw_direction, df_15m, df_5m)
    long_adj += sr_l + bb_l
    short_adj += sr_s + bb_s
    reasons_long += sr_rl + bb_rl
    reasons_short += sr_rs + bb_rs

    context = {
        "noise_status": noise_status, "noise_label": noise_label,
        "volatility_status": volatility, "volatility_label": volatility_label,
        "late_entry": late_entry, "late_entry_reason": late_entry_reason,
        "tp_space_ok": tp_ok, "tp_space_reason": tp_space_reason, "tp_space_atr": tp_space_atr,
        "liquidity_pool_status": liquidity["status"], "liquidity_pool_label": liquidity["label"],
        "liquidity_upside_level": liquidity["upside_level"], "liquidity_downside_level": liquidity["downside_level"],
        "mtf_structure_status": mtf["status"], "mtf_structure_label": mtf["label"], "mtf_structures": mtf["structures"],
        "technical_quality_long_adj": long_adj, "technical_quality_short_adj": short_adj,
    }
    context.update(sr_context)
    context.update(bb_context)
    return long_adj, short_adj, reasons_long, reasons_short, context


def btc_filter(symbol):
    if symbol == "BTCUSDT":
        return "neutral", 0, 0, [], []

    try:
        btc_15m = add_indicators(get_klines("BTCUSDT", "15m"))
        btc_5m = add_indicators(get_klines("BTCUSDT", "5m"))

        btc_15_trend = trend_direction(btc_15m)
        btc_5_trend = trend_direction(btc_5m)

        long_score = 0
        short_score = 0
        reasons_long = []
        reasons_short = []

        if btc_15_trend in ["bullish", "weak_bullish"] and btc_5_trend in ["bullish", "weak_bullish"]:
            long_score += 8
            reasons_long.append("BTC در تایم ورود صعودی است")

        elif btc_15_trend in ["bearish", "weak_bearish"] and btc_5_trend in ["bearish", "weak_bearish"]:
            short_score += 8
            reasons_short.append("BTC در تایم ورود نزولی است")
        else:
            reasons_long.append("BTC جهت واضحی ندارد")
            reasons_short.append("BTC جهت واضحی ندارد")

        return "ok", long_score, short_score, reasons_long, reasons_short

    except Exception:
        return "unknown", 0, 0, [], []


def signal_validity(score, direction):
    if direction == "NO TRADE":
        return "سیگنال معتبر نیست"

    if score >= 90:
        return "30 تا 60 دقیقه"

    if score >= 80:
        return "30 تا 60 دقیقه"

    if score >= 70:
        return "20 تا 45 دقیقه"

    return "اعتبار پایین"


def signal_timeframe(score, direction):
    if direction == "NO TRADE":
        return "بدون تایم‌فریم ورود"

    return "15M تا 30M"


def score_macro_trend(df_1d, df_4h, df_1h, df_30m):
    long_score = 0
    short_score = 0
    reasons_long = []
    reasons_short = []

    trends = {
        "1D": trend_direction(df_1d),
        "4H": trend_direction(df_4h),
        "1H": trend_direction(df_1h),
        "30M": trend_direction(df_30m),
    }

    weights = {
        "1D": 6,
        "4H": 10,
        "1H": 18,
        "30M": 20,
    }

    for tf, trend in trends.items():
        weight = weights[tf]

        if trend == "bullish":
            long_score += weight
            reasons_long.append(f"{tf}: روند صعودی")
        elif trend == "weak_bullish":
            long_score += int(weight * 0.5)
            reasons_long.append(f"{tf}: تمایل صعودی")
        elif trend == "bearish":
            short_score += weight
            reasons_short.append(f"{tf}: روند نزولی")
        elif trend == "weak_bearish":
            short_score += int(weight * 0.5)
            reasons_short.append(f"{tf}: تمایل نزولی")

    return long_score, short_score, reasons_long, reasons_short, trends


def score_entry(df_15m, df_5m):
    long_score = 0
    short_score = 0
    reasons_long = []
    reasons_short = []

    last_15 = df_15m.iloc[-1]
    last_5 = df_5m.iloc[-1]

    buy_power, sell_power = buy_sell_power(df_5m)

    if last_15["close"] > last_15["ema20"] > last_15["ema50"]:
        long_score += 15
        reasons_long.append("15M: قیمت بالای EMA20 و EMA50")

    if last_15["close"] < last_15["ema20"] < last_15["ema50"]:
        short_score += 15
        reasons_short.append("15M: قیمت زیر EMA20 و EMA50")

    if last_5["close"] > last_5["ema20"] and last_5["macd"] > last_5["macd_signal"]:
        long_score += 15
        reasons_long.append("5M: تایید ورود لانگ با EMA و MACD")

    if last_5["close"] < last_5["ema20"] and last_5["macd"] < last_5["macd_signal"]:
        short_score += 15
        reasons_short.append("5M: تایید ورود شورت با EMA و MACD")

    if 45 <= last_5["rsi"] <= 68:
        long_score += 10
        reasons_long.append("RSI مناسب برای لانگ در 5M")

    if 32 <= last_5["rsi"] <= 55:
        short_score += 10
        reasons_short.append("RSI مناسب برای شورت در 5M")

    if buy_power >= 62:
        long_score += 10
        reasons_long.append("قدرت خرید بالا در تایم ورود")

    if sell_power >= 62:
        short_score += 10
        reasons_short.append("قدرت فروش بالا در تایم ورود")

    pattern = candle_pattern(df_5m)
    multi_candle = multi_candle_confirmation(df_5m)

    if pattern in ["bullish_engulfing", "bullish_pinbar", "bullish_strong"]:
        long_score += 10
        reasons_long.append(f"کندل تاییدی لانگ: {pattern}")

    if pattern in ["bearish_engulfing", "bearish_pinbar", "bearish_strong"]:
        short_score += 10
        reasons_short.append(f"کندل تاییدی شورت: {pattern}")

    if multi_candle == "bullish":
        long_score += 8
        reasons_long.append("تایید چند کندلی صعودی")

    if multi_candle == "bearish":
        short_score += 8
        reasons_short.append("تایید چند کندلی نزولی")

    if volume_spike(df_5m):
        long_score += 6
        short_score += 6
        reasons_long.append("افزایش حجم واقعی")
        reasons_short.append("افزایش حجم واقعی")

    if last_5["adx"] >= 22:
        long_score += 5
        short_score += 5

    return long_score, short_score, reasons_long, reasons_short, buy_power, sell_power, pattern, multi_candle


def score_smart_money(df_15m, df_5m):
    long_score = 0
    short_score = 0
    reasons_long = []
    reasons_short = []

    liquidity_grab = detect_liquidity_grab(df_5m)
    stop_hunt = detect_stop_hunt(df_5m)
    fvg = detect_fvg(df_5m)
    order_block = detect_order_block(df_15m)

    if liquidity_grab == "bullish_liquidity_grab":
        long_score += 6
        reasons_long.append("Liquidity Grab صعودی")

    if liquidity_grab == "bearish_liquidity_grab":
        short_score += 6
        reasons_short.append("Liquidity Grab نزولی")

    if stop_hunt == "bullish_stop_hunt":
        long_score += 5
        reasons_long.append("Stop Hunt صعودی")

    if stop_hunt == "bearish_stop_hunt":
        short_score += 5
        reasons_short.append("Stop Hunt نزولی")

    if fvg == "bullish_fvg":
        long_score += 6
        reasons_long.append("FVG صعودی")

    if fvg == "bearish_fvg":
        short_score += 6
        reasons_short.append("FVG نزولی")

    if order_block == "bullish_order_block":
        long_score += 7
        reasons_long.append("Order Block صعودی")

    if order_block == "bearish_order_block":
        short_score += 7
        reasons_short.append("Order Block نزولی")

    trend15 = trend_direction(df_15m)

    if trend15 == "bullish" and order_block == "bearish_order_block":
        short_score -= 8
        reasons_short.append("جریمه: Order Block نزولی مخالف روند صعودی 15M است")

    if trend15 == "bearish" and order_block == "bullish_order_block":
        long_score -= 8
        reasons_long.append("جریمه: Order Block صعودی مخالف روند نزولی 15M است")

    return long_score, short_score, reasons_long, reasons_short, liquidity_grab, stop_hunt, fvg, order_block


def score_divergence(df_5m):
    long_score = 0
    short_score = 0
    reasons_long = []
    reasons_short = []

    rsi_divergence = detect_rsi_divergence(df_5m)
    macd_divergence = detect_macd_divergence(df_5m)

    if rsi_divergence == "bullish_rsi_divergence":
        long_score += 10
        reasons_long.append("واگرایی مثبت RSI")

    if rsi_divergence == "bearish_rsi_divergence":
        short_score += 10
        reasons_short.append("واگرایی منفی RSI")

    if macd_divergence == "bullish_macd_divergence":
        long_score += 10
        reasons_long.append("واگرایی مثبت MACD")

    if macd_divergence == "bearish_macd_divergence":
        short_score += 10
        reasons_short.append("واگرایی منفی MACD")

    return long_score, short_score, reasons_long, reasons_short, rsi_divergence, macd_divergence


def score_futures_data(symbol):
    funding_rate = get_funding_rate(symbol)
    open_interest = get_open_interest(symbol)

    long_score = 0
    short_score = 0
    risk_notes = []

    if funding_rate is not None:
        if funding_rate > 0.05:
            short_score += 4
            risk_notes.append("Funding مثبت و نسبتاً بالا")
        elif funding_rate < -0.05:
            long_score += 4
            risk_notes.append("Funding منفی و نسبتاً بالا")

    if open_interest is not None and open_interest > 0:
        long_score += 2
        short_score += 2

    return long_score, short_score, funding_rate, open_interest, risk_notes


def score_market_sentiment(symbol):
    market = get_market_sentiment()

    long_score = 0
    short_score = 0
    reasons_long = []
    reasons_short = []

    fear_value = market.get("fear_value")
    altseason = market.get("altseason_status")

    if fear_value is not None:
        if fear_value <= 25:
            long_score += 3
            reasons_long.append("Fear & Greed در ترس شدید")
        elif fear_value >= 80:
            short_score += 3
            reasons_short.append("Fear & Greed در طمع شدید")

    if symbol != "BTCUSDT":
        if altseason == "قوی":
            long_score += 3
            reasons_long.append("آلت‌سیزن برای آلت‌کوین‌ها مناسب است")
        elif altseason == "ضعیف":
            short_score += 3
            reasons_short.append("آلت‌سیزن ضعیف است")

    return long_score, short_score, reasons_long, reasons_short, market


def score_vwap_volume_profile(df_15m, df_5m):
    long_score = 0
    short_score = 0
    reasons_long = []
    reasons_short = []

    vwap_status = calculate_vwap_status(df_5m)
    poc_price, volume_profile_status = calculate_volume_profile(df_15m)

    if vwap_status == "above_vwap":
        long_score += 6
        reasons_long.append("قیمت بالای VWAP است")

    if vwap_status == "below_vwap":
        short_score += 6
        reasons_short.append("قیمت پایین VWAP است")

    if volume_profile_status == "above_poc":
        long_score += 5
        reasons_long.append("قیمت بالای POC حجمی است")

    if volume_profile_status == "below_poc":
        short_score += 5
        reasons_short.append("قیمت پایین POC حجمی است")

    return long_score, short_score, reasons_long, reasons_short, vwap_status, poc_price, volume_profile_status



def apply_direction_conflict_penalties(
    long_score,
    short_score,
    pattern,
    multi_candle,
    order_block,
    fvg,
    vwap_status,
    buy_power,
    sell_power,
    reasons_long,
    reasons_short
):
    """
    جریمه نرم برای تناقض‌های واضح.
    هدف: سیگنال شورت با کندل/اوردر بلاک صعودی یا لانگ با تاییدهای نزولی، 100/100 نشود.
    این تابع سیگنال را مستقیم حذف نمی‌کند، فقط امتیاز را واقعی‌تر می‌کند.
    """

    bullish_candle = pattern in ["bullish_engulfing", "bullish_pinbar", "bullish_strong"] or multi_candle == "bullish"
    bearish_candle = pattern in ["bearish_engulfing", "bearish_pinbar", "bearish_strong"] or multi_candle == "bearish"

    if bullish_candle:
        short_score -= 12
        reasons_short.append("جریمه: کندل یا تایید چندکندلی صعودی، خلاف شورت است")

    if bearish_candle:
        long_score -= 12
        reasons_long.append("جریمه: کندل یا تایید چندکندلی نزولی، خلاف لانگ است")

    if order_block == "bullish_order_block":
        short_score -= 10
        reasons_short.append("جریمه: اوردر بلاک صعودی، خلاف شورت است")

    if order_block == "bearish_order_block":
        long_score -= 10
        reasons_long.append("جریمه: اوردر بلاک نزولی، خلاف لانگ است")

    if fvg == "bullish_fvg":
        short_score -= 12
        reasons_short.append("جریمه سنگین: ناحیه نقدینگی صعودی، خلاف شورت است")

    if fvg == "bearish_fvg":
        long_score -= 12
        reasons_long.append("جریمه سنگین: ناحیه نقدینگی نزولی، خلاف لانگ است")

    if vwap_status == "above_vwap":
        short_score -= 8
        reasons_short.append("جریمه: قیمت بالای VWAP است و برای شورت ریسک دارد")

    if vwap_status == "below_vwap":
        long_score -= 8
        reasons_long.append("جریمه: قیمت پایین VWAP است و برای لانگ ریسک دارد")

    if buy_power >= sell_power + 12:
        short_score -= 7
        reasons_short.append("جریمه: قدرت خرید نسبت به فروش بالاتر است")

    if sell_power >= buy_power + 12:
        long_score -= 7
        reasons_long.append("جریمه: قدرت فروش نسبت به خرید بالاتر است")

    return max(0, long_score), max(0, short_score)


def normalize_score_by_quality(score, rr, raw_direction, pattern, multi_candle, order_block, vwap_status, fvg="none"):
    """
    محدود کردن امتیازهای خیلی بالا.
    هدف: 100/100 فقط برای سیگنال‌های واقعاً تمیز باشد، نه هر سیگنال قوی ظاهری.
    """
    if raw_direction == "NO TRADE":
        return score

    if rr < 1.2:
        score = min(score, 84)
    elif rr < 1.35:
        score = min(score, 90)
    elif rr < 1.5:
        score = min(score, 94)
    elif rr < 1.8:
        score = min(score, 97)

    if raw_direction == "LONG":
        if pattern in ["bearish_engulfing", "bearish_pinbar", "bearish_strong"] or multi_candle == "bearish":
            score = min(score, 88)

        if order_block == "bearish_order_block":
            score = min(score, 72)

        if fvg == "bearish_fvg":
            score = min(score, 82)

        if fvg == "bearish_fvg" and (pattern in ["bearish_engulfing", "bearish_pinbar", "bearish_strong"] or multi_candle == "bearish"):
            score = min(score, 74)

        if vwap_status == "below_vwap":
            score = min(score, 92)

    if raw_direction == "SHORT":
        if pattern in ["bullish_engulfing", "bullish_pinbar", "bullish_strong"] or multi_candle == "bullish":
            score = min(score, 88)

        if order_block == "bullish_order_block":
            score = min(score, 72)

        if fvg == "bullish_fvg":
            score = min(score, 82)

        if fvg == "bullish_fvg" and (pattern in ["bullish_engulfing", "bullish_pinbar", "bullish_strong"] or multi_candle == "bullish"):
            score = min(score, 74)

        if vwap_status == "above_vwap":
            score = min(score, 92)

    return cap_score(score)

def calculate_trade_levels(raw_direction, price, atr, support=None, resistance=None):
    """
    TP/SL هوشمندتر بر اساس ATR و حمایت/مقاومت، مناسب معاملات حدود 30 تا 60 دقیقه.
    اصلاح جدید:
    - TPها مثل نسخه قدیمی خیلی دور نیستند.
    - TP1 آنقدر نزدیک نمی‌شود که RR معمولاً زیر 1 بیاید.
    - حمایت/مقاومت فقط وقتی TP را نزدیک‌تر می‌کند که حداقل RR منطقی حفظ شود.
    """
    price = float(price)
    atr = float(atr)
    buffer = atr * 0.22

    if raw_direction == "LONG":
        atr_sl = price - (atr * 1.45)
        stop_loss = atr_sl

        if support is not None and float(support) < price:
            structure_sl = float(support) - buffer
            max_sl = price - (atr * 2.15)
            stop_loss = max(structure_sl, max_sl) if structure_sl < atr_sl else structure_sl

        risk = abs(price - stop_loss)
        min_tp1_reward = max(atr * 1.05, risk * 1.15)
        min_tp2_reward = max(atr * 1.80, risk * 1.65)

        tp1 = price + min_tp1_reward
        tp2 = price + min_tp2_reward

        if resistance is not None and float(resistance) > price:
            adjusted_tp1 = float(resistance) - buffer
            adjusted_reward = adjusted_tp1 - price

            # فقط اگر TP نزدیک مقاومت هنوز RR منطقی بدهد، TP1 را قبل مقاومت می‌گذاریم.
            if adjusted_reward >= max(atr * 0.75, risk * 1.05):
                tp1 = min(tp1, adjusted_tp1)

            # TP2 محافظه‌کارانه بماند ولی از TP1 پایین‌تر/خیلی نزدیک‌تر نشود.
            resistance_tp2 = float(resistance) + atr * 0.35
            if resistance_tp2 > tp1:
                tp2 = min(tp2, max(resistance_tp2, price + min_tp1_reward * 1.25))

        return stop_loss, tp1, tp2

    if raw_direction == "SHORT":
        atr_sl = price + (atr * 1.45)
        stop_loss = atr_sl

        if resistance is not None and float(resistance) > price:
            structure_sl = float(resistance) + buffer
            max_sl = price + (atr * 2.15)
            stop_loss = min(structure_sl, max_sl) if structure_sl > atr_sl else structure_sl

        risk = abs(stop_loss - price)
        min_tp1_reward = max(atr * 1.05, risk * 1.15)
        min_tp2_reward = max(atr * 1.80, risk * 1.65)

        tp1 = price - min_tp1_reward
        tp2 = price - min_tp2_reward

        if support is not None and float(support) < price:
            adjusted_tp1 = float(support) + buffer
            adjusted_reward = price - adjusted_tp1

            # فقط اگر TP نزدیک حمایت هنوز RR منطقی بدهد، TP1 را قبل حمایت می‌گذاریم.
            if adjusted_reward >= max(atr * 0.75, risk * 1.05):
                tp1 = max(tp1, adjusted_tp1)

            # TP2 محافظه‌کارانه بماند ولی از TP1 بالاتر/خیلی نزدیک‌تر نشود.
            support_tp2 = float(support) - atr * 0.35
            if support_tp2 < tp1:
                tp2 = max(tp2, min(support_tp2, price - min_tp1_reward * 1.25))

        return stop_loss, tp1, tp2

    return None, None, None

def risk_reward(raw_direction, price, stop_loss, tp1):
    if raw_direction == "NO TRADE" or stop_loss is None or tp1 is None:
        return 0

    risk = abs(price - stop_loss)
    reward = abs(tp1 - price)

    if risk <= 0:
        return 0

    return round(reward / risk, 2)


def calculate_risk_level(raw_direction, score, liquidity_risk, funding_rate, adx, spread_percent, rr):
    if raw_direction == "NO TRADE":
        return "بالا"

    risk = 0

    if score < 75:
        risk += 2

    if adx < 20:
        risk += 2

    if liquidity_risk == "بالا":
        risk += 2

    if funding_rate is not None and abs(funding_rate) > 0.07:
        risk += 1

    if spread_percent is not None and spread_percent > 0.08:
        risk += 2

    if rr < 1:
        risk += 2

    if risk >= 4:
        return "بالا"

    if risk >= 2:
        return "متوسط"

    return "پایین"


def entry_grade(score, risk_level, rr, final_direction):
    if final_direction == "NO TRADE":
        return "Reject"

    if score >= 92 and risk_level == "پایین" and rr >= 1.20:
        return "A+"

    if score >= 82 and risk_level in ["پایین", "متوسط"] and rr >= 1.05:
        return "A"

    # طبق درخواست، گرید B ارسال/قبول نمی‌شود.
    return "Reject"

def win_probability(score, risk_level, rr, adx, grade):
    p = 42 + int(score * 0.22)
    p += 8 if risk_level == "پایین" else 3 if risk_level == "متوسط" else -10
    p += 5 if rr >= 1.5 else 2 if rr >= 1 else -10
    p += 4 if adx >= 25 else -6 if adx < 19 else 0
    p += 4 if grade == "A+" else 2 if grade == "A" else -15 if grade == "Reject" else 0
    return max(0, min(p, 92))


def news_filter_status():
    """اخبار از تصمیم‌گیری حذف شده است."""
    return False, "غیرفعال"

def news_filter_active():
    return False



def tp_space_validation(raw_direction, price, atr, support, resistance):
    """
    بررسی می‌کند تا TP1 فضای کافی وجود داشته باشد.
    هدف: لانگ نزدیک مقاومت و شورت نزدیک حمایت، بی‌دلیل سیگنال نشود.
    """
    if raw_direction == "LONG":
        if resistance is None or resistance <= price:
            return True, None

        space = resistance - price
        if space < atr * 1.15:
            return False, "فضای کافی تا مقاومت برای TP وجود ندارد"

    if raw_direction == "SHORT":
        if support is None or support >= price:
            return True, None

        space = price - support
        if space < atr * 1.15:
            return False, "فضای کافی تا حمایت برای TP وجود ندارد"

    return True, None


def calculate_setup_zone(raw_direction, price, atr):
    """
    ناحیه ورود پیشنهادی برای حالت Setup -> Entry Trigger.
    این فقط برای راهنمایی ورود بهتر است و به تنهایی سفارش نیست.
    """
    if raw_direction == "LONG":
        zone_low = price - (atr * 0.35)
        zone_high = price + (atr * 0.10)
        trigger = "ورود لانگ فقط بعد از حفظ ناحیه ورود و تایید کندل صعودی در 15M/30M"

    elif raw_direction == "SHORT":
        zone_low = price - (atr * 0.10)
        zone_high = price + (atr * 0.35)
        trigger = "ورود شورت فقط بعد از حفظ ناحیه ورود و تایید کندل نزولی در 15M/30M"

    else:
        return "inactive", None, None, "ستاپ فعالی وجود ندارد"

    return "ready", zone_low, zone_high, trigger


def very_safe_status(raw_direction, score, win_probability_value, risk_level, rr, trends,
                     vwap_status, buy_power, sell_power, adx_value,
                     pattern=None, multi_candle=None, order_block=None, fvg=None,
                     market_regime="neutral"):
    """
    حالت Very Safe Mode:
    برای سیگنال‌های کم‌تعدادتر اما هم‌راستاتر.
    این تابع سیگنال معمولی را حذف نمی‌کند؛ فقط وضعیت خیلی امن را مشخص می‌کند.
    """
    reasons = []

    if raw_direction not in ["LONG", "SHORT"]:
        return False, ["جهت مشخص نیست"]

    if market_regime == "bearish" and raw_direction == "LONG":
        reasons.append("لانگ خلاف روند کلی نزولی بازار است")

    if market_regime == "bullish" and raw_direction == "SHORT":
        reasons.append("شورت خلاف روند کلی صعودی بازار است")

    if market_regime == "neutral":
        reasons.append("روند کلی بازار خنثی است")

    if score < 88:
        reasons.append("امتیاز کمتر از حد Very Safe است")

    if win_probability_value is not None and win_probability_value < 75:
        reasons.append("احتمال موفقیت کمتر از حد Very Safe است")

    if risk_level == "بالا":
        reasons.append("ریسک بالا است")

    if rr < 1.2:
        reasons.append("ریسک به ریوارد برای Very Safe کافی نیست")

    if adx_value < 22:
        reasons.append("ADX برای Very Safe کمی ضعیف است")

    if raw_direction == "LONG":
        aligned = 0
        for tf in ["4H", "1H", "30M"]:
            if trends.get(tf) in ["bullish", "weak_bullish"]:
                aligned += 1

        if aligned < 2:
            reasons.append("هم‌جهتی تایم‌فریم‌های بالاتر برای لانگ کافی نیست")

        if vwap_status != "above_vwap":
            reasons.append("VWAP برای لانگ تایید کامل نمی‌دهد")

        if buy_power < 55:
            reasons.append("قدرت خرید برای Very Safe کافی نیست")

        if pattern in ["bearish_engulfing", "bearish_pinbar", "bearish_strong"]:
            reasons.append("کندل تاییدی مخالف لانگ است")

        if multi_candle == "bearish":
            reasons.append("تایید چندکندلی مخالف لانگ است")

        if order_block == "bearish_order_block":
            reasons.append("اوردر بلاک مخالف لانگ است")

        if fvg == "bearish_fvg":
            reasons.append("ناحیه خالی نقدینگی مخالف لانگ است")

    if raw_direction == "SHORT":
        aligned = 0
        for tf in ["4H", "1H", "30M"]:
            if trends.get(tf) in ["bearish", "weak_bearish"]:
                aligned += 1

        if aligned < 2:
            reasons.append("هم‌جهتی تایم‌فریم‌های بالاتر برای شورت کافی نیست")

        if vwap_status != "below_vwap":
            reasons.append("VWAP برای شورت تایید کامل نمی‌دهد")

        if sell_power < 55:
            reasons.append("قدرت فروش برای Very Safe کافی نیست")

        if pattern in ["bullish_engulfing", "bullish_pinbar", "bullish_strong"]:
            reasons.append("کندل تاییدی مخالف شورت است")

        if multi_candle == "bullish":
            reasons.append("تایید چندکندلی مخالف شورت است")

        if order_block == "bullish_order_block":
            reasons.append("اوردر بلاک مخالف شورت است")

        if fvg == "bullish_fvg":
            reasons.append("ناحیه خالی نقدینگی مخالف شورت است")

    return len(reasons) == 0, reasons


def entry_filter(raw_direction, score, long_score, short_score, df_15m, df_5m, spread_percent, market_regime="neutral", order_block="none", fvg="none", buy_power=50, sell_power=50, rsi_divergence="none", macd_divergence="none"):
    last_5 = df_5m.iloc[-1]
    last_15 = df_15m.iloc[-1]
    price = float(last_5["close"])
    atr = float(last_15["atr"])
    support, resistance = support_resistance(df_15m)

    reasons_block = []
    liquidity_risk = "پایین"

    if raw_direction == "NO TRADE":
        reasons_block.append("اختلاف لانگ و شورت کافی نیست")
        return False, reasons_block, "بالا", "none", "none"

    # طبق درخواست: فقط اوردر بلاک مخالف رد قطعی است.
    if raw_direction == "LONG" and order_block == "bearish_order_block":
        reasons_block.append("اوردر بلاک نزولی خلاف سیگنال لانگ است")
        return False, reasons_block, "بالا", "none", "none"

    if raw_direction == "SHORT" and order_block == "bullish_order_block":
        reasons_block.append("اوردر بلاک صعودی خلاف سیگنال شورت است")
        return False, reasons_block, "بالا", "none", "none"

    # FVG مخالف به‌تنهایی رد قطعی نیست، اما ریسک را بالا می‌برد.
    # اگر FVG مخالف همراه با تایید چندکندلی/کندلی مخالف باشد، برای کاهش استاپ‌های بی‌کیفیت رد می‌شود.
    current_pattern = candle_pattern(df_5m)
    current_multi_candle = multi_candle_confirmation(df_5m)

    if raw_direction == "LONG" and fvg == "bearish_fvg":
        reasons_block.append("FVG نزولی خلاف سیگنال لانگ است")
        liquidity_risk = "بالا"
        if current_multi_candle == "bearish" or current_pattern in ["bearish_engulfing", "bearish_pinbar", "bearish_strong"]:
            reasons_block.append("FVG و تایید کندلی هر دو خلاف لانگ هستند")
            return False, reasons_block, "بالا", "none", "none"

    if raw_direction == "SHORT" and fvg == "bullish_fvg":
        reasons_block.append("FVG صعودی خلاف سیگنال شورت است")
        liquidity_risk = "بالا"
        if current_multi_candle == "bullish" or current_pattern in ["bullish_engulfing", "bullish_pinbar", "bullish_strong"]:
            reasons_block.append("FVG و تایید کندلی هر دو خلاف شورت هستند")
            return False, reasons_block, "بالا", "none", "none"

    # قدرت خرید/فروش فقط وقتی خیلی مخالف باشد ریسک را بالا می‌برد؛ سختگیرانه نیست.
    try:
        buy_power_value = float(buy_power)
        sell_power_value = float(sell_power)
    except Exception:
        buy_power_value = 50
        sell_power_value = 50

    if raw_direction == "LONG" and sell_power_value > buy_power_value:
        reasons_block.append("قدرت فروش از قدرت خرید بیشتر است؛ لانگ رد شد")
        return False, reasons_block, "بالا", "none", "none"

    if raw_direction == "SHORT" and buy_power_value > sell_power_value:
        reasons_block.append("قدرت خرید از قدرت فروش بیشتر است؛ شورت رد شد")
        return False, reasons_block, "بالا", "none", "none"

    # فقط واگرایی دوگانه مخالف رد می‌کند.
    if raw_direction == "LONG":
        if (
            rsi_divergence == "bearish_rsi_divergence"
            and macd_divergence == "bearish_macd_divergence"
        ):
            reasons_block.append("واگرایی دوگانه نزولی خلاف سیگنال لانگ است")
            return False, reasons_block, "بالا", "none", "none"

    if raw_direction == "SHORT":
        if (
            rsi_divergence == "bullish_rsi_divergence"
            and macd_divergence == "bullish_macd_divergence"
        ):
            reasons_block.append("واگرایی دوگانه صعودی خلاف سیگنال شورت است")
            return False, reasons_block, "بالا", "none", "none"

    # خلاف روند کلی بازار فقط ریسک را بالا می‌برد؛ برای اینکه ربات خشک نشود رد قطعی نیست.
    if market_regime == "bearish" and raw_direction == "LONG" and score < 90:
        reasons_block.append("لانگ خلاف روند کلی نزولی بازار است")
        liquidity_risk = "متوسط"

    if market_regime == "bullish" and raw_direction == "SHORT" and score < 90:
        reasons_block.append("شورت خلاف روند کلی صعودی بازار است")
        liquidity_risk = "متوسط"

    # اخبار از تصمیم‌گیری حذف شد؛ ترس‌وطمع، دامیننس و آلت‌سیزن در تحلیل باقی می‌مانند.

    if market_is_choppy(df_15m, df_5m):
        reasons_block.append("بازار رنج، فشرده یا کم‌قدرت است")
        liquidity_risk = "بالا"

    if not minimum_volatility_ok(df_5m):
        reasons_block.append("نوسان برای معامله کافی نیست")
        liquidity_risk = "بالا"

    if spread_percent is not None and spread_percent > 0.10:
        reasons_block.append("اسپرد برای معامله زیاد است")
        liquidity_risk = "بالا"

    if is_middle_of_range(price, support, resistance) and score < 88:
        reasons_block.append("قیمت وسط رنج است و امتیاز برای عبور از این ریسک کافی نیست")
        liquidity_risk = "متوسط"

    sr_l, sr_s, sr_rl, sr_rs, sr_context = support_resistance_entry_context(raw_direction, price, atr, support, resistance, df_5m)
    if raw_direction == "LONG" and sr_context.get("sr_entry_status") == "not_near_level" and score < SR_ENTRY_MIN_SCORE_BLOCK:
        reasons_block.append("ورود بر اساس حمایت هنوز تایید کافی ندارد")
        liquidity_risk = "متوسط"
    if raw_direction == "SHORT" and sr_context.get("sr_entry_status") == "not_near_level" and score < SR_ENTRY_MIN_SCORE_BLOCK:
        reasons_block.append("ورود بر اساس مقاومت هنوز تایید کافی ندارد")
        liquidity_risk = "متوسط"

    fake_breakout = detect_fake_breakout(df_5m)
    trend_exhaustion = detect_trend_exhaustion(df_5m)

    if raw_direction == "LONG":
        if long_score < short_score + 20:
            reasons_block.append("اختلاف امتیاز لانگ و شورت کافی نیست")

        if is_near_resistance(price, resistance, atr) and score < 88:
            reasons_block.append("قیمت نزدیک مقاومت است")
            liquidity_risk = "متوسط"

        if last_5["rsi"] > 68:
            reasons_block.append("RSI برای لانگ کمی بالاست")

        if last_15["adx"] < 18:
            reasons_block.append("قدرت روند برای لانگ کافی نیست")

        if fake_breakout == "fake_bullish_breakout":
            reasons_block.append("احتمال فیک بریک‌اوت صعودی")

        if trend_exhaustion == "bullish_exhaustion":
            reasons_block.append("خستگی روند صعودی")

    if raw_direction == "SHORT":
        if short_score < long_score + 20:
            reasons_block.append("اختلاف امتیاز شورت و لانگ کافی نیست")

        if is_near_support(price, support, atr) and score < 88:
            reasons_block.append("قیمت نزدیک حمایت است")
            liquidity_risk = "متوسط"

        if last_5["rsi"] < 32:
            reasons_block.append("RSI برای شورت کمی پایین است")

        if last_15["adx"] < 18:
            reasons_block.append("قدرت روند برای شورت کافی نیست")

        if fake_breakout == "fake_bearish_breakout":
            reasons_block.append("احتمال فیک بریک‌اوت نزولی")

        if trend_exhaustion == "bearish_exhaustion":
            reasons_block.append("خستگی روند نزولی")

    if score < 74:
        reasons_block.append("امتیاز سیگنال برای ورود کافی نیست")

    # هشدارهای نرم به‌تنهایی باعث رد نمی‌شوند.
    hard_block_words = [
        "اختلاف امتیاز",
        "قدرت روند",
        "امتیاز سیگنال",
        "فیک بریک",
        "خستگی روند",
        "نوسان",
        "اسپرد",
    ]
    hard_blocks = [r for r in reasons_block if any(w in r for w in hard_block_words)]

    if hard_blocks:
        return False, reasons_block, liquidity_risk, fake_breakout, trend_exhaustion

    return True, reasons_block, liquidity_risk, fake_breakout, trend_exhaustion


def apply_conflict_penalties(
    long_score,
    short_score,
    trendline,
    structure,
    reasons_long,
    reasons_short
):
    if trendline == "uptrend":
        long_score += 12
        short_score -= 15
        reasons_long.append("تقویت: خط روند صعودی است")
        reasons_short.append("جریمه: شورت خلاف خط روند صعودی است")

    elif trendline == "downtrend":
        short_score += 12
        long_score -= 15
        reasons_short.append("تقویت: خط روند نزولی است")
        reasons_long.append("جریمه: لانگ خلاف خط روند نزولی است")

    if structure == "bullish_structure":
        long_score += 15
        short_score -= 18
        reasons_long.append("تقویت: ساختار بازار صعودی است")
        reasons_short.append("جریمه: شورت خلاف ساختار صعودی بازار است")

    elif structure == "bearish_structure":
        short_score += 15
        long_score -= 18
        reasons_short.append("تقویت: ساختار بازار نزولی است")
        reasons_long.append("جریمه: لانگ خلاف ساختار نزولی بازار است")

    return max(0, long_score), max(0, short_score)


def analyze_symbol(symbol):
    df_1d = add_indicators(get_klines(symbol, "1d"))
    df_4h = add_indicators(get_klines(symbol, "4h"))
    df_1h = add_indicators(get_klines(symbol, "1h"))
    df_30m = add_indicators(get_klines(symbol, "30m"))
    df_15m = add_indicators(get_klines(symbol, "15m"))
    df_5m = add_indicators(get_klines(symbol, "5m"))

    long_score = 0
    short_score = 0
    reasons_long = []
    reasons_short = []

    l, s, rl, rs, trends = score_macro_trend(df_1d, df_4h, df_1h, df_30m)
    long_score += l
    short_score += s
    reasons_long += rl
    reasons_short += rs

    l, s, rl, rs, buy_power, sell_power, pattern, multi_candle = score_entry(df_15m, df_5m)
    long_score += l
    short_score += s
    reasons_long += rl
    reasons_short += rs

    l, s, rl, rs, liquidity_grab, stop_hunt, fvg, order_block = score_smart_money(df_15m, df_5m)
    long_score += l
    short_score += s
    reasons_long += rl
    reasons_short += rs

    l, s, rl, rs, rsi_divergence, macd_divergence = score_divergence(df_5m)
    long_score += l
    short_score += s
    reasons_long += rl
    reasons_short += rs

    l, s, rl, rs, vwap_status, poc_price, volume_profile_status = score_vwap_volume_profile(df_15m, df_5m)
    long_score += l
    short_score += s
    reasons_long += rl
    reasons_short += rs

    long_score, short_score = apply_direction_conflict_penalties(
        long_score,
        short_score,
        pattern,
        multi_candle,
        order_block,
        fvg,
        vwap_status,
        buy_power,
        sell_power,
        reasons_long,
        reasons_short
    )

    btc_status, l, s, rl, rs = btc_filter(symbol)
    long_score += l
    short_score += s
    reasons_long += rl
    reasons_short += rs

    trendline = detect_trendline(df_15m)
    l, s = trendline_score(trendline)
    long_score += l
    short_score += s

    if trendline == "uptrend":
        reasons_long.append("خط روند 15M صعودی است")
    elif trendline == "downtrend":
        reasons_short.append("خط روند 15M نزولی است")

    breakout = detect_breakout(df_5m)
    l, s = breakout_score(breakout)
    long_score += l
    short_score += s

    if breakout == "bullish_breakout":
        reasons_long.append("بریک‌اوت صعودی در 5M")
    elif breakout == "bearish_breakout":
        reasons_short.append("بریک‌اوت نزولی در 5M")
    elif breakout == "fake_bullish_breakout":
        reasons_short.append("احتمال فیک بریک‌اوت صعودی")
    elif breakout == "fake_bearish_breakout":
        reasons_long.append("احتمال فیک بریک‌اوت نزولی")

    structure = detect_market_structure(df_15m)
    l, s = structure_score(structure)
    long_score += l
    short_score += s

    if structure == "bullish_structure":
        reasons_long.append("ساختار بازار 15M صعودی است")
    elif structure == "bearish_structure":
        reasons_short.append("ساختار بازار 15M نزولی است")

    long_score, short_score = apply_conflict_penalties(
        long_score, short_score, trendline, structure, reasons_long, reasons_short
    )

    l, s, rl, rs, market = score_market_sentiment(symbol)
    long_score += l
    short_score += s
    reasons_long += rl
    reasons_short += rs

    market_regime, market_regime_text, market_regime_score, market_regime_reasons = detect_market_regime(
        symbol,
        df_4h,
        df_1h,
        df_30m,
        df_15m,
        market
    )

    long_score, short_score = apply_market_regime_to_scores(
        long_score,
        short_score,
        market_regime,
        reasons_long,
        reasons_short
    )

    l, s, funding_rate, open_interest, risk_notes = score_futures_data(symbol)
    long_score += l
    short_score += s

    long_score = cap_score(long_score)
    short_score = cap_score(short_score)

    last = df_5m.iloc[-1]
    last_15 = df_15m.iloc[-1]
    price = float(last["close"])

    # برای معاملات 30 تا 60 دقیقه، ATR و ADX تایم 15M مناسب‌تر از 5M است.
    atr = float(last_15["atr"])
    adx_value = float(last_15["adx"])

    support, resistance = support_resistance(df_15m)

    setup_status, entry_zone_low, entry_zone_high, entry_trigger = calculate_setup_zone(
        "NO TRADE",
        price,
        atr
    )

    spread_percent = get_spread_percent(symbol)

    pre_direction = "NO TRADE"
    if long_score >= short_score + 25:
        pre_direction = "LONG"
    elif short_score >= long_score + 25:
        pre_direction = "SHORT"

    tq_l, tq_s, tq_rl, tq_rs, technical_context = technical_quality_context(
        pre_direction, price, atr, support, resistance, df_15m, df_5m, df_30m, df_1h
    )
    long_score = cap_score(long_score + tq_l)
    short_score = cap_score(short_score + tq_s)
    reasons_long += tq_rl
    reasons_short += tq_rs

    if long_score >= short_score + 25:
        raw_direction = "LONG"
        score = long_score
        reasons = reasons_long + risk_notes
    elif short_score >= long_score + 25:
        raw_direction = "SHORT"
        score = short_score
        reasons = reasons_short + risk_notes
    else:
        raw_direction = "NO TRADE"
        score = max(long_score, short_score)
        reasons = ["اختلاف لانگ و شورت کافی نیست"]

    setup_status, entry_zone_low, entry_zone_high, entry_trigger = calculate_setup_zone(
        raw_direction,
        price,
        atr
    )

    stop_loss_raw, tp1_raw, tp2_raw = calculate_trade_levels(
        raw_direction,
        price,
        atr,
        support,
        resistance
    )

    rr = risk_reward(raw_direction, price, stop_loss_raw, tp1_raw)

    score = normalize_score_by_quality(
        score,
        rr,
        raw_direction,
        pattern,
        multi_candle,
        order_block,
        vwap_status,
        fvg
    )

    entry_ok, block_reasons, liquidity_risk, fake_breakout, trend_exhaustion = entry_filter(
        raw_direction,
        score,
        long_score,
        short_score,
        df_15m,
        df_5m,
        spread_percent,
        market_regime,
        order_block,
        fvg,
        buy_power,
        sell_power,
        rsi_divergence,
        macd_divergence
    )

    risk_level = calculate_risk_level(
        raw_direction=raw_direction,
        score=score,
        liquidity_risk=liquidity_risk,
        funding_rate=funding_rate,
        adx=adx_value,
        spread_percent=spread_percent,
        rr=rr
    )

    if raw_direction == "NO TRADE" or not entry_ok:
        final_direction = "NO TRADE"
        reasons = reasons + block_reasons
        stop_loss = None
        tp1 = None
        tp2 = None
    else:
        final_direction = raw_direction
        stop_loss = stop_loss_raw
        tp1 = tp1_raw
        tp2 = tp2_raw

    grade = entry_grade(score, risk_level, rr, final_direction)

    if grade == "Reject":
        final_direction = "NO TRADE"
        stop_loss = None
        tp1 = None
        tp2 = None

    win_prob = win_probability(score, risk_level, rr, adx_value, grade)

    # واقعی‌تر کردن احتمال موفقیت وقتی نشانه‌های مهم خلاف جهت سیگنال هستند.
    if final_direction == "LONG":
        if fvg == "bearish_fvg":
            win_prob -= 8
        if multi_candle == "bearish" or pattern in ["bearish_engulfing", "bearish_pinbar", "bearish_strong"]:
            win_prob -= 6
    elif final_direction == "SHORT":
        if fvg == "bullish_fvg":
            win_prob -= 8
        if multi_candle == "bullish" or pattern in ["bullish_engulfing", "bullish_pinbar", "bullish_strong"]:
            win_prob -= 6
    win_prob = max(0, min(int(win_prob), 92))

    very_safe_ok, very_safe_reasons = very_safe_status(
        final_direction,
        score,
        win_prob,
        risk_level,
        rr,
        trends,
        vwap_status,
        buy_power,
        sell_power,
        adx_value,
        pattern,
        multi_candle,
        order_block,
        fvg,
        market_regime
    )

    return {
        "symbol": symbol,
        "price": safe_round(price, 8),
        "direction": final_direction,
        "raw_direction": raw_direction,
        "score": cap_score(score),

        "entry_grade": grade,
        "risk_level": risk_level,
        "risk_reward": rr,
        "win_probability": win_prob,

        "validity": signal_validity(score, final_direction),
        "signal_timeframe": signal_timeframe(score, final_direction),

        "rsi": safe_round(last["rsi"], 2),
        "macd": safe_round(last["macd"], 6),
        "macd_signal": safe_round(last["macd_signal"], 6),
        "macd_hist": safe_round(last["macd_hist"], 6),
        "ema20": safe_round(last["ema20"], 8),
        "ema50": safe_round(last["ema50"], 8),
        "ema200": safe_round(last["ema200"], 8),
        "atr": safe_round(atr, 8),
        "adx": safe_round(adx_value, 2),
        "vwap": safe_round(last["vwap"], 8),

        "stop_loss": None if stop_loss is None else safe_round(stop_loss, 8),
        "tp1": None if tp1 is None else safe_round(tp1, 8),
        "tp2": None if tp2 is None else safe_round(tp2, 8),

        "candidate_stop_loss": None if stop_loss_raw is None else safe_round(stop_loss_raw, 8),
        "candidate_tp1": None if tp1_raw is None else safe_round(tp1_raw, 8),
        "candidate_tp2": None if tp2_raw is None else safe_round(tp2_raw, 8),

        "support": safe_round(support, 8),
        "resistance": safe_round(resistance, 8),

        "buy_power": buy_power,
        "sell_power": sell_power,

        "trendline": trendline,
        "breakout": breakout,
        "market_structure": structure,
        "trends": trends,
        "btc_filter": btc_status,

        "candle_pattern": pattern,
        "multi_candle": multi_candle,
        "liquidity_grab": liquidity_grab,
        "stop_hunt": stop_hunt,
        "fvg": fvg,
        "order_block": order_block,
        "rsi_divergence": rsi_divergence,
        "macd_divergence": macd_divergence,
        "fake_breakout": fake_breakout,
        "trend_exhaustion": trend_exhaustion,

        "vwap_status": vwap_status,
        "poc_price": safe_round(poc_price, 8),
        "volume_profile_status": volume_profile_status,

        "funding_rate": funding_rate,
        "open_interest": open_interest,
        "spread_percent": spread_percent,
        "liquidity_risk": liquidity_risk,

        "fear_value": market.get("fear_value"),
        "fear_text": market.get("fear_text"),
        "btc_dominance": market.get("btc_dominance"),
        "dominance_status": market.get("dominance_status"),
        "altseason_status": market.get("altseason_status"),

        "market_regime": market_regime,
        "market_regime_text": market_regime_text,
        "market_regime_score": market_regime_score,
        "market_regime_reasons": market_regime_reasons,

        "long_score": long_score,
        "short_score": short_score,

        "setup_status": setup_status,
        "entry_zone_low": None if entry_zone_low is None else safe_round(entry_zone_low, 8),
        "entry_zone_high": None if entry_zone_high is None else safe_round(entry_zone_high, 8),
        "entry_trigger": entry_trigger,

        "very_safe": very_safe_ok,
        "very_safe_reasons": very_safe_reasons[:8],

        "news_filter_active": news_filter_active(),

        "noise_status": technical_context.get("noise_status"),
        "noise_label": technical_context.get("noise_label"),
        "volatility_status": technical_context.get("volatility_status"),
        "volatility_label": technical_context.get("volatility_label"),
        "late_entry": technical_context.get("late_entry"),
        "late_entry_reason": technical_context.get("late_entry_reason"),
        "tp_space_ok": technical_context.get("tp_space_ok"),
        "tp_space_reason": technical_context.get("tp_space_reason"),
        "tp_space_atr": technical_context.get("tp_space_atr"),
        "sr_entry_status": technical_context.get("sr_entry_status"),
        "sr_entry_label": technical_context.get("sr_entry_label"),
        "sr_entry_confirmed": technical_context.get("sr_entry_confirmed"),
        "bollinger_status": technical_context.get("bollinger_status"),
        "bollinger_label": technical_context.get("bollinger_label"),
        "bb_high": technical_context.get("bb_high"),
        "bb_mid": technical_context.get("bb_mid"),
        "bb_low": technical_context.get("bb_low"),
        "bb_width": technical_context.get("bb_width"),

        "reasons": reasons[:18],
    }
