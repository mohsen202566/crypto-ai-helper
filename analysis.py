import requests
import pandas as pd
import ta


def get_klines(symbol, interval="15m", limit=250):
    url = "https://api.binance.com/api/v3/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    r = requests.get(url, params=params, timeout=15)
    data = r.json()

    df = pd.DataFrame(data, columns=[
        "time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades",
        "taker_buy_base", "taker_buy_quote", "ignore"
    ])

    for col in ["open", "high", "low", "close", "volume", "taker_buy_base"]:
        df[col] = df[col].astype(float)

    return df


def add_indicators(df):
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

    return df


def get_fear_greed():
    try:
        url = "https://api.alternative.me/fng/"
        r = requests.get(url, timeout=10)
        data = r.json()["data"][0]
        return int(data["value"]), data["value_classification"]
    except Exception:
        return None, "نامشخص"


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
    recent = df.tail(50)
    support = recent["low"].min()
    resistance = recent["high"].max()
    return support, resistance


def breakout_status(df):
    last = df.iloc[-1]
    previous = df.iloc[-21:-1]

    resistance = previous["high"].max()
    support = previous["low"].min()
    avg_volume = previous["volume"].mean()

    if last["close"] > resistance and last["volume"] > avg_volume * 1.2:
        return "bullish_breakout"

    if last["close"] < support and last["volume"] > avg_volume * 1.2:
        return "bearish_breakout"

    if last["high"] > resistance and last["close"] < resistance:
        return "fake_bullish_breakout"

    if last["low"] < support and last["close"] > support:
        return "fake_bearish_breakout"

    return "no_breakout"


def score_analysis(df_4h, df_1h, df_30m, df_15m, df_5m):
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

    for tf, trend in trends.items():
        if trend == "bullish":
            long_score += 15
            reasons_long.append(f"{tf} روند صعودی قوی")
        elif trend == "weak_bullish":
            long_score += 8
            reasons_long.append(f"{tf} روند صعودی ضعیف")
        elif trend == "bearish":
            short_score += 15
            reasons_short.append(f"{tf} روند نزولی قوی")
        elif trend == "weak_bearish":
            short_score += 8
            reasons_short.append(f"{tf} روند نزولی ضعیف")

    last = df_15m.iloc[-1]

    if 50 <= last["rsi"] <= 70:
        long_score += 10
        reasons_long.append("RSI مناسب برای لانگ")
    elif last["rsi"] > 75:
        short_score += 8
        reasons_short.append("RSI در ناحیه اشباع خرید")

    if 30 <= last["rsi"] <= 50:
        short_score += 10
        reasons_short.append("RSI مناسب برای شورت")
    elif last["rsi"] < 25:
        long_score += 8
        reasons_long.append("RSI در ناحیه اشباع فروش")

    if last["macd"] > last["macd_signal"]:
        long_score += 10
        reasons_long.append("MACD مثبت")
    else:
        short_score += 10
        reasons_short.append("MACD منفی")

    buy_power, sell_power = buy_sell_power(df_15m)

    if buy_power > 60:
        long_score += 10
        reasons_long.append("قدرت خرید بالا")
    elif sell_power > 60:
        short_score += 10
        reasons_short.append("قدرت فروش بالا")

    breakout = breakout_status(df_15m)

    if breakout == "bullish_breakout":
        long_score += 15
        reasons_long.append("بریک‌اوت صعودی با حجم")
    elif breakout == "bearish_breakout":
        short_score += 15
        reasons_short.append("بریک‌اوت نزولی با حجم")
    elif breakout == "fake_bullish_breakout":
        short_score += 8
        reasons_short.append("احتمال فیک بریک‌اوت صعودی")
    elif breakout == "fake_bearish_breakout":
        long_score += 8
        reasons_long.append("احتمال فیک بریک‌اوت نزولی")

    return long_score, short_score, reasons_long, reasons_short, trends, buy_power, sell_power, breakout


def analyze_symbol(symbol):
    df_4h = add_indicators(get_klines(symbol, "4h"))
    df_1h = add_indicators(get_klines(symbol, "1h"))
    df_30m = add_indicators(get_klines(symbol, "30m"))
    df_15m = add_indicators(get_klines(symbol, "15m"))
    df_5m = add_indicators(get_klines(symbol, "5m"))

    long_score, short_score, reasons_long, reasons_short, trends, buy_power, sell_power, breakout = score_analysis(
        df_4h, df_1h, df_30m, df_15m, df_5m
    )

    last = df_15m.iloc[-1]
    price = float(last["close"])
    atr = float(last["atr"])

    if long_score > short_score:
        direction = "LONG"
        score = min(long_score, 100)
        reasons = reasons_long
        stop_loss = price - (atr * 1.5)
        tp1 = price + (atr * 1.5)
        tp2 = price + (atr * 3)
    elif short_score > long_score:
        direction = "SHORT"
        score = min(short_score, 100)
        reasons = reasons_short
        stop_loss = price + (atr * 1.5)
        tp1 = price - (atr * 1.5)
        tp2 = price - (atr * 3)
    else:
        direction = "NO TRADE"
        score = 50
        reasons = ["بازار جهت واضحی ندارد"]
        stop_loss = None
        tp1 = None
        tp2 = None

    if score < 60:
        direction = "NO TRADE"

    support, resistance = support_resistance(df_15m)
    fear_value, fear_text = get_fear_greed()

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
        "breakout": breakout,
        "fear_value": fear_value,
        "fear_text": fear_text,
        "trends": trends,
        "long_score": long_score,
        "short_score": short_score,
        "reasons": reasons[:6],
    }
