# -*- coding: utf-8 -*-
import time
import os
import ccxt
import pandas as pd
import ta

from market_sentiment import get_market_sentiment


TECHNICAL_QUALITY_LATE_ENTRY_ATR = 1.65
TECHNICAL_QUALITY_MIN_TP_SPACE_ATR = 0.95
TECHNICAL_QUALITY_LOW_ATR_PCT = 0.08
TECHNICAL_QUALITY_EXTREME_ATR_PCT = 3.5

# تنظیمات معماری جدید: Setup تکنیکال، سپس فعال‌سازی سریع با Power دو کندلی.
# اعداد عمداً متعادل‌اند تا ربات خشک نشود، اما ورودهای ضعیف مثل شورت با Sell2 پایین را رد کند.
SETUP_MIN_SCORE = 7
SETUP_MIN_EDGE = 2
ENTRY_MIN_CONFIRMATIONS = 4
ENTRY_POWER2_MIN = 60.0
ENTRY_POWER2_STRONG = 64.0
ENTRY_OPPOSITE_POWER2_MAX = 42.0
ENTRY_POWER_ACCEL_MIN = 2.0
ENTRY_MAX_EMA_DISTANCE_ATR = 0.90
ENTRY_MAX_VWAP_DISTANCE_ATR = 1.05


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


def get_klines(symbol, interval="15m", limit=320, include_current=False):
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
    if not include_current:
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


def buy_sell_power(df, candles=20):
    recent = df.tail(candles)

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



