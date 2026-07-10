"""موتور تحلیل ۳۰ تا ۶۰ دقیقه‌ای.

معماری جدید:
1) PIOM  : تشخیص آماده‌شدن حرکت، بدون صدور سیگنال.
2) MDW   : واچ بالغ جهت‌دار؛ نه خیلی زود، نه بعد از بریک‌اوت.
3) DWE   : قفل جهت با امتیاز لانگ/شورت + اختلاف + پایداری.
4) IWG   : تأیید شروع حرکت در همان جهت قفل‌شده.
5) LTSF  : فقط حذف حرکت‌های واضحاً ضعیف؛ سیگنال‌های خوب را خفه نمی‌کند.

تمام دیتای تحلیل از OKX می‌آید. این فایل هیچ تماس شبکه‌ای ندارد.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median
from typing import Any
import time

import config


@dataclass
class StrategySignal:
    symbol_id: str
    okx_symbol: str
    toobit_symbol: str
    side: str
    entry: float
    strength: str
    strength_score: float
    compression_score: float
    flow_bias: float
    absorption_score: float
    reason: str


@dataclass
class WatchCandidate:
    side: str  # LONG / SHORT / UNCERTAIN
    trigger: str
    start_price: float
    early_flow: float
    compression_score: float
    volume_ratio: float
    range_ratio: float
    expected_move_pct: float
    late_limit_pct: float
    pre_move_score: float = 0.0
    long_score: float = 0.0
    short_score: float = 0.0
    conflict_score: float = 0.0
    watch_confidence: float = 0.0
    details: dict[str, float | str] = field(default_factory=dict)


@dataclass
class WatchState:
    symbol_id: str
    okx_symbol: str
    toobit_symbol: str
    side: str
    trigger: str
    start_price: float
    created_at: float
    expected_move_pct: float
    late_limit_pct: float
    early_flow: float
    compression_score: float
    direction_locked: bool = False
    side_changes: int = 0
    confirm_count: int = 0
    bad_count: int = 0
    last_price: float = 0.0
    last_update: float = 0.0

    # خروجی‌های معماری جدید؛ main لازم نیست هنگام ساخت همه را پر کند.
    pre_move_score: float = 0.0
    long_direction_score: float = 0.0
    short_direction_score: float = 0.0
    watch_confidence: float = 0.0
    conflict_score: float = 0.0
    direction_gap: float = 0.0
    persistence_count: int = 0
    locked_side: str = "UNCERTAIN"
    observations: list[float] = field(default_factory=list)
    max_favorable_pct: float = 0.0
    max_adverse_pct: float = 0.0
    weakness_count: int = 0


@dataclass
class WatchEvaluation:
    action: str  # KEEP / SIGNAL / REMOVE / SIDE_CHANGED
    reason_fa: str
    side: str
    signal: StrategySignal | None
    metrics: dict[str, float | str]


@dataclass
class StrategyAnalysisResult:
    """فقط برای سازگاری با ProfileBuilder."""
    signal: StrategySignal | None
    reject_reason: str
    details: dict[str, float | str]


# ---------------------------------------------------------------------------
# ابزارهای سبک عددی
# ---------------------------------------------------------------------------
def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(x)))


def _score01(x: float, lo: float, hi: float) -> float:
    if hi == lo:
        return 0.0
    return _clamp((float(x) - lo) / (hi - lo))


def _safe_median(values: list[float], default: float = 0.0) -> float:
    return median(values) if values else default


def _volume(c: dict[str, float]) -> float:
    return max(float(c.get("vol_quote") or c.get("volume") or 0.0), 0.0)


def pct_range(c: dict[str, float]) -> float:
    close = float(c.get("close") or 0.0)
    return (float(c["high"]) - float(c["low"])) / close * 100.0 if close > 0 else 0.0


def _body_pct(c: dict[str, float]) -> float:
    close = float(c.get("close") or 0.0)
    if close <= 0:
        return 0.0
    return (float(c["close"]) - float(c["open"])) / close * 100.0


def _close_location(c: dict[str, float]) -> float:
    rng = max(float(c["high"]) - float(c["low"]), 1e-12)
    return _clamp((float(c["close"]) - float(c["low"])) / rng)


def _lower_wick_share(c: dict[str, float]) -> float:
    rng = max(float(c["high"]) - float(c["low"]), 1e-12)
    lower = min(float(c["open"]), float(c["close"])) - float(c["low"])
    return _clamp(lower / rng)


def _upper_wick_share(c: dict[str, float]) -> float:
    rng = max(float(c["high"]) - float(c["low"]), 1e-12)
    upper = float(c["high"]) - max(float(c["open"]), float(c["close"]))
    return _clamp(upper / rng)


def _normalize_candle_input(candles_input: Any) -> tuple[list[dict[str, float]], list[dict[str, float]]]:
    """هم ورودی قدیمی list و هم ورودی جدید dict(30m/1H) را قبول می‌کند."""
    if isinstance(candles_input, dict):
        primary_key = str(getattr(config, "OKX_PRIMARY_BAR", "30m"))
        context_key = str(getattr(config, "OKX_CONTEXT_BAR", "1H"))
        primary = candles_input.get(primary_key) or candles_input.get("primary") or []
        context = candles_input.get(context_key) or candles_input.get("context") or primary
        return list(primary or []), list(context or [])
    return list(candles_input or []), list(candles_input or [])


def pre_move_flow_bias(candles: list[dict[str, float]]) -> float:
    """پروکسی خیلی سبک فشار جهت‌دار برای مرحله اسکن.
    این قفل جهت نیست؛ فقط کمک می‌کند واچ خیلی خام نباشد.
    """
    recent = candles[-max(3, int(getattr(config, "FLOW_BIAS_LOOKBACK", 6))):]
    total_vol = sum(_volume(c) for c in recent) or 1e-9
    value = 0.0
    for c in recent:
        rng = max(float(c["high"]) - float(c["low"]), 1e-12)
        body = (float(c["close"]) - float(c["open"])) / rng
        close_location = (_close_location(c) - 0.5) * 2.0
        value += (0.62 * max(-1.0, min(1.0, body)) + 0.38 * close_location) * (_volume(c) / total_vol)
    return max(-1.0, min(1.0, value))


def _direction_scores(primary: list[dict[str, float]], context: list[dict[str, float]]) -> dict[str, float]:
    """DWE پایه از کندل‌های ۳۰/۶۰ دقیقه: جهت را قبل از بریک‌اوت از شواهد فشار می‌گیرد."""
    recent = primary[-12:]
    small = primary[-6:]
    if len(recent) < 6:
        return {"long": 0.0, "short": 0.0, "conflict": 100.0, "flow": 0.0}

    total_vol = sum(_volume(c) for c in recent) or 1e-9
    up_vol = sum(_volume(c) for c in recent if float(c["close"]) >= float(c["open"]))
    down_vol = total_vol - up_vol
    up_share = up_vol / total_vol
    down_share = down_vol / total_vol

    first_close = float(recent[0]["close"])
    last_close = float(recent[-1]["close"])
    net_pct = (last_close - first_close) / max(first_close, 1e-9) * 100.0
    med_rng = _safe_median([pct_range(c) for c in recent], 0.0) or 1e-9
    directional_norm = _clamp(abs(net_pct) / max(med_rng * 2.4, 1e-9))

    lows = [float(c["low"]) for c in small]
    highs = [float(c["high"]) for c in small]
    closes = [float(c["close"]) for c in small]
    zone_low, zone_high = min(lows), max(highs)
    midpoint = (zone_low + zone_high) / 2.0 if zone_high > zone_low else closes[-1]
    hold_long = sum(1 for c in closes if c >= midpoint) / max(len(closes), 1)
    hold_short = 1.0 - hold_long

    higher_lows = sum(1 for i in range(1, len(lows)) if lows[i] >= lows[i - 1]) / max(len(lows) - 1, 1)
    lower_highs = sum(1 for i in range(1, len(highs)) if highs[i] <= highs[i - 1]) / max(len(highs) - 1, 1)

    avg_lower_wick = _safe_median([_lower_wick_share(c) for c in recent], 0.0)
    avg_upper_wick = _safe_median([_upper_wick_share(c) for c in recent], 0.0)
    flow = pre_move_flow_bias(primary)

    # جذب فشار مخالف: فروش زیاد + نتیجه نزولی کم + ویک دفاعی پایین => لانگ
    long_abs = 25.0 * _clamp((down_share - 0.38) / 0.34) * _clamp(_score01(net_pct, -med_rng * 1.15, med_rng * 0.35) * 0.72 + avg_lower_wick * 0.38)
    short_abs = 25.0 * _clamp((up_share - 0.38) / 0.34) * _clamp(_score01(-net_pct, -med_rng * 1.15, med_rng * 0.35) * 0.72 + avg_upper_wick * 0.38)

    # effort/result: طرف غالب با تلاش کمتر نتیجه بیشتر بگیرد، طرف مقابل با تلاش زیاد نتیجه نگیرد.
    buy_eff = max(net_pct, 0.0) / max(up_share * med_rng * 2.0, 1e-9)
    sell_eff = max(-net_pct, 0.0) / max(down_share * med_rng * 2.0, 1e-9)
    long_er = 20.0 * _clamp((buy_eff - sell_eff + 0.35) / 1.15)
    short_er = 20.0 * _clamp((sell_eff - buy_eff + 0.35) / 1.15)
    # اگر فروش زیاد بوده اما افت قوی رخ نداده، به لانگ کمک کند؛ و برعکس.
    long_er = max(long_er, 20.0 * _clamp((down_share - 0.48) / 0.28) * _clamp((med_rng * 1.2 + net_pct) / max(med_rng * 2.0, 1e-9)))
    short_er = max(short_er, 20.0 * _clamp((up_share - 0.48) / 0.28) * _clamp((med_rng * 1.2 - net_pct) / max(med_rng * 2.0, 1e-9)))

    long_zone = 15.0 * hold_long
    short_zone = 15.0 * hold_short
    long_structure = 15.0 * (0.58 * higher_lows + 0.42 * _score01(net_pct, -med_rng, med_rng))
    short_structure = 15.0 * (0.58 * lower_highs + 0.42 * _score01(-net_pct, -med_rng, med_rng))
    long_flow = 15.0 * _score01(flow, -0.08, 0.36)
    short_flow = 15.0 * _score01(-flow, -0.08, 0.36)

    context_long = context_short = 5.0
    if context and len(context) >= 4:
        c0 = float(context[-4]["close"])
        c1 = float(context[-1]["close"])
        ctx_pct = (c1 - c0) / max(c0, 1e-9) * 100.0
        context_long = 10.0 * _score01(ctx_pct, -0.30, 0.60)
        context_short = 10.0 * _score01(-ctx_pct, -0.30, 0.60)

    long_score = long_abs + long_er + long_zone + long_structure + long_flow + context_long
    short_score = short_abs + short_er + short_zone + short_structure + short_flow + context_short

    # ابهام وقتی است که هر دو سمت نزدیک‌اند یا مسیر خیلی نویزی/دوطرفه است.
    gap = abs(long_score - short_score)
    chop_penalty = (1.0 - directional_norm) * 18.0 if gap < 18.0 else 0.0
    conflict = max(0.0, 45.0 - gap) + chop_penalty
    if long_score > 72.0 and short_score > 72.0:
        conflict += 25.0

    return {
        "long": round(max(0.0, min(100.0, long_score)), 3),
        "short": round(max(0.0, min(100.0, short_score)), 3),
        "conflict": round(max(0.0, min(100.0, conflict)), 3),
        "flow": round(flow, 5),
        "net_pct": round(net_pct, 5),
        "med_range": round(med_rng, 5),
    }


def detect_watch_candidate(
    candles: Any,
    profile: dict[str, Any] | None = None,
) -> tuple[WatchCandidate | None, str, dict[str, float | str]]:
    """PIOM + MDW: فقط وقتی بازار بالغ شده وارد واچ می‌شود؛ سیگنال صادر نمی‌کند."""
    primary, context = _normalize_candle_input(candles)
    min_bars = int(getattr(config, "MIN_COMPRESSION_BARS", 24))
    if len(primary) < min_bars:
        return None, "داده کندلی کافی نیست", {"تعداد_کندل": len(primary)}

    current = primary[-1]
    prev = primary[-24:-6] if len(primary) >= 30 else primary[:-6]
    recent = primary[-6:]
    current_range = pct_range(current)
    base_range = _safe_median([pct_range(c) for c in prev], 1e-9) or 1e-9
    recent_range = _safe_median([pct_range(c) for c in recent], base_range) or base_range
    compression_ratio = recent_range / base_range
    range_ratio = current_range / base_range

    current_vol = _volume(current)
    base_vol = _safe_median([_volume(c) for c in prev], 1e-9) or 1e-9
    volume_ratio = current_vol / base_vol

    profile = profile or {}
    expected = float(profile.get("tp_p70") or profile.get("tp_median") or 0.0)
    if expected <= 0:
        expected = max(recent_range * 2.6, float(getattr(config, "RISK_FALLBACK_MIN_SL_PCT", 0.55)) * config.RISK_REWARD)
    late_limit = max(
        float(getattr(config, "WATCH_LATE_MIN_PCT", 0.16)),
        min(float(getattr(config, "WATCH_LATE_MAX_PCT", 0.72)), expected * float(getattr(config, "WATCH_LATE_EXPECTED_FRACTION", 0.38))),
    )

    direction = _direction_scores(primary, context)
    long_score = float(direction["long"])
    short_score = float(direction["short"])
    conflict = float(direction["conflict"])
    gap = abs(long_score - short_score)
    flow = float(direction["flow"])
    open_px = float(current["open"])
    close_px = float(current["close"])
    current_move = abs(close_px - open_px) / max(open_px, 1e-9) * 100.0

    compression_score = 20.0 * _clamp((1.05 - compression_ratio) / 0.55)
    absorption_score = 30.0 * _clamp((max(long_score, short_score) - 48.0) / 34.0)
    effort_score = 25.0 * _clamp((gap + max(long_score, short_score) - 62.0) / 45.0)
    origin_score = 15.0 if current_move <= late_limit else 0.0
    noise_score = 10.0 * _clamp((1.55 - range_ratio) / 1.20)
    pre_move_score = max(0.0, min(100.0, compression_score + absorption_score + effort_score + origin_score + noise_score))

    max_dir = max(long_score, short_score)
    watch_conf = max(0.0, min(100.0, pre_move_score * 0.50 + max_dir * 0.42 + (100.0 - conflict) * 0.08))

    details: dict[str, float | str] = {
        "PreMove": round(pre_move_score, 2),
        "LongScore": round(long_score, 2),
        "ShortScore": round(short_score, 2),
        "Gap": round(gap, 2),
        "Conflict": round(conflict, 2),
        "WatchConfidence": round(watch_conf, 2),
        "فشار_اولیه": round(flow, 4),
        "نسبت_حجم": round(volume_ratio, 3),
        "نسبت_دامنه": round(range_ratio, 3),
        "نسبت_فشردگی": round(compression_ratio, 3),
        "حرکت_فعلی_درصد": round(current_move, 4),
        "حد_دیرشدن_درصد": round(late_limit, 4),
    }

    if current_move > late_limit:
        return None, "حرکت قبل از ورود به واچ بیش‌ازحد جلو رفته بود", details

    # واچ نه خام، نه خفه‌کننده: PreMove یا جهت باید بالغ باشد، اما همه چیز کامل لازم نیست.
    min_watch = float(getattr(config, "MDW_MIN_WATCH_CONFIDENCE", 62.0))
    min_pre = float(getattr(config, "PIOM_MIN_PREMOVE_SCORE", 58.0))
    min_dir = float(getattr(config, "DWE_MIN_DIRECTION_FOR_WATCH", 62.0))
    if not (watch_conf >= min_watch and pre_move_score >= min_pre and max_dir >= min_dir and conflict <= 65.0):
        return None, "واچ هنوز بالغ نشده؛ نشانه کافی اما خام بود", details

    if gap >= float(getattr(config, "DWE_INITIAL_GAP", 10.0)) and max_dir >= 66.0:
        side = "LONG" if long_score > short_score else "SHORT"
    else:
        side = "UNCERTAIN"

    trigger = "واچ بالغ 30-60M: آماده‌شدن حرکت + جهت احتمالی"
    if compression_ratio <= 0.88:
        trigger = "فشردگی بالغ + آماده‌شدن حرکت"
    if gap >= 18.0:
        trigger = "جهت احتمالی قبل از بریک‌اوت"

    return WatchCandidate(
        side=side,
        trigger=trigger,
        start_price=close_px,
        early_flow=flow,
        compression_score=_clamp((1.05 - compression_ratio) / 0.55),
        volume_ratio=volume_ratio,
        range_ratio=range_ratio,
        expected_move_pct=expected,
        late_limit_pct=late_limit,
        pre_move_score=pre_move_score,
        long_score=long_score,
        short_score=short_score,
        conflict_score=conflict,
        watch_confidence=watch_conf,
        details=details,
    ), "ورود به واچ بالغ", details


# ---------------------------------------------------------------------------
# DWE + IWG + LTSF در واچ زنده
# ---------------------------------------------------------------------------
def _micro_direction_scores(state: WatchState, price: float, trade_imbalance: float, book_imbalance: float, intensity: float) -> tuple[float, float, float]:
    response_pct = (price - state.start_price) / max(state.start_price, 1e-9) * 100.0
    response_norm = max(-1.0, min(1.0, response_pct / max(state.late_limit_pct * 0.55, 0.06)))
    accel = _clamp(max(intensity, 0.0) / 0.85)
    long_micro = 50.0 + trade_imbalance * 23.0 + book_imbalance * 18.0 + response_norm * 25.0 + accel * (6.0 if response_pct >= 0 else -3.0)
    short_micro = 50.0 - trade_imbalance * 23.0 - book_imbalance * 18.0 - response_norm * 25.0 + accel * (6.0 if response_pct <= 0 else -3.0)
    long_micro = max(0.0, min(100.0, long_micro))
    short_micro = max(0.0, min(100.0, short_micro))
    conflict = max(0.0, 42.0 - abs(long_micro - short_micro))
    if long_micro > 74.0 and short_micro > 74.0:
        conflict += 25.0
    return long_micro, short_micro, min(100.0, conflict)


def _update_path_state(state: WatchState, price: float, side: str) -> None:
    state.observations.append(float(price))
    max_obs = int(getattr(config, "LTSF_OBSERVATION_MAX", 12))
    if len(state.observations) > max_obs:
        state.observations = state.observations[-max_obs:]
    if side == "LONG":
        fav = (price - state.start_price) / max(state.start_price, 1e-9) * 100.0
        adv = (state.start_price - price) / max(state.start_price, 1e-9) * 100.0
    elif side == "SHORT":
        fav = (state.start_price - price) / max(state.start_price, 1e-9) * 100.0
        adv = (price - state.start_price) / max(state.start_price, 1e-9) * 100.0
    else:
        fav = adv = 0.0
    state.max_favorable_pct = max(state.max_favorable_pct, fav)
    state.max_adverse_pct = max(state.max_adverse_pct, adv)


def _ltsf_weakness(state: WatchState, side: str, price: float, trade_imbalance: float, book_imbalance: float, response_pct: float) -> tuple[int, dict[str, float | str], bool]:
    """LTSF فقط ضعف واضح را بلاک می‌کند، نه اینکه روند خیلی قوی را شرط کند."""
    obs = state.observations or [state.start_price, price]
    if len(obs) < 3:
        return 0, {"WeaknessCount": 0, "LTSF": "PASS"}, False

    if side == "LONG":
        net = obs[-1] - obs[0]
        hold = sum(1 for x in obs if x >= state.start_price) / len(obs)
        pullback_damage = 0.0
        if state.max_favorable_pct > 0:
            pullback_damage = max(0.0, (state.max_favorable_pct - max(response_pct, 0.0)) / max(state.max_favorable_pct, 1e-9))
        severe_opposite = price < state.start_price and trade_imbalance < -0.18 and book_imbalance < -0.12
    else:
        net = obs[0] - obs[-1]
        hold = sum(1 for x in obs if x <= state.start_price) / len(obs)
        pullback_damage = 0.0
        resp_fav = max(-response_pct, 0.0)
        if state.max_favorable_pct > 0:
            pullback_damage = max(0.0, (state.max_favorable_pct - resp_fav) / max(state.max_favorable_pct, 1e-9))
        severe_opposite = price > state.start_price and trade_imbalance > 0.18 and book_imbalance > 0.12

    total_path = sum(abs(obs[i] - obs[i - 1]) for i in range(1, len(obs))) or 1e-12
    efficiency = max(0.0, net) / total_path
    favorable_now = max(response_pct, 0.0) if side == "LONG" else max(-response_pct, 0.0)
    age = max(0.0, time.time() - state.created_at)

    weakness = 0
    if len(obs) >= 5 and efficiency < float(getattr(config, "LTSF_MIN_EFFICIENCY", 0.22)):
        weakness += 1
    if len(obs) >= 5 and hold < float(getattr(config, "LTSF_MIN_HOLD_RATIO", 0.44)):
        weakness += 1
    if pullback_damage > float(getattr(config, "LTSF_PULLBACK_DAMAGE_MAX", 0.66)):
        weakness += 1
    if age > float(getattr(config, "LTSF_EXPANSION_GRACE_SECONDS", 90)) and favorable_now < float(getattr(config, "WATCH_MIN_START_DISPLACEMENT_PCT", 0.025)):
        weakness += 1
    if severe_opposite:
        weakness += 2

    status = "PASS" if weakness <= 1 else ("CAUTION" if weakness == 2 else "BLOCK")
    metrics = {
        "WeaknessCount": weakness,
        "LTSF": status,
        "Efficiency": round(efficiency, 3),
        "HoldRatio": round(hold, 3),
        "PullbackDamage": round(pullback_damage, 3),
    }
    return weakness, metrics, severe_opposite


def evaluate_watch(state: WatchState, snapshot: dict[str, Any], now: float | None = None) -> WatchEvaluation:
    now = now or time.time()
    age = now - state.created_at
    price = float(snapshot.get("mid_price") or snapshot.get("last_price") or 0.0)
    if price <= 0:
        return WatchEvaluation("KEEP", "قیمت معتبر دریافت نشد؛ واچ حفظ شد", state.side, None, {"سن_واچ_ثانیه": round(age, 1)})

    trade_imbalance = float(snapshot.get("trade_imbalance") or 0.0)
    book_imbalance = float(snapshot.get("book_imbalance") or 0.0)
    intensity = float(snapshot.get("intensity_acceleration") or 0.0)
    response_pct = (price - state.start_price) / max(state.start_price, 1e-9) * 100.0
    displacement = abs(response_pct)

    long_micro, short_micro, micro_conflict = _micro_direction_scores(state, price, trade_imbalance, book_imbalance, intensity)
    long_score = max(0.0, min(100.0, 0.56 * float(state.long_direction_score or 0.0) + 0.44 * long_micro))
    short_score = max(0.0, min(100.0, 0.56 * float(state.short_direction_score or 0.0) + 0.44 * short_micro))
    gap = abs(long_score - short_score)
    conflict = max(float(state.conflict_score or 0.0) * 0.48, micro_conflict)
    leading_side = "LONG" if long_score > short_score else "SHORT"
    leading_score = max(long_score, short_score)

    metrics: dict[str, float | str] = {
        "سن_واچ_ثانیه": round(age, 1),
        "LongScore": round(long_score, 1),
        "ShortScore": round(short_score, 1),
        "Gap": round(gap, 1),
        "Conflict": round(conflict, 1),
        "عدم_تعادل_معاملات": round(trade_imbalance, 4),
        "عدم_تعادل_دفتر": round(book_imbalance, 4),
        "شتاب_معاملات": round(intensity, 4),
        "واکنش_قیمت_درصد": round(response_pct, 4),
        "حد_دیرشدن_درصد": round(state.late_limit_pct, 4),
        "Persistence": state.persistence_count,
    }

    if age > float(getattr(config, "WATCH_TTL_SECONDS", 1800)):
        return WatchEvaluation("REMOVE", "زمان منطقی واچ 30-60M تمام شد", state.side, None, metrics)
    if displacement > state.late_limit_pct:
        return WatchEvaluation("REMOVE", "قیمت پیش از تأیید بیش‌ازحد حرکت کرد و ورود دیر شد", state.side, None, metrics)

    lock_score = float(getattr(config, "DWE_LOCK_SCORE", 75.0))
    lock_gap = float(getattr(config, "DWE_LOCK_GAP", 16.0))
    max_conflict = float(getattr(config, "DWE_MAX_CONFLICT", 38.0))
    persistence_needed = int(getattr(config, "DWE_PERSISTENCE_REQUIRED", 2))
    very_strong_lock = leading_score >= float(getattr(config, "DWE_FAST_LOCK_SCORE", 86.0)) and gap >= float(getattr(config, "DWE_FAST_LOCK_GAP", 25.0))
    lock_candidate = leading_score >= lock_score and gap >= lock_gap and conflict <= max_conflict

    if lock_candidate:
        if leading_side == state.locked_side or state.locked_side == "UNCERTAIN":
            state.persistence_count += 1
        else:
            state.persistence_count = 1
        state.locked_side = leading_side
    else:
        state.persistence_count = max(0, state.persistence_count - 1)

    if not state.direction_locked:
        if lock_candidate and (state.persistence_count >= persistence_needed or very_strong_lock):
            state.direction_locked = True
            state.side = leading_side
            logger_side = leading_side
        else:
            if conflict > 70.0:
                state.bad_count += 1
            else:
                state.bad_count = max(0, state.bad_count - 1)
            if state.bad_count >= int(getattr(config, "WATCH_BAD_OBSERVATIONS_TO_REMOVE", 4)):
                return WatchEvaluation("REMOVE", "جهت در واچ چند بار مبهم/متناقض شد", state.side, None, metrics)
            return WatchEvaluation("KEEP", "واچ بالغ است اما جهت هنوز با اختلاف و پایداری کافی قفل نشده", leading_side, None, metrics)
    else:
        logger_side = state.side
        # اگر بعد از قفل، طرف مقابل با اختلاف خیلی قوی برگردد، واچ حذف می‌شود نه اینکه کورکورانه برعکس شود.
        if leading_side != state.side and leading_score >= 82.0 and gap >= 22.0:
            state.bad_count += 1
        else:
            state.bad_count = max(0, state.bad_count - 1)
        if state.bad_count >= int(getattr(config, "WATCH_BAD_OBSERVATIONS_TO_REMOVE", 4)):
            return WatchEvaluation("REMOVE", "بعد از قفل جهت، جریان بازار معکوس و پایدار شد", state.side, None, metrics)

    side = state.side if state.direction_locked else logger_side
    _update_path_state(state, price, side)

    # IWG: شروع حرکت در جهت قفل‌شده، قبل از بریک‌اوت عمومی.
    dir_sign = 1.0 if side == "LONG" else -1.0
    favorable_response = response_pct * dir_sign
    supportive_trade = trade_imbalance * dir_sign >= float(getattr(config, "IWG_TRADE_SUPPORT_MIN", 0.08))
    supportive_book = book_imbalance * dir_sign >= float(getattr(config, "IWG_BOOK_SUPPORT_MIN", 0.05))
    price_started = favorable_response >= float(getattr(config, "WATCH_MIN_START_DISPLACEMENT_PCT", 0.025))
    no_strong_opposite = trade_imbalance * dir_sign > -float(getattr(config, "IWG_OPPOSITE_MAX", 0.18))
    accelerated = intensity >= float(getattr(config, "WATCH_INTENSITY_ACCEL_MIN", 0.12))

    ignition_score = 0.0
    ignition_score += min(30.0, max(0.0, favorable_response / max(state.late_limit_pct * 0.45, 0.06) * 30.0))
    ignition_score += 22.0 if supportive_trade else max(0.0, trade_imbalance * dir_sign * 80.0)
    ignition_score += 16.0 if supportive_book else max(0.0, book_imbalance * dir_sign * 55.0)
    ignition_score += min(18.0, max(0.0, intensity) * 18.0)
    ignition_score += 14.0 if no_strong_opposite else 0.0
    ignition_score = max(0.0, min(100.0, ignition_score))
    metrics["Ignition"] = round(ignition_score, 1)

    weakness_count, weakness_metrics, severe_weakness = _ltsf_weakness(state, side, price, trade_imbalance, book_imbalance, response_pct)
    state.weakness_count = weakness_count
    metrics.update(weakness_metrics)

    if severe_weakness or weakness_count >= int(getattr(config, "LTSF_BLOCK_WEAKNESS_COUNT", 3)):
        return WatchEvaluation("REMOVE", "LTSF ضعف واضح/چندگانه روند را دید؛ ورود بلاک شد", side, None, metrics)

    ignition_min = float(getattr(config, "IWG_MIN_IGNITION_SCORE", 72.0))
    if ignition_score < ignition_min or not price_started or not no_strong_opposite:
        return WatchEvaluation("KEEP", "جهت قفل شده؛ منتظر شروع واقعی حرکت در همان جهت", side, None, metrics)

    # اگر ضعف متوسط هست، فقط تأیید بیشتری می‌خواهد؛ سیگنال خوب را خفه نمی‌کند.
    strong_setup = leading_score >= 84.0 and ignition_score >= 82.0 and gap >= 20.0 and weakness_count <= 2
    needed = 1 if strong_setup else int(getattr(config, "WATCH_CONFIRMATIONS_REQUIRED", 2))
    state.confirm_count += 1
    if state.confirm_count < needed:
        return WatchEvaluation("KEEP", "تأیید اول شروع حرکت دریافت شد؛ برای حذف نویز یک مشاهده دیگر لازم است", side, None, metrics)

    ltsf_bonus = 18.0 if weakness_count == 0 else (10.0 if weakness_count == 1 else 3.0)
    strength_score = max(0.0, min(100.0, leading_score * 0.46 + ignition_score * 0.38 + ltsf_bonus))
    min_strength = float(getattr(config, "MIN_SIGNAL_STRENGTH_SCORE", 62.0))
    if strength_score < min_strength:
        return WatchEvaluation("KEEP", "امتیاز نهایی هنوز برای ورود 30-60M کافی نیست", side, None, metrics)

    if strength_score >= 84:
        strength = "خیلی قوی"
    elif strength_score >= 72:
        strength = "قوی"
    else:
        strength = "متوسط"

    signal = StrategySignal(
        symbol_id=state.symbol_id,
        okx_symbol=state.okx_symbol,
        toobit_symbol=state.toobit_symbol,
        side=side,
        entry=price,
        strength=strength,
        strength_score=round(strength_score, 2),
        compression_score=round(state.compression_score * 100.0, 2),
        flow_bias=round(trade_imbalance, 4),
        absorption_score=round(leading_score, 2),
        reason=(
            f"PIOM/MDW + DWE + IWG + LTSF | trigger={state.trigger} | "
            f"side={side} | DWE={leading_score:.1f} gap={gap:.1f} conflict={conflict:.1f} | "
            f"ignition={ignition_score:.1f} weakness={weakness_count} | "
            f"trade={trade_imbalance:.3f} book={book_imbalance:.3f} response={response_pct:.4f}%"
        ),
    )
    return WatchEvaluation("SIGNAL", "جهت قفل شد، شروع حرکت همان جهت تأیید شد، ضعف واضح دیده نشد", side, signal, metrics)


# ---------------------------------------------------------------------------
# سازگاری با ProfileBuilder: برای ساخت پروفایل روزانه از کندل‌ها.
# ---------------------------------------------------------------------------
def analyze_symbol_detailed(symbol_id: str, okx_symbol: str, toobit_symbol: str, candles: Any) -> StrategyAnalysisResult:
    candidate, reason, details = detect_watch_candidate(candles, profile=None)
    if not candidate:
        return StrategyAnalysisResult(None, "watch_candidate_fail", details)
    if candidate.side == "UNCERTAIN":
        return StrategyAnalysisResult(None, "direction_uncertain", details)
    score = min(100.0, 0.55 * max(candidate.long_score, candidate.short_score) + 0.45 * candidate.watch_confidence)
    strength = "قوی" if score >= 72 else "متوسط"
    signal = StrategySignal(
        symbol_id=symbol_id,
        okx_symbol=okx_symbol,
        toobit_symbol=toobit_symbol,
        side=candidate.side,
        entry=candidate.start_price,
        strength=strength,
        strength_score=round(score, 2),
        compression_score=round(candidate.compression_score * 100.0, 2),
        flow_bias=round(candidate.early_flow, 4),
        absorption_score=round(max(candidate.long_score, candidate.short_score), 2),
        reason=f"پروکسی تاریخی 30-60M: {candidate.trigger}",
    )
    return StrategyAnalysisResult(signal, "accepted", details)


def analyze_symbol(symbol_id: str, okx_symbol: str, toobit_symbol: str, candles: Any) -> StrategySignal | None:
    return analyze_symbol_detailed(symbol_id, okx_symbol, toobit_symbol, candles).signal
