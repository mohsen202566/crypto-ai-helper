def detect_trendline(df, lookback=80):
    recent = df.tail(lookback)

    first_low = recent["low"].iloc[:lookback // 2].min()
    second_low = recent["low"].iloc[lookback // 2:].min()

    first_high = recent["high"].iloc[:lookback // 2].max()
    second_high = recent["high"].iloc[lookback // 2:].max()

    if second_low > first_low and second_high > first_high:
        return "uptrend"

    if second_low < first_low and second_high < first_high:
        return "downtrend"

    return "sideways"


def detect_breakout(df, lookback=30):
    recent = df.tail(lookback + 1)
    last = recent.iloc[-1]
    previous = recent.iloc[:-1]

    resistance = previous["high"].max()
    support = previous["low"].min()
    avg_volume = previous["volume"].mean()

    if last["close"] > resistance and last["volume"] > avg_volume * 1.3:
        return "bullish_breakout"

    if last["close"] < support and last["volume"] > avg_volume * 1.3:
        return "bearish_breakout"

    if last["high"] > resistance and last["close"] < resistance:
        return "fake_bullish_breakout"

    if last["low"] < support and last["close"] > support:
        return "fake_bearish_breakout"

    return "no_breakout"


def trendline_score(trendline):
    if trendline == "uptrend":
        return 12, 0

    if trendline == "downtrend":
        return 0, 12

    return 0, 0


def breakout_score(breakout):
    if breakout == "bullish_breakout":
        return 15, 0

    if breakout == "bearish_breakout":
        return 0, 15

    if breakout == "fake_bullish_breakout":
        return 0, 8

    if breakout == "fake_bearish_breakout":
        return 8, 0

    return 0, 0