def volume_spike(df):
    last = df.iloc[-1]

    if last["volume_ma20"] == 0:
        return False

    return last["volume"] > last["volume_ma20"] * 1.5







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
    روند کلی بازار در نسخه ساده فقط بایاس خیلی نرم می‌دهد.
    """
    try:
        if symbol == "BTCUSDT":
            btc_1h = df_1h
            btc_30m = df_30m
        else:
            btc_1h = add_indicators(get_klines("BTCUSDT", "1h"))
            btc_30m = add_indicators(get_klines("BTCUSDT", "30m"))

        t1 = trend_direction(btc_1h)
        t30 = trend_direction(btc_30m)

        bearish = sum(1 for t in [t1, t30] if t in ["bearish", "weak_bearish"])
        bullish = sum(1 for t in [t1, t30] if t in ["bullish", "weak_bullish"])

        if bearish >= 2:
            return "bearish", "نزولی", -2, ["BTC در تایم‌های اصلی نزولی است"]
        if bullish >= 2:
            return "bullish", "صعودی", 2, ["BTC در تایم‌های اصلی صعودی است"]

        return "neutral", "خنثی", 0, ["BTC جهت واضحی ندارد"]

    except Exception:
        return "neutral", "نامشخص", 0, []

def apply_market_regime_to_scores(long_score, short_score, market_regime, reasons_long, reasons_short):
    """
    روند کلی بازار فقط اثر بسیار نرم دارد.
    """
    if market_regime == "bearish":
        short_score += 5
        long_score -= 5
        reasons_short.append("روند کلی بازار کمی به نفع شورت است")
    elif market_regime == "bullish":
        long_score += 5
        short_score -= 5
        reasons_long.append("روند کلی بازار کمی به نفع لانگ است")

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








def technical_quality_context(raw_direction, price, atr, support, resistance, df_15m, df_5m, df_30m, df_1h):
    """
    لایه حرفه‌ای اما نرم برای فیوچرز.
    بیشتر موارد فقط امتیاز را کم می‌کنند و برای آمار/دلایل SL ذخیره می‌شوند؛
    ربات را خشک نمی‌کند، اما ورودهای خیلی دیر یا TP بدون فضا را مشخص می‌کند.
    """
    long_adj = 0
    short_adj = 0
    reasons_long = []
    reasons_short = []

    context = {
        "technical_quality_long_adj": 0,
        "technical_quality_short_adj": 0,
        "sr_entry_status": "soft",
        "sr_entry_label": None,
        "sr_entry_confirmed": False,
        "tp_space_ok": True,
        "tp_space_reason": None,
        "tp_space_atr": None,
        "late_entry": False,
        "late_entry_reason": None,
        "trap_risk": False,
        "trap_reason": None,
        "distance_from_vwap_atr": None,
        "distance_from_ema20_atr": None,
        "candle_forecast": "neutral",
        "candle_forecast_reason": None,
    }

    try:
        last_15 = df_15m.iloc[-1]
        last_5 = df_5m.iloc[-1]
        prev_5 = df_5m.iloc[-2]
        atr_value = float(atr) if atr and atr > 0 else float(last_15.get("atr", 0))
        if atr_value <= 0:
            return 0, 0, [], [], context

        vwap_distance = abs(float(price) - float(last_5["vwap"])) / atr_value
        ema_distance = abs(float(price) - float(last_5["ema20"])) / atr_value
        context["distance_from_vwap_atr"] = round(vwap_distance, 2)
        context["distance_from_ema20_atr"] = round(ema_distance, 2)

        candle_body = abs(float(last_5["close"]) - float(last_5["open"]))
        candle_range = max(float(last_5["high"]) - float(last_5["low"]), 0)
        large_candle = candle_range >= atr_value * 0.85 or candle_body >= atr_value * 0.55

        # Anti Late Entry: فقط اگر فاصله خیلی زیاد یا کندل جهشی باشد، جریمه نرم می‌دهد.
        if raw_direction == "LONG" and price > last_5["ema20"] and (vwap_distance > 1.35 or ema_distance > 1.20 or large_candle):
            long_adj -= 7
            context["late_entry"] = True
            context["late_entry_reason"] = "فاصله قیمت از EMA/VWAP یا اندازه کندل برای لانگ زیاد بود"
            reasons_long.append("ورود لانگ کمی دیر است؛ امتیاز کاهش یافت")
        elif raw_direction == "SHORT" and price < last_5["ema20"] and (vwap_distance > 1.35 or ema_distance > 1.20 or large_candle):
            short_adj -= 7
            context["late_entry"] = True
            context["late_entry_reason"] = "فاصله قیمت از EMA/VWAP یا اندازه کندل برای شورت زیاد بود"
            reasons_short.append("ورود شورت کمی دیر است؛ امتیاز کاهش یافت")

        # TP Space / Trap: TP نباید روی حمایت/مقاومت بیفتد. اینجا نرم است؛ فقط اگر خیلی نزدیک باشد امتیاز کم می‌کند.
        if raw_direction == "LONG" and resistance is not None and resistance > price:
            space_atr = (float(resistance) - float(price)) / atr_value
            context["tp_space_atr"] = round(space_atr, 2)
            if space_atr < 0.70:
                long_adj -= 10
                context["tp_space_ok"] = False
                context["tp_space_reason"] = "مقاومت خیلی نزدیک است و فضای TP برای لانگ کم است"
                context["trap_risk"] = True
                context["trap_reason"] = "لانگ نزدیک مقاومت مهم ثبت شده بود"
                reasons_long.append("مقاومت نزدیک است؛ TP Space برای لانگ ضعیف است")
            elif space_atr < 1.00:
                long_adj -= 5
                context["tp_space_reason"] = "مقاومت نسبتاً نزدیک است"
                reasons_long.append("مقاومت نسبتاً نزدیک است؛ امتیاز لانگ کمی کاهش یافت")

        if raw_direction == "SHORT" and support is not None and support < price:
            space_atr = (float(price) - float(support)) / atr_value
            context["tp_space_atr"] = round(space_atr, 2)
            if space_atr < 0.70:
                short_adj -= 10
                context["tp_space_ok"] = False
                context["tp_space_reason"] = "حمایت خیلی نزدیک است و فضای TP برای شورت کم است"
                context["trap_risk"] = True
                context["trap_reason"] = "شورت نزدیک حمایت مهم ثبت شده بود"
                reasons_short.append("حمایت نزدیک است؛ TP Space برای شورت ضعیف است")
            elif space_atr < 1.00:
                short_adj -= 5
                context["tp_space_reason"] = "حمایت نسبتاً نزدیک است"
                reasons_short.append("حمایت نسبتاً نزدیک است؛ امتیاز شورت کمی کاهش یافت")

        # Candle Forecast ساده و سبک: فقط برای تصمیم داخلی و دلایل SL ذخیره می‌شود.
        if last_5["close"] > last_5["ema20"] and last_5["macd_hist"] > prev_5["macd_hist"] and last_5["close"] > last_5["vwap"]:
            context["candle_forecast"] = "bullish_continuation"
            context["candle_forecast_reason"] = "کندل، EMA، VWAP و شیب MACD به نفع ادامه صعود بودند"
            if raw_direction == "SHORT":
                short_adj -= 6
                reasons_short.append("پیش‌بینی کندلی کوتاه‌مدت خلاف شورت است")
        elif last_5["close"] < last_5["ema20"] and last_5["macd_hist"] < prev_5["macd_hist"] and last_5["close"] < last_5["vwap"]:
            context["candle_forecast"] = "bearish_continuation"
            context["candle_forecast_reason"] = "کندل، EMA، VWAP و شیب MACD به نفع ادامه نزول بودند"
            if raw_direction == "LONG":
                long_adj -= 6
                reasons_long.append("پیش‌بینی کندلی کوتاه‌مدت خلاف لانگ است")
        else:
            context["candle_forecast"] = "neutral_or_pullback"
            context["candle_forecast_reason"] = "کندل بعدی قطعیت کافی ندارد یا احتمال پولبک وجود دارد"

    except Exception:
        return 0, 0, [], [], context

    context["technical_quality_long_adj"] = long_adj
    context["technical_quality_short_adj"] = short_adj
    return long_adj, short_adj, reasons_long, reasons_short, context


def signal_validity(score, direction):
    if direction == "NO TRADE":
        return "سیگنال معتبر نیست"

    if score >= 90:
        return "5 تا 15 دقیقه"

    if score >= 80:
        return "5 تا 15 دقیقه"

    if score >= 70:
        return "5 تا 10 دقیقه"

    return "اعتبار پایین"


def signal_timeframe(score, direction):
    if direction == "NO TRADE":
        return "بدون تایم‌فریم ورود"

    return "5M تا 15M"


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

    # Fast Pump/Dump Scalp Mode:
    # 1H و 4H فقط جهت کلی هستند؛ 30M کیفیت ستاپ است و 5M داخل score_entry موتور اصلی ورود است.
    weights = {
        "1D": 1,
        "4H": 3,
        "1H": 6,
        "30M": 11,
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

    buy_power, sell_power = buy_sell_power(df_5m, candles=20)
    fast_buy_power, fast_sell_power = buy_sell_power(df_5m, candles=6)
    ultra_buy_power, ultra_sell_power = buy_sell_power(df_5m, candles=3)
    instant_buy_power, instant_sell_power = buy_sell_power(df_5m, candles=2)

    # 15M فقط تایید نرم جهت است، نه ترمز سنگین ورود.
    if last_15["close"] > last_15["ema20"]:
        long_score += 14
        reasons_long.append("15M: جهت ورود لانگ را تایید نرم می‌کند")

    if last_15["close"] < last_15["ema20"]:
        short_score += 14
        reasons_short.append("15M: جهت ورود شورت را تایید نرم می‌کند")

    # 5M موتور اصلی ورود است.
    if last_5["close"] > last_5["ema20"] and last_5["macd"] > last_5["macd_signal"]:
        long_score += 45
        reasons_long.append("5M: تایید ورود لانگ با EMA و MACD")

    if last_5["close"] < last_5["ema20"] and last_5["macd"] < last_5["macd_signal"]:
        short_score += 45
        reasons_short.append("5M: تایید ورود شورت با EMA و MACD")

    # RSI عددی فقط اثر ملایم دارد؛ ورود سریع با RSI slope پایین‌تر انجام می‌شود.
    if 45 <= last_5["rsi"] <= 68:
        long_score += 8
        reasons_long.append("RSI مناسب برای لانگ در 5M")

    if 32 <= last_5["rsi"] <= 55:
        short_score += 8
        reasons_short.append("RSI مناسب برای شورت در 5M")

    # Power کوتاه‌مدت برای پامپ/دامپ بیشترین اهمیت را دارد.
    if buy_power >= 62:
        long_score += 6
        reasons_long.append("قدرت خرید کلی بالا در تایم ورود")

    if fast_buy_power >= 62:
        long_score += 10
        reasons_long.append("قدرت خرید سریع در 5M بالا است")

    if ultra_buy_power >= 66:
        long_score += 14
        reasons_long.append("قدرت خرید خیلی سریع 3 کندلی بالا است")

    if instant_buy_power >= 68:
        long_score += 18
        reasons_long.append("قدرت خرید لحظه‌ای 2 کندلی بالا است")

    if sell_power >= 62:
        short_score += 6
        reasons_short.append("قدرت فروش کلی بالا در تایم ورود")

    if fast_sell_power >= 62:
        short_score += 10
        reasons_short.append("قدرت فروش سریع در 5M بالا است")

    if ultra_sell_power >= 66:
        short_score += 14
        reasons_short.append("قدرت فروش خیلی سریع 3 کندلی بالا است")

    if instant_sell_power >= 68:
        short_score += 18
        reasons_short.append("قدرت فروش لحظه‌ای 2 کندلی بالا است")

    pattern = candle_pattern(df_5m)
    multi_candle = "disabled"

    if pattern in ["bullish_engulfing", "bullish_pinbar", "bullish_strong"]:
        long_score += 10
        reasons_long.append(f"کندل تاییدی لانگ: {pattern}")

    if pattern in ["bearish_engulfing", "bearish_pinbar", "bearish_strong"]:
        short_score += 10
        reasons_short.append(f"کندل تاییدی شورت: {pattern}")

    if volume_spike(df_5m):
        long_score += 6
        short_score += 6
        reasons_long.append("افزایش حجم واقعی")
        reasons_short.append("افزایش حجم واقعی")

    if last_15["adx"] >= 18:
        long_score += 4
        short_score += 4
        reasons_long.append("ADX قابل قبول در 15M")
        reasons_short.append("ADX قابل قبول در 15M")

    # تریگر خیلی سریع پامپ/دامپ:
    # Histogram دو کندلی + RSI slope دو کندلی + Power دو کندلی + EMA20.
    try:
        prev_5 = df_5m.iloc[-2]

        macd_hist_rising = last_5["macd_hist"] > prev_5["macd_hist"]
        macd_hist_falling = last_5["macd_hist"] < prev_5["macd_hist"]
        rsi_rising = last_5["rsi"] > prev_5["rsi"]
        rsi_falling = last_5["rsi"] < prev_5["rsi"]

        if (
            last_5["close"] > last_5["ema20"]
            and macd_hist_rising
            and rsi_rising
            and instant_buy_power >= 64
        ):
            long_score += 28
            reasons_long.append("تریگر سریع پامپ: EMA، Histogram، RSI و Power دو کندلی همسو هستند")

        if (
            last_5["close"] < last_5["ema20"]
            and macd_hist_falling
            and rsi_falling
            and instant_sell_power >= 64
        ):
            short_score += 28
            reasons_short.append("تریگر سریع دامپ: EMA، Histogram، RSI و Power دو کندلی همسو هستند")
    except Exception:
        pass

    return long_score, short_score, reasons_long, reasons_short, buy_power, sell_power, pattern, multi_candle

def score_smart_money(df_15m, df_5m):
    """
    FVG و Order Block فقط اثر سبک دارند و هیچ سیگنالی را رد نمی‌کنند.
    """
    long_score = 0
    short_score = 0
    reasons_long = []
    reasons_short = []

    fvg = detect_fvg(df_5m)
    order_block = detect_order_block(df_15m)

    if fvg == "bullish_fvg":
        long_score += 2
        reasons_long.append("FVG صعودی، اثر سبک")

    if fvg == "bearish_fvg":
        short_score += 2
        reasons_short.append("FVG نزولی، اثر سبک")

    if order_block == "bullish_order_block":
        long_score += 3
        reasons_long.append("Order Block صعودی هم‌جهت، اثر سبک")

    if order_block == "bearish_order_block":
        short_score += 3
        reasons_short.append("Order Block نزولی هم‌جهت، اثر سبک")

    return long_score, short_score, reasons_long, reasons_short, "none", "none", fvg, order_block

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
    """
    Fear & Greed و Altseason فقط 1 امتیاز اثر ناچیز دارند.
    """
    market = get_market_sentiment()

    long_score = 0
    short_score = 0
    reasons_long = []
    reasons_short = []

    fear_value = market.get("fear_value")
    altseason = market.get("altseason_status")

    if fear_value is not None:
        if fear_value <= 25:
            long_score += 1
            reasons_long.append("Fear & Greed در ترس شدید، اثر ناچیز")
        elif fear_value >= 80:
            short_score += 1
            reasons_short.append("Fear & Greed در طمع شدید، اثر ناچیز")

    if symbol != "BTCUSDT":
        if altseason == "قوی":
            long_score += 1
            reasons_long.append("آلت‌سیزن قوی، اثر ناچیز")
        elif altseason == "ضعیف":
            short_score += 1
            reasons_short.append("آلت‌سیزن ضعیف، اثر ناچیز")

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
    تناقض‌ها فقط جریمه نرم دارند؛ هیچ موردی در این لایه سیگنال را حذف نمی‌کند.
    """
    bullish_candle = pattern in ["bullish_engulfing", "bullish_pinbar", "bullish_strong"]
    bearish_candle = pattern in ["bearish_engulfing", "bearish_pinbar", "bearish_strong"]

    if bullish_candle:
        short_score -= 4
        reasons_short.append("کندل صعودی خلاف شورت است")

    if bearish_candle:
        long_score -= 4
        reasons_long.append("کندل نزولی خلاف لانگ است")

    if vwap_status == "above_vwap":
        short_score -= 4
        reasons_short.append("قیمت بالای VWAP است و برای شورت ریسک دارد")

    if vwap_status == "below_vwap":
        long_score -= 4
        reasons_long.append("قیمت پایین VWAP است و برای لانگ ریسک دارد")

    try:
        power_gap = float(buy_power) - float(sell_power)
    except Exception:
        power_gap = 0

    if power_gap > 0:
        if power_gap >= 12:
            short_score -= 9
            reasons_short.append("قدرت خرید به‌طور واضح از فروش بالاتر است")
        elif power_gap >= 5:
            short_score -= 5
            reasons_short.append("قدرت خرید نسبت به فروش کمی بالاتر است")
        else:
            short_score -= 2
            reasons_short.append("قدرت خرید اندکی از فروش بالاتر است")

    elif power_gap < 0:
        sell_gap = abs(power_gap)
        if sell_gap >= 12:
            long_score -= 9
            reasons_long.append("قدرت فروش به‌طور واضح از خرید بالاتر است")
        elif sell_gap >= 5:
            long_score -= 5
            reasons_long.append("قدرت فروش نسبت به خرید کمی بالاتر است")
        else:
            long_score -= 2
            reasons_long.append("قدرت فروش اندکی از خرید بالاتر است")

    if order_block == "bullish_order_block":
        short_score -= 3
        reasons_short.append("Order Block صعودی خلاف شورت است؛ جریمه خیلی سبک اعمال شد")
    elif order_block == "bearish_order_block":
        long_score -= 3
        reasons_long.append("Order Block نزولی خلاف لانگ است؛ جریمه خیلی سبک اعمال شد")

    if fvg == "bullish_fvg":
        short_score -= 3
        reasons_short.append("FVG صعودی کمی خلاف شورت است")
    elif fvg == "bearish_fvg":
        long_score -= 3
        reasons_long.append("FVG نزولی کمی خلاف لانگ است")

    return max(0, long_score), max(0, short_score)

