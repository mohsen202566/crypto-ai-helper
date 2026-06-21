from __future__ import annotations

"""
Toobit safety layer.

Responsibilities:
- Enforce ISOLATED margin only.
- Verify/prepare quantity before real order.
- Block order if isolated cannot be set/confirmed.
- Protect against too-small quantity errors.
"""

from typing import Any, Dict, Optional
import math

from config import ISOLATED_MARGIN_ONLY
from diagnostics import safe, warning
import tobit_client


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return default
        return f
    except Exception:
        return default


@safe(default={})
def ensure_isolated(symbol: str, client: Optional[tobit_client.ToobitClient] = None) -> Dict[str, Any]:
    c = client or tobit_client.client()
    symbol = symbol.upper()
    if not ISOLATED_MARGIN_ONLY:
        return {"ok": True, "symbol": symbol, "isolated_required": False}
    res = c.set_margin_type(symbol, "ISOLATED")
    if not res.get("ok"):
        return {"ok": False, "symbol": symbol, "reason": "cannot_set_isolated", "raw": res}
    return {"ok": True, "symbol": symbol, "margin_type": "ISOLATED", "raw": res}


@safe(default={})
def prepare_quantity(symbol: str, desired_quantity: float, price: float, client: Optional[tobit_client.ToobitClient] = None) -> Dict[str, Any]:
    c = client or tobit_client.client()
    norm = c.normalize_quantity(symbol, desired_quantity, price)
    if not norm.get("ok"):
        return {"ok": False, "symbol": symbol.upper(), "reason": "quantity_normalization_failed", "raw": norm}
    q = _safe_float(norm.get("quantity"))
    if q <= 0:
        return {"ok": False, "symbol": symbol.upper(), "reason": "quantity_zero", "raw": norm}
    return {"ok": True, "symbol": symbol.upper(), "quantity": q, "raw": norm}


@safe(default={})
def preflight_real_order(symbol: str, side: str, desired_quantity: float, price: float, client: Optional[tobit_client.ToobitClient] = None) -> Dict[str, Any]:
    c = client or tobit_client.client()
    iso = ensure_isolated(symbol, c)
    if not iso.get("ok"):
        return {"ok": False, "symbol": symbol.upper(), "side": side.upper(), "reason": "isolated_not_confirmed", "isolated": iso}
    qty = prepare_quantity(symbol, desired_quantity, price, c)
    if not qty.get("ok"):
        return {"ok": False, "symbol": symbol.upper(), "side": side.upper(), "reason": "quantity_invalid", "quantity": qty}
    return {
        "ok": True,
        "symbol": symbol.upper(),
        "side": side.upper(),
        "quantity": qty.get("quantity"),
        "isolated": iso,
        "quantity_info": qty,
    }


@safe(default={})
def side_from_direction(direction: str) -> Dict[str, str]:
    d = str(direction).upper()
    if d == "LONG":
        return {"open_side": "BUY", "close_side": "SELL"}
    if d == "SHORT":
        return {"open_side": "SELL", "close_side": "BUY"}
    return {"open_side": "", "close_side": ""}
