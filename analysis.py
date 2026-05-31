import ccxt
import pandas as pd
import ta

from market_sentiment import get_market_sentiment
from trend_analysis import (
    detect_trendline,
    detect_breakout,
    trendline_score,
    breakout_score
)
from market_structure import (
    detect_market_structure,
    structure_score
)


exchange = ccxt.okx({
    "enableRateLimit": True,
    "options": {
        "defaultType": "swap"
    }
})


def to_okx_symbol(symbol):
    coin = symbol.replace("USDT", "")
    return f"{coin}/USDT:USDT"


def get_klines(symbol, interval="15m", limit=250):
    okx_symbol = to_okx_symbol(symbol)

    ohlcv = exchange.fetch_ohlcv(
        okx_symbol,
        timeframe=interval,
        limit=limit
    )

    if not ohlcv or len(ohlcv) < 210:
        raise Exception("داده کافی از OKX دریافت نشد")

    df = pd.DataFrame(
        ohlcv,
        columns=["time", "open", "high", "low", "close", "volume"]
    )

    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna()

    if len(df) < 210:
        raise Exception("داده کافی برای تحلیل وجود ندارد")

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

    df["atr"] = ta.volatility.average_true_range(
        df["high"], df["low"], df["close"], window=14
    )

    df["volume_ma20"] = df["volume"].rolling(20).mean()

    df = df.dropna()

    if len(df) < 30:
        raise Exception("اندیکاتورها کامل محاسبه نشدند")

    return df


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
    recent = df.tail(60)
    support = recent["low"].min()
    resistance = recent["high"].max()
    return support, resistance


def distance_percent(price, level):
    if price == 0:
        return 0
    return abs((price - level) / price) * 100


def is_near_resistance(price, resistance, atr):
    return (resistance - price) <= atr * 0.8


def is_near_support(price, support, atr):
    return (price - support) <= atr * 0.8


def candle_strength(df):
    last = df.iloc[-1]
    body = abs(last["close"] - last["open"])
    candle_range = last["high"] - last["low"]

    if candle_range == 0:
        return "weak"

    ratio = body / candle_range

    if ratio >= 0.6:
        if last["close"] > last["open"]:
            return "bullish_strong"
        else:
            return "bearish_strong"

    return "weak"


def volume_confirmation(df):
    last = df.iloc[-1]

    if last["volume"] > last["volume_ma20"] * 1.2:
        return True

    return False


def market_is_choppy(df_15m, df_5m):
    last_15 = df_15m.iloc[-1]
    last_5 = df_5m.iloc[-1]

    ema_gap_15 = abs(last_15["ema20"] - last_15["ema50"]) / last_15["close"] * 100
    ema_gap_5 = abs(last_5["ema20"] - last_5["ema50"]) / last_5["close"] * 100

    if ema_gap_15 < 0.08 and ema_gap_5 < 0.08:
        return True

    return False


def signal_validity(score, direction):
    if direction == "NO TRADE":
        return "سیگنال معتبر نیست"

    if score >= 85:
        return "30 دقیقه تا 3 ساعت"
    elif score >= 75:
        return "15 تا 90 دقیقه"
    elif score >= 65:
        return "10 تا 45 دقیقه"
    else:
        return "اعتبار پایین"


def signal_timeframe(score, direction):
    if direction == "NO TRADE":
        return "بدون تایم‌فریم ورود"

    if score >= 85:
        return "5M تا 15M"
    elif score >= 75:
        return "5M تا 15M"
    elif score >= 65:
        return "5M"
    else:
        return "نامعتبر"


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
        "1D": 10,
        "4H": 14,
        "1H": 16,
        "30M": 14,
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

    strength = candle_strength(df_5m)

    if strength == "bullish_strong":
        long_score += 8
        reasons_long.append("کندل 5M صعودی قوی")

    if strength == "bearish_strong":
        short_score += 8
        reasons_short.append("کندل 5M نزولی قوی")

    if volume_confirmation(df_5m):
        long_score += 4
        short_score += 4

    return long_score, short_score, reasons_long, reasons_short, buy_power, sell_power


def score_market_sentiment(symbol):
    market = get_market_sentiment()

    long_score = 0
    short_score = 0
    reasons_long = []
    reasons_short = []

    fear_value = market["fear_value"]
    altseason = market["altseason_status"]

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