def normalize_score_by_quality(score, rr, raw_direction, pattern, multi_candle, order_block, vwap_status, fvg="none"):
    """
    برای اسکالپ، RR پایین‌تر قابل قبول است؛ فقط RR خیلی بد امتیاز را محدود می‌کند.
    """
    if raw_direction == "NO TRADE":
        return score

    if rr < 0.55:
        score = min(score, 74)
    elif rr < 0.65:
        score = min(score, 82)
    elif rr < 0.80:
        score = min(score, 90)

    return cap_score(score)

def calculate_trade_levels(raw_direction, price, atr, support=None, resistance=None):
    """
    TP/SL اسکالپی:
    TP1 نزدیک‌تر است تا حرکت‌های 5 تا 15 دقیقه‌ای از دست نروند؛ SL بیش از حد تنگ نمی‌شود.
    """
    buffer = atr * 0.18

    if raw_direction == "LONG":
        stop_loss = price - (atr * 1.10)
        tp1 = price + (atr * 0.85)
        tp2 = price + (atr * 1.20)

        if support is not None and support < price:
            structural_sl = float(support) - buffer
            if abs(price - structural_sl) <= atr * 1.75:
                stop_loss = min(stop_loss, structural_sl)

        if resistance is not None and resistance > price:
            adjusted_tp1 = float(resistance) - buffer
            if adjusted_tp1 > price:
                tp1 = min(tp1, adjusted_tp1)
            adjusted_tp2 = float(resistance) + (atr * 0.22)
            if adjusted_tp2 > price:
                tp2 = min(tp2, adjusted_tp2)

        return stop_loss, tp1, tp2

    if raw_direction == "SHORT":
        stop_loss = price + (atr * 1.10)
        tp1 = price - (atr * 0.85)
        tp2 = price - (atr * 1.20)

        if resistance is not None and resistance > price:
            structural_sl = float(resistance) + buffer
            if abs(structural_sl - price) <= atr * 1.75:
                stop_loss = max(stop_loss, structural_sl)

        if support is not None and support < price:
            adjusted_tp1 = float(support) + buffer
            if adjusted_tp1 < price:
                tp1 = max(tp1, adjusted_tp1)
            adjusted_tp2 = float(support) - (atr * 0.22)
            if adjusted_tp2 < price:
                tp2 = max(tp2, adjusted_tp2)

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

    if score < 72:
        risk += 2

    if adx < 13:
        risk += 2
    elif adx < 16:
        risk += 1

    if liquidity_risk == "بالا":
        risk += 2
    elif liquidity_risk == "متوسط":
        risk += 1

    if funding_rate is not None and abs(funding_rate) > 0.07:
        risk += 1

    if spread_percent is not None and spread_percent > 0.08:
        risk += 2

    if rr < 0.60:
        risk += 2
    elif rr < 0.75:
        risk += 1

    if risk >= 4:
        return "بالا"

    if risk >= 2:
        return "متوسط"

    return "پایین"

