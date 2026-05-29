import requests
import pandas as pd
import ta


def get_klines(symbol, interval="15m", limit=200):
    url = "https://api.binance.com/api/v3/klines"
    params = {
        "symbol": symbol,
        "interval": interval,
        "limit": limit
    }

    r = requests.get(url, params=params, timeout=10)
    data = r.json()

    df = pd.DataFrame(data, columns=[
        "time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades",
        "taker_buy_base", "taker_buy_quote", "ignore"
    ])

    df["open"] = df["open"].astype(float)
    df["high"] = df["high"].astype(float)
    df["low"] = df["low"].astype(float)
    df["close"] = df["close"].astype(float)
    df["volume"] = df["volume"].astype(float)

    return df


def add_indicators(df):
    df["ema20"] = ta.trend.ema_indicator(df["close"], window=20)
    df["ema50"] = ta.trend.ema_indicator(df["close"], window=50)
    df["ema200"] = ta.trend.ema_indicator(df["close"], window=200)

    df["rsi"] = ta.momentum.rsi(df["close"], window=14)

    macd = ta.trend.MACD(df["close"])
    df["macd"] = macd.macd()
    df["macd_signal"] = macd.macd_signal()

    return df


def trend_score(df):
    last = df.iloc[-1]
    score_long = 0
    score_short = 0

    if last["close"] > last["ema20"] > last["ema50"]:
        score_long += 20

    if last["close"] < last["ema20"] < last["ema50"]:
        score_short += 20

    if last["close"] > last["ema200"]:
        score_long += 15

    if last["close"] < last["ema200"]:
        score_short += 15

    return score_long, score_short


def momentum_score(df):
    last = df.iloc[-1]
    score_long = 0
    score_short = 0

    rsi = last["rsi"]

    if 50 <= rsi <= 70:
        score_long += 15
    elif rsi > 75:
        score_short += 8

    if 30 <= rsi <= 50:
        score_short += 15
    elif rsi < 25:
        score_long += 8

    if last["macd"] > last["macd_signal"]:
        score_long += 15

    if last["macd"] < last["macd_signal"]:
        score_short += 15

    return score_long, score_short


def volume_score(df):
    last_volume = df["volume"].iloc[-1]
    avg_volume = df["volume"].tail(20).mean()

    if last_volume > avg_volume * 1.2:
        return 10

    if last_volume > avg_volume:
        return 5

    return 0


def analyze_symbol(symbol):
    df_4h = add_indicators(get_klines(symbol, "4h"))
    df_1h = add_indicators(get_klines(symbol, "1h"))
    df_15m = add_indicators(get_klines(symbol, "15m"))

    long_score = 0
    short_score = 0

    l4, s4 = trend_score(df_4h)
    l1, s1 = trend_score(df_1h)
    l15, s15 = trend_score(df_15m)

    ml15, ms15 = momentum_score(df_15m)

    long_score += l4 + l1 + l15 + ml15
    short_score += s4 + s1 + s15 + ms15

    vol = volume_score(df_15m)
    long_score += vol
    short_score += vol

    last = df_15m.iloc[-1]
    price = last["close"]

    if long_score > short_score:
        direction = "LONG"
        score = min(long_score, 100)
    elif short_score > long_score:
        direction = "SHORT"
        score = min(short_score, 100)
    else:
        direction = "NO TRADE"
        score = 50

    if score < 60:
        direction = "NO TRADE"

    return {
        "symbol": symbol,
        "price": price,
        "direction": direction,
        "score": score,
        "rsi": round(last["rsi"], 2),
        "macd": round(last["macd"], 4),
        "macd_signal": round(last["macd_signal"], 4),
        "ema20": round(last["ema20"], 4),
        "ema50": round(last["ema50"], 4),
        "ema200": round(last["ema200"], 4),
        "long_score": long_score,
        "short_score": short_score
    }
