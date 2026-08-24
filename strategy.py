"""استراتژی ورود پله‌ای در جهت روند.

منطق در سه لایه:
1. تشخیص جهت روند در تایم‌فریم بالادست (۴ساعته/روزانه) — با EMA و ساختار سقف/کف.
2. تأیید نقطهٔ ورود در تایم‌فریم پایین‌تر (۱۵ دقیقه) — پول‌بک در جهت روند.
3. تحویل به هستهٔ ریاضی (risk_engine) برای ساخت نقشهٔ پله‌ها.

نکتهٔ مهم و صادقانه: هیچ‌کدام از این سیگنال‌ها جهت آینده را «تضمین» نمی‌کنند.
کاری که می‌کنند فقط این است که ورود را به سمتی که ساختار فعلی بازار نشان
می‌دهد سوگیری کنند و از ورود در بازار بی‌جهت (رنج مبهم) جلوگیری کنند.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

import config
import risk_engine
from utils import atr as atr_of, ema, logger, percent_change, rsi, safe_float

Side = Literal["LONG", "SHORT"]


@dataclass
class TrendRead:
    """خواندهٔ جهت بازار در یک تایم‌فریم."""

    timeframe: str
    direction: str          # LONG | SHORT | NEUTRAL
    ema_fast: float = 0.0
    ema_slow: float = 0.0
    structure: str = "NEUTRAL"
    rsi_value: float = 50.0
    notes: list[str] = field(default_factory=list)


@dataclass
class Signal:
    """خروجی نهایی تحلیل — آماده برای تبدیل به چرخه."""

    ok: bool
    side: Side | None = None
    price: float = 0.0
    atr_value: float = 0.0
    spread_rate: float = 0.0
    confirmations: int = 0
    reads: list[TrendRead] = field(default_factory=list)
    reason: str = ""

    def summary(self) -> str:
        parts = [f"{r.timeframe}:{r.direction}" for r in self.reads]
        return " | ".join(parts)


# ----------------------------------------------------------------------
#  لایهٔ ۱ — جهت روند در تایم‌فریم بالادست
# ----------------------------------------------------------------------

def read_trend(candles: list[dict[str, float]], timeframe: str) -> TrendRead:
    """جهت روند یک تایم‌فریم را از روی کندل‌ها می‌خواند."""
    read = TrendRead(timeframe=timeframe, direction="NEUTRAL")
    if len(candles) < config.TREND_EMA_SLOW + 5:
        read.notes.append("کندل ناکافی")
        return read

    closes = [safe_float(c["close"]) for c in candles]
    read.ema_fast = ema(closes[-(config.TREND_EMA_FAST * 3):], config.TREND_EMA_FAST)
    read.ema_slow = ema(closes[-(config.TREND_EMA_SLOW * 3):], config.TREND_EMA_SLOW)
    read.rsi_value = rsi(closes, config.RSI_PERIOD)

    # --- ساختار سقف/کف: ۱۰ کندل اخیر در برابر ۱۰ کندل قبل از آن ---
    highs = [safe_float(c["high"]) for c in candles]
    lows = [safe_float(c["low"]) for c in candles]
    recent_high, prior_high = max(highs[-10:]), max(highs[-20:-10])
    recent_low, prior_low = min(lows[-10:]), min(lows[-20:-10])

    if recent_high > prior_high and recent_low > prior_low:
        read.structure = "LONG"           # سقف بالاتر + کف بالاتر
    elif recent_high < prior_high and recent_low < prior_low:
        read.structure = "SHORT"          # سقف پایین‌تر + کف پایین‌تر
    else:
        read.structure = "NEUTRAL"

    ema_dir = "LONG" if read.ema_fast > read.ema_slow else "SHORT"

    # جهت وقتی معتبر است که هم EMA و هم ساختار هم‌جهت باشند.
    if read.structure == ema_dir:
        read.direction = ema_dir
        read.notes.append(f"EMA و ساختار هر دو {ema_dir}")
    else:
        read.direction = "NEUTRAL"
        read.notes.append(f"EMA={ema_dir} ولی ساختار={read.structure} — بی‌جهت")
    return read


def combine_trends(reads: list[TrendRead]) -> tuple[str, int]:
    """جهت نهایی و تعداد تأییدهای هم‌جهت."""
    longs = sum(1 for r in reads if r.direction == "LONG")
    shorts = sum(1 for r in reads if r.direction == "SHORT")
    if longs >= config.MIN_TREND_CONFIRMATIONS and longs > shorts:
        return "LONG", longs
    if shorts >= config.MIN_TREND_CONFIRMATIONS and shorts > longs:
        return "SHORT", shorts
    return "NEUTRAL", max(longs, shorts)


# ----------------------------------------------------------------------
#  لایهٔ ۲ — تأیید نقطهٔ ورود
# ----------------------------------------------------------------------

def entry_is_favorable(side: Side, entry_candles: list[dict[str, float]]) -> tuple[bool, str]:
    """آیا نقطهٔ فعلی برای ورود در جهت روند مناسب است؟

    اصل ساده: در روند صعودی، ورود بعد از یک پول‌بک بهتر از ورود در اوج است
    (و برعکس). با RSI تایم‌فریم ورود سنجیده می‌شود.
    """
    if len(entry_candles) < config.RSI_PERIOD + 2:
        return False, "کندل ورود ناکافی"

    closes = [safe_float(c["close"]) for c in entry_candles]
    r = rsi(closes, config.RSI_PERIOD)

    if side == "LONG":
        if r > 72:
            return False, f"RSI ورود بیش از حد بالا ({r:.0f}) — ورود در اوج"
        return True, f"RSI ورود مناسب ({r:.0f})"

    if r < 28:
        return False, f"RSI ورود بیش از حد پایین ({r:.0f}) — ورود در کف"
    return True, f"RSI ورود مناسب ({r:.0f})"


# ----------------------------------------------------------------------
#  لایهٔ ۳ — تولید سیگنال کامل
# ----------------------------------------------------------------------

def build_signal(
    *,
    trend_candles: dict[str, list[dict[str, float]]],
    entry_candles: list[dict[str, float]],
    price: float,
    best_bid: float = 0.0,
    best_ask: float = 0.0,
) -> Signal:
    """تحلیل کامل و تصمیم به ورود یا صبر."""
    signal = Signal(ok=False, price=float(price))

    if price <= 0:
        signal.reason = "قیمت نامعتبر"
        return signal

    # --- اسپرد لحظه‌ای ---
    if best_bid > 0 and best_ask > 0:
        spread = (best_ask - best_bid) / ((best_ask + best_bid) / 2.0)
        signal.spread_rate = spread
        if spread > config.MAX_ENTRY_SPREAD_RATE:
            signal.reason = (
                f"اسپرد لحظه‌ای ({spread * 100:.3f}%) بیشتر از سقف مجاز "
                f"({config.MAX_ENTRY_SPREAD_RATE * 100:.3f}%) است"
            )
            return signal

    # --- روند بالادست ---
    reads = [read_trend(candles, tf) for tf, candles in trend_candles.items()]
    signal.reads = reads
    direction, confirmations = combine_trends(reads)
    signal.confirmations = confirmations

    if direction == "NEUTRAL":
        signal.reason = f"جهت بازار مشخص نیست ({signal.summary()}) — ورود انجام نمی‌شود"
        return signal
    if direction == "LONG" and not config.ALLOW_LONG:
        signal.reason = "روند صعودی است ولی لانگ غیرفعال شده"
        return signal
    if direction == "SHORT" and not config.ALLOW_SHORT:
        signal.reason = "روند نزولی است ولی شورت غیرفعال شده"
        return signal

    # --- ATR از تایم‌فریم ورود ---
    atr_value = atr_of(entry_candles, config.ATR_PERIOD)
    if atr_value <= 0:
        signal.reason = "ATR قابل محاسبه نیست"
        return signal
    signal.atr_value = atr_value

    # --- تأیید نقطهٔ ورود ---
    favorable, note = entry_is_favorable(direction, entry_candles)
    if not favorable:
        signal.reason = note
        return signal

    signal.ok = True
    signal.side = direction  # type: ignore[assignment]
    signal.reason = f"{signal.summary()} | {note}"
    return signal


# ----------------------------------------------------------------------
#  تصمیم دربارهٔ چرخهٔ باز
# ----------------------------------------------------------------------

def next_step_due(
    *,
    side: Side,
    steps: list[dict[str, Any]],
    current_price: float,
) -> dict[str, Any] | None:
    """اولین پلهٔ برنامه‌ریزی‌شده‌ای که قیمت به ماشهٔ آن رسیده."""
    for step in steps:
        if str(step.get("status")) != "planned":
            continue
        trigger = safe_float(step.get("trigger_price"))
        if trigger <= 0:
            continue
        if side == "LONG" and current_price <= trigger:
            return step
        if side == "SHORT" and current_price >= trigger:
            return step
        # پله‌ها مرتب‌اند؛ اگر این یکی نرسیده، بعدی‌ها هم نرسیده‌اند.
        break
    return None


def exit_decision(
    *,
    side: Side,
    avg_entry: float,
    quantity: float,
    current_price: float,
    take_profit: float,
    hard_stop: float,
    all_steps_filled: bool,
) -> tuple[str | None, float]:
    """آیا وقت بستن پوزیشن است؟ خروجی: (دلیل خروج یا None، سود/زیان ناخالص)."""
    if quantity <= 0 or avg_entry <= 0:
        return None, 0.0

    gross = risk_engine.unrealized_pnl(
        side=side, avg_entry=avg_entry, quantity=quantity, current_price=current_price
    )
    notional = avg_entry * quantity
    net = risk_engine.net_pnl_after_costs(gross, notional)

    if side == "LONG":
        hit_tp = current_price >= take_profit > 0
        hit_stop = 0 < hard_stop and current_price <= hard_stop
    else:
        hit_tp = 0 < take_profit and current_price <= take_profit
        hit_stop = current_price >= hard_stop > 0

    # حد سود فقط وقتی معتبر است که بعد از کسر هزینه‌ها واقعاً سود بدهد.
    if hit_tp and net >= config.MIN_NET_PROFIT_USDT:
        return "tp", gross
    # حد ضرر سخت فقط پس از مصرف همهٔ پله‌ها معنا دارد؛ قبل از آن، پلهٔ بعدی
    # وظیفهٔ مدیریت را دارد — مگر اینکه قیمت از حد ضرر هم عبور کرده باشد.
    if hit_stop and all_steps_filled:
        return "stop", gross
    if hit_stop and not all_steps_filled:
        return "stop", gross
    return None, gross


def plan_for_signal(
    *,
    signal: Signal,
    capital_usdt: float,
    max_steps: int,
    min_qty: float = 0.0,
    min_notional: float = 0.0,
) -> risk_engine.CyclePlan:
    """سیگنال تأییدشده را به نقشهٔ عددی چرخه تبدیل می‌کند."""
    return risk_engine.best_leverage_for(
        symbol=config.TARGET_SYMBOL,
        side=signal.side or "LONG",
        entry_price=signal.price,
        atr_value=signal.atr_value,
        capital_usdt=capital_usdt,
        max_steps=max_steps,
        min_qty=min_qty,
        min_notional=min_notional,
    )
