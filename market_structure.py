def find_swings(df, lookback=3):
    highs = []
    lows = []

    for i in range(lookback, len(df) - lookback):
        current_high = df["high"].iloc[i]
        current_low = df["low"].iloc[i]

        is_swing_high = True
        is_swing_low = True

        for j in range(1, lookback + 1):
            if current_high <= df["high"].iloc[i - j] or current_high <= df["high"].iloc[i + j]:
                is_swing_high = False

            if current_low >= df["low"].iloc[i - j] or current_low >= df["low"].iloc[i + j]:
                is_swing_low = False

        if is_swing_high:
            highs.append(current_high)

        if is_swing_low:
            lows.append(current_low)

    return highs[-5:], lows[-5:]


def detect_market_structure(df):
    highs, lows = find_swings(df)

    if len(highs) < 2 or len(lows) < 2:
        return "unknown"

    last_high = highs[-1]
    prev_high = highs[-2]

    last_low = lows[-1]
    prev_low = lows[-2]

    if last_high > prev_high and last_low > prev_low:
        return "bullish_structure"

    if last_high < prev_high and last_low < prev_low:
        return "bearish_structure"

    return "range_structure"


def structure_score(structure):
    if structure == "bullish_structure":
        return 15, 0

    if structure == "bearish_structure":
        return 0, 15

    return 0, 0
