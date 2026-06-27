"""
TP/SL engine for Crypto AI Helper bot.

Locked responsibility:
- One TP1 and one SL only.
- Risk/Reward only 1:1.2, 1:1.5 or 1:2.
- Checks expected movement, margin/leverage, fees and minimum net profit.
- Does not place orders, call APIs, send Telegram messages, or make AI decisions.

Design lock:
- Small, simple, strong.
- Reject invalid stop-loss direction instead of silently building a wrong plan.
- Prefer SIGNAL_ONLY when the expected move cannot cover R:R, fees, and minimum net profit.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from config import (
    ALLOWED_RISK_REWARD,
    COIN_MOVE_PROFILE,
    DEFAULT_CLOSE_FEE_RATE,
    DEFAULT_OPEN_FEE_RATE,
    DEFAULT_MIN_NET_PROFIT_USDT,
    DEFAULT_TRADE_DOLLAR,
    DEFAULT_LEVERAGE,
)

Direction = Literal["LONG", "SHORT"]
MoveStrength = Literal["weak", "normal", "strong"]
ExecutionMode = Literal["REAL_ALLOWED", "SIGNAL_ONLY"]


@dataclass(frozen=True)
class TPSLPlan:
    symbol: str
    direction: Direction
    entry: float
    tp: float
    sl: float
    risk_reward: float
    estimated_move_pct: float
    gross_profit_usdt: float
    estimated_fee_usdt: float
    net_profit_usdt: float
    execution_mode: ExecutionMode
    reason: str


def build_tp_sl_plan(
    symbol: str,
    direction: Direction,
    entry: float,
    suggested_sl: float,
    move_strength: MoveStrength,
    trade_margin_usdt: float = DEFAULT_TRADE_DOLLAR,
    leverage: int = DEFAULT_LEVERAGE,
    min_net_profit_usdt: float = DEFAULT_MIN_NET_PROFIT_USDT,
    open_fee_rate: float = DEFAULT_OPEN_FEE_RATE,
    close_fee_rate: float = DEFAULT_CLOSE_FEE_RATE,
) -> TPSLPlan:
    key = symbol.upper()
    _validate_inputs(
        symbol=key,
        direction=direction,
        entry=entry,
        suggested_sl=suggested_sl,
        move_strength=move_strength,
        trade_margin_usdt=trade_margin_usdt,
        leverage=leverage,
        min_net_profit_usdt=min_net_profit_usdt,
        open_fee_rate=open_fee_rate,
        close_fee_rate=close_fee_rate,
    )

    risk_pct = abs(entry - suggested_sl) / entry * 100.0
    expected_move_pct = _expected_move_pct(key, move_strength)
    rr = _select_risk_reward(risk_pct, expected_move_pct, move_strength)

    if rr is None:
        return _signal_only_plan(
            key,
            direction,
            entry,
            suggested_sl,
            risk_pct,
            expected_move_pct,
            "هیچ R:R مجاز به حرکت منطقی نمی‌رسد",
        )

    tp = _tp_from_rr(entry, suggested_sl, direction, rr)
    tp_move_pct = abs(tp - entry) / entry * 100.0
    notional = trade_margin_usdt * leverage
    gross_profit = _estimated_gross_profit(notional, tp_move_pct)
    fee = _estimated_fee(notional, open_fee_rate, close_fee_rate)
    net = gross_profit - fee

    if net < min_net_profit_usdt:
        return _signal_only_plan(
            key,
            direction,
            entry,
            suggested_sl,
            risk_pct,
            expected_move_pct,
            (
                "سود خالص کمتر از حداقل مجاز است"
                f" | Net={net:.4f} | MinNet={min_net_profit_usdt:.4f}"
            ),
            rr=rr,
            gross_profit=gross_profit,
            fee=fee,
            net=net,
        )

    return TPSLPlan(
        symbol=key,
        direction=direction,
        entry=round(entry, 8),
        tp=round(tp, 8),
        sl=round(suggested_sl, 8),
        risk_reward=rr,
        estimated_move_pct=round(tp_move_pct, 4),
        gross_profit_usdt=round(gross_profit, 4),
        estimated_fee_usdt=round(fee, 4),
        net_profit_usdt=round(net, 4),
        execution_mode="REAL_ALLOWED",
        reason="سود خالص کافی است",
    )


def _validate_inputs(
    *,
    symbol: str,
    direction: Direction,
    entry: float,
    suggested_sl: float,
    move_strength: MoveStrength,
    trade_margin_usdt: float,
    leverage: int,
    min_net_profit_usdt: float,
    open_fee_rate: float,
    close_fee_rate: float,
) -> None:
    if symbol not in COIN_MOVE_PROFILE:
        raise KeyError(f"کوین خارج از پروفایل حرکت قفل‌شده است: {symbol}")
    if direction not in ("LONG", "SHORT"):
        raise ValueError("جهت باید LONG یا SHORT باشد.")
    if move_strength not in ("weak", "normal", "strong"):
        raise ValueError("قدرت حرکت باید weak، normal یا strong باشد.")
    if entry <= 0 or suggested_sl <= 0:
        raise ValueError("ورود و استاپ باید مثبت باشند.")
    if direction == "LONG" and suggested_sl >= entry:
        raise ValueError("برای لانگ، SL باید پایین‌تر از Entry باشد.")
    if direction == "SHORT" and suggested_sl <= entry:
        raise ValueError("برای شورت، SL باید بالاتر از Entry باشد.")
    if trade_margin_usdt <= 0 or leverage <= 0:
        raise ValueError("مارجین و لوریج باید مثبت باشند.")
    if min_net_profit_usdt < 0:
        raise ValueError("حداقل سود خالص نمی‌تواند منفی باشد.")
    if open_fee_rate < 0 or close_fee_rate < 0:
        raise ValueError("نرخ کارمزد نمی‌تواند منفی باشد.")
    if not _allowed_rr_values_are_locked():
        raise ValueError("R:R مجاز فقط باید 1.2، 1.5 یا 2.0 باشد.")


def _allowed_rr_values_are_locked() -> bool:
    allowed = {float(rr) for rr in ALLOWED_RISK_REWARD}
    return bool(allowed) and allowed.issubset({1.2, 1.5, 2.0})


def _select_risk_reward(risk_pct: float, expected_move_pct: float, strength: MoveStrength) -> float | None:
    valid: list[float] = []
    for rr in sorted(float(item) for item in ALLOWED_RISK_REWARD):
        if rr in (1.2, 1.5, 2.0) and risk_pct * rr <= expected_move_pct:
            valid.append(rr)

    # 15m/30m lock:
    # - weak moves should not wait for a far TP; prefer 1.2, allow 1.5 only when room exists.
    # - normal moves target 1.5.
    # - strong moves target 2.0 when the expected move can cover it.
    preference: dict[MoveStrength, tuple[float, ...]] = {
        "weak": (1.2, 1.5),
        "normal": (1.5, 1.2),
        "strong": (2.0, 1.5, 1.2),
    }
    for rr in preference[strength]:
        if rr in valid:
            return rr
    return None


def _expected_move_pct(symbol: str, strength: MoveStrength) -> float:
    profile = COIN_MOVE_PROFILE[symbol]
    if strength == "strong":
        return (profile.strong_min + profile.strong_max) / 2.0
    if strength == "normal":
        return (profile.normal_min + profile.normal_max) / 2.0
    return (profile.weak_min + profile.weak_max) / 2.0


def _tp_from_rr(entry: float, sl: float, direction: Direction, rr: float) -> float:
    risk = abs(entry - sl)
    if direction == "LONG":
        return entry + risk * rr
    return entry - risk * rr


def _estimated_gross_profit(notional: float, tp_move_pct: float) -> float:
    return notional * (tp_move_pct / 100.0)


def _estimated_fee(notional: float, open_fee_rate: float, close_fee_rate: float) -> float:
    return notional * (open_fee_rate + close_fee_rate)


def _signal_only_plan(
    symbol: str,
    direction: Direction,
    entry: float,
    sl: float,
    risk_pct: float,
    expected_move_pct: float,
    reason: str,
    rr: float = 1.5,
    gross_profit: float = 0.0,
    fee: float = 0.0,
    net: float = 0.0,
) -> TPSLPlan:
    tp = _tp_from_rr(entry, sl, direction, rr)
    tp_move_pct = abs(tp - entry) / entry * 100.0
    return TPSLPlan(
        symbol=symbol,
        direction=direction,
        entry=round(entry, 8),
        tp=round(tp, 8),
        sl=round(sl, 8),
        risk_reward=rr,
        estimated_move_pct=round(tp_move_pct, 4),
        gross_profit_usdt=round(gross_profit, 4),
        estimated_fee_usdt=round(fee, 4),
        net_profit_usdt=round(net, 4),
        execution_mode="SIGNAL_ONLY",
        reason=f"{reason} | Risk={risk_pct:.4f}% | Expected={expected_move_pct:.4f}%",
    )


__all__ = ["TPSLPlan", "build_tp_sl_plan", "build_fast_tp_sl_plan", "suggest_fast_structural_sl"]

# =========================
# Level 4 structural TP/SL helper
# =========================
# This helper keeps SL construction inside the TP/SL/risk layer instead of
# strategy_manager.py.  strategy_manager only coordinates decision flow.


def build_fast_tp_sl_plan(
    symbol: str,
    direction: Direction,
    entry: float,
    candles_30m: object,
    candles_15m: object,
    move_strength: MoveStrength,
    trade_margin_usdt: float = DEFAULT_TRADE_DOLLAR,
    leverage: int = DEFAULT_LEVERAGE,
    min_net_profit_usdt: float = DEFAULT_MIN_NET_PROFIT_USDT,
    open_fee_rate: float = DEFAULT_OPEN_FEE_RATE,
    close_fee_rate: float = DEFAULT_CLOSE_FEE_RATE,
) -> TPSLPlan:
    """Build the locked 15m/30m TP/SL plan.

    Rules:
    - one TP and one SL only;
    - 30m candles define the main structural SL;
    - 15m candles may refine the SL closer to entry when the swing is valid;
    - weak/normal/strong moves map to dynamic RR through build_tp_sl_plan();
    - no order/exchange side effects.
    """
    suggested_sl = suggest_fast_structural_sl(
        direction=direction,
        entry=entry,
        candles_30m=candles_30m,
        candles_15m=candles_15m,
        move_strength=move_strength,
    )
    if suggested_sl <= 0:
        raise ValueError("SL منطقی برای مدل 15m/30m از ساختار کندل‌ها ساخته نشد.")
    return build_tp_sl_plan(
        symbol=symbol,
        direction=direction,
        entry=entry,
        suggested_sl=suggested_sl,
        move_strength=move_strength,
        trade_margin_usdt=trade_margin_usdt,
        leverage=leverage,
        min_net_profit_usdt=min_net_profit_usdt,
        open_fee_rate=open_fee_rate,
        close_fee_rate=close_fee_rate,
    )


def suggest_fast_structural_sl(
    direction: Direction,
    entry: float,
    candles_30m: object,
    candles_15m: object,
    move_strength: MoveStrength = "normal",
) -> float:
    """Return a 15m/30m structural SL for 30-60 minute trades.

    30m gives the main swing. 15m may tighten the SL only when it stays on the
    correct side of entry and passes simple risk-distance limits.
    """
    recent_30m = _coerce_level4_candles(candles_30m)[-12:]
    recent_15m = _coerce_level4_candles(candles_15m)[-16:]
    if len(recent_30m) < 6 or entry <= 0 or move_strength not in ("weak", "normal", "strong"):
        return 0.0

    buffer_30m = entry * 0.0012
    buffer_15m = entry * 0.0008
    candidates: list[float] = []

    if direction == "LONG":
        swing_30m = min(c[2] for c in recent_30m[-8:])
        sl_30m = swing_30m - buffer_30m
        if 0 < sl_30m < entry:
            candidates.append(sl_30m)

        if len(recent_15m) >= 6:
            swing_15m = min(c[2] for c in recent_15m[-8:])
            sl_15m = swing_15m - buffer_15m
            if 0 < sl_15m < entry:
                candidates.append(sl_15m)

        valid = _valid_sl_candidates(entry, candidates, move_strength)
        return round(max(valid), 8) if valid else 0.0

    if direction == "SHORT":
        swing_30m = max(c[1] for c in recent_30m[-8:])
        sl_30m = swing_30m + buffer_30m
        if sl_30m > entry:
            candidates.append(sl_30m)

        if len(recent_15m) >= 6:
            swing_15m = max(c[1] for c in recent_15m[-8:])
            sl_15m = swing_15m + buffer_15m
            if sl_15m > entry:
                candidates.append(sl_15m)

        valid = _valid_sl_candidates(entry, candidates, move_strength)
        return round(min(valid), 8) if valid else 0.0

    return 0.0


def _valid_sl_candidates(entry: float, candidates: list[float], strength: MoveStrength) -> list[float]:
    min_risk_pct, max_risk_pct = _risk_distance_limits(strength)
    valid: list[float] = []
    for sl in candidates:
        risk_pct = abs(entry - sl) / entry * 100.0
        if min_risk_pct <= risk_pct <= max_risk_pct:
            valid.append(sl)
    return valid


def _risk_distance_limits(strength: MoveStrength) -> tuple[float, float]:
    # Fast 15m/30m trades need tighter SL than the old 1H model.
    if strength == "weak":
        return 0.12, 0.80
    if strength == "strong":
        return 0.18, 1.60
    return 0.15, 1.20


def build_level4_tp_sl_plan(
    symbol: str,
    direction: Direction,
    entry: float,
    candles: object,
    move_strength: MoveStrength,
    trade_margin_usdt: float = DEFAULT_TRADE_DOLLAR,
    leverage: int = DEFAULT_LEVERAGE,
    min_net_profit_usdt: float = DEFAULT_MIN_NET_PROFIT_USDT,
    open_fee_rate: float = DEFAULT_OPEN_FEE_RATE,
    close_fee_rate: float = DEFAULT_CLOSE_FEE_RATE,
) -> TPSLPlan:
    """Build the locked Level 4 plan from candles without exposing SL logic to strategy_manager.

    Rules:
    - one TP and one SL only;
    - SL is structural from recent 30m candles, refined by 15m swing when provided;
    - TP is derived only from locked R:R by build_tp_sl_plan();
    - no order/exchange side effects.
    """
    suggested_sl = suggest_level4_structural_sl(direction=direction, entry=entry, candles=candles)
    if suggested_sl <= 0:
        raise ValueError("SL منطقی برای Level 4 از ساختار کندل‌ها ساخته نشد.")
    return build_tp_sl_plan(
        symbol=symbol,
        direction=direction,
        entry=entry,
        suggested_sl=suggested_sl,
        move_strength=move_strength,
        trade_margin_usdt=trade_margin_usdt,
        leverage=leverage,
        min_net_profit_usdt=min_net_profit_usdt,
        open_fee_rate=open_fee_rate,
        close_fee_rate=close_fee_rate,
    )


def suggest_level4_structural_sl(direction: Direction, entry: float, candles: object) -> float:
    """Return a conservative structural SL from recent candles.

    Kept in tp_sl_engine because it is risk/TP-SL construction, not strategy
    decision logic.  Accepts Candle objects, dicts, or OKX-like arrays.
    """
    recent = _coerce_level4_candles(candles)[-12:]
    if len(recent) < 6 or entry <= 0:
        return 0.0
    buffer = entry * 0.0015
    if direction == "LONG":
        swing = min(c[2] for c in recent[-8:])  # low
        sl = swing - buffer
        return round(sl, 8) if 0 < sl < entry else 0.0
    if direction == "SHORT":
        swing = max(c[1] for c in recent[-8:])  # high
        sl = swing + buffer
        return round(sl, 8) if sl > entry else 0.0
    return 0.0


def _coerce_level4_candles(value: object) -> list[tuple[int, float, float, float, float, float]]:
    """Return candles as (timestamp, high, low, open, close, volume), sorted oldest-first."""
    if value is None or isinstance(value, (str, bytes, bytearray)):
        return []
    try:
        iterator = list(value)  # type: ignore[arg-type]
    except TypeError:
        return []
    out: list[tuple[int, float, float, float, float, float]] = []
    for row in iterator:
        try:
            if hasattr(row, "timestamp") and hasattr(row, "high") and hasattr(row, "low"):
                out.append((int(float(row.timestamp)), float(row.high), float(row.low), float(row.open), float(row.close), float(getattr(row, "volume", 0.0))))
            elif isinstance(row, dict):
                out.append((
                    int(float(row.get("timestamp") or row.get("ts") or row.get("time") or 0)),
                    float(row.get("high")),
                    float(row.get("low")),
                    float(row.get("open")),
                    float(row.get("close")),
                    float(row.get("volume") or row.get("vol") or 0.0),
                ))
            else:
                seq = list(row)  # type: ignore[arg-type]
                if len(seq) >= 6:
                    # OKX/canonical array: ts, open, high, low, close, volume
                    out.append((int(float(seq[0])), float(seq[2]), float(seq[3]), float(seq[1]), float(seq[4]), float(seq[5])))
        except Exception:
            continue
    out.sort(key=lambda item: item[0])
    return out


__all__ = ["TPSLPlan", "build_tp_sl_plan", "build_fast_tp_sl_plan", "suggest_fast_structural_sl", "build_level4_tp_sl_plan", "suggest_level4_structural_sl"]
