"""هستهٔ ریاضی ورود پله‌ای.

این ماژول هیچ وابستگی به صرافی یا تلگرام ندارد و کاملاً قابل تست مستقل است.
همهٔ تصمیم‌های عددی (اندازهٔ هر پله، لوریج، قیمت فعال‌شدن پله‌ها، قیمت لیکوئید،
حد سود و حد ضرر، و اینکه اصلاً ورود امن هست یا نه) اینجا حساب می‌شود.

اصول ثابت:
1. سرمایه هرگز عدد ثابت نیست؛ همیشه از بیرون (موجودی زندهٔ حساب) تزریق می‌شود.
2. مجموع مارجین همهٔ پله‌ها هرگز از ``MAX_CAPITAL_ENGAGED_RATE`` × سرمایه بیشتر نمی‌شود.
3. پلهٔ اول باید فاصلهٔ لیکوئید عملاً غیرقابل‌دسترس داشته باشد.
4. اگر بعد از آخرین پله هم قیمت برنگردد، حد ضرر سخت اجرا می‌شود — نه پلهٔ جدید.
5. هر پله بر اساس ATR واقعی فاصله می‌گیرد، نه درصد دلخواه ثابت.
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

@dataclass
class StepPlan:
    """نقشهٔ یک پلهٔ ورود (هنوز اجرا نشده)."""

    index: int                 # شمارهٔ پله، از ۱
    trigger_price: float       # قیمتی که این پله در آن فعال می‌شود
    margin_usdt: float         # مارجین اختصاص‌یافته به این پله
    notional_usdt: float       # ارزش پوزیشن این پله (مارجین × لوریج)
    quantity: float            # حجم تقریبی (به واحد ارز)
    cum_margin_usdt: float     # مجموع مارجین تا این پله
    cum_notional_usdt: float   # مجموع ارزش پوزیشن تا این پله
    avg_entry_price: float     # میانگین قیمت ورود پس از این پله
    liquidation_price: float   # قیمت لیکوئید پس از این پله
    liq_distance_rate: float   # فاصلهٔ نسبی لیکوئید تا قیمت این پله

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CyclePlan:
    """نقشهٔ کامل یک چرخهٔ معاملاتی، قبل از باز شدن هر پوزیشنی."""

    symbol: str
    side: Side
    leverage: int
    entry_price: float
    atr: float
    capital_usdt: float             # سرمایهٔ مبنا (موجودی زندهٔ لحظهٔ برنامه‌ریزی)
    max_engaged_usdt: float         # سقف مجاز درگیری سرمایه
    steps: list[StepPlan] = field(default_factory=list)
    take_profit_price: float = 0.0
    hard_stop_price: float = 0.0
    step_gap_rate: float = 0.0      # فاصلهٔ نسبی بین پله‌ها
    max_loss_usdt: float = 0.0      # بدترین ضرر ممکن (اگر حد ضرر سخت بخورد)
    expected_profit_usdt: float = 0.0
    expected_net_profit_usdt: float = 0.0
    total_fee_usdt: float = 0.0
    liq_to_stop_ratio: float = 0.0  # فاصلهٔ لیکوئید تقسیم بر فاصلهٔ حد ضرر
    stop_distance_rate: float = 0.0
    final_liq_distance_rate: float = 0.0
    ok: bool = False
    reason: str = ""

    @property
    def step_count(self) -> int:
        return len(self.steps)

    @property
    def total_margin_usdt(self) -> float:
        return self.steps[-1].cum_margin_usdt if self.steps else 0.0

    @property
    def first_step(self) -> StepPlan | None:
        return self.steps[0] if self.steps else None

    @property
    def final_step(self) -> StepPlan | None:
        return self.steps[-1] if self.steps else None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["step_count"] = self.step_count
        data["total_margin_usdt"] = self.total_margin_usdt
        return data


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


def step_gap_rate_from_atr(atr_value: float, price: float) -> float:
    """فاصلهٔ بین پله‌ها بر پایهٔ ATR واقعی، محدودشده به کف و سقف امن."""
    if price <= 0:
        return config.MIN_STEP_GAP_PERCENT
    raw = (atr_value * config.STEP_TRIGGER_ATR_MULTIPLIER) / price
    return clamp(raw, config.MIN_STEP_GAP_PERCENT, config.MAX_STEP_GAP_PERCENT)


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
#  برنامه‌ریزی چرخه
# ----------------------------------------------------------------------

def _step_margins(total_budget: float, step_count: int, multiplier: float) -> list[float]:
    """تقسیم بودجه بین پله‌ها با ضریب رشد مشخص.

    مجموع سری هندسی ``m × (1 + r + r² + ...)`` برابر بودجه قرار داده می‌شود،
    پس مجموع پله‌ها هرگز از سقف مجاز عبور نمی‌کند.
    """
    step_count = max(1, step_count)
    multiplier = max(1.0, multiplier)
    if abs(multiplier - 1.0) < 1e-9:
        series_sum = float(step_count)
    else:
        series_sum = (multiplier ** step_count - 1.0) / (multiplier - 1.0)
    base = total_budget / series_sum if series_sum > 0 else 0.0
    return [base * (multiplier ** i) for i in range(step_count)]


def plan_cycle(
    *,
    symbol: str,
    side: Side,
    entry_price: float,
    atr_value: float,
    capital_usdt: float,
    leverage: int | None = None,
    max_steps: int | None = None,
    min_qty: float = 0.0,
    min_notional: float = 0.0,
) -> CyclePlan:
    """نقشهٔ کامل یک چرخه را می‌سازد و ایمنی آن را می‌سنجد.

    خروجی همیشه یک ``CyclePlan`` است؛ اگر ``ok`` نادرست باشد، ``reason`` می‌گوید
    چرا ورود مجاز نیست. هیچ سفارشی اینجا ثبت نمی‌شود.
    """
    side = "LONG" if str(side).upper() == "LONG" else "SHORT"
    leverage = int(leverage or config.DEFAULT_STAGED_LEVERAGE)
    leverage = int(clamp(leverage, config.STAGED_LEVERAGE_MIN, config.STAGED_LEVERAGE_MAX))
    max_steps = int(max_steps or config.MAX_ENTRY_STEPS)
    max_steps = int(clamp(max_steps, 1, config.MAX_ENTRY_STEPS_LIMIT))

    plan = CyclePlan(
        symbol=symbol,
        side=side,
        leverage=leverage,
        entry_price=float(entry_price),
        atr=float(atr_value),
        capital_usdt=float(capital_usdt),
        max_engaged_usdt=float(capital_usdt) * config.MAX_CAPITAL_ENGAGED_RATE,
    )

    if entry_price <= 0:
        plan.reason = "قیمت ورود نامعتبر است"
        return plan
    if capital_usdt < config.MIN_CAPITAL_TO_TRADE_USDT:
        plan.reason = (
            f"سرمایه ({capital_usdt:.2f}$) کمتر از حداقل "
            f"({config.MIN_CAPITAL_TO_TRADE_USDT:.2f}$) است"
        )
        return plan
    if atr_value <= 0:
        plan.reason = "ATR نامعتبر است (دادهٔ کندل ناکافی)"
        return plan

    gap = step_gap_rate_from_atr(atr_value, entry_price)
    plan.step_gap_rate = gap

    budget = plan.max_engaged_usdt
    margins = _step_margins(budget, max_steps, config.STEP_SIZE_MULTIPLIER)

    # اگر پلهٔ اول از حداقل سفارش صرافی کوچک‌تر شد، تعداد پله را کم می‌کنیم
    # تا هر پله واقعاً قابل اجرا باشد (به‌جای رد کردن کل چرخه).
    while max_steps > 1:
        first_notional = margins[0] * leverage
        if min_notional <= 0 or first_notional >= min_notional:
            break
        max_steps -= 1
        margins = _step_margins(budget, max_steps, config.STEP_SIZE_MULTIPLIER)

    first_notional = margins[0] * leverage
    if min_notional > 0 and first_notional < min_notional:
        plan.reason = (
            f"حتی با یک پله، ارزش پوزیشن ({first_notional:.2f}$) کمتر از حداقل "
            f"صرافی ({min_notional:.2f}$) است"
        )
        return plan

    cum_margin = 0.0
    cum_notional = 0.0
    cum_qty = 0.0
    steps: list[StepPlan] = []

    for i in range(max_steps):
        if side == "LONG":
            trigger = entry_price * (1.0 - gap * i)
        else:
            trigger = entry_price * (1.0 + gap * i)
        if trigger <= 0:
            break

        margin = margins[i]
        notional = margin * leverage
        qty = notional / trigger

        cum_margin += margin
        cum_notional += notional
        cum_qty += qty
        avg_entry = cum_notional / cum_qty if cum_qty > 0 else trigger

        liq = liquidation_price(
            side=side,
            avg_entry=avg_entry,
            total_margin=cum_margin,
            total_quantity=cum_qty,
        )
        steps.append(
            StepPlan(
                index=i + 1,
                trigger_price=trigger,
                margin_usdt=margin,
                notional_usdt=notional,
                quantity=qty,
                cum_margin_usdt=cum_margin,
                cum_notional_usdt=cum_notional,
                avg_entry_price=avg_entry,
                liquidation_price=liq,
                liq_distance_rate=liq_distance_rate(side, trigger, liq),
            )
        )

    if not steps:
        plan.reason = "ساخت پله‌ها ممکن نشد"
        return plan

    plan.steps = steps
    first = steps[0]
    final = steps[-1]

    final_liq_distance = liq_distance_rate(side, final.avg_entry_price, final.liquidation_price)
    if final_liq_distance < config.MIN_LIQUIDATION_DISTANCE_FINAL_RATE:
        plan.reason = (
            f"فاصلهٔ لیکوئید پس از آخرین پله ({final_liq_distance * 100:.1f}%) کمتر از "
            f"کف مطلق ({config.MIN_LIQUIDATION_DISTANCE_FINAL_RATE * 100:.0f}%) است؛ "
            f"لوریج {leverage}x برای این سرمایه بیش از حد بالاست"
        )
        return plan

    # --- حد سود و حد ضرر ---
    tp_rate = clamp(
        (atr_value * config.TAKE_PROFIT_ATR_MULTIPLIER) / entry_price,
        config.MIN_TAKE_PROFIT_PERCENT,
        config.MAX_TAKE_PROFIT_PERCENT,
    )
    stop_rate = (atr_value * config.HARD_STOP_ATR_MULTIPLIER) / entry_price

    if side == "LONG":
        plan.take_profit_price = entry_price * (1.0 + tp_rate)
        plan.hard_stop_price = final.avg_entry_price * (1.0 - stop_rate)
    else:
        plan.take_profit_price = entry_price * (1.0 - tp_rate)
        plan.hard_stop_price = final.avg_entry_price * (1.0 + stop_rate)

    # --- شرط اصلی ایمنی: حد ضرر باید خیلی زودتر از لیکوئید فعال شود ---
    # اگر استاپ روی ۸٪ و لیکوئید روی ۲۰٪ باشد، عملاً هرگز به لیکوئید نمی‌رسیم
    # چون پوزیشن خیلی قبل‌تر بسته شده. این «لیکوئید غیرقابل‌دسترس» واقعی است.
    stop_distance = abs(final.avg_entry_price - plan.hard_stop_price) / final.avg_entry_price
    if stop_distance <= 0:
        plan.reason = "فاصلهٔ حد ضرر نامعتبر است"
        return plan
    required_liq_distance = stop_distance * config.LIQUIDATION_TO_STOP_BUFFER
    if final_liq_distance < required_liq_distance:
        plan.reason = (
            f"لیکوئید ({final_liq_distance * 100:.1f}%) به حد ضرر "
            f"({stop_distance * 100:.1f}%) خیلی نزدیک است؛ لازم است حداقل "
            f"{config.LIQUIDATION_TO_STOP_BUFFER:.1f} برابر فاصله داشته باشد. "
            f"لوریج {leverage}x امن نیست"
        )
        return plan
    plan.liq_to_stop_ratio = final_liq_distance / stop_distance if stop_distance > 0 else 0.0
    plan.stop_distance_rate = stop_distance
    plan.final_liq_distance_rate = final_liq_distance

    # --- اقتصاد معامله ---
    cost_rate = round_trip_cost_rate()
    plan.total_fee_usdt = final.cum_notional_usdt * cost_rate
    # سود مورد انتظار: بستن کل حجم در حد سود، از میانگین ورود
    if side == "LONG":
        gross = (plan.take_profit_price - first.avg_entry_price) * first.quantity
    else:
        gross = (first.avg_entry_price - plan.take_profit_price) * first.quantity
    plan.expected_profit_usdt = gross
    plan.expected_net_profit_usdt = gross - (first.notional_usdt * cost_rate)

    # بدترین حالت: همهٔ پله‌ها پر شده و حد ضرر سخت خورده
    if side == "LONG":
        worst = (final.avg_entry_price - plan.hard_stop_price) * (final.cum_notional_usdt / final.avg_entry_price)
    else:
        worst = (plan.hard_stop_price - final.avg_entry_price) * (final.cum_notional_usdt / final.avg_entry_price)
    plan.max_loss_usdt = abs(worst) + plan.total_fee_usdt

    max_allowed_loss = plan.max_engaged_usdt * config.MAX_CYCLE_LOSS_RATE
    if plan.max_loss_usdt > max_allowed_loss:
        # حد ضرر را تنگ‌تر می‌کنیم تا در سقف مجاز جا شود، به‌جای رد کردن چرخه.
        qty_total = final.cum_notional_usdt / final.avg_entry_price
        allowed_move = max(0.0, (max_allowed_loss - plan.total_fee_usdt) / qty_total) if qty_total > 0 else 0.0
        if allowed_move <= 0:
            plan.reason = "حد ضرر قابل تنظیم در سقف ریسک مجاز نیست"
            return plan
        if side == "LONG":
            plan.hard_stop_price = final.avg_entry_price - allowed_move
        else:
            plan.hard_stop_price = final.avg_entry_price + allowed_move
        plan.max_loss_usdt = max_allowed_loss

    if plan.expected_net_profit_usdt < config.MIN_NET_PROFIT_USDT:
        plan.reason = (
            f"سود خالص مورد انتظار ({plan.expected_net_profit_usdt:.2f}$) کمتر از "
            f"حداقل ({config.MIN_NET_PROFIT_USDT:.2f}$) است"
        )
        return plan

    plan.ok = True
    plan.reason = "نقشهٔ چرخه معتبر است"
    return plan


def best_leverage_for(
    *,
    symbol: str,
    side: Side,
    entry_price: float,
    atr_value: float,
    capital_usdt: float,
    max_steps: int | None = None,
    min_qty: float = 0.0,
    min_notional: float = 0.0,
) -> CyclePlan:
    """کم‌ریسک‌ترین لوریجی را پیدا می‌کند که هنوز سود معنادار بدهد.

    عمداً *پایین‌ترین* لوریج معتبر برگردانده می‌شود، نه بالاترین: هر لوریج
    اضافه فقط ریسک را زیاد می‌کند، و به محض اینکه شرط حداقل سود خالص برآورده
    شد، بالا رفتن بیشتر توجیهی ندارد.
    """
    last_failed: CyclePlan | None = None
    for lev in range(config.STAGED_LEVERAGE_MIN, config.STAGED_LEVERAGE_MAX + 1):
        candidate = plan_cycle(
            symbol=symbol,
            side=side,
            entry_price=entry_price,
            atr_value=atr_value,
            capital_usdt=capital_usdt,
            leverage=lev,
            max_steps=max_steps,
            min_qty=min_qty,
            min_notional=min_notional,
        )
        if candidate.ok:
            return candidate  # اولین (یعنی کم‌ریسک‌ترین) گزینهٔ معتبر
        last_failed = candidate
    return last_failed or plan_cycle(
        symbol=symbol,
        side=side,
        entry_price=entry_price,
        atr_value=atr_value,
        capital_usdt=capital_usdt,
        max_steps=max_steps,
    )


def recompute_after_fill(
    *,
    side: Side,
    filled_steps: list[dict[str, Any]],
    leverage: int,
) -> dict[str, float]:
    """وضعیت واقعی پوزیشن را بعد از پرشدن پله‌ها دوباره حساب می‌کند.

    ورودی هر پله باید ``price`` و ``quantity`` و ``margin`` داشته باشد.
    """
    total_qty = sum(safe_float(s.get("quantity")) for s in filled_steps)
    total_margin = sum(safe_float(s.get("margin")) for s in filled_steps)
    total_notional = sum(
        safe_float(s.get("quantity")) * safe_float(s.get("price")) for s in filled_steps
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
