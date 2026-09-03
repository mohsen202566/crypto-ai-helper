"""هستهٔ ریاضی اندازه‌گیری پوزیشن و ریسک.

این ماژول هیچ وابستگی به صرافی یا تلگرام ندارد و کاملاً قابل تست مستقل است.
همهٔ تصمیم‌های عددی (اندازهٔ هر پله، لوریج، قیمت فعال‌شدن پله‌ها، قیمت لیکوئید،
حد سود و حد ضرر، و اینکه اصلاً ورود امن هست یا نه) اینجا حساب می‌شود.

اصول ثابت:
1. سرمایه هرگز عدد ثابت نیست؛ همیشه از بیرون (موجودی زندهٔ حساب) تزریق می‌شود.
2. مجموع مارجین پوزیشن‌های باز از ``MAX_CAPITAL_ENGAGED_RATE`` × سرمایه بیشتر نمی‌شود.
3. هر پوزیشن از همان لحظهٔ ورود حد ضرر و حد سود دارد؛ خبری از پله و مارتینگل نیست.
4. حد ضرر بر پایهٔ ATR واقعی همان ارز تعیین می‌شود، نه درصد ثابت.
5. ورودی که سودش را کارمزد بخورد اصلاً باز نمی‌شود.
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


def stop_distance(entry_price: float, atr_value: float) -> float:
    """فاصلهٔ حد ضرر بر پایهٔ ATR واقعی، محدودشده به کف و سقف امن.

    ATR یعنی حد ضرر با نوسان همان ارز تنظیم می‌شود: ارز پرنوسان استاپ دورتر
    می‌گیرد و ارز آرام استاپ نزدیک‌تر. درصد ثابت این تفاوت را نادیده می‌گیرد و
    باعث می‌شود روی ارز پرنوسان مدام با نویز عادی بازار استاپ بخوریم.
    """
    if entry_price <= 0:
        return 0.0
    raw = atr_value * config.STOP_ATR_MULTIPLIER
    lo = entry_price * config.MIN_STOP_DISTANCE_RATE
    hi = entry_price * config.MAX_STOP_DISTANCE_RATE
    return clamp(raw, lo, hi)


def plan_entry(
    *,
    symbol: str,
    side: Side,
    entry_price: float,
    atr_value: float,
    slot_margin_usdt: float,
    leverage: int | None = None,
    min_qty: float = 0.0,
    min_notional: float = 0.0,
) -> EntryPlan:
    """یک پوزیشن تکی با حد ضرر و حد سود مشخص می‌سازد.

    شرط کلیدی: سود مورد انتظار باید بعد از کسر کارمزد و اسلیپیج، حداقل
    ``MIN_PROFIT_TO_COST_RATIO`` برابر خودِ هزینه باشد. اگر نباشد، ورود رد
    می‌شود — چون معامله‌ای که سودش را کارمزد می‌خورد، ارزش ریسک ندارد.
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

    distance = stop_distance(entry_price, atr_value)
    if distance <= 0:
        blank.reason = "فاصلهٔ حد ضرر قابل محاسبه نیست"
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

    reward = distance * config.RISK_REWARD_RATIO
    if side == "LONG":
        stop_price = entry_price - distance
        tp_price = entry_price + reward
    else:
        stop_price = entry_price + distance
        tp_price = entry_price - reward
    if stop_price <= 0 or tp_price <= 0:
        blank.reason = "سطوح خروج نامعتبر"
        return blank

    cost = notional * round_trip_cost_rate()
    gross_profit = reward * quantity
    gross_loss = distance * quantity
    net_profit = gross_profit - cost
    net_loss = gross_loss + cost

    # --- شرط «بعد از کارمزد صرف کند» ---
    if net_profit < config.MIN_NET_PROFIT_USDT:
        blank.reason = (
            f"سود خالص مورد انتظار ({net_profit:.3f}$) از حداقل "
            f"({config.MIN_NET_PROFIT_USDT:.2f}$) کمتر است"
        )
        return blank
    if cost > 0 and (gross_profit / cost) < config.MIN_PROFIT_TO_COST_RATIO:
        blank.reason = (
            f"سود ناخالص فقط {gross_profit / cost:.1f} برابر کارمزد است "
            f"(حداقل {config.MIN_PROFIT_TO_COST_RATIO:.1f} لازم است)"
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
        stop_price=stop_price, take_profit_price=tp_price, liquidation_price=liq,
        risk_usdt=net_loss, expected_profit_usdt=net_profit, cost_usdt=cost,
        reason=(
            f"ریسک {net_loss:.2f}$ در برابر سود {net_profit:.2f}$ "
            f"(۱:{config.RISK_REWARD_RATIO:.1f}) با لوریج {lev}x"
        ),
    )


def best_leverage_for_entry(
    *,
    symbol: str,
    side: Side,
    entry_price: float,
    atr_value: float,
    slot_margin_usdt: float,
    min_qty: float = 0.0,
    min_notional: float = 0.0,
) -> EntryPlan:
    """کم‌ریسک‌ترین لوریجی که هنوز شرط سوددهی بعد از کارمزد را برآورده کند.

    از پایین شروع می‌کند: اگر لوریج ۱ کافی بود، همان انتخاب می‌شود. لوریج بالاتر
    فقط وقتی استفاده می‌شود که بدون آن حجم پوزیشن آن‌قدر کوچک شود که کارمزد
    سود را بخورد.
    """
    last: EntryPlan | None = None
    for lev in range(config.LEVERAGE_MIN, config.LEVERAGE_MAX + 1):
        plan = plan_entry(
            symbol=symbol, side=side, entry_price=entry_price, atr_value=atr_value,
            slot_margin_usdt=slot_margin_usdt, leverage=lev,
            min_qty=min_qty, min_notional=min_notional,
        )
        if plan.ok:
            return plan
        last = plan
    return last or plan_entry(
        symbol=symbol, side=side, entry_price=entry_price, atr_value=atr_value,
        slot_margin_usdt=slot_margin_usdt,
    )


def slot_margin(
    *, capital_usdt: float, max_positions: int, open_margin_usdt: float = 0.0
) -> float:
    """مارجین هر اسلات — سرمایه بین تعداد پوزیشن هم‌زمان پخش می‌شود.

    سقف درگیری کل هم رعایت می‌شود: مجموع مارجین پوزیشن‌های باز به‌علاوهٔ این
    اسلات جدید هرگز از ``MAX_CAPITAL_ENGAGED_RATE`` × سرمایه بیشتر نمی‌شود.
    """
    capital = max(0.0, safe_float(capital_usdt))
    slots = max(1, int(max_positions))
    budget = capital * config.MAX_CAPITAL_ENGAGED_RATE
    remaining = max(0.0, budget - max(0.0, safe_float(open_margin_usdt)))
    per_slot = budget / slots
    return min(per_slot, remaining)


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
