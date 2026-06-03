# -*- coding: utf-8 -*-
import os
import ccxt
import pandas as pd
import ta

from market_sentiment import get_market_sentiment
from trend_analysis import detect_trendline, detect_breakout, trendline_score, breakout_score
from market_structure import detect_market_structure, structure_score


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
    recent = df.tail(80)
    return recent["low"].min(), recent["high"].max()


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
        short_score -= 18
        reasons_short.append("جریمه سنگین: اوردر بلاک صعودی، خلاف شورت است")

    if order_block == "bearish_order_block":
        long_score -= 18
        reasons_long.append("جریمه سنگین: اوردر بلاک نزولی، خلاف لانگ است")

    if fvg == "bullish_fvg":
        short_score -= 14
        reasons_short.append("جریمه سنگین: FVG صعودی، خلاف شورت است")

    if fvg == "bearish_fvg":
        long_score -= 14
        reasons_long.append("جریمه سنگین: FVG نزولی، خلاف لانگ است")

    if vwap_status == "above_vwap":
        short_score -= 8
        reasons_short.append("جریمه: قیمت بالای VWAP است و برای شورت ریسک دارد")

    if vwap_status == "below_vwap":
        long_score -= 8
        reasons_long.append("جریمه: قیمت پایین VWAP است و برای لانگ ریسک دارد")

    if buy_power >= sell_power + 10:
        short_score -= 18
        reasons_short.append("جریمه سنگین: قدرت خرید واضحاً خلاف شورت است")
    elif buy_power >= sell_power + 5:
        short_score -= 10
        reasons_short.append("جریمه: قدرت خرید نسبت به فروش بالاتر است")

    if sell_power >= buy_power + 10:
        long_score -= 18
        reasons_long.append("جریمه سنگین: قدرت فروش واضحاً خلاف لانگ است")
    elif sell_power >= buy_power + 5:
        long_score -= 10
        reasons_long.append("جریمه: قدرت فروش نسبت به خرید بالاتر است")

    return max(0, long_score), max(0, short_score)


def normalize_score_by_quality(score, rr, raw_direction, pattern, multi_candle, order_block, fvg, vwap_status, buy_power=50, sell_power=50, rsi_divergence="none", macd_divergence="none"):
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
            score = min(score, 84)

        if fvg == "bearish_fvg":
            score = min(score, 84)

        if buy_power < sell_power + 10:
            score = min(score, 84)

        if (
            rsi_divergence == "bearish_rsi_divergence"
            and macd_divergence == "bearish_macd_divergence"
        ):
            score = min(score, 84)

        if vwap_status == "below_vwap":
            score = min(score, 92)

    if raw_direction == "SHORT":
        if pattern in ["bullish_engulfing", "bullish_pinbar", "bullish_strong"] or multi_candle == "bullish":
            score = min(score, 88)

        if order_block == "bullish_order_block":
            score = min(score, 84)

        if fvg == "bullish_fvg":
            score = min(score, 84)

        if sell_power < buy_power + 10:
            score = min(score, 84)

        if (
            rsi_divergence == "bullish_rsi_divergence"
            and macd_divergence == "bullish_macd_divergence"
        ):
            score = min(score, 84)

        if vwap_status == "above_vwap":
            score = min(score, 92)

    return cap_score(score)

def calculate_trade_levels(raw_direction, price, atr, support=None, resistance=None):
    """
    TP/SL برای معاملات 30 تا 60 دقیقه.
    بعد از آمار واقعی، مشخص شد جهت خیلی از سیگنال‌ها درست است ولی SL با پولبک کوتاه لمس می‌شود.
    بنابراین SL کمی بازتر شده و TPها هم متناسب تنظیم شده‌اند تا R/R خراب نشود.
    """
    buffer = atr * 0.20

    if raw_direction == "LONG":
        stop_loss = price - (atr * 1.65)
        tp1 = price + (atr * 2.05)
        tp2 = price + (atr * 3.05)

        if resistance is not None and resistance > price:
            adjusted_tp1 = resistance - buffer
            if adjusted_tp1 > price + (atr * 1.25):
                tp1 = min(tp1, adjusted_tp1)

        return stop_loss, tp1, tp2

    if raw_direction == "SHORT":
        stop_loss = price + (atr * 1.65)
        tp1 = price - (atr * 2.05)
        tp2 = price - (atr * 3.05)

        if support is not None and support < price:
            adjusted_tp1 = support + buffer
            if adjusted_tp1 < price - (atr * 1.25):
                tp1 = max(tp1, adjusted_tp1)

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

    if score >= 94 and risk_level == "پایین" and rr >= 1.30:
        return "A+"

    if score >= 86 and risk_level in ["پایین", "متوسط"] and rr >= 1.15:
        return "A"

    if score >= 78 and rr >= 1:
        return "B"

    return "Reject"


