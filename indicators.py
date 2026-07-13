from __future__ import annotations


def closes(candles):
    return [float(x["close"]) for x in candles]


def ema(values, period):
    if not values:
        return []
    alpha = 2.0 / (period + 1.0)
    out = [float(values[0])]
    for value in values[1:]:
        out.append(alpha * float(value) + (1.0 - alpha) * out[-1])
    return out


def sma(values, period):
    if not values:
        return 0.0
    window = values[-max(1, int(period)):]
    return sum(window) / len(window)


def atr(candles, period=14):
    if not candles:
        return []
    true_ranges = []
    for index, candle in enumerate(candles):
        high = float(candle["high"])
        low = float(candle["low"])
        previous_close = float(candles[index - 1]["close"]) if index else float(candle["close"])
        true_ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    return ema(true_ranges, period)


def rsi(values, period=14):
    if len(values) < 2:
        return [50.0] * len(values)
    gains = [0.0]
    losses = [0.0]
    for previous, current in zip(values, values[1:]):
        change = float(current) - float(previous)
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    average_gain = ema(gains, period)
    average_loss = ema(losses, period)
    out = []
    for gain, loss in zip(average_gain, average_loss):
        if loss == 0 and gain == 0:
            out.append(50.0)
        elif loss == 0:
            out.append(100.0)
        else:
            out.append(100.0 - 100.0 / (1.0 + gain / loss))
    return out


def macd(values, fast=12, slow=26, signal=9):
    fast_ema = ema(values, fast)
    slow_ema = ema(values, slow)
    line = [a - b for a, b in zip(fast_ema, slow_ema)]
    signal_line = ema(line, signal)
    histogram = [a - b for a, b in zip(line, signal_line)]
    return line, signal_line, histogram


def dmi_adx(candles, period=14):
    if not candles:
        return [], [], []
    plus_dm = [0.0]
    minus_dm = [0.0]
    true_ranges = [float(candles[0]["high"]) - float(candles[0]["low"])]
    for previous, current in zip(candles, candles[1:]):
        up = float(current["high"]) - float(previous["high"])
        down = float(previous["low"]) - float(current["low"])
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
        high = float(current["high"])
        low = float(current["low"])
        previous_close = float(previous["close"])
        true_ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    smoothed_tr = ema(true_ranges, period)
    smoothed_plus = ema(plus_dm, period)
    smoothed_minus = ema(minus_dm, period)
    plus_di = [100.0 * p / tr if tr else 0.0 for p, tr in zip(smoothed_plus, smoothed_tr)]
    minus_di = [100.0 * m / tr if tr else 0.0 for m, tr in zip(smoothed_minus, smoothed_tr)]
    dx = [100.0 * abs(p - m) / (p + m) if p + m else 0.0 for p, m in zip(plus_di, minus_di)]
    return plus_di, minus_di, ema(dx, period)


def efficiency(values, lookback=12):
    if len(values) < 2:
        return 0.0
    window = values[-max(2, int(lookback)):]
    denominator = sum(abs(float(b) - float(a)) for a, b in zip(window, window[1:]))
    return abs(float(window[-1]) - float(window[0])) / denominator if denominator else 0.0


def candle_features(candle):
    open_, high, low, close = map(float, (candle["open"], candle["high"], candle["low"], candle["close"]))
    candle_range = max(high - low, 1e-12)
    return {
        "body_ratio": abs(close - open_) / candle_range,
        "close_location": (close - low) / candle_range,
        "upper_wick": (high - max(open_, close)) / candle_range,
        "lower_wick": (min(open_, close) - low) / candle_range,
        "direction": 1 if close > open_ else -1 if close < open_ else 0,
    }


def swing_points(candles, window=2):
    highs, lows = [], []
    window = max(1, int(window))
    for index in range(window, len(candles) - window):
        high = float(candles[index]["high"])
        low = float(candles[index]["low"])
        neighborhood = candles[index - window:index + window + 1]
        if high >= max(float(x["high"]) for x in neighborhood):
            highs.append((index, high))
        if low <= min(float(x["low"]) for x in neighborhood):
            lows.append((index, low))
    return highs, lows


def normalized_atr(candles, period=14):
    values = atr(candles, period)
    price = float(candles[-1]["close"]) if candles else 0.0
    return values[-1] / price * 100.0 if values and price else 0.0


def volume_ratio(candles, lookback=20):
    if len(candles) < 2:
        return 1.0
    volumes = [float(x.get("volume", 0.0)) for x in candles]
    baseline = sma(volumes[:-1], lookback)
    return volumes[-1] / baseline if baseline else 1.0


def session_vwap(candles, lookback=96):
    window = candles[-max(1, int(lookback)):]
    weighted = 0.0
    volume = 0.0
    for candle in window:
        vol = float(candle.get("volume", 0.0))
        typical = (float(candle["high"]) + float(candle["low"]) + float(candle["close"])) / 3.0
        weighted += typical * vol
        volume += vol
    return weighted / volume if volume else (float(window[-1]["close"]) if window else 0.0)


def overlap_ratio(candles, lookback=12):
    window = candles[-max(2, int(lookback)):]
    if len(window) < 2:
        return 0.0
    overlaps = []
    for previous, current in zip(window, window[1:]):
        intersection = max(0.0, min(float(previous["high"]), float(current["high"])) - max(float(previous["low"]), float(current["low"])))
        union = max(float(previous["high"]), float(current["high"])) - min(float(previous["low"]), float(current["low"]))
        overlaps.append(intersection / union if union else 0.0)
    return sum(overlaps) / len(overlaps)