def calculate_trade_levels(direction, price, atr):
    if direction == "LONG":
        stop_loss = price - (atr * 1.2)
        tp1 = price + (atr * 1.2)
        tp2 = price + (atr * 2.2)
    elif direction == "SHORT":
        stop_loss = price + (atr * 1.2)
        tp1 = price - (atr * 1.2)
        tp2 = price - (atr * 2.2)
    else:
        stop_loss = None
        tp1 = None
        tp2 = None

    return stop_loss, tp1, tp2


def entry_filter(direction, score, long_score, short_score, df_15m, df_5m):
    last_5 = df_5m.iloc[-1]
    price = float(last_5["close"])
    atr = float(last_5["atr"])
    support, resistance = support_resistance(df_15m)

    reasons_block = []

    if market_is_choppy(df_15m, df_5m):
        reasons_block.append("بازار رنج و کم‌جهت است")

    if direction == "LONG":
        if long_score < short_score + 12:
            reasons_block.append("اختلاف امتیاز لانگ و شورت کافی نیست")

        if is_near_resistance(price, resistance, atr):
            reasons_block.append("قیمت نزدیک مقاومت است")

        if last_5["rsi"] > 72:
            reasons_block.append("RSI برای لانگ بیش از حد بالاست")

    if direction == "SHORT":
        if short_score < long_score + 12:
            reasons_block.append("اختلاف امتیاز شورت و لانگ کافی نیست")

        if is_near_support(price, support, atr):
            reasons_block.append("قیمت نزدیک حمایت است")

        if last_5["rsi"] < 28:
            reasons_block.append("RSI برای شورت بیش از حد پایین است")

    if score < 65:
        reasons_block.append("امتیاز سیگنال برای ورود کافی نیست")

    if reasons_block:
        return False, reasons_block

    return True, []


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

    l, s, rl, rs, buy_power, sell_power = score_entry(df_15m, df_5m)
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

    l, s, rl, rs, market = score_market_sentiment(symbol)
    long_score += l
    short_score += s
    reasons_long += rl
    reasons_short += rs

    last = df_5m.iloc[-1]
    price = float(last["close"])
    atr = float(last["atr"])

    raw_direction = "NO TRADE"

    if long_score >= short_score + 12:
        raw_direction = "LONG"
        score = min(long_score, 100)
        reasons = reasons_long
    elif short_score >= long_score + 12:
        raw_direction = "SHORT"
        score = min(short_score, 100)
        reasons = reasons_short
    else:
        score = max(long_score, short_score)
        reasons = ["اختلاف لانگ و شورت کافی نیست"]

    entry_ok, block_reasons = entry_filter(
        raw_direction,
        score,
        long_score,
        short_score,
        df_15m,
        df_5m
    )

    if raw_direction == "NO TRADE" or not entry_ok:
        direction = "NO TRADE"
        reasons = reasons + block_reasons
    else:
        direction = raw_direction

    stop_loss, tp1, tp2 = calculate_trade_levels(direction, price, atr)
    support, resistance = support_resistance(df_15m)

    return {
        "symbol": symbol,
        "price": round(price, 8),
        "direction": direction,
        "score": min(score, 100),

        "validity": signal_validity(score, direction),
        "signal_timeframe": signal_timeframe(score, direction),

        "rsi": round(float(last["rsi"]), 2),
        "macd": round(float(last["macd"]), 6),
        "macd_signal": round(float(last["macd_signal"]), 6),
        "ema20": round(float(last["ema20"]), 8),
        "ema50": round(float(last["ema50"]), 8),
        "ema200": round(float(last["ema200"]), 8),
        "atr": round(atr, 8),

        "stop_loss": None if stop_loss is None else round(stop_loss, 8),
        "tp1": None if tp1 is None else round(tp1, 8),
        "tp2": None if tp2 is None else round(tp2, 8),

        "support": round(float(support), 8),
        "resistance": round(float(resistance), 8),

        "buy_power": buy_power,
        "sell_power": sell_power,

        "trendline": trendline,
        "breakout": breakout,
        "market_structure": structure,
        "trends": trends,

        "fear_value": market["fear_value"],
        "fear_text": market["fear_text"],
        "btc_dominance": market["btc_dominance"],
        "dominance_status": market["dominance_status"],
        "altseason_status": market["altseason_status"],

        "long_score": long_score,
        "short_score": short_score,
        "reasons": reasons[:10],
    }
