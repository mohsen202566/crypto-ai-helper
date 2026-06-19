# -*- coding: utf-8 -*-
"""
toobit_safety.py

Safety guard layer for Toobit real futures trading.

Purpose:
- Keep all pre-order safety checks in one place.
- Block real orders if symbol, direction, leverage, margin/notional, TP, or SL are invalid.
- Never allow Toobit to open a position with arbitrary leverage/size when bot settings specify different values.

This file does not touch AI, scanner, auto-signal, learning, or Telegram output logic.
It is designed to be imported by real_trade_manager.py / tobit_client.py.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Any, Dict, Optional, Tuple


MIN_LEVERAGE = 1.0
MAX_LEVERAGE = 50.0
MIN_MARGIN_USD = 1.0
MAX_MARGIN_USD = 1_000_000.0
TP_SL_MIN_DISTANCE_PCT = 0.00005  # 0.005% safety floor against equal/invalid TP/SL
LEVERAGE_TOLERANCE = 0.01
NOTIONAL_TOLERANCE_PCT = 0.08  # exchange rounding can slightly change qty/notional


@dataclass
class SafetyResult:
    ok: bool
    blocked: bool = False
    error: str = ""
    symbol: str = ""
    direction: str = ""
    margin_usd: float = 0.0
    leverage: float = 0.0
    entry_price: float = 0.0
    notional_usd: float = 0.0
    quantity: float = 0.0
    take_profit: Optional[float] = None
    stop_loss: Optional[float] = None
    details: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        if data.get("details") is None:
            data["details"] = {}
        return data


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        text = str(value).strip()
        if text == "":
            return default
        v = float(text)
        if v != v or v in (float("inf"), float("-inf")):
            return default
        return v
    except Exception:
        return default


def _plain_symbol(symbol: Any) -> str:
    s = str(symbol or "").upper().strip()
    if not s:
        return ""
    s = s.replace("/", "").replace("_", "-")
    if "-SWAP-USDT" in s:
        return s.replace("-SWAP-USDT", "USDT")
    if "-SWAP-USDC" in s:
        return s.replace("-SWAP-USDC", "USDC")
    s = s.replace("-", "")
    return s


def normalize_direction(direction: Any) -> str:
    d = str(direction or "").upper().strip()
    if d in {"LONG", "BUY", "BUY_OPEN"}:
        return "LONG"
    if d in {"SHORT", "SELL", "SELL_OPEN"}:
        return "SHORT"
    return ""




def normalize_margin_mode(value: Any) -> str:
    m = str(value or "").upper().strip().replace("-", "_").replace(" ", "_")
    if m in {"ISOLATED", "ISOLATE", "ISOLATED_MARGIN", "ISOLATEDMARGIN", "FIXED"}:
        return "ISOLATED"
    if m in {"CROSS", "CROSSED", "CROSS_MARGIN", "CROSSMARGIN"}:
        return "CROSS"
    return m


def extract_position_direction(position: Dict[str, Any]) -> str:
    if not isinstance(position, dict):
        return ""
    for key in ("direction", "side", "positionSide", "holdSide", "tradeSide", "posSide"):
        d = normalize_direction(position.get(key))
        if d:
            return d
    qty = _safe_float(position.get("positionAmt") or position.get("size") or position.get("qty"), 0.0)
    if qty > 0:
        return "LONG"
    if qty < 0:
        return "SHORT"
    return ""


def extract_margin_mode(position: Dict[str, Any]) -> str:
    if not isinstance(position, dict):
        return ""
    for key in ("marginMode", "margin_mode", "marginType", "margin_type", "positionMode", "mode"):
        mode = normalize_margin_mode(position.get(key))
        if mode:
            return mode
    return ""


def quantize_down(value: Any, precision: int = 6) -> str:
    try:
        q = Decimal("1." + ("0" * int(precision)))
        return str(Decimal(str(value)).quantize(q, rounding=ROUND_DOWN))
    except (InvalidOperation, ValueError, TypeError):
        return "0"


def calculate_quantity_from_margin(
    margin_usd: Any,
    leverage: Any,
    entry_price: Any,
    precision: int = 6,
) -> Tuple[float, float]:
    """
    Convert bot settings to order quantity.

    Bot rule:
      margin_usd = `ترید دلار`
      leverage   = `ترید لوریج`
      notional   = margin_usd * leverage
      quantity   = notional / entry_price
    """
    margin = _safe_float(margin_usd)
    lev = _safe_float(leverage)
    price = _safe_float(entry_price)
    if margin <= 0 or lev <= 0 or price <= 0:
        return 0.0, 0.0
    notional = margin * lev
    qty = float(quantize_down(notional / price, precision))
    return qty, notional


def validate_basic_order_inputs(
    symbol: Any,
    direction: Any,
    margin_usd: Any,
    leverage: Any,
    entry_price: Any,
    take_profit: Any,
    stop_loss: Any,
) -> SafetyResult:
    sym = _plain_symbol(symbol)
    side = normalize_direction(direction)
    margin = _safe_float(margin_usd)
    lev = _safe_float(leverage)
    entry = _safe_float(entry_price)
    tp = _safe_float(take_profit, 0.0)
    sl = _safe_float(stop_loss, 0.0)

    if not sym or not (sym.endswith("USDT") or sym.endswith("USDC")):
        return SafetyResult(False, True, "نماد سفارش نامعتبر است", symbol=sym, direction=side)

    if side not in {"LONG", "SHORT"}:
        return SafetyResult(False, True, "جهت سفارش باید LONG یا SHORT باشد", symbol=sym, direction=side)

    if margin < MIN_MARGIN_USD or margin > MAX_MARGIN_USD:
        return SafetyResult(False, True, f"مارجین باید بین {MIN_MARGIN_USD} تا {MAX_MARGIN_USD} دلار باشد", symbol=sym, direction=side, margin_usd=margin)

    if lev < MIN_LEVERAGE or lev > MAX_LEVERAGE:
        return SafetyResult(False, True, f"لوریج باید بین {int(MIN_LEVERAGE)} تا {int(MAX_LEVERAGE)} باشد", symbol=sym, direction=side, margin_usd=margin, leverage=lev)

    if entry <= 0:
        return SafetyResult(False, True, "قیمت ورود برای محاسبه حجم نامعتبر است", symbol=sym, direction=side, margin_usd=margin, leverage=lev)

    if tp <= 0 or sl <= 0:
        return SafetyResult(False, True, "TP و SL باید قبل از سفارش معتبر باشند", symbol=sym, direction=side, margin_usd=margin, leverage=lev, entry_price=entry)

    min_dist = max(entry * TP_SL_MIN_DISTANCE_PCT, 1e-12)
    if side == "LONG":
        if not (tp > entry + min_dist):
            return SafetyResult(False, True, "برای LONG، حد سود باید بالاتر از ورود باشد", symbol=sym, direction=side, margin_usd=margin, leverage=lev, entry_price=entry, take_profit=tp, stop_loss=sl)
        if not (sl < entry - min_dist):
            return SafetyResult(False, True, "برای LONG، حد ضرر باید پایین‌تر از ورود باشد", symbol=sym, direction=side, margin_usd=margin, leverage=lev, entry_price=entry, take_profit=tp, stop_loss=sl)
    else:
        if not (tp < entry - min_dist):
            return SafetyResult(False, True, "برای SHORT، حد سود باید پایین‌تر از ورود باشد", symbol=sym, direction=side, margin_usd=margin, leverage=lev, entry_price=entry, take_profit=tp, stop_loss=sl)
        if not (sl > entry + min_dist):
            return SafetyResult(False, True, "برای SHORT، حد ضرر باید بالاتر از ورود باشد", symbol=sym, direction=side, margin_usd=margin, leverage=lev, entry_price=entry, take_profit=tp, stop_loss=sl)

    qty, notional = calculate_quantity_from_margin(margin, lev, entry)
    if qty <= 0 or notional <= 0:
        return SafetyResult(False, True, "حجم سفارش قابل محاسبه نیست", symbol=sym, direction=side, margin_usd=margin, leverage=lev, entry_price=entry, take_profit=tp, stop_loss=sl)

    return SafetyResult(
        True,
        False,
        "",
        symbol=sym,
        direction=side,
        margin_usd=margin,
        leverage=lev,
        entry_price=entry,
        notional_usd=notional,
        quantity=qty,
        take_profit=tp,
        stop_loss=sl,
        details={"rule": "margin * leverage = notional; notional / entry = quantity"},
    )


def extract_position_leverage(position: Dict[str, Any]) -> float:
    if not isinstance(position, dict):
        return 0.0
    for key in ("leverage", "lever", "leverageValue"):
        v = _safe_float(position.get(key))
        if v > 0:
            return v
    return 0.0


def extract_position_quantity(position: Dict[str, Any]) -> float:
    if not isinstance(position, dict):
        return 0.0
    for key in ("positionAmt", "positionSize", "size", "qty", "quantity", "positionQuantity", "availablePosition"):
        v = _safe_float(position.get(key))
        if v != 0:
            return abs(v)
    return 0.0


def extract_position_entry(position: Dict[str, Any]) -> float:
    if not isinstance(position, dict):
        return 0.0
    for key in ("entryPrice", "avgPrice", "openPrice", "positionAvgPrice", "averagePrice"):
        v = _safe_float(position.get(key))
        if v > 0:
            return v
    return 0.0


def extract_position_margin(position: Dict[str, Any]) -> float:
    if not isinstance(position, dict):
        return 0.0
    for key in ("margin", "positionMargin", "initialMargin", "isolatedMargin", "usedMargin"):
        v = _safe_float(position.get(key))
        if v > 0:
            return v
    entry = extract_position_entry(position)
    qty = extract_position_quantity(position)
    lev = extract_position_leverage(position)
    if entry > 0 and qty > 0 and lev > 0:
        return (entry * qty) / lev
    return 0.0


def position_matches_order(
    position: Dict[str, Any],
    symbol: Any,
    direction: Any,
    expected_leverage: Any,
    expected_margin_usd: Any,
    expected_notional_usd: Any,
) -> SafetyResult:
    sym = _plain_symbol(symbol)
    side = normalize_direction(direction)
    lev_expected = _safe_float(expected_leverage)
    margin_expected = _safe_float(expected_margin_usd)
    notional_expected = _safe_float(expected_notional_usd)

    if not isinstance(position, dict):
        return SafetyResult(False, True, "پوزیشن توبیت قابل خواندن نیست", symbol=sym, direction=side)

    pos_symbol = _plain_symbol(position.get("symbol") or position.get("contractCode") or position.get("instrument") or position.get("instId") or position.get("pair"))
    if pos_symbol and pos_symbol != sym:
        return SafetyResult(False, True, f"نماد پوزیشن با سفارش یکی نیست: {pos_symbol} != {sym}", symbol=sym, direction=side, details={"position": position})

    pos_side = extract_position_direction(position)
    if pos_side and side and pos_side != side:
        return SafetyResult(False, True, f"جهت پوزیشن با سفارش یکی نیست: {pos_side} != {side}", symbol=sym, direction=side, details={"position": position})

    margin_mode = extract_margin_mode(position)
    if margin_mode and margin_mode != "ISOLATED":
        return SafetyResult(False, True, f"مارجین پوزیشن باید ISOLATED باشد، مقدار فعلی: {margin_mode}", symbol=sym, direction=side, details={"position": position})

    pos_lev = extract_position_leverage(position)
    if pos_lev > 0 and lev_expected > 0 and abs(pos_lev - lev_expected) > LEVERAGE_TOLERANCE:
        return SafetyResult(False, True, f"لوریج پوزیشن با تنظیم ربات یکی نیست: {pos_lev} != {lev_expected}", symbol=sym, direction=side, leverage=lev_expected, details={"position": position})

    pos_entry = extract_position_entry(position)
    pos_qty = extract_position_quantity(position)
    pos_notional = pos_entry * pos_qty if pos_entry > 0 and pos_qty > 0 else 0.0
    pos_margin = extract_position_margin(position)

    if notional_expected > 0 and pos_notional > 0:
        diff_pct = abs(pos_notional - notional_expected) / max(notional_expected, 1e-12)
        if diff_pct > NOTIONAL_TOLERANCE_PCT:
            return SafetyResult(False, True, f"حجم پوزیشن با سفارش ربات اختلاف زیاد دارد: {round(pos_notional, 6)} != {round(notional_expected, 6)}", symbol=sym, direction=side, margin_usd=margin_expected, leverage=lev_expected, notional_usd=notional_expected, details={"position_notional": pos_notional, "position": position})

    return SafetyResult(
        True,
        False,
        "",
        symbol=sym,
        direction=side,
        margin_usd=margin_expected or pos_margin,
        leverage=lev_expected or pos_lev,
        entry_price=pos_entry,
        notional_usd=notional_expected or pos_notional,
        quantity=pos_qty,
        details={"position_margin": pos_margin, "position_notional": pos_notional, "position": position},
    )


def ensure_isolated_margin(client: Any, symbol: Any) -> Dict[str, Any]:
    """Set and verify ISOLATED margin mode before every real order.

    This is intentionally strict: if the client cannot set/read/verify isolated
    mode, the real order must be blocked rather than opened with CROSS margin.
    """
    sym = _plain_symbol(symbol)
    if not sym:
        return {"ok": False, "blocked": True, "error": "نماد برای تنظیم مارجین نامعتبر است"}

    candidate_names = (
        "ensure_isolated_margin",
        "verify_isolated_margin",
        "set_and_verify_isolated_margin",
        "set_isolated_margin",
        "set_margin_mode_isolated",
    )
    last_result = None
    for name in candidate_names:
        if not hasattr(client, name):
            continue
        try:
            fn = getattr(client, name)
            try:
                result = fn(sym)
            except TypeError:
                result = fn(symbol=sym)
            last_result = result
            if isinstance(result, dict):
                mode = normalize_margin_mode(
                    result.get("margin_mode")
                    or result.get("marginMode")
                    or result.get("mode")
                    or result.get("actual_margin_mode")
                    or result.get("data", {}).get("marginMode") if isinstance(result.get("data"), dict) else None
                )
                if result.get("ok") and (not mode or mode == "ISOLATED"):
                    return {"ok": True, "symbol": sym, "margin_mode": "ISOLATED", "data": result}
                if mode and mode != "ISOLATED":
                    return {"ok": False, "blocked": True, "error": f"مارجین توبیت ISOLATED تأیید نشد: {mode}", "data": result}
            elif result is True:
                return {"ok": True, "symbol": sym, "margin_mode": "ISOLATED", "data": result}
        except Exception as e:
            return {"ok": False, "blocked": True, "error": f"خطا در تنظیم/تأیید ISOLATED margin: {str(e)[:250]}"}

    return {"ok": False, "blocked": True, "error": "تابع تنظیم/تأیید ISOLATED margin در tobit_client.py وجود ندارد", "data": last_result}


def ensure_exchange_leverage(client: Any, symbol: Any, leverage: Any) -> Dict[str, Any]:
    """
    Set and verify leverage using methods provided by tobit_client.py.
    If the client cannot verify leverage, block the order for safety.
    """
    sym = _plain_symbol(symbol)
    lev = _safe_float(leverage)
    if lev < MIN_LEVERAGE or lev > MAX_LEVERAGE:
        return {"ok": False, "blocked": True, "error": f"لوریج باید بین {int(MIN_LEVERAGE)} تا {int(MAX_LEVERAGE)} باشد"}

    if not hasattr(client, "verify_leverage"):
        return {"ok": False, "blocked": True, "error": "تابع verify_leverage در tobit_client.py وجود ندارد"}

    try:
        result = client.verify_leverage(sym, lev)
    except TypeError:
        result = client.verify_leverage(symbol=sym, leverage=lev)
    except Exception as e:
        return {"ok": False, "blocked": True, "error": f"خطا در Verify لوریج: {str(e)[:250]}"}

    if not isinstance(result, dict) or not result.get("ok"):
        return {"ok": False, "blocked": True, "error": (result or {}).get("error") or "لوریج در توبیت تأیید نشد", "data": result}

    actual = _safe_float(result.get("actual_leverage") or result.get("leverage") or lev)
    if actual > 0 and abs(actual - lev) > LEVERAGE_TOLERANCE:
        return {"ok": False, "blocked": True, "error": f"لوریج واقعی توبیت تأیید نشد: {actual} != {lev}", "data": result}

    return {"ok": True, "symbol": sym, "leverage": lev, "data": result}


def pre_order_safety_check(
    client: Any,
    symbol: Any,
    direction: Any,
    margin_usd: Any,
    leverage: Any,
    entry_price: Any,
    take_profit: Any,
    stop_loss: Any,
) -> Dict[str, Any]:
    """
    Main pre-order safety gate.

    Returns a dict with ok=True only when:
    - inputs are valid,
    - margin mode is set/read/verified as ISOLATED on Toobit,
    - leverage is set/read/verified on Toobit,
    - quantity is calculated from margin * leverage,
    - TP/SL direction is valid.
    """
    basic = validate_basic_order_inputs(symbol, direction, margin_usd, leverage, entry_price, take_profit, stop_loss)
    if not basic.ok:
        return basic.to_dict()

    margin_result = ensure_isolated_margin(client, basic.symbol)
    if not margin_result.get("ok"):
        data = basic.to_dict()
        data.update({"ok": False, "blocked": True, "error": margin_result.get("error"), "margin_mode_check": margin_result})
        return data

    lev_result = ensure_exchange_leverage(client, basic.symbol, basic.leverage)
    if not lev_result.get("ok"):
        data = basic.to_dict()
        data.update({"ok": False, "blocked": True, "error": lev_result.get("error"), "margin_mode_check": margin_result, "leverage_check": lev_result})
        return data

    data = basic.to_dict()
    data["margin_mode_check"] = margin_result
    data["leverage_check"] = lev_result
    data["ok"] = True
    data["blocked"] = False
    return data


def post_order_position_safety_check(
    position: Dict[str, Any],
    planned_order: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Verify actual opened position against the planned order.
    This does not close the position; it only reports mismatch so the manager can warn/repair/sync safely.
    """
    return position_matches_order(
        position=position,
        symbol=planned_order.get("symbol"),
        direction=planned_order.get("direction"),
        expected_leverage=planned_order.get("leverage"),
        expected_margin_usd=planned_order.get("margin_usd"),
        expected_notional_usd=planned_order.get("notional_usd"),
    ).to_dict()


__all__ = [
    "SafetyResult",
    "validate_basic_order_inputs",
    "calculate_quantity_from_margin",
    "ensure_exchange_leverage",
    "ensure_isolated_margin",
    "pre_order_safety_check",
    "post_order_position_safety_check",
    "position_matches_order",
    "normalize_direction",
    "normalize_margin_mode",
    "extract_position_direction",
    "extract_margin_mode",
    "quantize_down",
]
