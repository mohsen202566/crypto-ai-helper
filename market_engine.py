"""موتور یکپارچه رفتار بازار: کنترل جهت + شروع حرکت + کیفیت ورود."""
from __future__ import annotations
from statistics import median
import time
import config
from models import BehaviorState, MarketSignal, MicroSnapshot
from symbols import SymbolMap


def _median(values: list[float], default: float = 0.0) -> float:
    return median(values) if values else default


def _candle_noise(candles: list[dict[str, float]]) -> float:
    closed = [c for c in candles if int(c.get("confirm", 1)) == 1]
    recent = closed[-20:]
    ranges = [abs(c["high"] - c["low"]) / c["close"] * 100.0 for c in recent if c["close"] > 0]
    return _median(ranges, 0.0)


def _recent_behavior(candles: list[dict[str, float]]) -> dict[str, float]:
    closed = [c for c in candles if int(c.get("confirm", 1)) == 1]
    recent = closed[-8:]
    if len(recent) < 6:
        return {}
    close = float(recent[-1]["close"])
    ranges = [max(float(c["high"]) - float(c["low"]), 1e-12) for c in recent]
    bodies = [abs(float(c["close"]) - float(c["open"])) for c in recent]
    signed = [float(c["close"]) - float(c["open"]) for c in recent]
    net = close - float(recent[0]["open"])
    path = sum(abs(x) for x in signed) or 1e-12
    efficiency = abs(net) / path
    body_quality = _median([b / r for b, r in zip(bodies, ranges)], 0.0)
    high = max(float(c["high"]) for c in recent[:-1])
    low = min(float(c["low"]) for c in recent[:-1])
    return {
        "close": close,
        "net_pct": net / float(recent[0]["open"]) * 100.0 if recent[0]["open"] > 0 else 0.0,
        "efficiency": efficiency,
        "body_quality": body_quality,
        "prior_high": high,
        "prior_low": low,
        "last_high": float(recent[-1]["high"]),
        "last_low": float(recent[-1]["low"]),
    }


