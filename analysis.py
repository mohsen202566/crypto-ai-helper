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

    df["taker_buy_base"] = df["volume"] / 2

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


def score_timeframe_trends(df_4h, df_1h, df_30m, df_15m, df_5m):
    long_score = 0
    short_score = 0
    reasons_long = []
    reasons_short = []

    trends = {
        "4H": trend_direction(df_4h),
        "1H": trend_direction(df_1h),
        "30M": trend_direction(df_30m),
        "15M": trend_direction(df_15m),
        "5M": trend_direction(df_5m),
    }

    weights = {
        "4H": 18,
        "1H": 16,
        "30M": 12,
        "15M": 10,
        "5M": 6,
    }

    for tf, trend in trends.items():
        weight = weights[tf]

        if trend == "bullish":
            long_score += weight
            reasons_long.append(f"{tf}: روند صعودی قوی")
        elif trend == "weak_bullish":
            long_score += int(weight * 0.55)
            reasons_long.append(f"{tf}: روند صعودی ضعیف")
        elif trend == "bearish":
            short_score += weight
            reasons_short.append(f"{tf}: روند نزولی قوی")
        elif trend == "weak_bearish":
            short_score += int(weight * 0.55)
            reasons_short.append(f"{tf}: روند نزولی ضعیف")

    return long_score, short_score, reasons_long, reasons_short, trends


def score_momentum(df_15m):
    long_score = 0
    short_score = 0
    reasons_long = []
    reasons_short = []

    last = df_15m.iloc[-1]
    rsi = last["rsi"]

    if 50 <= rsi <= 70:
        long_score += 10
        reasons_long.append("RSI مناسب برای لانگ")
    elif rsi > 75:
        short_score += 8
        reasons_short.append("RSI در اشباع خرید")

    if 30 <= rsi <= 50:
        short_score += 10
        reasons_short.append("RSI مناسب برای شورت")
    elif rsi < 25:
        long_score += 8
        reasons_long.append("RSI در اشباع فروش")

    if last["macd"] > last["macd_signal"]:
        long_score += 10
        reasons_long.append("MACD مثبت")
    elif last["macd"] < last["macd_signal"]:
        short_score += 10
        reasons_short.append("MACD منفی")

    return long_score, short_score, reasons_long, reasons_short


def score_volume(df_15m):
    long_score = 0
    short_score = 0
    reasons_long = []
    reasons_short = []

    buy_power, sell_power = buy_sell_power(df_15m)

    if buy_power >= 60:
        long_score += 10
        reasons_long.append("قدرت خرید بالا")
    elif sell_power >= 60:
        short_score += 10
        reasons_short.append("قدرت فروش بالا")

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
            long_score += 4
            reasons_long.append("Fear & Greed در ترس شدید؛ احتمال برگشت")
        elif fear_value >= 80:
            short_score += 4
            reasons_short.append("Fear & Greed در طمع شدید؛ ریسک اصلاح")

    if symbol != "BTCUSDT":
        if altseason == "قوی":
            long_score += 5
            reasons_long.append("آلت‌سیزن برای آلت‌کوین‌ها مناسب است")
        elif altseason == "ضعیف":
            short_score += 4
            reasons_short.append("آلت‌سیزن ضعیف؛ ریسک آلت‌کوین‌ها بالاتر است")

    return long_score, short_score, reasons_long, reasons_short, market


def calculate_trade_levels(direction, price, atr):
    if direction == "LONG":
        stop_loss = price - (atr * 1.5)
        tp1 = price + (atr * 1.5)
        tp2 = price + (atr * 3)
    elif direction == "SHORT":
        stop_loss = price + (atr * 1.5)
        tp1 = price - (atr * 1.5)
        tp2 = price - (atr * 3)
    else:
        stop_loss = None
        tp1 = None
        tp2 = None

    return stop_loss, tp1, tp2


def analyze_symbol(symbol):
    df_4h = add_indicators(get_klines(symbol, "4h"))
    df_1h = add_indicators(get_klines(symbol, "1h"))
    df_30m = add_indicators(get_klines(symbol, "30m"))
    df_15m = add_indicators(get_klines(symbol, "15m"))
    df_5m = add_indicators(get_klines(symbol, "5m"))

    long_score = 0
    short_score = 0
    reasons_long = []
    reasons_short = []

    l, s, rl, rs, trends = score_timeframe_trends(
        df_4h, df_1h, df_30m, df_15m, df_5m
    )
    long_score += l
    short_score += s
    reasons_long += rl
    reasons_short += rs

    l, s, rl, rs = score_momentum(df_15m)
    long_score += l
    short_score += s
    reasons_long += rl
    reasons_short += rs

    l, s, rl, rs, buy_power, sell_power = score_volume(df_15m)
    long_score += l
    short_score += s
    reasons_long += rl
    reasons_short += rs

    trendline = detect_trendline(df_15m)
    l, s = trendline_score(trendline)
    long_score += l
    short_score += s

    if trendline == "uptrend":
        reasons_long.append("خط روند صعودی تشخیص داده شد")
    elif trendline == "downtrend":
        reasons_short.append("خط روند نزولی تشخیص داده شد")

    breakout = detect_breakout(df_15m)
    l, s = breakout_score(breakout)
    long_score += l
    short_score += s

    if breakout == "bullish_breakout":
        reasons_long.append("بریک‌اوت صعودی با حجم")
    elif breakout == "bearish_breakout":
        reasons_short.append("بریک‌اوت نزولی با حجم")
    elif breakout == "fake_bullish_breakout":
        reasons_short.append("احتمال فیک بریک‌اوت صعودی")
    elif breakout == "fake_bearish_breakout":
        reasons_long.append("احتمال فیک بریک‌اوت نزولی")

    structure = detect_market_structure(df_15m)
    l, s = structure_score(structure)
    long_score += l
    short_score += s

    if structure == "bullish_structure":
        reasons_long.append("ساختار بازار صعودی است")
    elif structure == "bearish_structure":
        reasons_short.append("ساختار بازار نزولی است")

    l, s, rl, rs, market = score_market_sentiment(symbol)
    long_score += l
    short_score += s
    reasons_long += rl
    reasons_short += rs

    last = df_15m.iloc[-1]
    price = float(last["close"])
    atr = float(last["atr"])

    if long_score > short_score:
        direction = "LONG"
        score = min(long_score, 100)
        reasons = reasons_long
    elif short_score > long_score:
        direction = "SHORT"
        score = min(short_score, 100)
        reasons = reasons_short
    else:
        direction = "NO TRADE"
        score = 50
        reasons = ["بازار جهت واضحی ندارد"]

    if score < 60:
        direction = "NO TRADE"

    stop_loss, tp1, tp2 = calculate_trade_levels(direction, price, atr)
    support, resistance = support_resistance(df_15m)

    return {
        "symbol": symbol,
        "price": round(price, 6),
        "direction": direction,
        "score": score,

        "rsi": round(float(last["rsi"]), 2),
        "macd": round(float(last["macd"]), 6),
        "macd_signal": round(float(last["macd_signal"]), 6),
        "ema20": round(float(last["ema20"]), 6),
        "ema50": round(float(last["ema50"]), 6),
        "ema200": round(float(last["ema200"]), 6),
        "atr": round(atr, 6),

        "stop_loss": None if stop_loss is None else round(stop_loss, 6),
        "tp1": None if tp1 is None else round(tp1, 6),
        "tp2": None if tp2 is None else round(tp2, 6),

        "support": round(float(support), 6),
        "resistance": round(float(resistance), 6),

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
        "reasons": reasons[:8],
    }