def win_probability(score, risk_level, rr, adx, entry_grade_value):
    probability = 42

    probability += int(score * 0.22)

    if risk_level == "پایین":
        probability += 10
    elif risk_level == "متوسط":
        probability += 4
    else:
        probability -= 8

    if rr >= 1.5:
        probability += 6
    elif rr >= 1:
        probability += 3
    else:
        probability -= 8

    if adx >= 25:
        probability += 5
    elif adx < 18:
        probability -= 5

    if entry_grade_value == "A+":
        probability += 5
    elif entry_grade_value == "A":
        probability += 3
    elif entry_grade_value == "Reject":
        probability -= 15

    return max(0, min(probability, 95))


def news_filter_status():
    """
    فیلتر خبر مهم.
    فعلاً بدون API خارجی طراحی شده تا پایدار باشد:
    اگر HIGH_IMPACT_NEWS=1 یا NEWS_FILTER_ENABLED=1 شود، ورود جدید بلاک می‌شود.
    بعداً می‌توانیم این تابع را به API تقویم اقتصادی وصل کنیم.
    """
    high_impact_env = os.getenv("HIGH_IMPACT_NEWS", "0") == "1"
    news_enabled_env = os.getenv("NEWS_FILTER_ENABLED", "0") == "1"

    if high_impact_env or news_enabled_env:
        return True, "فیلتر خبر مهم فعال است"

    return False, "غیرفعال"


def news_filter_active():
    active, _ = news_filter_status()
    return active


def tp_space_validation(raw_direction, price, atr, support, resistance):
    """
    بررسی می‌کند تا TP1 فضای کافی وجود داشته باشد.
    هدف: لانگ نزدیک مقاومت و شورت نزدیک حمایت، بی‌دلیل سیگنال نشود.
    """
    if raw_direction == "LONG":
        if resistance is None or resistance <= price:
            return True, None

        space = resistance - price
        if space < atr * 1.30:
            return False, "فضای کافی تا مقاومت برای TP وجود ندارد"

    if raw_direction == "SHORT":
        if support is None or support >= price:
            return True, None

        space = price - support
        if space < atr * 1.30:
            return False, "فضای کافی تا حمایت برای TP وجود ندارد"

    return True, None


