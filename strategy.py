"""موتور امتیازدهی سه‌بخشی برای اسکن چندارزی.

هر ارز در هر چرخهٔ اسکن دو امتیاز می‌گیرد: یکی برای لانگ، یکی برای شورت.
هر امتیاز میانگین وزن‌دار سه بخش مستقل است:

    ۱. روند    (وزن ۳۵٪) — EMA سریع/کند، شیب، جایگاه قیمت، تأیید تایم‌فریم بالاتر
    ۲. مومنتوم (وزن ۴۰٪) — RSI و کراس آن، MACD و کراس هیستوگرام
    ۳. حجم     (وزن ۲۵٪) — حجم نسبت به میانگین، جهت بدنهٔ کندل

چرا امتیازی و نه «همهٔ شرط‌ها باید درست باشند»؟ چون اندیکاتورها به‌ندرت کاملاً
هم‌جهت می‌شوند؛ شرط AND سیگنال را تقریباً صفر می‌کند و شرط OR سیگنال بی‌کیفیت
می‌دهد. امتیاز پیوسته اجازه می‌دهد یک بخش خیلی قوی، ضعف نسبی بخش دیگر را جبران
کند، و در عین حال آستانه جلوی ورودهای ضعیف را بگیرد.

هیچ‌کدام از این‌ها آینده را پیش‌بینی نمی‌کند. کاری که می‌کنند فقط این است که
ورود را به سمتی سوگیری کنند که ساختار فعلی بازار نشان می‌دهد.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

import config
from utils import (
    atr as atr_of,
    clamp,
    ema_series,
    logger,
    macd_series,
    rsi_series,
    safe_float,
    sma_series,
)

Side = Literal["LONG", "SHORT"]


# ----------------------------------------------------------------------
#  ساختار خروجی
# ----------------------------------------------------------------------

@dataclass
class PartScore:
    """امتیاز یک بخش (روند/مومنتوم/حجم) در هر دو جهت."""

    name: str
    long: float = 50.0
    short: float = 50.0
    notes: list[str] = field(default_factory=list)


@dataclass
class SymbolScore:
    """نتیجهٔ کامل تحلیل یک ارز."""

    symbol: str
    ok: bool = False
    side: Side | None = None
    score: float = 0.0            # امتیاز جهت انتخاب‌شده (۰..۱۰۰)
    opposite: float = 0.0         # امتیاز جهت مخالف
    price: float = 0.0
    atr_value: float = 0.0
    efficiency: float = 0.0
    parts: list[PartScore] = field(default_factory=list)
    reason: str = ""

    def breakdown(self) -> str:
        if not self.side:
            return "—"
        key = "long" if self.side == "LONG" else "short"
        return " | ".join(f"{p.name} {getattr(p, key):.0f}" for p in self.parts)


# ----------------------------------------------------------------------
#  کمکی‌ها
# ----------------------------------------------------------------------

def _closes(candles: list[dict[str, float]]) -> list[float]:
    return [safe_float(c["close"]) for c in candles]


def efficiency_ratio(closes: list[float], period: int) -> float:
    """نسبت کارایی کافمن: حرکت خالص تقسیم بر مجموع حرکت‌ها.

    نزدیک ۱ = روند تمیز. نزدیک ۰ = رنج اره‌ای؛ قیمت تکان می‌خورد ولی جایی نمی‌رود.
    """
    if len(closes) < period + 1:
        return 0.0
    window = closes[-(period + 1):]
    net = abs(window[-1] - window[0])
    total = sum(abs(window[i] - window[i - 1]) for i in range(1, len(window)))
    return net / total if total > 0 else 0.0


# ----------------------------------------------------------------------
#  بخش ۱ — روند
# ----------------------------------------------------------------------

def score_trend(
    entry_candles: list[dict[str, float]],
    trend_candles: list[dict[str, float]] | None,
) -> PartScore:
    """جایگاه قیمت نسبت به EMAها، شیب EMA و تأیید تایم‌فریم بالاتر."""
    part = PartScore("روند")
    closes = _closes(entry_candles)
    if len(closes) < config.EMA_SLOW + config.EMA_SLOPE_LOOKBACK:
        part.notes.append("کندل ناکافی")
        return part

    fast = ema_series(closes, config.EMA_FAST)
    slow = ema_series(closes, config.EMA_SLOW)
    price = closes[-1]

    long_score = 50.0
    long_score += 14.0 if price > fast[-1] else -14.0
    long_score += 14.0 if price > slow[-1] else -14.0
    long_score += 10.0 if fast[-1] > slow[-1] else -10.0

    # شیب EMA سریع: روند قوی‌تر امتیاز بیشتر می‌گیرد.
    lookback = config.EMA_SLOPE_LOOKBACK
    if fast[-1 - lookback] > 0:
        slope = (fast[-1] - fast[-1 - lookback]) / fast[-1 - lookback]
        long_score += clamp(slope * 2000.0, -12.0, 12.0)

    # تأیید تایم‌فریم بالاتر: روند ۱ساعته از ۱۵دقیقه معتبرتر است.
    if trend_candles:
        hcloses = _closes(trend_candles)
        if len(hcloses) >= config.EMA_FAST + 2:
            hfast = ema_series(hcloses, config.EMA_FAST)
            hprice = hcloses[-1]
            long_score += 10.0 if hprice > hfast[-1] else -10.0
            part.notes.append(f"تایم بالاتر {'صعودی' if hprice > hfast[-1] else 'نزولی'}")

    part.long = clamp(long_score, 0.0, 100.0)
    part.short = 100.0 - part.long
    part.notes.append(f"EMA{config.EMA_FAST}/{config.EMA_SLOW}")
    return part


# ----------------------------------------------------------------------
#  بخش ۲ — مومنتوم
# ----------------------------------------------------------------------

def score_momentum(entry_candles: list[dict[str, float]]) -> PartScore:
    """RSI و MACD — با تأکید روی «کراس تازه»، نه فقط مقدار مطلق."""
    part = PartScore("مومنتوم")
    closes = _closes(entry_candles)
    if len(closes) < config.MACD_SLOW + config.MACD_SIGNAL + 2:
        part.notes.append("کندل ناکافی")
        return part

    rsi_vals = rsi_series(closes, config.RSI_PERIOD)
    _, _, hist = macd_series(closes, config.MACD_FAST, config.MACD_SLOW, config.MACD_SIGNAL)

    r_now, r_prev = rsi_vals[-1], rsi_vals[-2]
    h_now, h_prev = hist[-1], hist[-2]

    long_score = short_score = 50.0

    # کراس خروج از اشباع: قوی‌ترین سیگنال این بخش.
    if r_prev < config.RSI_OVERSOLD <= r_now:
        long_score += 26.0
        part.notes.append(f"خروج از اشباع فروش (RSI {r_now:.0f})")
    if r_prev > config.RSI_OVERBOUGHT >= r_now:
        short_score += 26.0
        part.notes.append(f"خروج از اشباع خرید (RSI {r_now:.0f})")

    # موقعیت RSI: هرچه پایین‌تر، فضای رشد بیشتر (و برعکس).
    long_score += clamp((55.0 - r_now) * 0.45, -13.0, 13.0)
    short_score += clamp((r_now - 45.0) * 0.45, -13.0, 13.0)

    # کراس هیستوگرام MACD.
    if h_prev < 0 <= h_now:
        long_score += 17.0
        part.notes.append("کراس صعودی MACD")
    if h_prev > 0 >= h_now:
        short_score += 17.0
        part.notes.append("کراس نزولی MACD")

    # جهت فعلی هیستوگرام و اینکه در حال قوی‌تر شدن است یا نه.
    if h_now > 0:
        long_score += 8.0 + (4.0 if h_now > h_prev else 0.0)
    else:
        short_score += 8.0 + (4.0 if h_now < h_prev else 0.0)

    part.long = clamp(long_score, 0.0, 100.0)
    part.short = clamp(short_score, 0.0, 100.0)
    return part


# ----------------------------------------------------------------------
#  بخش ۳ — حجم
# ----------------------------------------------------------------------

def score_volume(entry_candles: list[dict[str, float]]) -> PartScore:
    """حجم نسبت به میانگین، همراه با جهت بدنهٔ کندل.

    حرکت با حجم بالا معتبرتر از حرکت با حجم پایین است؛ حجم کم‌تر از میانگین
    یعنی حرکت پشتوانه ندارد و احتمال فیک‌اوت بیشتر است.
    """
    part = PartScore("حجم")
    if len(entry_candles) < config.VOLUME_SMA_PERIOD + 2:
        part.notes.append("کندل ناکافی")
        return part

    volumes = [safe_float(c.get("volume")) for c in entry_candles]
    if max(volumes) <= 0:
        part.notes.append("دادهٔ حجم موجود نیست")
        return part

    avg = sma_series(volumes, config.VOLUME_SMA_PERIOD)
    ratio = volumes[-1] / avg[-1] if avg[-1] > 0 else 1.0
    last = entry_candles[-1]
    body = safe_float(last["close"]) - safe_float(last["open"])

    long_score = short_score = 50.0
    push = clamp((ratio - 1.0) * 40.0, 0.0, 34.0)
    if body > 0:
        long_score += push
        short_score -= push * 0.4
    elif body < 0:
        short_score += push
        long_score -= push * 0.4

    if ratio < 0.8:
        long_score -= 12.0
        short_score -= 12.0
        part.notes.append(f"حجم کم‌رمق ({ratio:.2f}× میانگین)")
    else:
        part.notes.append(f"حجم {ratio:.2f}× میانگین")

    part.long = clamp(long_score, 0.0, 100.0)
    part.short = clamp(short_score, 0.0, 100.0)
    return part


# ----------------------------------------------------------------------
#  ترکیب و تصمیم
# ----------------------------------------------------------------------

def score_symbol(
    *,
    symbol: str,
    entry_candles: list[dict[str, float]],
    trend_candles: list[dict[str, float]] | None = None,
    price: float = 0.0,
    best_bid: float = 0.0,
    best_ask: float = 0.0,
    threshold: float | None = None,
) -> SymbolScore:
    """تحلیل کامل یک ارز و تصمیم به ورود یا رد شدن."""
    out = SymbolScore(symbol=symbol)
    limit = config.SCORE_THRESHOLD if threshold is None else float(threshold)

    if len(entry_candles) < config.EMA_SLOW + 10:
        out.reason = "کندل کافی برای تحلیل موجود نیست"
        return out

    closes = _closes(entry_candles)
    out.price = float(price) if price > 0 else closes[-1]
    if out.price <= 0:
        out.reason = "قیمت نامعتبر"
        return out

    # --- اسپرد: ورود در بازار کم‌عمق یعنی هزینهٔ پنهان ---
    if best_bid > 0 and best_ask > 0:
        spread = (best_ask - best_bid) / ((best_ask + best_bid) / 2.0)
        if spread > config.MAX_ENTRY_SPREAD_RATE:
            out.reason = f"اسپرد بالا ({spread * 100:.3f}%)"
            return out

    parts = [
        score_trend(entry_candles, trend_candles),
        score_momentum(entry_candles),
        score_volume(entry_candles),
    ]
    out.parts = parts
    weights = (config.WEIGHT_TREND, config.WEIGHT_MOMENTUM, config.WEIGHT_VOLUME)
    total_weight = sum(weights) or 1.0

    long_total = sum(p.long * w for p, w in zip(parts, weights)) / total_weight
    short_total = sum(p.short * w for p, w in zip(parts, weights)) / total_weight

    # --- فیلتر رنج: در بازار بی‌جهت، امتیاز به سمت خنثی میرا می‌شود ---
    out.efficiency = efficiency_ratio(closes, config.EFFICIENCY_PERIOD)
    if out.efficiency < config.MIN_EFFICIENCY_RATIO:
        damp = out.efficiency / config.MIN_EFFICIENCY_RATIO if config.MIN_EFFICIENCY_RATIO > 0 else 0.0
        long_total = 50.0 + (long_total - 50.0) * damp
        short_total = 50.0 + (short_total - 50.0) * damp

    if long_total >= short_total:
        out.side, out.score, out.opposite = "LONG", long_total, short_total
    else:
        out.side, out.score, out.opposite = "SHORT", short_total, long_total

    # --- ATR: مبنای حد ضرر و حد سود ---
    out.atr_value = atr_of(entry_candles, config.ATR_PERIOD)
    if out.atr_value <= 0:
        out.reason = "ATR قابل محاسبه نیست"
        out.side = None
        return out

    # --- شرط‌های رد ---
    if out.score < limit:
        out.reason = f"امتیاز {out.score:.0f} زیر آستانهٔ {limit:.0f}"
        return out
    if out.opposite > config.MAX_OPPOSITE_SCORE:
        out.reason = f"بازار مبهم — امتیاز جهت مخالف هم بالاست ({out.opposite:.0f})"
        return out
    if out.efficiency < config.MIN_EFFICIENCY_RATIO:
        out.reason = f"بازار رنج (کارایی {out.efficiency:.2f})"
        return out
    if out.side == "LONG" and not config.ALLOW_LONG:
        out.reason = "لانگ غیرفعال است"
        return out
    if out.side == "SHORT" and not config.ALLOW_SHORT:
        out.reason = "شورت غیرفعال است"
        return out

    out.ok = True
    out.reason = f"امتیاز {out.score:.0f} | {out.breakdown()} | کارایی {out.efficiency:.2f}"
    return out


# ----------------------------------------------------------------------
#  تصمیم خروج
# ----------------------------------------------------------------------

def exit_decision(
    *,
    side: Side,
    entry_price: float,
    quantity: float,
    current_price: float,
    take_profit: float,
    hard_stop: float,
    reversal_score: float = 0.0,
) -> tuple[str | None, float]:
    """آیا وقت بستن پوزیشن است؟ خروجی: (دلیل خروج یا None، سود/زیان ناخالص).

    اولویت با حد ضرر است: اگر هر دو سطح در یک کندل لمس شده باشند، محافظه‌کارانه
    فرض می‌شود اول حد ضرر خورده — چون خلافش قابل اثبات نیست.
    """
    import risk_engine

    if quantity <= 0 or entry_price <= 0 or current_price <= 0:
        return None, 0.0

    gross = risk_engine.unrealized_pnl(
        side=side, avg_entry=entry_price, quantity=quantity, current_price=current_price
    )

    if side == "LONG":
        hit_stop = 0 < hard_stop and current_price <= hard_stop
        hit_tp = current_price >= take_profit > 0
    else:
        hit_stop = current_price >= hard_stop > 0
        hit_tp = 0 < take_profit and current_price <= take_profit

    if hit_stop:
        return "stop", gross
    if hit_tp:
        return "tp", gross

    # خروج زودهنگام وقتی مومنتوم قاطعانه برگشته و پوزیشن در سود است.
    if (
        config.EARLY_EXIT_ON_REVERSAL
        and reversal_score >= config.REVERSAL_EXIT_SCORE
        and gross > 0
    ):
        notional = entry_price * quantity
        if risk_engine.net_pnl_after_costs(gross, notional) >= config.MIN_NET_PROFIT_USDT:
            return "reversal", gross

    # پوزیشن تا رسیدن به حد سود یا حد ضرر باز می‌ماند — بستن به‌خاطر گذشت زمان حذف شده است.
    return None, gross