def analyze_market_diagnostic(
    sym: SymbolMap,
    candles_5m: list[dict[str, float]],
    snapshot: MicroSnapshot,
    state: BehaviorState,
) -> tuple[MarketSignal | None, str, dict[str, float | int | str | bool]]:
    """بدون امتیازدهی؛ چند مشاهده یک داستان واحدِ کنترل مؤثر را می‌سازند."""
    now = time.time()
    if state.updated_at and now - state.updated_at > config.STATE_STALE_SECONDS:
        state.prices.clear(); state.trade_imbalances.clear(); state.book_imbalances.clear()
        state.micro_biases.clear(); state.spreads.clear(); state.last_control = "RANGE"

    behavior = _recent_behavior(candles_5m)
    noise = _candle_noise(candles_5m)
    metrics: dict[str, float | int | str | bool] = {
        "price": snapshot.last,
        "spread_pct": snapshot.spread_pct,
        "trade_imbalance": snapshot.trade_imbalance,
        "book_imbalance": snapshot.book_imbalance,
        "micro_bias_pct": snapshot.microprice_bias_pct,
        "trade_count": snapshot.trade_count,
        "noise_pct": noise,
    }
    metrics.update(behavior)
    if not behavior or noise <= 0:
        state.append(snapshot, config.STATE_HISTORY_SIZE); state.updated_at = now
        return None, "داده پنج‌دقیقه‌ای کافی یا معتبر نیست", metrics
    if snapshot.spread_pct > config.MAX_SPREAD_PCT:
        state.append(snapshot, config.STATE_HISTORY_SIZE); state.updated_at = now
        return None, "اسپرد برای ورود اقتصادی مناسب نیست", metrics
    if snapshot.trade_count < config.MIN_TRADES_FOR_SNAPSHOT:
        state.append(snapshot, config.STATE_HISTORY_SIZE); state.updated_at = now
        return None, "تعداد معاملات لحظه‌ای برای خواندن رفتار کافی نیست", metrics

    prev_price = state.prices[-1] if state.prices else snapshot.last
    price_change = (snapshot.last - prev_price) / prev_price * 100.0 if prev_price > 0 else 0.0
    base_trade = _median([abs(x) for x in state.trade_imbalances], abs(snapshot.trade_imbalance))
    base_book = _median([abs(x) for x in state.book_imbalances], abs(snapshot.book_imbalance))
    base_micro = _median([abs(x) for x in state.micro_biases], abs(snapshot.microprice_bias_pct))
    metrics.update({"snapshot_price_change_pct": price_change, "base_trade": base_trade, "base_book": base_book, "base_micro": base_micro})

    # حمله، واکنش نقدینگی، اثر قیمت و نگهداری سطح با هم خوانده می‌شوند.
    buy_attack = snapshot.trade_imbalance > 0 and snapshot.trade_imbalance >= max(base_trade * 0.9, 0.02)
    sell_attack = snapshot.trade_imbalance < 0 and abs(snapshot.trade_imbalance) >= max(base_trade * 0.9, 0.02)
    buy_support = snapshot.book_imbalance > -max(base_book * 0.55, 0.03) and snapshot.microprice_bias_pct >= -max(base_micro * 0.45, 0.001)
    sell_support = snapshot.book_imbalance < max(base_book * 0.55, 0.03) and snapshot.microprice_bias_pct <= max(base_micro * 0.45, 0.001)
    price_accepts_up = price_change > 0 or snapshot.last >= behavior["close"]
    price_accepts_down = price_change < 0 or snapshot.last <= behavior["close"]
    long_retained = snapshot.last > behavior["prior_low"] and behavior["net_pct"] >= -noise
    short_retained = snapshot.last < behavior["prior_high"] and behavior["net_pct"] <= noise

    long_control = buy_attack and buy_support and price_accepts_up and long_retained
    short_control = sell_attack and sell_support and price_accepts_down and short_retained

    # جذب معکوس: حمله طرف مقابل هست ولی قیمت بر خلاف آن حفظ می‌شود.
    long_absorption = sell_attack and price_change >= 0 and snapshot.book_imbalance >= 0 and snapshot.microprice_bias_pct >= 0
    short_absorption = buy_attack and price_change <= 0 and snapshot.book_imbalance <= 0 and snapshot.microprice_bias_pct <= 0
    long_control = long_control or long_absorption
    short_control = short_control or short_absorption

    if long_control and short_control:
        control = "RANGE"
    elif long_control:
        control = "LONG"
    elif short_control:
        control = "SHORT"
    else:
        control = "RANGE"

    # شروع حرکت: کنترل تازه یا اولین ادامه معتبر، نه حرکت دیرهنگام.
    fresh_transfer = control in ("LONG", "SHORT") and state.last_control != control
    if control == "LONG":
        extension = max(0.0, (snapshot.last - behavior["prior_high"]) / snapshot.last * 100.0)
        early = fresh_transfer or extension <= max(noise * 0.65, abs(price_change) * 2.0)
        invalidation = min(behavior["prior_low"], behavior["last_low"])
    elif control == "SHORT":
        extension = max(0.0, (behavior["prior_low"] - snapshot.last) / snapshot.last * 100.0)
        early = fresh_transfer or extension <= max(noise * 0.65, abs(price_change) * 2.0)
        invalidation = max(behavior["prior_high"], behavior["last_high"])
    else:
        extension = 0.0; early = False; invalidation = snapshot.last
    metrics.update({"control": control, "fresh_transfer": fresh_transfer, "extension_pct": extension, "early": early})

    state.append(snapshot, config.STATE_HISTORY_SIZE)
    state.updated_at = now
    state.last_control = control

    if control == "RANGE":
        return None, "کنترل مؤثر یک‌طرفه شکل نگرفته یا فشار جذب شده است", metrics
    if not early:
        return None, "جهت معتبر است اما ورود دیر شده و نسبت اقتصادی خراب می‌شود", metrics

    # ظرفیت حرکت بر اساس نویز، کارایی حرکت و اثر فشار؛ نه هدف ثابت.
    behavior_power = max(noise, abs(behavior["net_pct"]) * max(behavior["efficiency"], 0.35))
    pressure_power = abs(snapshot.trade_imbalance) + abs(snapshot.microprice_bias_pct) / max(noise, 1e-9)
    expected = behavior_power * (1.25 if fresh_transfer else 1.05)
    if behavior["efficiency"] > 0.60 and behavior["body_quality"] > 0.55:
        expected *= 1.18
    expected = max(expected, noise * 0.9)

    very_strong = fresh_transfer and behavior["efficiency"] > 0.60 and pressure_power > 0.35
    strength = "بسیار قوی" if very_strong else ("قوی" if fresh_transfer or behavior["efficiency"] > 0.45 else "متوسط")
    side_fa = "خریدار" if control == "LONG" else "فروشنده"
    direction_reason = f"کنترل مؤثر دست {side_fa} است؛ فشار، واکنش نقدینگی و اثر قیمت هم‌جهت‌اند"
    strength_reason = f"انتقال کنترل {'تازه' if fresh_transfer else 'حفظ‌شده'} است و کارایی حرکت {behavior['efficiency']:.2f} است"
    entry_reason = "حرکت هنوز در بخش آغازین است و قیمت از محدوده ورود اقتصادی دور نشده"
    signal = MarketSignal(
        sym.id, sym.okx, sym.toobit, control, snapshot.last, invalidation, noise, expected,
        strength, direction_reason, strength_reason, entry_reason, snapshot.spread_pct,
        snapshot.trade_imbalance, snapshot.book_imbalance, snapshot.microprice_bias_pct,
    )
    return signal, "سیگنال رفتارمحور تأیید شد", metrics
