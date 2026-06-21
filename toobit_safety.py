from __future__ import annotations

"""
Toobit safety layer.

Responsibilities:
- Enforce ISOLATED margin only.
- Verify/set leverage before real order.
- Verify/prepare quantity before real order.
- Block order if isolated cannot be set OR confirmed.
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


def _extract_margin_type(raw: Any) -> str:
    if isinstance(raw, dict):
        for k in ("marginType", "margin_type", "marginMode", "margin_mode"):
            if raw.get(k):
                return str(raw.get(k)).upper()
    return ""


def _extract_leverage(raw: Any) -> int:
    if isinstance(raw, dict):
        for k in ("leverage", "lev"):
            if raw.get(k) is not None:
                try:
                    return int(float(raw.get(k)))
                except Exception:
                    pass
    return 0


@safe(default={})
def verify_isolated(symbol: str, client: Optional[tobit_client.ToobitClient] = None) -> Dict[str, Any]:
    """
    Read current Toobit margin mode. This is important when the user has already
    set the pair to ISOLATED manually inside Toobit.
    """
    c = client or tobit_client.client()
    symbol = symbol.upper()

    # accountLeverage is the clean endpoint for leverage + marginType before a position exists.
    lev = c.account_leverage(symbol) if hasattr(c, "account_leverage") else {}
    if lev.get("ok"):
        raw = lev.get("raw", {})
        mt = _extract_margin_type(raw)
        if mt in {"ISOLATED", "ISOLATED_MARGIN"}:
            return {"ok": True, "symbol": symbol, "margin_type": "ISOLATED", "source": "account_leverage", "raw": lev}
        if mt:
            return {"ok": False, "symbol": symbol, "reason": "not_isolated", "margin_type": mt, "source": "account_leverage", "raw": lev}

    # Position endpoint can confirm marginType once there is a position/open context.
    pos = c.get_position(symbol)
    if pos.get("ok"):
        mt = str(pos.get("margin_type") or _extract_margin_type(pos.get("raw", {}))).upper()
        if mt in {"ISOLATED", "ISOLATED_MARGIN"}:
            return {"ok": True, "symbol": symbol, "margin_type": "ISOLATED", "source": "position", "raw": pos}
        if mt:
            return {"ok": False, "symbol": symbol, "reason": "not_isolated", "margin_type": mt, "source": "position", "raw": pos}

    return {"ok": False, "symbol": symbol, "reason": "cannot_read_margin_type", "account_leverage": lev, "position": pos}


@safe(default={})
def ensure_isolated(symbol: str, client: Optional[tobit_client.ToobitClient] = None) -> Dict[str, Any]:
    """
    Enforce user rule: real orders are allowed only when ISOLATED is confirmed.

    Flow:
    1) Try to set ISOLATED.
    2) If set fails, read current margin mode.
    3) Allow only if current mode is confirmed ISOLATED.
    """
    c = client or tobit_client.client()
    symbol = symbol.upper()
    if not ISOLATED_MARGIN_ONLY:
        return {"ok": True, "symbol": symbol, "isolated_required": False}

    set_res = c.set_margin_type(symbol, "ISOLATED")
    if set_res.get("ok"):
        return {"ok": True, "symbol": symbol, "margin_type": "ISOLATED", "source": "set_margin_type", "raw": set_res}

    verify = verify_isolated(symbol, c)
    if verify.get("ok"):
        return {"ok": True, "symbol": symbol, "margin_type": "ISOLATED", "source": "verify_after_set_failed", "set_raw": set_res, "verify": verify}

    return {"ok": False, "symbol": symbol, "reason": "cannot_set_or_verify_isolated", "set_raw": set_res, "verify": verify}


@safe(default={})
def ensure_leverage(symbol: str, leverage: int, client: Optional[tobit_client.ToobitClient] = None) -> Dict[str, Any]:
    """
    Try to set leverage. If setting fails, verify current leverage. If current leverage
    matches requested leverage, allow. Otherwise block real order.
    """
    c = client or tobit_client.client()
    symbol = symbol.upper()
    target = max(1, min(125, int(leverage or 1)))

    set_res = c.set_leverage(symbol, target) if hasattr(c, "set_leverage") else {"ok": False, "error": "client_has_no_set_leverage"}
    if set_res.get("ok"):
        return {"ok": True, "symbol": symbol, "leverage": target, "source": "set_leverage", "raw": set_res}

    lev = c.account_leverage(symbol) if hasattr(c, "account_leverage") else {}
    if lev.get("ok"):
        current = _extract_leverage(lev.get("raw", {}))
        if current == target:
            return {"ok": True, "symbol": symbol, "leverage": current, "source": "verify_after_set_failed", "set_raw": set_res, "verify": lev}
        return {"ok": False, "symbol": symbol, "reason": "leverage_mismatch", "target": target, "current": current, "set_raw": set_res, "verify": lev}

    return {"ok": False, "symbol": symbol, "reason": "cannot_set_or_verify_leverage", "target": target, "set_raw": set_res, "verify": lev}


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
def preflight_real_order(
    symbol: str,
    side: str,
    desired_quantity: float,
    price: float,
    client: Optional[tobit_client.ToobitClient] = None,
    leverage: int = 1,
) -> Dict[str, Any]:
    c = client or tobit_client.client()

    iso = ensure_isolated(symbol, c)
    if not iso.get("ok"):
        return {"ok": False, "symbol": symbol.upper(), "side": side.upper(), "reason": "isolated_not_confirmed", "isolated": iso}

    lev = ensure_leverage(symbol, leverage, c)
    if not lev.get("ok"):
        return {"ok": False, "symbol": symbol.upper(), "side": side.upper(), "reason": "leverage_not_confirmed", "leverage": lev}

    qty = prepare_quantity(symbol, desired_quantity, price, c)
    if not qty.get("ok"):
        return {"ok": False, "symbol": symbol.upper(), "side": side.upper(), "reason": "quantity_invalid", "quantity": qty}

    return {
        "ok": True,
        "symbol": symbol.upper(),
        "side": side.upper(),
        "quantity": qty.get("quantity"),
        "isolated": iso,
        "leverage": lev,
        "quantity_info": qty,
    }


@safe(default={})
def side_from_direction(direction: str) -> Dict[str, str]:
    d = str(direction).upper()
    if d == "LONG":
        return {"open_side": "BUY_OPEN", "close_side": "SELL_CLOSE", "position_side": "LONG"}
    if d == "SHORT":
        return {"open_side": "SELL_OPEN", "close_side": "BUY_CLOSE", "position_side": "SHORT"}
    return {"open_side": "", "close_side": "", "position_side": ""}