def calculate_setup_zone(raw_direction, price, atr):
    """
    ناحیه ورود پیشنهادی برای حالت Setup -> Entry Trigger.
    هدف: ورود عجولانه کمتر شود و سیگنال قبل از تایید 15M/30M وارد معامله نشود.
    """
    if raw_direction == "LONG":
        zone_low = price - (atr * 0.45)
        zone_high = price + (atr * 0.05)
        trigger = "ورود لانگ فقط بعد از پولبک به ناحیه ورود و تایید کندل صعودی در 15M/30M"

    elif raw_direction == "SHORT":
        zone_low = price - (atr * 0.05)
        zone_high = price + (atr * 0.45)
        trigger = "ورود شورت فقط بعد از پولبک به ناحیه ورود و تایید کندل نزولی در 15M/30M"

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

        if buy_power < sell_power + 10:
            reasons.append("اختلاف قدرت خرید و فروش برای Very Safe لانگ کافی نیست")

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

        if sell_power < buy_power + 10:
            reasons.append("اختلاف قدرت فروش و خرید برای Very Safe شورت کافی نیست")

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

    # فیلتر سخت بر اساس آمار واقعی:
    # اوردر بلاک مخالف جهت سیگنال، سیگنال را رد می‌کند.
    if raw_direction == "LONG" and order_block == "bearish_order_block":
        reasons_block.append("اوردر بلاک نزولی خلاف سیگنال لانگ است")
        liquidity_risk = "بالا"

    if raw_direction == "SHORT" and order_block == "bullish_order_block":
        reasons_block.append("اوردر بلاک صعودی خلاف سیگنال شورت است")
        liquidity_risk = "بالا"

    # فیلتر سخت FVG:
    # اگر ناحیه خالی نقدینگی خلاف جهت سیگنال باشد، سیگنال رد می‌شود.
    if raw_direction == "LONG" and fvg == "bearish_fvg":
        reasons_block.append("FVG نزولی خلاف سیگنال لانگ است")
        liquidity_risk = "بالا"

    if raw_direction == "SHORT" and fvg == "bullish_fvg":
        reasons_block.append("FVG صعودی خلاف سیگنال شورت است")
        liquidity_risk = "بالا"

    # فیلتر سخت قدرت خرید/فروش:
    # قدرت جهت سیگنال باید حداقل 10٪ برتری داشته باشد.
    if raw_direction == "LONG":
        if buy_power < sell_power + 10:
            reasons_block.append("اختلاف قدرت خرید و فروش برای لانگ کافی نیست")
            liquidity_risk = "بالا"

    if raw_direction == "SHORT":
        if sell_power < buy_power + 10:
            reasons_block.append("اختلاف قدرت فروش و خرید برای شورت کافی نیست")
            liquidity_risk = "بالا"

    # فیلتر سخت واگرایی دوگانه مخالف:
    if raw_direction == "LONG":
        if (
            rsi_divergence == "bearish_rsi_divergence"
            and macd_divergence == "bearish_macd_divergence"
        ):
            reasons_block.append("واگرایی دوگانه نزولی خلاف سیگنال لانگ است")
            liquidity_risk = "بالا"

    if raw_direction == "SHORT":
        if (
            rsi_divergence == "bullish_rsi_divergence"
            and macd_divergence == "bullish_macd_divergence"
        ):
            reasons_block.append("واگرایی دوگانه صعودی خلاف سیگنال شورت است")
            liquidity_risk = "بالا"

    # خلاف روند کلی بازار فقط برای سیگنال‌های خیلی قوی مجاز است.
    if market_regime == "bearish" and raw_direction == "LONG" and score < 95:
        reasons_block.append("لانگ خلاف روند کلی نزولی بازار است")
        liquidity_risk = "بالا"

    if market_regime == "bullish" and raw_direction == "SHORT" and score < 95:
        reasons_block.append("شورت خلاف روند کلی صعودی بازار است")
        liquidity_risk = "بالا"

    if market_regime == "neutral" and score < 86:
        reasons_block.append("بازار کلی خنثی است و سیگنال قدرت کافی ندارد")
        liquidity_risk = "بالا"

    news_active, news_reason = news_filter_status()
    if news_active:
        reasons_block.append(news_reason)
        liquidity_risk = "بالا"

    if market_is_choppy(df_15m, df_5m):
        reasons_block.append("بازار رنج، فشرده یا کم‌قدرت است")
        liquidity_risk = "بالا"

    if not minimum_volatility_ok(df_5m):
        reasons_block.append("نوسان برای اسکالپ کافی نیست")
        liquidity_risk = "بالا"

    if spread_percent is not None and spread_percent > 0.08:
        reasons_block.append("اسپرد برای اسکالپ زیاد است")
        liquidity_risk = "بالا"

    if is_middle_of_range(price, support, resistance):
        reasons_block.append("قیمت وسط رنج است")
        liquidity_risk = "بالا"

    fake_breakout = detect_fake_breakout(df_5m)
    trend_exhaustion = detect_trend_exhaustion(df_5m)

    if raw_direction == "LONG":
        if long_score < short_score + 25:
            reasons_block.append("اختلاف امتیاز لانگ و شورت کافی نیست")

        if is_near_resistance(price, resistance, atr):
            reasons_block.append("قیمت نزدیک مقاومت است")
            liquidity_risk = "بالا"

        if last_5["rsi"] > 65:
            reasons_block.append("RSI برای لانگ بیش از حد بالاست")

        if last_15["adx"] < 20:
            reasons_block.append("قدرت روند برای لانگ کافی نیست")

        if fake_breakout == "fake_bullish_breakout":
            reasons_block.append("احتمال فیک بریک‌اوت صعودی")

        if trend_exhaustion == "bullish_exhaustion":
            reasons_block.append("خستگی روند صعودی")

    if raw_direction == "SHORT":
        if short_score < long_score + 25:
            reasons_block.append("اختلاف امتیاز شورت و لانگ کافی نیست")

        if is_near_support(price, support, atr):
            reasons_block.append("قیمت نزدیک حمایت است")
            liquidity_risk = "بالا"

        if last_5["rsi"] < 35:
            reasons_block.append("RSI برای شورت بیش از حد پایین است")

        if last_15["adx"] < 20:
            reasons_block.append("قدرت روند برای شورت کافی نیست")

        if fake_breakout == "fake_bearish_breakout":
            reasons_block.append("احتمال فیک بریک‌اوت نزولی")

        if trend_exhaustion == "bearish_exhaustion":
            reasons_block.append("خستگی روند نزولی")

    if score < 72:
        reasons_block.append("امتیاز سیگنال برای ورود کافی نیست")

    if reasons_block:
        return False, reasons_block, liquidity_risk, fake_breakout, trend_exhaustion

    return True, [], liquidity_risk, fake_breakout, trend_exhaustion


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
        fvg,
        vwap_status,
        buy_power,
        sell_power,
        rsi_divergence,
        macd_divergence
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

        "reasons": reasons[:24],
    }
