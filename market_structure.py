# -*- coding: utf-8 -*-
def find_swings(df, lookback=3):
    highs, lows = [], []
    for i in range(lookback, len(df) - lookback):
        current_high = df["high"].iloc[i]
        current_low = df["low"].iloc[i]
        is_high = True
        is_low = True
        for j in range(1, lookback + 1):
            if current_high <= df["high"].iloc[i - j] or current_high <= df["high"].iloc[i + j]:
                is_high = False
            if current_low >= df["low"].iloc[i - j] or current_low >= df["low"].iloc[i + j]:
                is_low = False
        if is_high:
            highs.append(current_high)
        if is_low:
            lows.append(current_low)
    return highs[-5:], lows[-5:]


def detect_market_structure(df):
    highs, lows = find_swings(df)
    if len(highs) < 2 or len(lows) < 2:
        return "unknown"
    if highs[-1] > highs[-2] and lows[-1] > lows[-2]:
        return "bullish_structure"
    if highs[-1] < highs[-2] and lows[-1] < lows[-2]:
        return "bearish_structure"
    return "range_structure"


def structure_score(structure):
    if structure == "bullish_structure":
        return 12, 0
    if structure == "bearish_structure":
        return 0, 12
    return 0, 0
