"""
Coin technical analyzer for Crypto AI Helper bot.

Locked responsibility:
- Uses OKX candle data only.
- Produces technical scores for one coin.
- No Toobit, no Telegram, no order execution, no AI decision, no TP/SL.

15m/30m mode lock:
- 30m is the main signal timeframe.
- 15m can be passed as entry_candles for entry confirmation.
- Volume and ATR are confirmation sections only; they do not create direction by themselves.
- MACD histogram and Fear & Greed are not used here.
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
    ema_slope: SectionScore
    rsi_slope: SectionScore
    market_structure: SectionScore
    breakout_confirmation: SectionScore
    consolidation: SectionScore
    liquidity_sweep: SectionScore
    weighted_long_score: float
    weighted_short_score: float
    move_strength: Strength


_FALLBACK_WEIGHTS = {
    "structure": 10.0,
    "market_structure": 18.0,
    "ema_slope": 16.0,
    "rsi_slope": 14.0,
    "momentum": 8.0,
    "acceleration": 8.0,
    "breakout_confirmation": 10.0,
    "candle_price_action": 6.0,
    "liquidity": 5.0,
    "volume": 3.0,
    "volatility_atr": 2.0,
    "consolidation": 0.0,
    "liquidity_sweep": 0.0,
}


def analyze_coin(symbol: str, candles: Sequence[Candle], entry_candles: Sequence[Candle] | None = None) -> CoinAnalysis:
    """Analyze one locked-watchlist coin from OKX candles only.

    candles: main 30m candles.
    entry_candles: optional 15m candles for entry confirmation.
    """
    key = symbol.upper()
    if key not in WATCHLIST:
        raise KeyError(f"کوین خارج از واچ‌لیست قفل‌شده است: {symbol}")
    if len(candles) < 40:
        raise ValueError("برای تحلیل ۳۰m حداقل ۴۰ کندل لازم است.")
    if entry_candles is not None and len(entry_candles) < 40:
        raise ValueError("برای تأیید ورود ۱۵m حداقل ۴۰ کندل لازم است.")

    main = list(candles)
    entry = list(entry_candles) if entry_candles is not None else main

    structure = _analyze_structure(main)
    market_structure = _analyze_market_structure(main)
    ema_slope = _analyze_ema_slope(main)
    rsi_slope = _analyze_rsi_slope(main, entry)
    momentum = _analyze_momentum(main, entry)
    volume = _analyze_volume(main)
    acceleration = _analyze_acceleration(main)
    volatility = _analyze_volatility(main)
    breakout = _analyze_breakout_confirmation(main)
    consolidation = _analyze_consolidation(main)
    candle_pa = _analyze_candle_price_action(main)
    liquidity = _analyze_liquidity(main)
    liquidity_sweep = _analyze_liquidity_sweep(main)

    sections = {
        "structure": structure,
        "market_structure": market_structure,
        "ema_slope": ema_slope,
        "rsi_slope": rsi_slope,
        "momentum": momentum,
        "acceleration": acceleration,
        "breakout_confirmation": breakout,
        "candle_price_action": candle_pa,
        "liquidity": liquidity,
        "volume": volume,
        "volatility_atr": volatility,
        "consolidation": consolidation,
        "liquidity_sweep": liquidity_sweep,
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
        ema_slope=ema_slope,
        rsi_slope=rsi_slope,
        market_structure=market_structure,
        breakout_confirmation=breakout,
        consolidation=consolidation,
        liquidity_sweep=liquidity_sweep,
        weighted_long_score=long_score,
        weighted_short_score=short_score,
        move_strength=_move_strength(volume, acceleration, volatility, breakout, consolidation),
    )


def _analyze_structure(candles: Sequence[Candle]) -> SectionScore:
    # Kept for backward compatibility. Market structure below is the stronger 15m/30m section.
    recent = list(candles)[-20:]
    swing_high_now = max(c.high for c in recent[-6:])
    swing_high_prev = max(c.high for c in recent[-14:-6])
    swing_low_now = min(c.low for c in recent[-6:])
    swing_low_prev = min(c.low for c in recent[-14:-6])
    last_close = recent[-1].close

    if swing_high_now > swing_high_prev and swing_low_now > swing_low_prev:
        return SectionScore("bullish", 72.0, "ساختار کلی صعودی")
    if swing_high_now < swing_high_prev and swing_low_now < swing_low_prev:
        return SectionScore("bearish", 72.0, "ساختار کلی نزولی")
    if last_close > swing_high_prev:
        return SectionScore("bullish", 66.0, "شکست ساختار رو به بالا")
    if last_close < swing_low_prev:
        return SectionScore("bearish", 66.0, "شکست ساختار رو به پایین")
    return SectionScore("neutral", 45.0, "ساختار کلی نامشخص")


def _analyze_market_structure(candles: Sequence[Candle]) -> SectionScore:
    recent = list(candles)[-24:]
    older = recent[:8]
    middle = recent[8:16]
    latest = recent[16:]

    high_older = max(c.high for c in older)
    high_middle = max(c.high for c in middle)
    high_latest = max(c.high for c in latest)
    low_older = min(c.low for c in older)
    low_middle = min(c.low for c in middle)
    low_latest = min(c.low for c in latest)

    hh = high_latest > high_middle > high_older
    hl = low_latest > low_middle > low_older
    lh = high_latest < high_middle < high_older
    ll = low_latest < low_middle < low_older

    if hh and hl:
        return SectionScore("bullish", 86.0, "Market Structure صعودی: HH/HL معتبر")
    if lh and ll:
        return SectionScore("bearish", 86.0, "Market Structure نزولی: LH/LL معتبر")
    if high_latest > high_middle and low_latest >= low_middle:
        return SectionScore("bullish", 68.0, "ساختار متمایل به صعود")
    if low_latest < low_middle and high_latest <= high_middle:
        return SectionScore("bearish", 68.0, "ساختار متمایل به نزول")
    return SectionScore("neutral", 42.0, "Market Structure جهت تمیز ندارد")


def _analyze_ema_slope(candles: Sequence[Candle]) -> SectionScore:
    closes = [c.close for c in candles]
    if len(closes) < 40:
        return SectionScore("neutral", 40.0, "EMA slope داده کافی ندارد")

    ema20 = _ema(closes, 20)
    ema50 = _ema(closes, 50) if len(closes) >= 50 else _ema(closes, 34)
    fast_slope = _pct_change(ema20[-6], ema20[-1])
    slow_slope = _pct_change(ema50[-6], ema50[-1])
    last_close = closes[-1]

    bullish = fast_slope > 0.10 and slow_slope > 0.03 and last_close > ema20[-1]
    bearish = fast_slope < -0.10 and slow_slope < -0.03 and last_close < ema20[-1]

    if bullish:
        score = min(88.0, 58.0 + fast_slope * 80.0 + slow_slope * 60.0)
        return SectionScore("bullish", score, "EMA slope 30m صعودی و قیمت بالای EMA")
    if bearish:
        score = min(88.0, 58.0 + abs(fast_slope) * 80.0 + abs(slow_slope) * 60.0)
        return SectionScore("bearish", score, "EMA slope 30m نزولی و قیمت زیر EMA")
    return SectionScore("neutral", 45.0, "EMA slope جهت کافی ندارد")


def _analyze_rsi_slope(main_candles: Sequence[Candle], entry_candles: Sequence[Candle]) -> SectionScore:
    main_rsi = _rsi_series([c.close for c in main_candles], 14)
    entry_rsi = _rsi_series([c.close for c in entry_candles], 14)
    if len(main_rsi) < 6 or len(entry_rsi) < 6:
        return SectionScore("neutral", 40.0, "RSI slope داده کافی ندارد")

    main_slope = main_rsi[-1] - main_rsi[-5]
    entry_slope = entry_rsi[-1] - entry_rsi[-4]
    main_now = main_rsi[-1]
    entry_now = entry_rsi[-1]

    bullish = main_slope >= 2.2 and entry_slope >= 1.2 and main_now < 78.0 and entry_now < 82.0
    bearish = main_slope <= -2.2 and entry_slope <= -1.2 and main_now > 22.0 and entry_now > 18.0

    if bullish:
        score = min(90.0, 58.0 + main_slope * 3.2 + entry_slope * 2.5)
        return SectionScore("bullish", score, "RSI slope در 30m و 15m صعودی")
    if bearish:
        score = min(90.0, 58.0 + abs(main_slope) * 3.2 + abs(entry_slope) * 2.5)
        return SectionScore("bearish", score, "RSI slope در 30m و 15m نزولی")
    return SectionScore("neutral", 46.0, "RSI slope هم‌جهت و تمیز نیست")


def _analyze_momentum(main_candles: Sequence[Candle], entry_candles: Sequence[Candle]) -> SectionScore:
    # Momentum is RSI-slope + price continuation only. MACD histogram is intentionally removed.
    closes = [c.close for c in main_candles]
    rsi_values = _rsi_series(closes, 14)
    if len(rsi_values) < 6:
        return SectionScore("neutral", 40.0, "مومنتوم داده کافی ندارد")

    price_change = _pct_change(closes[-5], closes[-1])
    rsi_slope = rsi_values[-1] - rsi_values[-5]

    if price_change > 0.18 and rsi_slope > 2.0 and rsi_values[-1] < 78.0:
        return SectionScore("bullish", min(86.0, 58.0 + price_change * 24.0 + rsi_slope * 2.0), "مومنتوم صعودی بدون MACD histogram")
    if price_change < -0.18 and rsi_slope < -2.0 and rsi_values[-1] > 22.0:
        return SectionScore("bearish", min(86.0, 58.0 + abs(price_change) * 24.0 + abs(rsi_slope) * 2.0), "مومنتوم نزولی بدون MACD histogram")
    return SectionScore("neutral", 46.0, "مومنتوم جهت‌دار کافی نیست")


def _analyze_volume(candles: Sequence[Candle]) -> SectionScore:
    recent = list(candles)[-24:]
    base = [c.volume for c in recent[:-4]]
    last = [c.volume for c in recent[-4:]]
    avg_base = sum(base) / max(len(base), 1)
    avg_last = sum(last) / 4
    if avg_base <= 0:
        return SectionScore("neutral", 40.0, "حجم پایه نامعتبر")

    ratio = avg_last / avg_base
    price_change = _pct_change(recent[-5].close, recent[-1].close)
    direction = _direction_from_change(price_change, threshold=0.08)

    if ratio >= 1.35 and direction != "neutral":
        return SectionScore(direction, min(82.0, 50.0 + ratio * 18.0), "Volume تاییدکننده بالاتر از میانگین 20 کندل")
    if ratio <= 0.70:
        return SectionScore("neutral", 34.0, "Volume ضعیف؛ تأیید ورود ندارد")
    return SectionScore("neutral", 48.0, "Volume عادی؛ جهت‌ساز نیست")


def _analyze_acceleration(candles: Sequence[Candle]) -> SectionScore:
    closes = [c.close for c in candles]
    rsi_values = _rsi_series(closes, 14)
    if len(rsi_values) < 8:
        return SectionScore("neutral", 40.0, "شتاب داده کافی ندارد")

    price_change_now = _pct_change(closes[-4], closes[-1])
    price_change_prev = _pct_change(closes[-8], closes[-4])
    price_accel = price_change_now - price_change_prev

    rsi_slope_now = rsi_values[-1] - rsi_values[-3]
    rsi_slope_prev = rsi_values[-3] - rsi_values[-5]
    rsi_accel = rsi_slope_now - rsi_slope_prev

    volume_now = sum(c.volume for c in candles[-3:]) / 3
    volume_prev = sum(c.volume for c in candles[-8:-3]) / 5
    atr_now = _avg_true_range(candles[-7:])
    atr_prev = _avg_true_range(candles[-16:-7])

    volume_boost = max(0.0, _ratio_delta(volume_prev, volume_now))
    volatility_boost = max(0.0, _ratio_delta(atr_prev, atr_now))
    strength_boost = min(16.0, volume_boost * 8.0 + volatility_boost * 8.0)

    if price_change_now > 0.18 and price_accel > 0 and rsi_slope_now > 1.0 and rsi_accel >= -0.5:
        base = 56.0 + price_change_now * 20.0 + max(rsi_slope_now, 0.0) * 1.8
        return SectionScore("bullish", min(90.0, base + strength_boost), "شتاب صعودی تازه با RSI slope")
    if price_change_now < -0.18 and price_accel < 0 and rsi_slope_now < -1.0 and rsi_accel <= 0.5:
        base = 56.0 + abs(price_change_now) * 20.0 + abs(min(rsi_slope_now, 0.0)) * 1.8
        return SectionScore("bearish", min(90.0, base + strength_boost), "شتاب نزولی تازه با RSI slope")
    return SectionScore("neutral", 46.0, "شتاب جهت‌دار کافی نیست")


def _analyze_volatility(candles: Sequence[Candle]) -> SectionScore:
    atr_now = _avg_true_range(candles[-7:])
    atr_prev = _avg_true_range(candles[-20:-7])
    if atr_prev <= 0:
        return SectionScore("neutral", 40.0, "ATR نامعتبر")

    ratio = atr_now / atr_prev
    price_change = _pct_change(candles[-6].close, candles[-1].close)
    direction = _direction_from_change(price_change, threshold=0.10)

    if 1.08 <= ratio <= 1.85 and direction != "neutral":
        return SectionScore(direction, min(78.0, 48.0 + ratio * 17.0), "ATR expansion تأییدکننده حرکت")
    if ratio > 2.35:
        return SectionScore("neutral", 34.0, "ATR بیش از حد؛ ریسک حرکت آشفته")
    return SectionScore("neutral", 44.0, "ATR هنوز تأیید کافی ندارد")


def _analyze_breakout_confirmation(candles: Sequence[Candle]) -> SectionScore:
    recent = list(candles)[-24:]
    last = recent[-1]
    prior = recent[-18:-1]
    resistance = max(c.high for c in prior)
    support = min(c.low for c in prior)
    candle_range = max(last.high - last.low, 1e-12)
    close_location = (last.close - last.low) / candle_range
    avg_volume = sum(c.volume for c in prior[-20:]) / max(len(prior[-20:]), 1)
    volume_ok = avg_volume > 0 and last.volume >= avg_volume * 1.05

    if last.close > resistance and close_location >= 0.62 and volume_ok:
        return SectionScore("bullish", 82.0, "Breakout تأییدشده بالای مقاومت با close معتبر")
    if last.close < support and close_location <= 0.38 and volume_ok:
        return SectionScore("bearish", 82.0, "Breakdown تأییدشده زیر حمایت با close معتبر")
    if last.high > resistance and last.close < resistance:
        return SectionScore("bearish", 66.0, "Failed breakout / احتمال فیک‌بریک‌اوت بالا")
    if last.low < support and last.close > support:
        return SectionScore("bullish", 66.0, "Failed breakdown / برگشت از لیکوییدیتی پایین")
    return SectionScore("neutral", 42.0, "شکست تأییدشده دیده نشد")


def _analyze_consolidation(candles: Sequence[Candle]) -> SectionScore:
    recent = list(candles)[-20:]
    high = max(c.high for c in recent)
    low = min(c.low for c in recent)
    last_close = recent[-1].close
    atr = _avg_true_range(recent)
    range_pct = _pct_change(low, high)
    atr_pct = atr / max(last_close, 1e-12) * 100.0

    if range_pct <= max(0.55, atr_pct * 2.2):
        return SectionScore("neutral", 76.0, "Consolidation/Range فعال؛ ورود ممنوع")
    if range_pct <= max(0.85, atr_pct * 2.8):
        return SectionScore("neutral", 60.0, "بازار نیمه‌رنج؛ نیاز به شکست تأییدشده")
    return SectionScore("neutral", 30.0, "رنج فشرده فعال نیست")


def _analyze_candle_price_action(candles: Sequence[Candle]) -> SectionScore:
    last = candles[-1]
    body = abs(last.close - last.open)
    candle_range = max(last.high - last.low, 1e-12)
    body_ratio = body / candle_range
    upper_wick = last.high - max(last.open, last.close)
    lower_wick = min(last.open, last.close) - last.low

    if body_ratio >= 0.58 and last.close > last.open and upper_wick < body * 0.7:
        return SectionScore("bullish", 70.0, "کندل صعودی با بدنه قوی")
    if body_ratio >= 0.58 and last.close < last.open and lower_wick < body * 0.7:
        return SectionScore("bearish", 70.0, "کندل نزولی با بدنه قوی")
    return SectionScore("neutral", 44.0, "کندل تصمیم قوی ندارد")


def _analyze_liquidity(candles: Sequence[Candle]) -> SectionScore:
    # Backward-compatible alias of the stronger sweep detector.
    return _analyze_liquidity_sweep(candles)


def _analyze_liquidity_sweep(candles: Sequence[Candle]) -> SectionScore:
    recent = list(candles)[-14:]
    last = recent[-1]
    prev_high = max(c.high for c in recent[:-1])
    prev_low = min(c.low for c in recent[:-1])

    body = abs(last.close - last.open)
    candle_range = max(last.high - last.low, 1e-12)
    upper_wick = last.high - max(last.open, last.close)
    lower_wick = min(last.open, last.close) - last.low

    low_swept = last.low < prev_low and last.close > prev_low
    high_swept = last.high > prev_high and last.close < prev_high
    bullish_rejection = lower_wick >= max(body * 1.4, candle_range * 0.50)
    bearish_rejection = upper_wick >= max(body * 1.4, candle_range * 0.50)

    if low_swept and bullish_rejection:
        return SectionScore("bullish", 76.0, "Liquidity sweep پایین با rejection معتبر")
    if high_swept and bearish_rejection:
        return SectionScore("bearish", 76.0, "Liquidity sweep بالا با rejection معتبر")
    if high_swept or low_swept:
        return SectionScore("neutral", 62.0, "Sweep بدون rejection کافی؛ احتیاط")
    return SectionScore("neutral", 42.0, "Liquidity sweep معتبر دیده نشد")


def _weighted_scores(sections: dict[str, SectionScore]) -> tuple[float, float]:
    long_score = 0.0
    short_score = 0.0
    for name, section in sections.items():
        weight = float(ANALYZER_WEIGHTS.get(name, _FALLBACK_WEIGHTS.get(name, 0.0))) / 100.0
        if section.direction == "bullish":
            long_score += section.score * weight
        elif section.direction == "bearish":
            short_score += section.score * weight
        else:
            long_score += section.score * weight * 0.10
            short_score += section.score * weight * 0.10
    return round(_clamp(long_score, 0.0, 100.0), 2), round(_clamp(short_score, 0.0, 100.0), 2)


def _move_strength(
    volume: SectionScore,
    acceleration: SectionScore,
    volatility: SectionScore,
    breakout: SectionScore,
    consolidation: SectionScore,
) -> Strength:
    if consolidation.score >= 60.0:
        return "weak"
    avg = (volume.score * 0.20 + acceleration.score * 0.35 + volatility.score * 0.20 + breakout.score * 0.25)
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


def _direction_from_change(change_pct: float, threshold: float) -> Direction:
    if change_pct >= threshold:
        return "bullish"
    if change_pct <= -threshold:
        return "bearish"
    return "neutral"


def _clamp(value: float, min_value: float, max_value: float) -> float:
    return max(min_value, min(max_value, value))


__all__ = ["SectionScore", "CoinAnalysis", "analyze_coin"]