def entry_grade(score, risk_level, rr, final_direction):
    if final_direction == "NO TRADE":
        return "Reject"

    if score >= 90 and rr >= 0.80 and risk_level != "بالا":
        return "A+"

    if score >= 81 and rr >= 0.62 and risk_level != "بالا":
        return "A"

    return "Reject"

def win_probability(score, risk_level, rr, adx, grade):
    """
    احتمال موفقیت متناسب با اسکالپ؛ ADX پایین به اندازه نسخه‌های قبلی تنبیه نمی‌شود.
    """
    p = 40 + int(score * 0.28)
    p += 6 if risk_level == "پایین" else 2 if risk_level == "متوسط" else -4
    p += 5 if rr >= 1.0 else 2 if rr >= 0.70 else -4
    p += 3 if adx >= 20 else -3 if adx < 13 else -1 if adx < 16 else 0
    p += 4 if grade == "A+" else 2 if grade == "A" else -5
    return max(0, min(p, 92))

def news_filter_status():
    """اخبار از تصمیم‌گیری حذف شده است."""
    return False, "غیرفعال"

def news_filter_active():
    return False




def calculate_setup_zone(raw_direction, price, atr):
    """
    ناحیه ورود پیشنهادی برای اسکالپ سریع.
    """
    if raw_direction == "LONG":
        zone_low = price - (atr * 0.25)
        zone_high = price + (atr * 0.08)
        trigger = "ورود لانگ بعد از حفظ EMA20 و ادامه قدرت خرید در 5M/15M"

    elif raw_direction == "SHORT":
        zone_low = price - (atr * 0.08)
        zone_high = price + (atr * 0.25)
        trigger = "ورود شورت بعد از حفظ EMA20 و ادامه قدرت فروش در 5M/15M"

    else:
        return "inactive", None, None, "ستاپ فعالی وجود ندارد"

    return "ready", zone_low, zone_high, trigger

def very_safe_status(raw_direction, score, win_probability_value, risk_level, rr, trends,
                     vwap_status, buy_power, sell_power, adx_value,
                     pattern=None, multi_candle=None, order_block=None, fvg=None,
                     market_regime="neutral"):
    """
    حالت خیلی امن فقط نمایشی است و روی سیگنال عادی اثر ندارد.
    """
    reasons = []

    if raw_direction not in ["LONG", "SHORT"]:
        return False, ["جهت مشخص نیست"]

    if score < 88:
        reasons.append("امتیاز کمتر از حد Very Safe است")

    if win_probability_value is not None and win_probability_value < 68:
        reasons.append("احتمال موفقیت کمتر از حد Very Safe است")

    if rr < 1.0:
        reasons.append("ریسک به ریوارد برای Very Safe کافی نیست")

    if adx_value < 25:
        reasons.append("ADX برای Very Safe قوی نیست")

    if raw_direction == "LONG" and buy_power < 60:
        reasons.append("قدرت خرید برای Very Safe کافی نیست")

    if raw_direction == "SHORT" and sell_power < 60:
        reasons.append("قدرت فروش برای Very Safe کافی نیست")

    return len(reasons) == 0, reasons

def apply_final_momentum_balance(score, raw_direction, adx_value, buy_power, sell_power, rsi_value, reasons):
    """
    بالانس نهایی سریع‌تر برای Fast Pump/Dump Mode.
    ADX و RSI دیگر شروع حرکت را خفه نمی‌کنند؛ فقط خلاف‌جهت‌های واضح امتیاز را کم می‌کنند.
    """
    if raw_direction == "NO TRADE":
        return cap_score(score)

    try:
        adx_value = float(adx_value)
    except Exception:
        adx_value = 18.0

    try:
        buy_power = float(buy_power)
        sell_power = float(sell_power)
    except Exception:
        buy_power = 50.0
        sell_power = 50.0

    try:
        rsi_value = float(rsi_value)
    except Exception:
        rsi_value = 50.0

    power_gap = buy_power - sell_power

    if raw_direction == "SHORT":
        aligned_power = power_gap <= -4
        opposite_power = power_gap >= 6
    elif raw_direction == "LONG":
        aligned_power = power_gap >= 4
        opposite_power = power_gap <= -6
    else:
        aligned_power = False
        opposite_power = False

    if adx_value < 13:
        if opposite_power:
            score -= 10
            score = min(score, 82)
            reasons.append("ADX بسیار پایین و قدرت خلاف جهت است؛ امتیاز محدود شد")
        elif not aligned_power:
            score -= 6
            score = min(score, 86)
            reasons.append("ADX بسیار پایین است و قدرت تایید کامل ندارد")
        else:
            score -= 2
            score = min(score, 94)
            reasons.append("ADX پایین است اما قدرت هم‌جهت دیده می‌شود")
    elif adx_value < 16:
        if opposite_power:
            score -= 7
            score = min(score, 86)
            reasons.append("ADX ضعیف و قدرت خلاف جهت است")
        elif aligned_power:
            score -= 1
            score = min(score, 96)
            reasons.append("ADX ضعیف است اما قدرت هم‌جهت اجازه عبور می‌دهد")
        else:
            score -= 4
            score = min(score, 90)
            reasons.append("ADX ضعیف و قدرت خنثی است")

    if raw_direction == "SHORT":
        if power_gap >= 12:
            score -= 9
            reasons.append("قدرت خرید به‌وضوح خلاف شورت است")
        elif power_gap >= 6:
            score -= 5
            reasons.append("قدرت خرید خلاف شورت است")
        if rsi_value > 58:
            score -= 4
            reasons.append("RSI برای شورت کمی بالاست")
    elif raw_direction == "LONG":
        sell_gap = -power_gap
        if sell_gap >= 12:
            score -= 9
            reasons.append("قدرت فروش به‌وضوح خلاف لانگ است")
        elif sell_gap >= 6:
            score -= 5
            reasons.append("قدرت فروش خلاف لانگ است")
        if rsi_value < 42:
            score -= 4
            reasons.append("RSI برای لانگ کمی پایین است")

    return cap_score(score)

