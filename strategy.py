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
    """خواندهٔ جهت بازار در یک تایم‌فریم.

    به‌جای برچسب دودویی، امتیاز پیوسته در بازهٔ -۱ تا +۱ می‌دهد:
    مثبت = صعودی، منفی = نزولی، نزدیک صفر = بی‌جهت.
    این باعث می‌شود اختلاف جزئی بین EMA و ساختار، کل تایم‌فریم را
    بی‌اعتبار نکند (اشکال نسخهٔ قبلی).
    """

    timeframe: str
    direction: str          # LONG | SHORT | NEUTRAL — فقط برای نمایش
    score: float = 0.0      # -1 .. +1
    ema_fast: float = 0.0
    ema_slow: float = 0.0
    structure: str = "NEUTRAL"
    efficiency: float = 0.0   # نسبت کارایی: ۱ = روند تمیز، ۰ = رنج
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
    score: float = 0.0
    reads: list[TrendRead] = field(default_factory=list)
    reason: str = ""

    def summary(self) -> str:
        parts = [f"{r.timeframe}:{r.direction}({r.score:+.2f})" for r in self.reads]
        return " | ".join(parts)


# ----------------------------------------------------------------------
#  لایهٔ ۱ — جهت روند در تایم‌فریم بالادست
# ----------------------------------------------------------------------

def efficiency_ratio(closes: list[float], period: int) -> float:
    """نسبت کارایی کافمن: حرکت خالص تقسیم بر مجموع حرکت‌ها.

    نزدیک ۱ = روند تمیز و یک‌طرفه.
    نزدیک ۰ = بازار رنج/اره‌ای — قیمت زیاد تکان می‌خورد ولی جایی نمی‌رود.

    این همان چیزی است که «نوسان در رنج» را از «روند واقعی» جدا می‌کند؛
    بدون آن، هر نوسان محلی به‌اشتباه روند خوانده می‌شود.
    """
    if len(closes) < period + 1:
        return 0.0
    window = closes[-(period + 1):]
    net_move = abs(window[-1] - window[0])
    total_move = sum(abs(window[i] - window[i - 1]) for i in range(1, len(window)))
    if total_move <= 0:
        return 0.0
    return net_move / total_move


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

    # --- امتیاز EMA: هم جهت، هم فاصلهٔ نسبی (شیب قوی‌تر = امتیاز بیشتر) ---
    if read.ema_slow > 0:
        ema_gap = (read.ema_fast - read.ema_slow) / read.ema_slow
    else:
        ema_gap = 0.0
    # فاصلهٔ ۱٪ یا بیشتر امتیاز کامل می‌گیرد.
    ema_score = max(-1.0, min(1.0, ema_gap / 0.01))

    structure_score = {"LONG": 1.0, "SHORT": -1.0}.get(read.structure, 0.0)

    # وزن‌دهی: ساختار قیمت کمی مهم‌تر از EMA است چون دیرتر ولی مطمئن‌تر است.
    raw_score = ema_score * config.EMA_WEIGHT + structure_score * config.STRUCTURE_WEIGHT

    # --- فیلتر رنج ---
    # اگر بازار کارایی جهتی پایینی دارد (رنج)، امتیاز به سمت صفر میرا می‌شود.
    # بدون این، نوسان بالا-پایین داخل یک رنج به‌اشتباه «روند» خوانده می‌شود
    # و ربات مدام در سقف و کف رنج پوزیشن باز می‌کند.
    read.efficiency = efficiency_ratio(closes, config.EFFICIENCY_PERIOD)
    if read.efficiency < config.MIN_EFFICIENCY_RATIO:
        # این تایم‌فریم رنج است: هیچ اطلاعات جهتی معتبری ندارد، پس امتیاز صفر
        # می‌گیرد (نه منفی، نه مثبت) و در میانگین نهایی خنثی می‌ماند.
        read.score = 0.0
        read.notes.append(
            f"رنج (کارایی {read.efficiency:.2f} < {config.MIN_EFFICIENCY_RATIO}) — بی‌اطلاع"
        )
    else:
        read.score = raw_score

    if read.score >= config.TREND_SCORE_THRESHOLD:
        read.direction = "LONG"
    elif read.score <= -config.TREND_SCORE_THRESHOLD:
        read.direction = "SHORT"
    else:
        read.direction = "NEUTRAL"

    ema_dir = "LONG" if ema_score > 0 else ("SHORT" if ema_score < 0 else "FLAT")
    read.notes.append(
        f"EMA={ema_dir}({ema_score:+.2f}) ساختار={read.structure} → امتیاز {read.score:+.2f}"
    )
    return read


def combine_trends(reads: list[TrendRead], threshold: float | None = None) -> tuple[str, float]:
    """جهت نهایی از میانگین وزن‌دار امتیاز تایم‌فریم‌ها.

    تایم‌فریم بالاتر وزن بیشتری دارد (روند روزانه از ۴ساعته معتبرتر است).
    برخلاف نسخهٔ قبلی، یک تایم‌فریم مبهم کل سیگنال را باطل نمی‌کند؛
    فقط امتیاز کل را پایین می‌آورد.

    یک شرط سخت باقی می‌ماند: هیچ تایم‌فریمی نباید *مخالف* جهت نهایی باشد.
    """
    if not reads:
        return "NEUTRAL", 0.0

    total_weight = 0.0
    weighted = 0.0
    for r in reads:
        w = config.TIMEFRAME_WEIGHTS.get(r.timeframe, 1.0)
        weighted += r.score * w
        total_weight += w
    combined = weighted / total_weight if total_weight > 0 else 0.0

    # تضاد صریح: اگر تایم‌فریمی قاطعانه در جهت مخالف باشد، ورود ممنوع.
    if combined > 0 and any(r.score <= -config.TREND_CONFLICT_THRESHOLD for r in reads):
        return "NEUTRAL", combined
    if combined < 0 and any(r.score >= config.TREND_CONFLICT_THRESHOLD for r in reads):
        return "NEUTRAL", combined

    limit = config.COMBINED_SCORE_THRESHOLD if threshold is None else float(threshold)
    if combined >= limit:
        return "LONG", combined
    if combined <= -limit:
        return "SHORT", combined
    return "NEUTRAL", combined


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
        if r > config.ENTRY_RSI_MAX:
            return False, f"RSI ورود بیش از حد بالا ({r:.0f}) — ورود در اوج"
        return True, f"RSI ورود مناسب ({r:.0f})"

    if r < config.ENTRY_RSI_MIN:
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
    score_threshold: float | None = None,
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
    direction, combined_score = combine_trends(reads, score_threshold)
    signal.score = combined_score

    if direction == "NEUTRAL":
        signal.reason = (
            f"جهت بازار به اندازهٔ کافی قوی نیست — امتیاز {combined_score:+.2f} "
            f"(آستانه {config.COMBINED_SCORE_THRESHOLD if score_threshold is None else score_threshold:.2f}) "
            f"| {signal.summary()}"
        )
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
    signal.reason = f"امتیاز {combined_score:+.2f} | {signal.summary()} | {note}"
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
