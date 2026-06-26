"""
Coin technical analyzer for Crypto AI Helper bot.

Locked responsibility:
- Uses OKX candle data only.
- Produces technical scores for one coin.
- No Toobit, no Telegram, no order execution, no AI decision, no TP/SL.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Sequence

from config import ANALYZER_WEIGHTS, WATCHLIST
from market_context import Candle

Direction = Literal["bullish", "bearish", "neutral"]
Strength = Literal["weak", "normal", "strong"]


@dataclass(frozen=True)
class SectionScore:
    direction: Direction
    score: float
    reason: str


@dataclass(frozen=True)
class CoinAnalysis:
    symbol: str
    structure: SectionScore
    momentum: SectionScore
    volume: SectionScore
    acceleration: SectionScore
    volatility_atr: SectionScore
    candle_price_action: SectionScore
    liquidity: SectionScore
    weighted_long_score: float
    weighted_short_score: float
    move_strength: Strength


def analyze_coin(symbol: str, candles: Sequence[Candle]) -> CoinAnalysis:
    """Analyze one locked-watchlist coin from OKX candles only."""
    key = symbol.upper()
    if key not in WATCHLIST:
        raise KeyError(f"کوین خارج از واچ‌لیست قفل‌شده است: {symbol}")
    if len(candles) < 40:
        raise ValueError("برای تحلیل کوین حداقل ۴۰ کندل لازم است.")

    structure = _analyze_structure(candles)
    momentum = _analyze_momentum(candles)
    volume = _analyze_volume(candles)
    acceleration = _analyze_acceleration(candles)
    volatility = _analyze_volatility(candles)
    candle_pa = _analyze_candle_price_action(candles)
    liquidity = _analyze_liquidity(candles)

    sections = {
        "structure": structure,
        "momentum": momentum,
        "volume": volume,
        "acceleration": acceleration,
        "volatility_atr": volatility,
        "candle_price_action": candle_pa,
        "liquidity": liquidity,
    }
    long_score, short_score = _weighted_scores(sections)

    return CoinAnalysis(
        symbol=key,
        structure=structure,
        momentum=momentum,
        volume=volume,
        acceleration=acceleration,
        volatility_atr=volatility,
        candle_price_action=candle_pa,
        liquidity=liquidity,
        weighted_long_score=long_score,
        weighted_short_score=short_score,
        move_strength=_move_strength(volume, acceleration, volatility),
    )


def _analyze_structure(candles: Sequence[Candle]) -> SectionScore:
    recent = list(candles)[-20:]
    swing_high_now = max(c.high for c in recent[-6:])
    swing_high_prev = max(c.high for c in recent[-14:-6])
    swing_low_now = min(c.low for c in recent[-6:])
    swing_low_prev = min(c.low for c in recent[-14:-6])
    last_close = recent[-1].close

    if swing_high_now > swing_high_prev and swing_low_now > swing_low_prev:
        return SectionScore("bullish", 78.0, "HH/HL ساختار صعودی")
    if swing_high_now < swing_high_prev and swing_low_now < swing_low_prev:
        return SectionScore("bearish", 78.0, "LH/LL ساختار نزولی")
    if last_close > swing_high_prev:
        return SectionScore("bullish", 72.0, "شکست ساختار رو به بالا")
    if last_close < swing_low_prev:
        return SectionScore("bearish", 72.0, "شکست ساختار رو به پایین")
    return SectionScore("neutral", 45.0, "ساختار نامشخص")


def _analyze_momentum(candles: Sequence[Candle]) -> SectionScore:
    closes = [c.close for c in candles]
    rsi_values = _rsi_series(closes, 14)
    macd_hist = _macd_histogram(closes)
    if len(rsi_values) < 6 or len(macd_hist) < 6:
        return SectionScore("neutral", 40.0, "مومنتوم داده کافی ندارد")

    rsi_slope = rsi_values[-1] - rsi_values[-4]
    hist_slope = macd_hist[-1] - macd_hist[-4]

    if rsi_slope > 3.0 and hist_slope > 0:
        return SectionScore("bullish", min(88.0, 62.0 + rsi_slope * 3.0), "شیب RSI و MACD مثبت")
    if rsi_slope < -3.0 and hist_slope < 0:
        return SectionScore("bearish", min(88.0, 62.0 + abs(rsi_slope) * 3.0), "شیب RSI و MACD منفی")
    return SectionScore("neutral", 48.0, "مومنتوم هم‌جهت قوی نیست")


def _analyze_volume(candles: Sequence[Candle]) -> SectionScore:
    recent = list(candles)[-20:]
    base = [c.volume for c in recent[:-3]]
    last = [c.volume for c in recent[-3:]]
    avg_base = sum(base) / max(len(base), 1)
    avg_last = sum(last) / 3
    last_candle = recent[-1]
    direction: Direction = "bullish" if last_candle.close >= last_candle.open else "bearish"

    if avg_base <= 0:
        return SectionScore("neutral", 40.0, "حجم پایه نامعتبر")
    ratio = avg_last / avg_base
    if ratio >= 1.45:
        return SectionScore(direction, min(90.0, 55.0 + ratio * 18.0), "ورود حجم بالاتر از میانگین")
    if ratio <= 0.65:
        return SectionScore("neutral", 35.0, "حجم ضعیف")
    return SectionScore("neutral", 50.0, "حجم عادی")


def _analyze_acceleration(candles: Sequence[Candle]) -> SectionScore:
    """Detect early pump/dump acceleration.

    Direction is decided only by price, RSI slope, and MACD histogram slope.
    Volume and ATR expansion only increase confidence after direction is known;
    they must never flip a dump into bullish or a pump into bearish.
    """
    closes = [c.close for c in candles]
    rsi_values = _rsi_series(closes, 14)
    macd_hist = _macd_histogram(closes)
    if len(rsi_values) < 8 or len(macd_hist) < 8:
        return SectionScore("neutral", 40.0, "شتاب داده کافی ندارد")

    price_change_now = _pct_change(closes[-4], closes[-1])
    price_change_prev = _pct_change(closes[-8], closes[-4])
    price_accel = price_change_now - price_change_prev

    rsi_slope_now = rsi_values[-1] - rsi_values[-3]
    rsi_slope_prev = rsi_values[-3] - rsi_values[-5]
    hist_slope_now = macd_hist[-1] - macd_hist[-3]
    hist_slope_prev = macd_hist[-3] - macd_hist[-5]

    rsi_accel = rsi_slope_now - rsi_slope_prev
    hist_accel = (hist_slope_now - hist_slope_prev) * 100.0

    volume_now = sum(c.volume for c in candles[-3:]) / 3
    volume_prev = sum(c.volume for c in candles[-8:-3]) / 5
    atr_now = _avg_true_range(candles[-7:])
    atr_prev = _avg_true_range(candles[-16:-7])

    volume_boost = max(0.0, _ratio_delta(volume_prev, volume_now))
    volatility_boost = max(0.0, _ratio_delta(atr_prev, atr_now))
    strength_boost = min(18.0, volume_boost * 9.0 + volatility_boost * 9.0)

    # Directional core: price is mandatory; RSI/MACD support it when they are not saturated.
    # Volume/ATR are intentionally not part of the direction decision.
    bullish_votes = 0
    bearish_votes = 0
    if price_change_now > 0.20:
        bullish_votes += 2
    elif price_change_now > 0.08 and price_accel > 0:
        bullish_votes += 1
    if rsi_slope_now > 1.2 or (rsi_values[-1] >= 68.0 and price_change_now > 0):
        bullish_votes += 1
    if hist_slope_now > 0 or (macd_hist[-1] > macd_hist[-5] and price_change_now > 0):
        bullish_votes += 1

    if price_change_now < -0.20:
        bearish_votes += 2
    elif price_change_now < -0.08 and price_accel < 0:
        bearish_votes += 1
    if rsi_slope_now < -1.2 or (rsi_values[-1] <= 32.0 and price_change_now < 0):
        bearish_votes += 1
    if hist_slope_now < 0 or (macd_hist[-1] < macd_hist[-5] and price_change_now < 0):
        bearish_votes += 1

    if bullish_votes >= 2 and bullish_votes > bearish_votes:
        base = 56.0 + abs(price_change_now) * 18.0 + max(rsi_slope_now, 0.0) * 1.5
        return SectionScore("bullish", min(92.0, base + strength_boost), "شتاب جهت‌دار پامپ با تأیید قیمت/RSI/MACD")
    if bearish_votes >= 2 and bearish_votes > bullish_votes:
        base = 56.0 + abs(price_change_now) * 18.0 + abs(min(rsi_slope_now, 0.0)) * 1.5
        return SectionScore("bearish", min(92.0, base + strength_boost), "شتاب جهت‌دار دامپ با تأیید قیمت/RSI/MACD")
    return SectionScore("neutral", 48.0, "شتاب جهت‌دار کافی نیست")


def _analyze_volatility(candles: Sequence[Candle]) -> SectionScore:
    atr_now = _avg_true_range(candles[-7:])
    atr_prev = _avg_true_range(candles[-20:-7])
    last = candles[-1]
    direction: Direction = "bullish" if last.close >= last.open else "bearish"
    if atr_prev <= 0:
        return SectionScore("neutral", 40.0, "ATR نامعتبر")
    ratio = atr_now / atr_prev
    if 1.08 <= ratio <= 1.9:
        return SectionScore(direction, min(82.0, 50.0 + ratio * 18.0), "ATR در حال باز شدن منطقی")
    if ratio > 2.4:
        return SectionScore("neutral", 35.0, "نوسان بیش از حد آشفته")
    return SectionScore("neutral", 45.0, "نوسان هنوز کافی نیست")


def _analyze_candle_price_action(candles: Sequence[Candle]) -> SectionScore:
    last = candles[-1]
    body = abs(last.close - last.open)
    candle_range = max(last.high - last.low, 1e-12)
    body_ratio = body / candle_range
    upper_wick = last.high - max(last.open, last.close)
    lower_wick = min(last.open, last.close) - last.low

    if body_ratio >= 0.58 and last.close > last.open and upper_wick < body * 0.7:
        return SectionScore("bullish", 72.0, "کندل صعودی با بدنه قوی")
    if body_ratio >= 0.58 and last.close < last.open and lower_wick < body * 0.7:
        return SectionScore("bearish", 72.0, "کندل نزولی با بدنه قوی")
    return SectionScore("neutral", 45.0, "کندل تصمیم قوی ندارد")


def _analyze_liquidity(candles: Sequence[Candle]) -> SectionScore:
    recent = list(candles)[-12:]
    last = recent[-1]
    prev_high = max(c.high for c in recent[:-1])
    prev_low = min(c.low for c in recent[:-1])

    body = abs(last.close - last.open)
    candle_range = max(last.high - last.low, 1e-12)
    upper_wick = last.high - max(last.open, last.close)
    lower_wick = min(last.open, last.close) - last.low

    # A valid sweep must show real rejection, not only a tiny break of high/low.
    low_swept = last.low < prev_low and last.close > prev_low
    high_swept = last.high > prev_high and last.close < prev_high
    bullish_rejection = lower_wick >= max(body * 1.5, candle_range * 0.55)
    bearish_rejection = upper_wick >= max(body * 1.5, candle_range * 0.55)

    if low_swept and bullish_rejection:
        return SectionScore("bullish", 72.0, "Liquidity sweep پایین با wick/rejection معتبر")
    if high_swept and bearish_rejection:
        return SectionScore("bearish", 72.0, "Liquidity sweep بالا با wick/rejection معتبر")
    return SectionScore("neutral", 45.0, "نقدینگی معتبر دیده نشد")


def _weighted_scores(sections: dict[str, SectionScore]) -> tuple[float, float]:
    long_score = 0.0
    short_score = 0.0
    for name, section in sections.items():
        weight = ANALYZER_WEIGHTS[name] / 100.0
        if section.direction == "bullish":
            long_score += section.score * weight
        elif section.direction == "bearish":
            short_score += section.score * weight
        else:
            long_score += section.score * weight * 0.25
            short_score += section.score * weight * 0.25
    return round(long_score, 2), round(short_score, 2)


def _move_strength(volume: SectionScore, acceleration: SectionScore, volatility: SectionScore) -> Strength:
    avg = (volume.score + acceleration.score + volatility.score) / 3.0
    if avg >= 72.0:
        return "strong"
    if avg >= 52.0:
        return "normal"
    return "weak"


def _rsi_series(closes: Sequence[float], period: int) -> list[float]:
    if len(closes) <= period:
        return []
    values: list[float] = []
    gains: list[float] = []
    losses: list[float] = []
    for idx in range(1, len(closes)):
        delta = closes[idx] - closes[idx - 1]
        gains.append(max(delta, 0.0))
        losses.append(abs(min(delta, 0.0)))
        if idx >= period:
            avg_gain = sum(gains[-period:]) / period
            avg_loss = sum(losses[-period:]) / period
            if avg_loss == 0:
                values.append(100.0)
            else:
                rs = avg_gain / avg_loss
                values.append(100.0 - (100.0 / (1.0 + rs)))
    return values


def _ema(values: Sequence[float], period: int) -> list[float]:
    if not values:
        return []
    alpha = 2.0 / (period + 1.0)
    result = [float(values[0])]
    for value in values[1:]:
        result.append(alpha * value + (1.0 - alpha) * result[-1])
    return result


def _macd_histogram(closes: Sequence[float]) -> list[float]:
    if len(closes) < 35:
        return []
    ema12 = _ema(closes, 12)
    ema26 = _ema(closes, 26)
    macd = [a - b for a, b in zip(ema12[-len(ema26):], ema26)]
    signal = _ema(macd, 9)
    return [m - s for m, s in zip(macd[-len(signal):], signal)]


def _avg_true_range(candles: Sequence[Candle]) -> float:
    items = list(candles)
    if len(items) < 2:
        return 0.0
    ranges: list[float] = []
    for idx in range(1, len(items)):
        current = items[idx]
        previous = items[idx - 1]
        ranges.append(max(current.high - current.low, abs(current.high - previous.close), abs(current.low - previous.close)))
    return sum(ranges) / len(ranges)


def _ratio_delta(old: float, new: float) -> float:
    if old <= 0:
        return 0.0
    return (new - old) / old


def _pct_change(old: float, new: float) -> float:
    if old == 0:
        return 0.0
    return (new - old) / old * 100.0


__all__ = ["SectionScore", "CoinAnalysis", "analyze_coin"]