def entry_filter(raw_direction, score, long_score, short_score, df_15m, df_5m, spread_percent, market_regime="neutral", order_block="none", fvg="none", buy_power=50, sell_power=50, rsi_divergence="none", macd_divergence="none"):
    """
    فیلتر ورود برای Fast Pump/Dump Mode:
    فقط شرایط واقعاً ضعیف را رد می‌کند و اجازه می‌دهد 5M قوی زودتر وارد شود.
    """
    reasons_block = []
    liquidity_risk = "پایین"

    if raw_direction == "NO TRADE":
        return False, reasons_block, "بالا", "none", "none"

    if score < 76:
        reasons_block.append("امتیاز سیگنال برای ورود کافی نیست")
        return False, reasons_block, "متوسط", "none", "none"

    try:
        adx_15m = float(df_15m.iloc[-1].get("adx", 0))
    except Exception:
        adx_15m = 0

    try:
        buy_power_value = float(buy_power)
        sell_power_value = float(sell_power)
    except Exception:
        buy_power_value = 50
        sell_power_value = 50

    power_gap = buy_power_value - sell_power_value

    if adx_15m < 13:
        reasons_block.append("ADX در 15M خیلی پایین است")
        return False, reasons_block, "متوسط", "none", "none"

    if adx_15m < 15:
        if raw_direction == "LONG" and not (score >= 82 and power_gap >= 7):
            reasons_block.append("ADX پایین است و تایید قدرت خرید کافی نیست")
            return False, reasons_block, "متوسط", "none", "none"
        if raw_direction == "SHORT" and not (score >= 82 and power_gap <= -7):
            reasons_block.append("ADX پایین است و تایید قدرت فروش کافی نیست")
            return False, reasons_block, "متوسط", "none", "none"

    if raw_direction == "LONG":
        if power_gap < 4:
            reasons_block.append("اختلاف قدرت خرید نسبت به فروش برای لانگ کافی نیست")
            return False, reasons_block, "متوسط", "none", "none"
        if fvg == "bearish_fvg" and score < 84:
            reasons_block.append("FVG نزولی خلاف لانگ است و امتیاز برای عبور کافی نیست")
            return False, reasons_block, "متوسط", "none", "none"

    if raw_direction == "SHORT":
        if power_gap > -4:
            reasons_block.append("اختلاف قدرت فروش نسبت به خرید برای شورت کافی نیست")
            return False, reasons_block, "متوسط", "none", "none"
        if fvg == "bullish_fvg" and score < 84:
            reasons_block.append("FVG صعودی خلاف شورت است و امتیاز برای عبور کافی نیست")
            return False, reasons_block, "متوسط", "none", "none"

    if spread_percent is not None and spread_percent > 0.12:
        reasons_block.append("اسپرد برای معامله زیاد است")
        return False, reasons_block, "بالا", "none", "none"

    return True, reasons_block, liquidity_risk, "none", "none"

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



def _power_snapshot(df_5m):
    """قدرت خرید/فروش سریع.
    Power سه و شش کندلی برای تشخیص Setup استفاده می‌شود؛
    Power دو کندلی و شتاب آن تریگر اصلی فعال‌سازی ورود است.
    """
    buy2, sell2 = buy_sell_power(df_5m, candles=2)
    buy3, sell3 = buy_sell_power(df_5m, candles=3)
    buy6, sell6 = buy_sell_power(df_5m, candles=6)

    prev = df_5m.iloc[:-1]
    prev_buy2, prev_sell2 = buy_sell_power(prev, candles=2)
    prev_buy3, prev_sell3 = buy_sell_power(prev, candles=3)

    buy2 = float(buy2); sell2 = float(sell2)
    buy3 = float(buy3); sell3 = float(sell3)
    buy6 = float(buy6); sell6 = float(sell6)
    prev_buy2 = float(prev_buy2); prev_sell2 = float(prev_sell2)
    prev_buy3 = float(prev_buy3); prev_sell3 = float(prev_sell3)

    return {
        "buy2": buy2, "sell2": sell2,
        "buy3": buy3, "sell3": sell3,
        "buy6": buy6, "sell6": sell6,
        "prev_buy2": prev_buy2, "prev_sell2": prev_sell2,
        "prev_buy3": prev_buy3, "prev_sell3": prev_sell3,
        "buy_accel": round(buy2 - prev_buy2, 1),
        "sell_accel": round(sell2 - prev_sell2, 1),
        "buy3_accel": round(buy3 - prev_buy3, 1),
        "sell3_accel": round(sell3 - prev_sell3, 1),
    }



def detect_compression_context(df_30m, df_15m, df_5m):
    """تشخیص فشردگی/رنج قبل از شکست؛ فقط برای Setup استفاده می‌شود."""
    context = {"compression_active": False, "compression_score": 0, "compression_label": "غیرفعال", "compression_reasons": []}
    try:
        last15 = df_15m.iloc[-1]
        recent15 = df_15m.tail(24)
        recent5 = df_5m.tail(24)
        atr_now = float(last15.get("atr", 0) or 0)
        atr_ma = float(df_15m["atr"].tail(60).mean())
        if atr_now <= 0:
            return context
        range15 = float(recent15["high"].max() - recent15["low"].min())
        range5 = float(recent5["high"].max() - recent5["low"].min())
        atr_contracting = atr_ma > 0 and atr_now <= atr_ma * 0.82
        range_tight = range15 <= atr_now * 4.2 or range5 <= atr_now * 2.6
        vwap_flat = abs(float(df_5m.iloc[-1]["vwap"]) - float(df_5m.iloc[-6]["vwap"])) <= atr_now * 0.35
        score = 0
        if atr_contracting:
            score += 1; context["compression_reasons"].append("ATR نسبت به میانگین کاهش یافته")
        if range_tight:
            score += 1; context["compression_reasons"].append("قیمت در محدوده فشرده حرکت می‌کند")
        if vwap_flat:
            score += 1; context["compression_reasons"].append("VWAP تقریباً صاف و رنج است")
        context["compression_score"] = score
        context["compression_active"] = score >= 2
        context["compression_label"] = "فعال" if score >= 2 else "ضعیف"
    except Exception:
        pass
    return context


