"""موتور سیگنال V3 — Extreme Pump Fade (Short-Only).

قوانین این فایل FROZEN هستند. هیچ پارامتری نباید بعد از دیدن نتایج
Paper Trading تغییر کند (نه 0.5 به 0.4، نه 2× به 1.8، نه ATR 14 به 20).

معماری:

    24h Gainer >= +15%  ->  WATCHLIST
                                |
                          5m MONITORING
                                |
                 +--------------+--------------+
                 |                             |
          NORMAL_REVERSAL                FAST_CASCADE
                 |                             |
          Exhaustion                    Downside Acceleration
          (wick >= 0.5)                 (نرخ افت در حال افزایش)
                 |                             +
          Deceleration                  Range >= 2x baseline
          (ret_recent < ret_prior)             +
                 |                      Volume >= 2x baseline
          Confirmed Swing-Low Break            |
                 |                      ورود فوری
                 +--------------+--------------+
                                |
                             SHORT
                                |
              Hard Stop = SwingHigh + 1.5*ATR(14)
              (ATR در لحظهٔ ورود فریز می‌شود)
                                |
              HOLD — نویز باعث خروج نمی‌شود
                                |
        Confirmed Higher High + Confirmed Higher Low
                                |
                              EXIT
                                |
                        COOLDOWN (ساعت)

اصول غیرقابل‌تغییر:
  * فقط SHORT. هیچ لانگی وجود ندارد.
  * TP ثابت وجود ندارد. تریلینگ درصدی وجود ندارد.
  * یک کندل سبز/یک Wick/یک EMA به‌تنهایی خروج نیست.
  * Stop بعد از ورود هرگز با ATR جدید دورتر نمی‌شود.
  * Pivot فقط با تأیید ۲ کندل از هر طرف معتبر است (تأخیر ساختاری عمدی).
  * اگر داده حجم معتبر نباشد، FAST_CASCADE مجاز نیست (بدون Fallback).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Literal

import config
from utils import median, safe_float

Side = Literal["SHORT"]
EntryType = Literal["NORMAL_REVERSAL", "FAST_CASCADE"]


# ======================================================================
#  ساختارهای خروجی
# ======================================================================

@dataclass
class Pivot:
    """یک نقطهٔ چرخش تأییدشده (Swing High یا Swing Low)."""

    index: int
    price: float
    timestamp: int


@dataclass
class SignalSnapshot:
    """عکس لحظه‌ای کامل شرایط در زمان تریگر — برای Audit بعدی.

    هر عددی که در تصمیم‌گیری دخیل بوده اینجا ثبت می‌شود تا بعداً بتوان
    دقیقاً فهمید چرا ورود انجام شد، بدون حدس و گمان.
    """

    symbol: str = ""
    trigger_time_ms: int = 0
    entry_price: float = 0.0
    entry_type: str | None = None
    change_24h: float = 0.0

    # Exhaustion
    upper_wick_ratio: float = 0.0

    # Deceleration
    ret_recent: float = 0.0
    ret_prior: float = 0.0

    # Cascade
    rate_current: float = 0.0
    rate_previous: float = 0.0
    elapsed_seconds: float = 0.0
    range_partial: float = 0.0
    range_baseline: float = 0.0
    volume_partial: float = 0.0
    volume_baseline: float = 0.0
    volume_data_available: bool = False

    # Structure / risk
    swing_low: float = 0.0
    swing_high: float = 0.0
    atr14: float = 0.0
    initial_stop: float = 0.0
    stop_distance_pct: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "trigger_time_ms": self.trigger_time_ms,
            "entry_price": self.entry_price,
            "entry_type": self.entry_type,
            "change_24h": round(self.change_24h, 4),
            "upper_wick_ratio": round(self.upper_wick_ratio, 4),
            "ret_recent": round(self.ret_recent, 6),
            "ret_prior": round(self.ret_prior, 6),
            "rate_current": round(self.rate_current, 8),
            "rate_previous": round(self.rate_previous, 8),
            "elapsed_seconds": round(self.elapsed_seconds, 1),
            "range_partial": round(self.range_partial, 6),
            "range_baseline": round(self.range_baseline, 6),
            "volume_partial": round(self.volume_partial, 4),
            "volume_baseline": round(self.volume_baseline, 4),
            "volume_data_available": self.volume_data_available,
            "swing_low": self.swing_low,
            "swing_high": self.swing_high,
            "atr14": round(self.atr14, 8),
            "initial_stop": self.initial_stop,
            "stop_distance_pct": round(self.stop_distance_pct, 4),
        }


@dataclass
class EntrySignal:
    """نتیجهٔ ارزیابی یک نماد برای ورود SHORT."""

    symbol: str
    ok: bool = False
    entry_type: str | None = None
    price: float = 0.0
    stop_price: float = 0.0
    stop_distance_pct: float = 0.0
    atr_at_entry: float = 0.0
    reason: str = ""
    reject_code: str = ""
    snapshot: SignalSnapshot = field(default_factory=SignalSnapshot)


@dataclass
class ExitDecision:
    """تصمیم خروج. ``reason=None`` یعنی پوزیشن باید باز بماند."""

    reason: str | None
    gross_pnl: float
    exit_price: float = 0.0
    detail: str = ""


# ======================================================================
#  کمکی‌های داده
# ======================================================================

def _f(candle: dict[str, Any], key: str) -> float:
    return safe_float(candle.get(key))


def _closed_candles(candles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """فقط کندل‌های کاملاً بسته‌شده.

    آخرین کندل دریافتی از API معمولاً در حال شکل‌گیری است؛ برای هر
    محاسبهٔ baseline یا pivot باید حذف شود تا leakage رخ ندهد.
    """
    return candles[:-1] if len(candles) >= 2 else []


# ======================================================================
#  ۱) Exhaustion  —  upper_wick_ratio >= 0.5
# ======================================================================

def upper_wick_ratio(candle: dict[str, Any]) -> float:
    """نسبت سایهٔ بالایی به کل دامنهٔ کندل.

    مقدار نزدیک ۱ یعنی قیمت بالا رفته و تقریباً تمامش پس زده شده:
    خریدارها نتوانستند سطح را نگه دارند.
    """
    high = _f(candle, "high")
    low = _f(candle, "low")
    open_ = _f(candle, "open")
    close = _f(candle, "close")
    total_range = high - low
    if total_range <= 0:
        return 0.0
    body_top = max(open_, close)
    return max(0.0, (high - body_top) / total_range)


def has_exhaustion(candles: list[dict[str, Any]]) -> tuple[bool, float]:
    """آیا آخرین کندل بسته‌شده نشانهٔ Exhaustion دارد؟

    خروجی: (نتیجه، نسبت سایهٔ بالایی).
    توجه: شرط «سقف جدید نساخته باشد» عمداً حذف شده — یک سقف جزئی بالاتر
    که شدیداً پس زده شود هم Exhaustion معتبر است.
    """
    closed = _closed_candles(candles)
    if not closed:
        return False, 0.0
    ratio = upper_wick_ratio(closed[-1])
    return ratio >= config.WICK_RATIO_MIN, ratio


# ======================================================================
#  ۲) Momentum Deceleration
# ======================================================================

def momentum_deceleration(candles: list[dict[str, Any]]) -> tuple[bool, float, float]:
    """آیا سرعت رشد کم شده است؟

    بازدهی ۳ کندل اخیر با ۳ کندل قبل از آن مقایسه می‌شود (روی کندل ۵
    دقیقه‌ای یعنی ۱۵ دقیقه در برابر ۱۵ دقیقهٔ قبل). شرط ``ret_prior > 0``
    تضمین می‌کند که فاز صعودی وجود داشته، وگرنه مقایسه بی‌معنی است.

    خروجی: (نتیجه، ret_recent، ret_prior) — هر دو بازده به درصد.
    """
    closed = _closed_candles(candles)
    window = config.DECEL_WINDOW_BARS
    if len(closed) < window * 2 + 1:
        return False, 0.0, 0.0

    c_now = _f(closed[-1], "close")
    c_mid = _f(closed[-1 - window], "close")
    c_old = _f(closed[-1 - window * 2], "close")
    if c_mid <= 0 or c_old <= 0:
        return False, 0.0, 0.0

    ret_recent = (c_now - c_mid) / c_mid * 100.0
    ret_prior = (c_mid - c_old) / c_old * 100.0
    ok = ret_prior > 0 and ret_recent < ret_prior
    return ok, ret_recent, ret_prior


# ======================================================================
#  ۳) Swing Pivots  —  تأیید ۲ کندل از هر طرف
# ======================================================================

def find_confirmed_pivots(
    candles: list[dict[str, Any]],
    *,
    kind: str,
) -> list[Pivot]:
    """همهٔ Pivotهای تأییدشده را برمی‌گرداند (قدیمی به جدید).

    یک Pivot در ایندکس ``i`` معتبر است اگر از ``n`` کندل چپ و ``n`` کندل
    راست خود بالاتر (برای high) یا پایین‌تر (برای low) باشد.

    نکتهٔ causal بسیار مهم: چون تأیید راست لازم است، یک Pivot فقط ``n``
    کندل بعد از وقوعش شناخته می‌شود. این تأخیر عمدی است و Look-Ahead
    نیست؛ ما هرگز از آینده استفاده نمی‌کنیم، فقط دیرتر می‌فهمیم.
    """
    closed = _closed_candles(candles)
    n = config.PIVOT_CONFIRM_BARS
    if len(closed) < n * 2 + 1:
        return []

    out: list[Pivot] = []
    key = "high" if kind == "high" else "low"
    for i in range(n, len(closed) - n):
        value = _f(closed[i], key)
        if value <= 0:
            continue
        left = [_f(closed[i - k], key) for k in range(1, n + 1)]
        right = [_f(closed[i + k], key) for k in range(1, n + 1)]
        if kind == "high":
            if all(value > x for x in left) and all(value > x for x in right):
                out.append(Pivot(i, value, int(safe_float(closed[i].get("time")))))
        else:
            if all(value < x for x in left) and all(value < x for x in right):
                out.append(Pivot(i, value, int(safe_float(closed[i].get("time")))))
    return out


def last_confirmed_pivot(candles: list[dict[str, Any]], *, kind: str) -> Pivot | None:
    pivots = find_confirmed_pivots(candles, kind=kind)
    return pivots[-1] if pivots else None


def structure_break_down(
    candles: list[dict[str, Any]], current_price: float
) -> tuple[bool, float]:
    """آیا ساختار نزولی شکسته شده؟ (قیمت زیر آخرین Swing Low تأییدشده)

    خروجی: (نتیجه، قیمت Swing Low).
    """
    pivot = last_confirmed_pivot(candles, kind="low")
    if pivot is None or current_price <= 0:
        return False, 0.0
    return current_price < pivot.price, pivot.price


# ======================================================================
#  ۴) Cascade  —  Acceleration + Range + Volume
# ======================================================================

def _log_return(new: float, old: float) -> float:
    """بازدهی لگاریتمی؛ برای مقایسهٔ نرخ حرکت مناسب‌تر است."""
    if new <= 0 or old <= 0:
        return 0.0
    return math.log(new / old)


def detect_cascade(
    candles: list[dict[str, Any]],
    *,
    current_price: float,
    current_high: float,
    current_low: float,
    current_volume: float,
    elapsed_seconds: float,
    volume_data_available: bool,
    bar_seconds: float,
) -> tuple[bool, dict[str, Any]]:
    """تشخیص آبشار فروش در لحظه (intra-candle، بدون انتظار برای بسته‌شدن).

    سه شرط، همه باید هم‌زمان برقرار باشند:

      ۱. Downside Acceleration — نرخ افت در دقیقه در حال افزایش است.
         مقایسهٔ بازهٔ ناقص جاری با بازهٔ کامل قبلی، با نرمال‌سازی زمانی
         (وگرنه مقایسه ناعادلانه است: کندلی که ۱ دقیقه از آن گذشته
         طبیعتاً بازدهی کمتری دارد).

      ۲. Range Expansion — دامنهٔ جزئی کندل جاری بیش از ۲× میانهٔ
         دامنهٔ ۱۲ کندل بسته‌شدهٔ قبلی.

      ۳. Volume Expansion — حجم تجمعی جاری بیش از ۲× میانهٔ حجم همان
         ۱۲ کندل.

    اگر داده حجم معتبر نباشد، Cascade مجاز نیست (بدون Fallback) — چون
    تغییر تعریف Setup در لحظه، دو Setup متفاوت را یکی حساب می‌کند.

    نکته: Range و Volume جزئی عمداً نرمال‌سازی زمانی نمی‌شوند؛ این سمت
    محافظه‌کارانه است و احتمال تریگر کاذب را کم می‌کند.
    """
    info: dict[str, Any] = {
        "rate_current": 0.0,
        "rate_previous": 0.0,
        "elapsed_seconds": elapsed_seconds,
        "range_partial": 0.0,
        "range_baseline": 0.0,
        "volume_partial": current_volume,
        "volume_baseline": 0.0,
        "volume_data_available": volume_data_available,
        "reject": "",
    }

    if not volume_data_available:
        info["reject"] = "cascade_blocked_no_volume"
        return False, info

    if elapsed_seconds < config.CASCADE_MIN_ELAPSED_SECONDS:
        info["reject"] = "cascade_too_early_in_bar"
        return False, info

    closed = _closed_candles(candles)
    window = config.CASCADE_BASELINE_BARS
    if len(closed) < window + 2:
        info["reject"] = "cascade_not_enough_history"
        return False, info

    # --- شرط ۱: شتاب‌گیری نزولی (نرخ در دقیقه) ---
    c_prev = _f(closed[-1], "close")
    c_prev2 = _f(closed[-2], "close")
    if c_prev <= 0 or c_prev2 <= 0 or current_price <= 0:
        info["reject"] = "cascade_invalid_price"
        return False, info

    minutes_elapsed = max(elapsed_seconds / 60.0, 1e-9)
    minutes_full = max(bar_seconds / 60.0, 1e-9)
    rate_current = _log_return(current_price, c_prev) / minutes_elapsed
    rate_previous = _log_return(c_prev, c_prev2) / minutes_full
    info["rate_current"] = rate_current
    info["rate_previous"] = rate_previous

    if not (rate_current < 0 and rate_previous < 0 and rate_current < rate_previous):
        info["reject"] = "cascade_no_acceleration"
        return False, info

    # --- شرط ۲: انبساط دامنه ---
    baseline_rows = closed[-window:]
    range_baseline = median(
        (_f(c, "high") - _f(c, "low")) / _f(c, "close")
        for c in baseline_rows
        if _f(c, "close") > 0
    )
    range_partial = (current_high - current_low) / current_price if current_price > 0 else 0.0
    info["range_baseline"] = range_baseline
    info["range_partial"] = range_partial

    if range_baseline <= 0 or range_partial <= config.CASCADE_EXPANSION_MULT * range_baseline:
        info["reject"] = "cascade_no_range_expansion"
        return False, info

    # --- شرط ۳: انبساط حجم ---
    volume_baseline = median(_f(c, "volume") for c in baseline_rows)
    info["volume_baseline"] = volume_baseline
    if volume_baseline <= 0 or current_volume <= config.CASCADE_EXPANSION_MULT * volume_baseline:
        info["reject"] = "cascade_no_volume_expansion"
        return False, info

    return True, info


# ======================================================================
#  ۵) Adaptive Hard Stop  —  SwingHigh + 1.5*ATR(14)
# ======================================================================

def atr(candles: list[dict[str, Any]], period: int | None = None) -> float:
    """میانگین دامنهٔ واقعی (True Range) روی کندل‌های بسته‌شده."""
    closed = _closed_candles(candles)
    n = int(period or config.ATR_PERIOD)
    if len(closed) < n + 1:
        return 0.0
    trs: list[float] = []
    for i in range(len(closed) - n, len(closed)):
        high = _f(closed[i], "high")
        low = _f(closed[i], "low")
        prev_close = _f(closed[i - 1], "close")
        if high <= 0 or low <= 0 or prev_close <= 0:
            continue
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    return sum(trs) / len(trs) if trs else 0.0


def compute_hard_stop(
    candles: list[dict[str, Any]],
    *,
    entry_price: float,
) -> tuple[float, float, float, float]:
    """حد ضرر سخت برای SHORT.

    ``stop = مرجع ساختاری + k * ATR``

    مرجع ساختاری = آخرین Swing High تأییدشده؛ اگر Pivot تأییدشده‌ای وجود
    نداشت، بالاترین High کندل‌های بستهٔ اخیر به‌عنوان مرجع استفاده می‌شود
    (محافظه‌کارانه‌تر است، چون معمولاً بالاتر می‌افتد).

    ATR در همین لحظه محاسبه و در پوزیشن فریز می‌شود؛ بعد از ورود هرگز
    با ATR جدید بازمحاسبه نمی‌شود تا ریسک پوزیشن تغییر نکند.

    خروجی: (stop_price, stop_distance_pct, atr_value, reference_used)
    """
    atr_value = atr(candles)
    if entry_price <= 0 or atr_value <= 0:
        return 0.0, 0.0, atr_value, 0.0

    pivot = last_confirmed_pivot(candles, kind="high")
    if pivot is not None:
        reference = pivot.price
    else:
        closed = _closed_candles(candles)
        lookback = closed[-config.CASCADE_BASELINE_BARS:] if closed else []
        reference = max((_f(c, "high") for c in lookback), default=entry_price)

    reference = max(reference, entry_price)
    stop_price = reference + config.STOP_ATR_MULT * atr_value
    distance_pct = (stop_price - entry_price) / entry_price * 100.0
    return stop_price, distance_pct, atr_value, reference


# ======================================================================
#  ۶) ارزیابی ورود
# ======================================================================

def evaluate_entry(
    *,
    symbol: str,
    candles: list[dict[str, Any]],
    current_price: float,
    change_24h: float,
    current_high: float = 0.0,
    current_low: float = 0.0,
    current_volume: float = 0.0,
    elapsed_seconds: float = 0.0,
    volume_data_available: bool = False,
    bar_seconds: float = 300.0,
    trigger_time_ms: int = 0,
    best_bid: float = 0.0,
    best_ask: float = 0.0,
) -> EntrySignal:
    """آیا همین الان باید روی این نماد SHORT باز کنیم؟

    ترتیب ارزیابی: ابتدا FAST_CASCADE (چون سریع‌تر است و تأخیر ساختاری
    ندارد)، سپس NORMAL_REVERSAL. اگر هیچ‌کدام تأیید نشد، ورود انجام
    نمی‌شود و دلیل رد در ``reject_code`` ثبت می‌شود.
    """
    out = EntrySignal(symbol=symbol)
    snap = out.snapshot
    snap.symbol = symbol
    snap.trigger_time_ms = trigger_time_ms
    snap.entry_price = current_price
    snap.change_24h = change_24h
    snap.elapsed_seconds = elapsed_seconds
    snap.volume_data_available = volume_data_available
    snap.volume_partial = current_volume

    if current_price <= 0:
        out.reject_code = "invalid_price"
        out.reason = "قیمت نامعتبر"
        return out

    if len(candles) < config.MIN_CANDLES_REQUIRED:
        out.reject_code = "not_enough_candles"
        out.reason = "کندل کافی برای تحلیل موجود نیست"
        return out

    # اسپرد: ورود در بازار کم‌عمق هزینهٔ پنهان دارد.
    if best_bid > 0 and best_ask > 0:
        mid = (best_ask + best_bid) / 2.0
        if mid > 0:
            spread = (best_ask - best_bid) / mid
            if spread > config.MAX_ENTRY_SPREAD_RATE:
                out.reject_code = "spread_too_wide"
                out.reason = f"اسپرد بالا ({spread * 100:.3f}%)"
                return out

    # ---------------- مسیر A: FAST_CASCADE ----------------
    cascade_ok, cinfo = detect_cascade(
        candles,
        current_price=current_price,
        current_high=current_high or current_price,
        current_low=current_low or current_price,
        current_volume=current_volume,
        elapsed_seconds=elapsed_seconds,
        volume_data_available=volume_data_available,
        bar_seconds=bar_seconds,
    )
    snap.rate_current = cinfo["rate_current"]
    snap.rate_previous = cinfo["rate_previous"]
    snap.range_partial = cinfo["range_partial"]
    snap.range_baseline = cinfo["range_baseline"]
    snap.volume_baseline = cinfo["volume_baseline"]

    if cascade_ok:
        stop_price, distance_pct, atr_value, reference = compute_hard_stop(
            candles, entry_price=current_price
        )
        if stop_price <= 0:
            out.reject_code = "stop_unavailable"
            out.reason = "محاسبهٔ حد ضرر ممکن نشد (ATR نامعتبر)"
            return out
        snap.entry_type = "FAST_CASCADE"
        snap.swing_high = reference
        snap.atr14 = atr_value
        snap.initial_stop = stop_price
        snap.stop_distance_pct = distance_pct
        out.ok = True
        out.entry_type = "FAST_CASCADE"
        out.price = current_price
        out.stop_price = stop_price
        out.stop_distance_pct = distance_pct
        out.atr_at_entry = atr_value
        range_mult = (
            cinfo["range_partial"] / cinfo["range_baseline"]
            if cinfo["range_baseline"] > 0
            else 0.0
        )
        vol_mult = (
            cinfo["volume_partial"] / cinfo["volume_baseline"]
            if cinfo["volume_baseline"] > 0
            else 0.0
        )
        out.reason = (
            f"آبشار فروش | نرخ {cinfo['rate_current'] * 100:.3f}%/دقیقه "
            f"(قبلی {cinfo['rate_previous'] * 100:.3f}%) | "
            f"دامنه {range_mult:.1f}× | حجم {vol_mult:.1f}×"
        )
        return out

    # ---------------- مسیر B: NORMAL_REVERSAL ----------------
    exhausted, wick = has_exhaustion(candles)
    snap.upper_wick_ratio = wick
    if not exhausted:
        out.reject_code = "no_exhaustion"
        out.reason = (
            f"نشانهٔ خستگی نیامده (سایهٔ بالا {wick:.2f} < {config.WICK_RATIO_MIN})"
        )
        return out

    decel_ok, ret_recent, ret_prior = momentum_deceleration(candles)
    snap.ret_recent = ret_recent
    snap.ret_prior = ret_prior
    if not decel_ok:
        out.reject_code = "no_deceleration"
        out.reason = (
            f"کاهش شتاب تأیید نشد (اخیر {ret_recent:+.2f}% در برابر قبلی {ret_prior:+.2f}%)"
        )
        return out

    broke, swing_low = structure_break_down(candles, current_price)
    snap.swing_low = swing_low
    if not broke:
        out.reject_code = "no_structure_break"
        out.reason = (
            f"شکست ساختار تأیید نشد (کف تأییدشده {swing_low:.6g})"
            if swing_low > 0
            else "کف تأییدشده‌ای برای سنجش شکست ساختار موجود نیست"
        )
        return out

    stop_price, distance_pct, atr_value, reference = compute_hard_stop(
        candles, entry_price=current_price
    )
    if stop_price <= 0:
        out.reject_code = "stop_unavailable"
        out.reason = "محاسبهٔ حد ضرر ممکن نشد (ATR نامعتبر)"
        return out

    snap.entry_type = "NORMAL_REVERSAL"
    snap.swing_high = reference
    snap.atr14 = atr_value
    snap.initial_stop = stop_price
    snap.stop_distance_pct = distance_pct

    out.ok = True
    out.entry_type = "NORMAL_REVERSAL"
    out.price = current_price
    out.stop_price = stop_price
    out.stop_distance_pct = distance_pct
    out.atr_at_entry = atr_value
    out.reason = (
        f"برگشت معتبر | سایهٔ بالا {wick:.2f} | "
        f"شتاب {ret_recent:+.2f}% < {ret_prior:+.2f}% | "
        f"شکست کف {swing_low:.6g}"
    )
    return out


# ======================================================================
#  ۷) خروج — Hard Stop یا Structural Reversal
# ======================================================================

def structural_reversal(
    candles: list[dict[str, Any]],
    *,
    entry_time_ms: int,
) -> tuple[bool, str]:
    """آیا ساختار واقعاً برگشته؟ (نه نویز)

    هر دو شرط لازم است:
      ۱. Confirmed Higher High — آخرین Swing High تأییدشده بالاتر از
         Swing High تأییدشدهٔ قبلی باشد.
      ۲. Confirmed Higher Low — آخرین Swing Low تأییدشده بالاتر از
         Swing Low تأییدشدهٔ قبلی باشد.

    فقط Pivotهایی شمرده می‌شوند که بعد از ورود شکل گرفته‌اند. چون تأیید
    Pivot دو کندل تأخیر دارد، خروج عمداً چند کندل latency دارد — این در
    رکورد معامله ثبت می‌شود و با hindsight انتخاب نمی‌شود.

    یک کندل سبز، یک Wick یا یک عبور EMA به‌تنهایی هرگز خروج نیست.
    """
    highs = [
        p for p in find_confirmed_pivots(candles, kind="high") if p.timestamp >= entry_time_ms
    ]
    lows = [
        p for p in find_confirmed_pivots(candles, kind="low") if p.timestamp >= entry_time_ms
    ]

    if len(highs) < 2 or len(lows) < 2:
        return False, ""

    higher_high = highs[-1].price > highs[-2].price
    higher_low = lows[-1].price > lows[-2].price
    if higher_high and higher_low:
        return True, (
            f"سقف بالاتر ({highs[-2].price:.6g} → {highs[-1].price:.6g}) و "
            f"کف بالاتر ({lows[-2].price:.6g} → {lows[-1].price:.6g})"
        )
    return False, ""


def exit_decision(
    *,
    entry_price: float,
    quantity: float,
    current_price: float,
    hard_stop: float,
    candles: list[dict[str, Any]] | None = None,
    entry_time_ms: int = 0,
) -> ExitDecision:
    """آیا وقت بستن پوزیشن SHORT است؟

    اولویت: Hard Stop مطلق است. اگر فعال شود، فوراً بسته می‌شود و منتظر
    هیچ تأیید تکنیکالی نمی‌مانیم — این محافظت از حساب است، نه تحلیل.

    در غیر این صورت، تنها راه خروج، برگشت ساختاری تأییدشده است. هیچ TP
    ثابتی وجود ندارد؛ تا زمانی که ساختار نزولی معتبر است، پوزیشن باز
    می‌ماند و نویز باعث خروج نمی‌شود.
    """
    import risk_engine

    if quantity <= 0 or entry_price <= 0 or current_price <= 0:
        return ExitDecision(None, 0.0)

    gross = risk_engine.unrealized_pnl(
        side="SHORT", avg_entry=entry_price, quantity=quantity, current_price=current_price
    )

    # ۱) حد ضرر سخت — بدون استثنا، بدون انتظار
    if hard_stop > 0 and current_price >= hard_stop:
        return ExitDecision("HARD_STOP", gross, current_price, f"حد ضرر {hard_stop:.6g}")

    # ۲) برگشت ساختاری تأییدشده
    if candles:
        reversed_, detail = structural_reversal(candles, entry_time_ms=entry_time_ms)
        if reversed_:
            return ExitDecision("TECHNICAL_REVERSAL", gross, current_price, detail)

    return ExitDecision(None, gross, current_price)
