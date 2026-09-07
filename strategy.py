"""موتور سیگنال Momentum Ignition برای اسکن چندارزی.

منطق (نتیجهٔ بک‌تست روی دادهٔ واقعی توبیت، تکرارشده در ۴ بازهٔ زمانی):
وقتی یک ارز در یک پنجرهٔ کوتاه حرکت شدید می‌کند (PUMP_THRESHOLD_PCT) و حجم آن
پنجره به‌وضوح بالاتر از میانگین است (VOL_MULT)، یعنی پول واقعی وارد شده،
نه فقط نوسان کم‌عمق. برخلاف استراتژی‌های fade (که امتحان و رد شدند)، این
حرکت را دنبال می‌کنیم، نه اینکه برخلافش وارد شویم.

خروج با استاپ ثابت اولیه (INITIAL_STOP_PCT) + استاپ دنبال‌کننده (TRAIL_PCT)
انجام می‌شود، نه حد سود ثابت — چون در حرکت‌های پارابولیک، هدف ثابت سود را
زودتر از موعد می‌بندد.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import config
from utils import safe_float

Side = Literal["LONG", "SHORT"]


# ----------------------------------------------------------------------
#  ساختار خروجی
# ----------------------------------------------------------------------

@dataclass
class SymbolScore:
    """نتیجهٔ کامل تحلیل یک ارز برای سیگنال ورود."""

    symbol: str
    ok: bool = False
    side: Side | None = None
    price: float = 0.0
    window_return: float = 0.0     # درصد حرکت در پنجرهٔ پامپ (علامت‌دار)
    volume_ratio: float = 0.0      # حجم پنجره ÷ میانگین مورد نیاز
    stop_price: float = 0.0        # حد ضرر اولیهٔ پیشنهادی
    score: float = 0.0             # برای رتبه‌بندی چند نامزد هم‌زمان (= |window_return|)
    reason: str = ""


# ----------------------------------------------------------------------
#  کمکی‌ها
# ----------------------------------------------------------------------

def _closes(candles: list[dict[str, float]]) -> list[float]:
    return [safe_float(c["close"]) for c in candles]


def _volumes(candles: list[dict[str, float]]) -> list[float]:
    return [safe_float(c.get("volume")) for c in candles]


# ----------------------------------------------------------------------
#  تشخیص سیگنال ورود
# ----------------------------------------------------------------------

def score_symbol(
    *,
    symbol: str,
    entry_candles: list[dict[str, float]],
    price: float = 0.0,
    best_bid: float = 0.0,
    best_ask: float = 0.0,
    pump_threshold: float | None = None,
    vol_mult: float | None = None,
) -> SymbolScore:
    """تحلیل یک ارز: آیا همین الان یک پامپ/دامپ تازه با حجم تأییدشده دارد؟

    کندل «سیگنال» آخرین کندل کاملاً بسته‌شده است (index ‎-2‎؛ چون آخرین کندل
    دریافتی ممکن است هنوز در حال شکل‌گیری باشد). تأیید ادامهٔ حرکت با قیمت
    لحظه‌ای انجام می‌شود: اگر قیمت الان فراتر از بستهٔ کندل سیگنال رفته باشد،
    یعنی حرکت هنوز ادامه دارد، نه اینکه برگشته باشد.
    """
    out = SymbolScore(symbol=symbol)
    pump_th = config.PUMP_THRESHOLD_PCT if pump_threshold is None else float(pump_threshold)
    vmult = config.VOL_MULT if vol_mult is None else float(vol_mult)

    needed = config.PUMP_WINDOW_BARS + config.VOLUME_AVG_PERIOD + 3
    if len(entry_candles) < needed:
        out.reason = "کندل کافی برای تحلیل موجود نیست"
        return out

    out.price = float(price) if price > 0 else entry_candles[-1]["close"]
    if out.price <= 0:
        out.reason = "قیمت نامعتبر"
        return out

    # --- اسپرد: ورود در بازار کم‌عمق یعنی هزینهٔ پنهان ---
    if best_bid > 0 and best_ask > 0:
        spread = (best_ask - best_bid) / ((best_ask + best_bid) / 2.0)
        if spread > config.MAX_ENTRY_SPREAD_RATE:
            out.reason = f"اسپرد بالا ({spread * 100:.3f}%)"
            return out

    closes = _closes(entry_candles)
    volumes = _volumes(entry_candles)

    # کندل سیگنال = آخرین کندل کاملاً بسته (index -2؛ -1 ممکن است در حال شکل‌گیری باشد)
    sig_idx = len(entry_candles) - 2
    window_start = sig_idx - config.PUMP_WINDOW_BARS
    avg_start = window_start - config.VOLUME_AVG_PERIOD
    if avg_start < 0:
        out.reason = "کندل کافی برای پنجرهٔ پامپ موجود نیست"
        return out

    base_close = closes[window_start]
    if base_close <= 0:
        out.reason = "قیمت پایهٔ پنجره نامعتبر است"
        return out
    window_return = (closes[sig_idx] - base_close) / base_close * 100.0
    out.window_return = window_return
    out.score = abs(window_return)  # حتی رد شده‌ها هم قابل رتبه‌بندی باشند (نزدیک‌ترین به آستانه)

    avg_window = volumes[avg_start:window_start]
    vol_avg = sum(avg_window) / len(avg_window) if avg_window else 0.0
    window_volume = sum(volumes[window_start:sig_idx + 1])
    window_vol_baseline = vol_avg * (config.PUMP_WINDOW_BARS + 1)
    volume_ratio = (window_volume / window_vol_baseline) if window_vol_baseline > 0 else 0.0

    if window_vol_baseline <= 0 or volume_ratio < vmult:
        out.reason = (
            f"حجم کافی نبود ({volume_ratio:.2f}× میانگین، نیاز {vmult:.1f}×)"
        )
        return out

    if window_return >= pump_th:
        side: Side = "LONG"
    elif window_return <= -pump_th:
        side = "SHORT"
    else:
        out.reason = f"حرکت {window_return:+.2f}% کمتر از آستانهٔ {pump_th:.1f}%"
        return out

    if side == "LONG" and not config.ALLOW_LONG:
        out.reason = "لانگ غیرفعال است"
        return out
    if side == "SHORT" and not config.ALLOW_SHORT:
        out.reason = "شورت غیرفعال است"
        return out

    # --- تأیید ادامهٔ حرکت: قیمت لحظه‌ای باید از کندل سیگنال فراتر رفته باشد ---
    sig_close = closes[sig_idx]
    if side == "LONG" and out.price <= sig_close:
        out.reason = f"هنوز تأیید ادامهٔ صعود نیامده (سیگنال {sig_close:.6g})"
        return out
    if side == "SHORT" and out.price >= sig_close:
        out.reason = f"هنوز تأیید ادامهٔ نزول نیامده (سیگنال {sig_close:.6g})"
        return out

    out.side = side
    out.volume_ratio = volume_ratio
    out.stop_price = (
        out.price * (1 - config.INITIAL_STOP_PCT / 100)
        if side == "LONG"
        else out.price * (1 + config.INITIAL_STOP_PCT / 100)
    )
    out.ok = True
    out.reason = (
        f"{'پامپ' if side == 'LONG' else 'دامپ'} {window_return:+.2f}% روی "
        f"{config.PUMP_WINDOW_BARS} کندل | حجم {volume_ratio:.1f}× میانگین"
    )
    return out


# ----------------------------------------------------------------------
#  تصمیم خروج — استاپ ثابت اولیه + استاپ دنبال‌کننده
# ----------------------------------------------------------------------

@dataclass
class ExitDecision:
    reason: str | None       # None یعنی هنوز وقت خروج نیست
    gross_pnl: float
    best_price: float        # بهترین قیمت طی عمر پوزیشن (برای ذخیره در دیتابیس)
    active_stop: float       # استاپ فعال فعلی (اولیه یا دنبال‌کننده، هرکدام تنگ‌تر)


def update_trailing_stop(
    *,
    side: Side,
    current_price: float,
    best_price: float,
    hard_stop: float,
    trail_pct: float | None = None,
) -> tuple[float, float]:
    """بهترین قیمت و استاپ فعال را به‌روز می‌کند؛ استاپ هرگز عقب نمی‌رود.

    خروجی: (best_price جدید، active_stop جدید).
    """
    trail = config.TRAIL_PCT if trail_pct is None else float(trail_pct)
    if side == "LONG":
        new_best = max(best_price, current_price)
        trailing = new_best * (1 - trail / 100)
        active_stop = max(hard_stop, trailing)
    else:
        new_best = min(best_price, current_price) if best_price > 0 else current_price
        trailing = new_best * (1 + trail / 100)
        active_stop = min(hard_stop, trailing) if hard_stop > 0 else trailing
    return new_best, active_stop


def exit_decision(
    *,
    side: Side,
    entry_price: float,
    quantity: float,
    current_price: float,
    best_price: float,
    hard_stop: float,
    trail_pct: float | None = None,
    opened_at_ms: int = 0,
    max_hold_seconds: float = 0.0,
) -> ExitDecision:
    """آیا وقت بستن پوزیشن است؟ استاپ ثابت اولیه، استاپ دنبال‌کننده، یا پایان مهلت.

    اولویت با «هرکدام زودتر رخ داد» است: چون استاپ فعال همیشه تنگ‌تر از دو
    حالت است (max برای لانگ، min برای شورت)، همین یک شرط هر دو را پوشش می‌دهد.
    """
    import risk_engine
    import utils

    if quantity <= 0 or entry_price <= 0 or current_price <= 0:
        return ExitDecision(None, 0.0, best_price, hard_stop)

    ref_best = best_price if best_price > 0 else entry_price
    new_best, active_stop = update_trailing_stop(
        side=side, current_price=current_price, best_price=ref_best,
        hard_stop=hard_stop, trail_pct=trail_pct,
    )

    gross = risk_engine.unrealized_pnl(
        side=side, avg_entry=entry_price, quantity=quantity, current_price=current_price
    )

    if side == "LONG":
        hit = current_price <= active_stop
    else:
        hit = current_price >= active_stop

    if hit:
        # اگر با سود بسته شده، یعنی استاپ دنبال‌کننده کار خودش را کرده
        # (حتی اگر عددش با حد ضرر اولیه یکی شده باشد، چون قبلاً بالا رفته بود).
        # اگر با ضرر یا سربه‌سر بسته شده، یعنی همان حد ضرر اولیه خورده.
        reason = "trail" if gross > 0 else "stop"
        return ExitDecision(reason, gross, new_best, active_stop)

    if max_hold_seconds > 0 and opened_at_ms > 0:
        age_seconds = (utils.now_ms() - opened_at_ms) / 1000.0
        if age_seconds >= max_hold_seconds:
            return ExitDecision("timeout", gross, new_best, active_stop)

    return ExitDecision(None, gross, new_best, active_stop)