def predictive_setup_decision(df_4h, df_1h, df_30m, df_15m, df_5m, price, atr, long_score, short_score, trends, market_regime="neutral"):
    """مرحله اول: انتخاب ارزهای آماده برای Watchlist.
    اینجا نقطه ورود لحظه‌ای صادر نمی‌شود؛ فقط ارزهایی پذیرفته می‌شوند که از نظر تکنیکال
    احتمال پامپ/دامپ دارند و ارزش مانیتور ثانیه‌ای را دارند.
    """
    reasons_long, reasons_short = [], []
    last15 = df_15m.iloc[-1]
    last5 = df_5m.iloc[-1]
    prev5 = df_5m.iloc[-2]
    p = _power_snapshot(df_5m)
    compression = detect_compression_context(df_30m, df_15m, df_5m)

    trend30 = trend_direction(df_30m)
    trend15 = trend_direction(df_15m)
    trend1h = trend_direction(df_1h)
    trend4h = trend_direction(df_4h)

    long_setup_score = 0
    short_setup_score = 0

    # 30M و 15M جهت آماده شدن حرکت را می‌دهند؛ 1H/4H فقط کانتکست هستند.
    if trend30 in ["bullish", "weak_bullish"]:
        long_setup_score += 2; reasons_long.append("30M به نفع لانگ است")
    if trend30 in ["bearish", "weak_bearish"]:
        short_setup_score += 2; reasons_short.append("30M به نفع شورت است")

    if trend15 in ["bullish", "weak_bullish"] or float(last15["close"]) >= float(last15["ema20"]):
        long_setup_score += 2; reasons_long.append("15M برای لانگ آماده است")
    if trend15 in ["bearish", "weak_bearish"] or float(last15["close"]) <= float(last15["ema20"]):
        short_setup_score += 2; reasons_short.append("15M برای شورت آماده است")

    # Setup با 3 و 6 کندل ساخته می‌شود؛ Power 2 فقط نقش کمکی دارد تا خیلی دیر نشویم.
    if p["buy3"] >= 56 or p["buy6"] >= 54:
        long_setup_score += 2; reasons_long.append("قدرت خرید 3/6 کندلی در حال شکل‌گیری است")
    if p["sell3"] >= 56 or p["sell6"] >= 54:
        short_setup_score += 2; reasons_short.append("قدرت فروش 3/6 کندلی در حال شکل‌گیری است")

    if p["buy3_accel"] >= 2 or p["buy2"] >= 58:
        long_setup_score += 1; reasons_long.append("شتاب اولیه خرید دیده می‌شود")
    if p["sell3_accel"] >= 2 or p["sell2"] >= 58:
        short_setup_score += 1; reasons_short.append("شتاب اولیه فروش دیده می‌شود")

    if float(last5["macd_hist"]) > float(prev5["macd_hist"]):
        long_setup_score += 1; reasons_long.append("هیستوگرام MACD برای لانگ بهتر شده")
    if float(last5["macd_hist"]) < float(prev5["macd_hist"]):
        short_setup_score += 1; reasons_short.append("هیستوگرام MACD برای شورت ضعیف‌تر شده")

    if float(last5["rsi"]) > float(prev5["rsi"]) and float(last5["rsi"]) < 72:
        long_setup_score += 1; reasons_long.append("RSI برای لانگ در حال تقویت است")
    if float(last5["rsi"]) < float(prev5["rsi"]) and float(last5["rsi"]) > 28:
        short_setup_score += 1; reasons_short.append("RSI برای شورت در حال تضعیف است")

    if float(last5["close"]) >= float(last5["ema20"]):
        long_setup_score += 1; reasons_long.append("قیمت بالای EMA20 پنج دقیقه است")
    if float(last5["close"]) <= float(last5["ema20"]):
        short_setup_score += 1; reasons_short.append("قیمت زیر EMA20 پنج دقیقه است")

    if market_regime == "bullish":
        long_setup_score += 1; reasons_long.append("رژیم کلی بازار کمی به نفع لانگ است")
    elif market_regime == "bearish":
        short_setup_score += 1; reasons_short.append("رژیم کلی بازار کمی به نفع شورت است")

    if compression.get("compression_active"):
        if long_setup_score >= short_setup_score:
            long_setup_score += 1; reasons_long.append("فشردگی فعال است و احتمال شکست صعودی بررسی می‌شود")
        else:
            short_setup_score += 1; reasons_short.append("فشردگی فعال است و احتمال شکست نزولی بررسی می‌شود")

    # جلوگیری از ورود Watchlist به ارزهایی که خلاف کانتکست قوی هستند.
    long_block = trend1h == "bearish" and trend4h in ["bearish", "weak_bearish"]
    short_block = trend1h == "bullish" and trend4h in ["bullish", "weak_bullish"]

    direction = "NO TRADE"
    reasons = []
    setup_score = max(long_setup_score, short_setup_score)

    if long_setup_score >= SETUP_MIN_SCORE and long_setup_score >= short_setup_score + SETUP_MIN_EDGE and not long_block:
        direction = "LONG"; reasons = reasons_long
    elif short_setup_score >= SETUP_MIN_SCORE and short_setup_score >= long_setup_score + SETUP_MIN_EDGE and not short_block:
        direction = "SHORT"; reasons = reasons_short

    return {
        "direction": direction,
        "setup_ok": direction in ["LONG", "SHORT"],
        "setup_score": setup_score,
        "setup_long_score": long_setup_score,
        "setup_short_score": short_setup_score,
        "setup_reasons": reasons[:10],
        "compression": compression,
        "entry_status": "WAITING_ACTIVATION" if direction in ["LONG", "SHORT"] else "NO_SETUP"
    }


