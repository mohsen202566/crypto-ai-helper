"""هستهٔ ریاضی اندازه‌گیری پوزیشن و ریسک.

این ماژول هیچ وابستگی به صرافی یا تلگرام ندارد و کاملاً قابل تست مستقل است.
همهٔ تصمیم‌های عددی (اندازهٔ هر پله، لوریج، قیمت فعال‌شدن پله‌ها، قیمت لیکوئید،
حد سود و حد ضرر، و اینکه اصلاً ورود امن هست یا نه) اینجا حساب می‌شود.

اصول ثابت:
1. سرمایه هرگز عدد ثابت نیست؛ همیشه از بیرون (موجودی زندهٔ حساب) تزریق می‌شود.
2. مجموع مارجین پوزیشن‌های باز از ``MAX_CAPITAL_ENGAGED_RATE`` × سرمایه بیشتر نمی‌شود.
3. هر پوزیشن از همان لحظهٔ ورود حد ضرر اولیه دارد؛ خبری از پله و مارتینگل نیست.
4. حد ضرر اولیه درصد ثابت از قیمت ورود است (نتیجهٔ بک‌تست)؛ خروج نهایی با
   استاپ دنبال‌کننده در ``strategy.py`` انجام می‌شود، نه حد سود ثابت.
5. ورودی که فاصلهٔ حد ضررش را کارمزد ببلعد اصلاً باز نمی‌شود.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Literal

import config
from utils import clamp, safe_float

Side = Literal["LONG", "SHORT"]


# ----------------------------------------------------------------------
#  ساختارهای داده
# ----------------------------------------------------------------------

# ----------------------------------------------------------------------
#  توابع پایه
# ----------------------------------------------------------------------

def liquidation_price(
    *,
    side: Side,
    avg_entry: float,
    total_margin: float,
    total_quantity: float,
    maintenance_rate: float = config.MAINTENANCE_MARGIN_RATE,
) -> float:
    """قیمت لیکوئید یک پوزیشن ترکیبی (ایزوله).

    لیکوئید وقتی رخ می‌دهد که ضرر تحقق‌نیافته، مارجین را منهای حاشیهٔ نگهداری
    مصرف کند:  qty × |avg_entry − P| = margin × (1 − maintenance_rate)
    """
    if total_quantity <= 0 or avg_entry <= 0:
        return 0.0
    usable = max(0.0, total_margin * (1.0 - maintenance_rate))
    move = usable / total_quantity
    if side == "LONG":
        return max(0.0, avg_entry - move)
    return avg_entry + move


def liq_distance_rate(side: Side, price: float, liq_price: float) -> float:
    """فاصلهٔ نسبی لیکوئید تا قیمت فعلی (۰.۹۵ یعنی ۹۵٪ دورتر = عملاً امن)."""
    if price <= 0:
        return 0.0
    if side == "LONG":
        if liq_price <= 0:
            return 1.0
        return max(0.0, (price - liq_price) / price)
    return max(0.0, (liq_price - price) / price)


def round_trip_cost_rate() -> float:
    """کل هزینهٔ رفت‌وبرگشت یک پوزیشن به‌صورت نسبت (کارمزد + اسلیپیج + فاندینگ)."""
    return (
        config.TAKER_FEE_RATE * 2.0
        + config.ROUND_TRIP_SLIPPAGE_RATE
        + config.FUNDING_RESERVE_RATE
    )


def available_capital(
    *,
    live_balance: float,
    virtual: bool = False,
    virtual_balance: float = 0.0,
) -> float:
    """سرمایهٔ مبنا برای محاسبات؛ همیشه از موجودی زنده، نه عدد ثابت.

    اگر ``CAPITAL_CAP_USDT`` تنظیم شده باشد، سقف نرم اعمال می‌شود تا حتی با
    موجودی بالا، ربات بیش از آن وارد نشود.
    """
    base = safe_float(virtual_balance if virtual else live_balance)
    if base <= 0:
        base = safe_float(config.FALLBACK_CAPITAL_USDT)
    cap = safe_float(config.CAPITAL_CAP_USDT)
    if cap > 0:
        base = min(base, cap)
    return max(0.0, base)


# ----------------------------------------------------------------------
#  برنامه‌ریزی پوزیشن تکی (جایگزین ورود پله‌ای)
# ----------------------------------------------------------------------

@dataclass
class EntryPlan:
    """نقشهٔ کامل یک پوزیشن، قبل از ارسال سفارش."""

    ok: bool
    symbol: str
    side: Side
    entry_price: float
    leverage: int
    margin_usdt: float
    notional_usdt: float
    quantity: float
    stop_price: float
    take_profit_price: float
    liquidation_price: float
    risk_usdt: float              # حداکثر ضرر اگر حد ضرر بخورد (با کارمزد)
    expected_profit_usdt: float   # سود خالص اگر حد سود بخورد (بعد از کارمزد)
    cost_usdt: float              # هزینهٔ رفت‌وبرگشت
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def initial_stop_distance(entry_price: float, stop_price: float) -> float:
    """فاصلهٔ حد ضرر — از قیمت استاپِ ساختاری که استراتژی داده است.

    در V3 حد ضرر درصد ثابت نیست: ``strategy.compute_hard_stop`` آن را از
    آخرین سقف تأییدشده به‌علاوهٔ بافر ATR می‌سازد و همان مقدار در لحظهٔ
    ورود فریز می‌شود. اینجا فقط فاصله محاسبه می‌شود، بدون هیچ فرضی.
    """
    if entry_price <= 0 or stop_price <= 0:
        return 0.0
    return abs(stop_price - entry_price)


def plan_entry(
    *,
    symbol: str,
    side: Side,
    entry_price: float,
    stop_price: float,
    slot_margin_usdt: float,
    leverage: int | None = None,
    min_qty: float = 0.0,
    min_notional: float = 0.0,
) -> EntryPlan:
    """یک پوزیشن SHORT با حد ضرر ساختاری می‌سازد.

    ``stop_price`` از ``strategy.compute_hard_stop`` می‌آید (سقف تأییدشده +
    بافر ATR) و اینجا هیچ استاپی ساخته یا حدس زده نمی‌شود.

    چون V3 حد سود ثابت ندارد، شرط «سود مورد انتظار × N برابر کارمزد» بی‌معناست
    — سود از قبل معلوم نیست. تنها شرط این است که فاصلهٔ حد ضرر از هزینهٔ
    رفت‌وبرگشت بزرگ‌تر باشد؛ وگرنه یک نوسان عادی کل ریسک را کارمزد می‌کند.

    اگر فاصلهٔ استاپ آن‌قدر بزرگ باشد که با مارجین موجود لیکوئید نزدیک شود،
    نتیجه ``ok=False`` است — یعنی «معامله نکردن» یک خروجی معتبر است.
    """
    lev = int(leverage or config.DEFAULT_LEVERAGE)
    lev = int(clamp(lev, config.LEVERAGE_MIN, config.LEVERAGE_MAX))
    margin = max(0.0, safe_float(slot_margin_usdt))

    blank = EntryPlan(
        ok=False, symbol=symbol, side=side, entry_price=entry_price, leverage=lev,
        margin_usdt=margin, notional_usdt=0.0, quantity=0.0, stop_price=0.0,
        take_profit_price=0.0, liquidation_price=0.0, risk_usdt=0.0,
        expected_profit_usdt=0.0, cost_usdt=0.0,
    )
    if entry_price <= 0 or margin <= 0:
        blank.reason = "قیمت یا مارجین نامعتبر"
        return blank

    distance = initial_stop_distance(entry_price, stop_price)
    if distance <= 0:
        blank.reason = "حد ضرر ساختاری از استراتژی دریافت نشد"
        return blank

    notional = margin * lev
    quantity = notional / entry_price
    if min_qty > 0 and quantity < min_qty:
        blank.reason = (
            f"حجم {quantity:.6f} کمتر از حداقل صرافی ({min_qty:.6f}) است — "
            "سرمایه یا تعداد اسلات را تنظیم کنید"
        )
        return blank
    if min_notional > 0 and notional < min_notional:
        blank.reason = (
            f"ارزش پوزیشن {notional:.2f}$ کمتر از حداقل صرافی ({min_notional:.2f}$) است"
        )
        return blank

    # V3 فقط شورت است و استاپ همیشه بالای قیمت ورود قرار دارد.
    if stop_price <= entry_price:
        blank.reason = "حد ضرر شورت باید بالاتر از قیمت ورود باشد"
        return blank

    cost = notional * round_trip_cost_rate()
    gross_loss = distance * quantity
    net_loss = gross_loss + cost

    # --- شرط «کارمزد ریسک را نبلعد» ---
    if cost > 0 and (gross_loss / cost) < 1.0:
        blank.reason = (
            f"فاصلهٔ حد ضرر ({gross_loss:.3f}$) کوچک‌تر از هزینهٔ رفت‌وبرگشت "
            f"({cost:.3f}$) است — لوریج را کم کنید یا مارجین را زیاد کنید"
        )
        return blank

    liq = liquidation_price(
        side=side, avg_entry=entry_price, total_margin=margin, total_quantity=quantity
    )
    liq_gap = abs(entry_price - liq)
    if liq_gap > 0 and liq_gap < distance * config.LIQUIDATION_TO_STOP_BUFFER:
        blank.reason = (
            f"لیکوئید ({liq_gap / entry_price * 100:.1f}%) به حد ضرر "
            f"({distance / entry_price * 100:.1f}%) خیلی نزدیک است — لوریج را کم کنید"
        )
        return blank

    return EntryPlan(
        ok=True, symbol=symbol, side=side, entry_price=entry_price, leverage=lev,
        margin_usdt=margin, notional_usdt=notional, quantity=quantity,
        stop_price=stop_price, take_profit_price=0.0, liquidation_price=liq,
        risk_usdt=net_loss, expected_profit_usdt=0.0, cost_usdt=cost,
        reason=(
            f"حد ضرر ساختاری {distance / entry_price * 100:.2f}% "
            f"(ریسک {net_loss:.2f}$) | خروج: برگشت ساختاری | لوریج {lev}x"
        ),
    )


def slot_margin(
    *,
    capital_usdt: float,
    max_positions: int,
    open_margin_usdt: float = 0.0,
    fixed_size_usdt: float = 0.0,
) -> float:
    """مارجین هر پوزیشن.

    دو حالت کاملاً متفاوت:

    • **اندازهٔ ثابت** (``fixed_size_usdt`` > 0): کاربر گفته هر پوزیشن دقیقاً
      چند دلار باشد. این عدد هرگز کوچک نمی‌شود — یا دقیقاً همان مقدار باز
      می‌شود، یا اگر موجودی آزاد کافی نباشد صفر برمی‌گردد و ورود انجام
      نمی‌شود. مبنای موجودی آزاد در این حالت کل سرمایه است، نه درصد آن،
      چون خودِ کاربر با تعیین عدد، ریسکش را انتخاب کرده.

    • **خودکار** (صفر): سرمایهٔ مجاز (``MAX_CAPITAL_ENGAGED_RATE`` × سرمایه)
      بین اسلات‌ها پخش می‌شود و با پر شدن اسلات‌ها کوچک‌تر می‌شود.
    """
    capital = max(0.0, safe_float(capital_usdt))
    slots = max(1, int(max_positions))
    used = max(0.0, safe_float(open_margin_usdt))
    fixed = max(0.0, safe_float(fixed_size_usdt))

    if fixed > 0:
        free = capital - used
        # یا دقیقاً همان عدد، یا هیچ. هرگز نصفه‌نیمه.
        return fixed if free >= fixed else 0.0

    budget = capital * config.MAX_CAPITAL_ENGAGED_RATE
    remaining = max(0.0, budget - used)
    return min(budget / slots, remaining)


def position_snapshot(
    *,
    side: Side,
    fills: list[dict[str, Any]],
    leverage: int,
) -> dict[str, float]:
    """وضعیت واقعی پوزیشن را بعد از پرشدن سفارش حساب می‌کند.

    هر ورودی باید ``price`` و ``quantity`` و ``margin`` داشته باشد.
    """
    total_qty = sum(safe_float(s.get("quantity")) for s in fills)
    total_margin = sum(safe_float(s.get("margin")) for s in fills)
    total_notional = sum(
        safe_float(s.get("quantity")) * safe_float(s.get("price")) for s in fills
    )
    avg_entry = total_notional / total_qty if total_qty > 0 else 0.0
    liq = liquidation_price(
        side=side,
        avg_entry=avg_entry,
        total_margin=total_margin,
        total_quantity=total_qty,
    )
    return {
        "quantity": total_qty,
        "margin": total_margin,
        "notional": total_notional,
        "avg_entry": avg_entry,
        "liquidation_price": liq,
        "liq_distance_rate": liq_distance_rate(side, avg_entry, liq),
        "leverage": int(leverage),
    }


def unrealized_pnl(
    *,
    side: Side,
    avg_entry: float,
    quantity: float,
    current_price: float,
) -> float:
    if avg_entry <= 0 or quantity <= 0 or current_price <= 0:
        return 0.0
    if side == "LONG":
        return (current_price - avg_entry) * quantity
    return (avg_entry - current_price) * quantity


def net_pnl_after_costs(gross_pnl: float, notional: float) -> float:
    return gross_pnl - notional * round_trip_cost_rate()