def predictive_entry_decision(df_4h, df_1h, df_30m, df_15m, df_5m, price, atr, spread_percent=None):
    """مرحله دوم: فعال‌سازی ورود.
    تریگر اصلی Power دو کندلی است؛ اما برای جلوگیری از SLهای ضعیف، MACD، RSI، EMA/VWAP،
    فاصله قیمت و کانتکست بالاتر هم باید همسو باشند.
    """
    reasons_long = []
    reasons_short = []
    block_reasons = []
    confirmations_long = 0
    confirmations_short = 0

    last = df_5m.iloc[-1]
    prev = df_5m.iloc[-2]
    prev2 = df_5m.iloc[-3]
    last15 = df_15m.iloc[-1]

    atr_value = float(atr) if atr and atr > 0 else float(last.get("atr", 0) or 0)
    if atr_value <= 0:
        return {"direction": "NO TRADE", "ok": False, "reasons": ["ATR معتبر نیست"], "block_reasons": ["ATR معتبر نیست"], "confirmations": 0, "freshness": "LOW"}

    p = _power_snapshot(df_5m)

    macd_rising = float(last["macd_hist"]) > float(prev["macd_hist"])
    macd_falling = float(last["macd_hist"]) < float(prev["macd_hist"])
    macd_turn_up = macd_rising and float(prev["macd_hist"]) <= float(prev2["macd_hist"])
    macd_turn_down = macd_falling and float(prev["macd_hist"]) >= float(prev2["macd_hist"])

    rsi_rising = float(last["rsi"]) > float(prev["rsi"])
    rsi_falling = float(last["rsi"]) < float(prev["rsi"])

    close_above_ema = float(last["close"]) >= float(last["ema20"])
    close_below_ema = float(last["close"]) <= float(last["ema20"])
    close_above_vwap = float(last["close"]) >= float(last["vwap"])
    close_below_vwap = float(last["close"]) <= float(last["vwap"])
    vwap_reclaim_long = float(prev["close"]) <= float(prev["vwap"]) and close_above_vwap
    vwap_reclaim_short = float(prev["close"]) >= float(prev["vwap"]) and close_below_vwap

    ema_distance_atr = abs(float(price) - float(last["ema20"])) / atr_value
    vwap_distance_atr = abs(float(price) - float(last["vwap"])) / atr_value
    recent = df_5m.tail(10)
    recent_high = float(recent["high"].max())
    recent_low = float(recent["low"].min())
    move_from_low_atr = (float(price) - recent_low) / atr_value
    move_from_high_atr = (recent_high - float(price)) / atr_value
    near_recent_high = recent_high - float(price) <= atr_value * 0.18
    near_recent_low = float(price) - recent_low <= atr_value * 0.18

    trend_1h = trend_direction(df_1h)
    trend_4h = trend_direction(df_4h)
    strong_context_long_block = trend_1h == "bearish" and trend_4h in ["bearish", "weak_bearish"]
    strong_context_short_block = trend_1h == "bullish" and trend_4h in ["bullish", "weak_bullish"]

    # Power دو کندلی: شرط اصلی فعال‌سازی. اگر کندل دوم واقعاً در حال اوج گرفتن نباشد، ورود صادر نمی‌شود.
    long_power2_ok = (
        p["buy2"] >= ENTRY_POWER2_MIN
        and p["sell2"] <= ENTRY_OPPOSITE_POWER2_MAX
        and (p["buy_accel"] >= ENTRY_POWER_ACCEL_MIN or p["buy2"] >= ENTRY_POWER2_STRONG or p["buy2"] >= p["buy3"] - 2)
    )
    short_power2_ok = (
        p["sell2"] >= ENTRY_POWER2_MIN
        and p["buy2"] <= ENTRY_OPPOSITE_POWER2_MAX
        and (p["sell_accel"] >= ENTRY_POWER_ACCEL_MIN or p["sell2"] >= ENTRY_POWER2_STRONG or p["sell2"] >= p["sell3"] - 2)
    )

    if long_power2_ok:
        confirmations_long += 2; reasons_long.append("Power دو کندلی خرید به نقطه فعال‌سازی رسیده")
    else:
        reasons_long.append("Power دو کندلی خرید هنوز برای ورود کافی نیست")

    if short_power2_ok:
        confirmations_short += 2; reasons_short.append("Power دو کندلی فروش به نقطه فعال‌سازی رسیده")
    else:
        reasons_short.append("Power دو کندلی فروش هنوز برای ورود کافی نیست")

    if p["buy3"] >= 55:
        confirmations_long += 1; reasons_long.append("Power سه کندلی خرید Setup را تایید می‌کند")
    if p["sell3"] >= 55:
        confirmations_short += 1; reasons_short.append("Power سه کندلی فروش Setup را تایید می‌کند")

    if macd_rising or macd_turn_up:
        confirmations_long += 1; reasons_long.append("هیستوگرام MACD به نفع لانگ تقویت شده")
    if macd_falling or macd_turn_down:
        confirmations_short += 1; reasons_short.append("هیستوگرام MACD به نفع شورت ضعیف‌تر شده")

    if rsi_rising and float(last["rsi"]) < 72:
        confirmations_long += 1; reasons_long.append("RSI در کندل دوم به نفع لانگ رشد کرده")
    if rsi_falling and float(last["rsi"]) > 28:
        confirmations_short += 1; reasons_short.append("RSI در کندل دوم به نفع شورت افت کرده")

    if close_above_ema and (close_above_vwap or vwap_reclaim_long):
        confirmations_long += 1; reasons_long.append("EMA20/VWAP برای لانگ همسو است")
    if close_below_ema and (close_below_vwap or vwap_reclaim_short):
        confirmations_short += 1; reasons_short.append("EMA20/VWAP برای شورت همسو است")

    if float(last15["close"]) >= float(last15["ema20"]):
        confirmations_long += 1; reasons_long.append("15M تایید نرم لانگ می‌دهد")
    if float(last15["close"]) <= float(last15["ema20"]):
        confirmations_short += 1; reasons_short.append("15M تایید نرم شورت می‌دهد")

    late_long = (
        ema_distance_atr > ENTRY_MAX_EMA_DISTANCE_ATR
        or vwap_distance_atr > ENTRY_MAX_VWAP_DISTANCE_ATR
        or (near_recent_high and move_from_low_atr > 1.20)
        or float(last["rsi"]) >= 73
    )
    late_short = (
        ema_distance_atr > ENTRY_MAX_EMA_DISTANCE_ATR
        or vwap_distance_atr > ENTRY_MAX_VWAP_DISTANCE_ATR
        or (near_recent_low and move_from_high_atr > 1.20)
        or float(last["rsi"]) <= 27
    )

    if late_long:
        reasons_long.append("رد لانگ: حرکت دیر شده یا قیمت از EMA/VWAP زیاد فاصله گرفته")
    if late_short:
        reasons_short.append("رد شورت: حرکت دیر شده یا قیمت از EMA/VWAP زیاد فاصله گرفته")

    if spread_percent is not None and spread_percent > 0.12:
        # Classic mode: اسپرد فقط هشدار است و سیگنال تکنیکال را حذف نمی‌کند.
        reasons_long.append("هشدار: اسپرد برای ورود سریع کمی زیاد است")
        reasons_short.append("هشدار: اسپرد برای ورود سریع کمی زیاد است")

    # Classic technical activation:
    # تصمیم ورود فقط با موتور 5M گرفته می‌شود: Power دو کندلی + MACD slope + RSI slope + EMA20.
    # VWAP، Late Entry و کانتکست 1H/4H فقط در دلایل/ریسک ذخیره می‌شوند و ورود را خشک نمی‌کنند.
    long_ok = (
        confirmations_long >= ENTRY_MIN_CONFIRMATIONS
        and long_power2_ok
        and macd_rising
        and rsi_rising
        and close_above_ema
    )
    short_ok = (
        confirmations_short >= ENTRY_MIN_CONFIRMATIONS
        and short_power2_ok
        and macd_falling
        and rsi_falling
        and close_below_ema
    )

    if long_ok and short_ok:
        if confirmations_long > confirmations_short:
            short_ok = False
        elif confirmations_short > confirmations_long:
            long_ok = False
        else:
            long_ok = short_ok = False
            block_reasons.append("تریگر لانگ و شورت همزمان و مبهم است")

    if block_reasons:
        long_ok = False
        short_ok = False

    base = {
        "power2_buy": p["buy2"], "power2_sell": p["sell2"],
        "power3_buy": p["buy3"], "power3_sell": p["sell3"],
        "power_accel": p["buy_accel"] if confirmations_long >= confirmations_short else p["sell_accel"],
        "buy_accel": p["buy_accel"], "sell_accel": p["sell_accel"],
        "ema_distance_atr": round(ema_distance_atr, 2),
        "vwap_distance_atr": round(vwap_distance_atr, 2),
        "entry_mode": "PREDICTIVE_TRIGGER"
    }

    if long_ok:
        freshness = "HIGH" if (p["buy_accel"] >= 6 or macd_turn_up or vwap_reclaim_long) and ema_distance_atr <= 0.70 else "MEDIUM"
        return {**base, "direction": "LONG", "ok": True, "reasons": reasons_long[:10], "block_reasons": [], "confirmations": confirmations_long, "freshness": freshness, "late_entry": False}

    if short_ok:
        freshness = "HIGH" if (p["sell_accel"] >= 6 or macd_turn_down or vwap_reclaim_short) and ema_distance_atr <= 0.70 else "MEDIUM"
        return {**base, "direction": "SHORT", "ok": True, "reasons": reasons_short[:10], "block_reasons": [], "confirmations": confirmations_short, "freshness": freshness, "late_entry": False}

    no_trade_reasons = block_reasons[:]
    if confirmations_long < ENTRY_MIN_CONFIRMATIONS and confirmations_short < ENTRY_MIN_CONFIRMATIONS:
        no_trade_reasons.append("تریگر ورود هنوز کامل نیست")
    if confirmations_long >= ENTRY_MIN_CONFIRMATIONS and not long_power2_ok:
        no_trade_reasons.append("لانگ از نظر تکنیکال نزدیک بود اما Power دو کندلی کافی نبود")
    if confirmations_short >= ENTRY_MIN_CONFIRMATIONS and not short_power2_ok:
        no_trade_reasons.append("شورت از نظر تکنیکال نزدیک بود اما Power دو کندلی کافی نبود")
    if confirmations_long >= ENTRY_MIN_CONFIRMATIONS and late_long:
        no_trade_reasons.append("لانگ از نظر قدرت خوب بود ولی دیر شده بود")
    if confirmations_short >= ENTRY_MIN_CONFIRMATIONS and late_short:
        no_trade_reasons.append("شورت از نظر قدرت خوب بود ولی دیر شده بود")
    if strong_context_long_block and confirmations_long >= confirmations_short:
        no_trade_reasons.append("1H/4H به‌طور قوی خلاف لانگ است")
    if strong_context_short_block and confirmations_short >= confirmations_long:
        no_trade_reasons.append("1H/4H به‌طور قوی خلاف شورت است")

    best_reasons = reasons_long if confirmations_long >= confirmations_short else reasons_short
    return {
        **base,
        "direction": "NO TRADE", "ok": False,
        "reasons": (no_trade_reasons + best_reasons)[:10],
        "block_reasons": no_trade_reasons[:8],
        "confirmations": max(confirmations_long, confirmations_short),
        "freshness": "LOW",
        "late_entry": late_long if confirmations_long >= confirmations_short else late_short,
    }

def analyze_symbol(symbol):
    df_1d = add_indicators(get_klines(symbol, "1d"))
    df_4h = add_indicators(get_klines(symbol, "4h"))
    df_1h = add_indicators(get_klines(symbol, "1h"))
    df_30m = add_indicators(get_klines(symbol, "30m"))
    df_15m = add_indicators(get_klines(symbol, "15m"))
    df_5m = add_indicators(get_klines(symbol, "5m", include_current=True))

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

    # BTC filter/lead disabled: اثر مستقیم بیت‌کوین حذف شده و فقط Market Regime باقی می‌ماند.
    btc_status = "disabled"

    # موارد حذف‌شده از نسخه ساده؛ نه امتیاز دارند، نه نمایش داده می‌شوند.
    trendline = "none"
    breakout = "none"
    structure = "none"

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

    predictive_context = predictive_entry_decision(
        df_4h, df_1h, df_30m, df_15m, df_5m, price, atr, spread_percent
    )

    setup_context = predictive_setup_decision(
        df_4h, df_1h, df_30m, df_15m, df_5m,
        price, atr, long_score, short_score, trends, market_regime
    )

    pre_direction = predictive_context.get("direction", "NO TRADE")
    if pre_direction == "NO TRADE" and setup_context.get("setup_ok"):
        pre_direction = setup_context.get("direction", "NO TRADE")

    tq_l, tq_s, tq_rl, tq_rs, technical_context = technical_quality_context(
        pre_direction, price, atr, support, resistance, df_15m, df_5m, df_30m, df_1h
    )
    # در نسخه پیش‌بینی‌محور، کیفیت تکنیکال فقط برای تشخیص دیر بودن/ریسک ذخیره می‌شود؛
    # تصمیم نهایی با امتیاز کم و زیاد نمی‌شود.
    reasons_long += tq_rl
    reasons_short += tq_rs

    # داده‌های حرفه‌ای پنهان برای ثبت در Tracker و تحلیل علت SL؛ در نمایش سیگنال نشان داده نمی‌شوند.
    late_entry = technical_context.get("late_entry", False)
    late_entry_reason = technical_context.get("late_entry_reason")
    tp_space_ok = technical_context.get("tp_space_ok", True)
    tp_space_reason = technical_context.get("tp_space_reason")
    tp_space_atr = technical_context.get("tp_space_atr")
    trap_risk = technical_context.get("trap_risk", False)
    trap_reason = technical_context.get("trap_reason")
    candle_forecast = technical_context.get("candle_forecast")
    candle_forecast_reason = technical_context.get("candle_forecast_reason")

    raw_direction = predictive_context.get("direction", "NO TRADE")
    entry_confirmed_now = bool(predictive_context.get("ok")) and raw_direction in ["LONG", "SHORT"]
    if raw_direction == "NO TRADE" and setup_context.get("setup_ok"):
        raw_direction = setup_context.get("direction", "NO TRADE")

    score = max(long_score, short_score)  # فقط برای سازگاری داخلی/آمار قدیمی؛ تصمیم‌گیر نیست.
    if entry_confirmed_now and raw_direction in ["LONG", "SHORT"]:
        reasons = predictive_context.get("reasons", []) + risk_notes
    elif raw_direction in ["LONG", "SHORT"]:
        reasons = setup_context.get("setup_reasons", []) + ["وضعیت: منتظر فعال‌سازی ورود با تایید 5M"] + risk_notes
    else:
        reasons = predictive_context.get("reasons", ["تریگر پیش‌بینی ورود کامل نیست"])

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

    # معماری دو مرحله‌ای: Setup کامل صادر می‌شود، اما ورود فقط بعد از Activation فعال است.
    # نکته مهم: TP Space / Trap Risk نباید Setup را حذف کند؛ اما نباید اجازه Entry فعال بدهد.
    # این کار باعث می‌شود ارز همچنان زیر نظر بماند، ولی ورود فقط وقتی تمیز و دور از حمایت/مقاومت خطرناک باشد صادر شود.
    entry_ok = entry_confirmed_now
    block_reasons = list(predictive_context.get("block_reasons", []))

    # Classic mode: TP Space / Trap / RR فقط برای آمار و تحلیل علت SL ذخیره می‌شوند.
    # این موارد دیگر جلوی سیگنال فعال را نمی‌گیرند؛ تصمیم فعال‌سازی فقط با موتور تکنیکال 5M است.
    preliminary_stop, preliminary_tp1, _ = calculate_trade_levels(raw_direction, price, atr, support, resistance)
    preliminary_rr = risk_reward(raw_direction, price, preliminary_stop, preliminary_tp1)

    setup_waiting = (not entry_ok) and raw_direction in ["LONG", "SHORT"] and setup_context.get("setup_ok")
    liquidity_risk = "پایین" if entry_ok else "متوسط"
    fake_breakout = "none"
    trend_exhaustion = "none"

    risk_level = calculate_risk_level(
        raw_direction=raw_direction,
        score=score,
        liquidity_risk=liquidity_risk,
        funding_rate=funding_rate,
        adx=adx_value,
        spread_percent=spread_percent,
        rr=rr
    )

    if raw_direction == "NO TRADE":
        final_direction = "NO TRADE"
        reasons = reasons + block_reasons
        stop_loss = None
        tp1 = None
        tp2 = None
        grade = "NO_ENTRY"
    elif setup_waiting:
        final_direction = raw_direction
        stop_loss = stop_loss_raw
        tp1 = tp1_raw
        tp2 = tp2_raw
        grade = "WAITING_ACTIVATION"
    else:
        final_direction = raw_direction
        stop_loss = stop_loss_raw
        tp1 = tp1_raw
        tp2 = tp2_raw
        grade = "ENTRY"

    # احتمال موفقیت هم دیگر در تصمیم ورود دخالت ندارد؛ فقط مقدار نمایشی/سازگاری است.
    win_prob = win_probability(score, risk_level, rr, adx_value, "A" if grade == "ENTRY" else "Reject")
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

    display_buy_power = predictive_context.get("power2_buy", buy_power)
    display_sell_power = predictive_context.get("power2_sell", sell_power)

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

        "buy_power": display_buy_power,
        "sell_power": display_sell_power,
        "power2_buy": predictive_context.get("power2_buy"),
        "power2_sell": predictive_context.get("power2_sell"),
        "power3_buy": predictive_context.get("power3_buy"),
        "power3_sell": predictive_context.get("power3_sell"),
        "power_acceleration": predictive_context.get("power_accel"),
        "predictive_confirmations": predictive_context.get("confirmations"),
        "freshness": predictive_context.get("freshness"),
        "entry_mode": "PREDICTIVE_SETUP" if setup_waiting else predictive_context.get("entry_mode"),
        "entry_status": "WAITING_ACTIVATION" if setup_waiting else ("ACTIVE" if entry_ok else "NO_ENTRY"),
        "entry_confirmed": bool(entry_ok),
        "setup_waiting_activation": bool(setup_waiting),
        "setup_score": setup_context.get("setup_score"),
        "setup_long_score": setup_context.get("setup_long_score"),
        "setup_short_score": setup_context.get("setup_short_score"),
        "setup_reasons": setup_context.get("setup_reasons", []),
        "compression_active": setup_context.get("compression", {}).get("compression_active"),
        "compression_score": setup_context.get("compression", {}).get("compression_score"),
        "compression_label": setup_context.get("compression", {}).get("compression_label"),
        "compression_reasons": setup_context.get("compression", {}).get("compression_reasons", []),
        "ema_distance_atr": predictive_context.get("ema_distance_atr"),
        "vwap_distance_atr": predictive_context.get("vwap_distance_atr"),

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

        # Hidden professional diagnostics: bot.py does not display these in signal text,
        # but signal_tracker stores them for SL reasons and statistics.
        "late_entry": late_entry,
        "late_entry_reason": late_entry_reason,
        "tp_space_ok": tp_space_ok,
        "tp_space_reason": tp_space_reason,
        "tp_space_atr": tp_space_atr,
        "trap_risk": trap_risk,
        "trap_reason": trap_reason,
        "candle_forecast": candle_forecast,
        "candle_forecast_reason": candle_forecast_reason,

        "news_filter_active": news_filter_active(),

        "reasons": reasons[:18],
    }
