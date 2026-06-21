from __future__ import annotations

"""
Toobit client.

Responsibilities:
- Public candles / ticker / exchange info.
- Private order, margin mode, position, closed PnL calls.
- Quantity/min-size rounding.
- Never crash bot on API errors.

This client is intentionally conservative and Binance-compatible where possible.
Exact Toobit endpoint differences can be adjusted here without touching AI/tracker.
"""

import hashlib
import hmac
import time
import math
from decimal import Decimal, ROUND_DOWN
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import requests

from config import TOOBIT_BASE_URL, TOOBIT_API_KEY, TOOBIT_API_SECRET
from diagnostics import safe, record_error, warning


DEFAULT_TIMEOUT = 12


def _ts_ms() -> int:
    return int(time.time() * 1000)


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


def _round_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    d_value = Decimal(str(value))
    d_step = Decimal(str(step))
    return float((d_value / d_step).to_integral_value(rounding=ROUND_DOWN) * d_step)


class ToobitClient:
    def __init__(
        self,
        api_key: str = TOOBIT_API_KEY,
        api_secret: str = TOOBIT_API_SECRET,
        base_url: str = TOOBIT_BASE_URL,
        timeout: int = DEFAULT_TIMEOUT,
    ):
        self.api_key = api_key or ""
        self.api_secret = api_secret or ""
        self.base_url = (base_url or "https://api.toobit.com").rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()

    def _headers(self) -> Dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["X-BB-APIKEY"] = self.api_key
        return h

    def _sign(self, params: Dict[str, Any]) -> Dict[str, Any]:
        params = dict(params or {})
        params.setdefault("timestamp", _ts_ms())
        query = urlencode(params, doseq=True)
        sig = hmac.new(self.api_secret.encode(), query.encode(), hashlib.sha256).hexdigest() if self.api_secret else ""
        params["signature"] = sig
        return params

    def _request(self, method: str, path: str, params: Optional[Dict[str, Any]] = None, signed: bool = False) -> Dict[str, Any]:
        params = dict(params or {})
        if signed:
            params = self._sign(params)
        url = self.base_url + path
        try:
            if method.upper() == "GET":
                r = self.session.get(url, params=params, headers=self._headers(), timeout=self.timeout)
            elif method.upper() == "POST":
                r = self.session.post(url, params=params, headers=self._headers(), timeout=self.timeout)
            elif method.upper() == "DELETE":
                r = self.session.delete(url, params=params, headers=self._headers(), timeout=self.timeout)
            else:
                return {"ok": False, "error": f"unsupported_method:{method}"}
            try:
                data = r.json()
            except Exception:
                data = {"raw": r.text}
            if r.status_code >= 400:
                return {"ok": False, "status_code": r.status_code, "error": data, "path": path}
            return {"ok": True, "data": data, "status_code": r.status_code}
        except Exception as e:
            record_error(e, module="tobit_client", function="_request", context={"method": method, "path": path})
            return {"ok": False, "error": str(e), "path": path}

    # Public endpoints

    @safe(default={})
    def exchange_info(self, symbol: Optional[str] = None) -> Dict[str, Any]:
        params = {}
        if symbol:
            params["symbol"] = symbol.upper()
        # Toobit futures endpoints are Binance-like in many wrappers.
        res = self._request("GET", "/api/v1/exchangeInfo", params=params, signed=False)
        if not res.get("ok"):
            # fallback path
            res = self._request("GET", "/fapi/v1/exchangeInfo", params=params, signed=False)
        return res

    @safe(default={})
    def ticker_price(self, symbol: str) -> Dict[str, Any]:
        symbol = symbol.upper()
        for path in ["/api/v1/ticker/price", "/fapi/v1/ticker/price"]:
            res = self._request("GET", path, params={"symbol": symbol})
            if res.get("ok"):
                data = res.get("data", {})
                price = _safe_float(data.get("price") if isinstance(data, dict) else 0)
                return {"ok": True, "symbol": symbol, "price": price, "raw": data}
        return {"ok": False, "symbol": symbol, "price": 0.0}

    @safe(default=[])
    def klines(self, symbol: str, interval: str = "5m", limit: int = 120) -> List[Dict[str, Any]]:
        symbol = symbol.upper()
        for path in ["/api/v1/klines", "/fapi/v1/klines"]:
            res = self._request("GET", path, params={"symbol": symbol, "interval": interval, "limit": limit})
            if not res.get("ok"):
                continue
            raw = res.get("data", [])
            if isinstance(raw, dict):
                raw = raw.get("data", raw.get("rows", []))
            candles = []
            for row in raw:
                if isinstance(row, list) and len(row) >= 6:
                    candles.append({
                        "timestamp": int(_safe_float(row[0])),
                        "open": _safe_float(row[1]),
                        "high": _safe_float(row[2]),
                        "low": _safe_float(row[3]),
                        "close": _safe_float(row[4]),
                        "volume": _safe_float(row[5]),
                    })
                elif isinstance(row, dict):
                    candles.append({
                        "timestamp": int(_safe_float(row.get("openTime", row.get("timestamp", row.get("time", 0))))),
                        "open": _safe_float(row.get("open")),
                        "high": _safe_float(row.get("high")),
                        "low": _safe_float(row.get("low")),
                        "close": _safe_float(row.get("close")),
                        "volume": _safe_float(row.get("volume")),
                    })
            if candles:
                return candles
        return []

    # Symbol filters

    @safe(default={})
    def symbol_filters(self, symbol: str) -> Dict[str, Any]:
        symbol = symbol.upper()
        info = self.exchange_info(symbol)
        data = info.get("data", {})
        symbols = []
        if isinstance(data, dict):
            symbols = data.get("symbols") or data.get("data", {}).get("symbols", [])
        if isinstance(symbols, dict):
            symbols = [symbols]
        target = None
        for s in symbols or []:
            if str(s.get("symbol", "")).upper() == symbol:
                target = s
                break
        if not target and isinstance(data, dict) and str(data.get("symbol", "")).upper() == symbol:
            target = data
        filters = target.get("filters", []) if isinstance(target, dict) else []
        lot = {}
        price_filter = {}
        min_notional = {}
        for f in filters:
            t = f.get("filterType", "")
            if t in {"LOT_SIZE", "MARKET_LOT_SIZE"} and not lot:
                lot = f
            if t == "PRICE_FILTER":
                price_filter = f
            if t in {"MIN_NOTIONAL", "NOTIONAL"}:
                min_notional = f
        return {
            "symbol": symbol,
            "min_qty": _safe_float(lot.get("minQty", target.get("minQty", 0) if target else 0)),
            "step_size": _safe_float(lot.get("stepSize", target.get("stepSize", 0) if target else 0)),
            "tick_size": _safe_float(price_filter.get("tickSize", target.get("tickSize", 0) if target else 0)),
            "min_notional": _safe_float(min_notional.get("notional", min_notional.get("minNotional", 0))),
            "raw": target or {},
        }

    @safe(default={})
    def normalize_quantity(self, symbol: str, quantity: float, price: float = 0.0) -> Dict[str, Any]:
        f = self.symbol_filters(symbol)
        min_qty = _safe_float(f.get("min_qty"))
        step = _safe_float(f.get("step_size"))
        min_notional = _safe_float(f.get("min_notional"))
        q = _safe_float(quantity)
        if min_notional > 0 and price > 0:
            q = max(q, min_notional / price)
        if min_qty > 0:
            q = max(q, min_qty)
        if step > 0:
            q = _round_step(q, step)
            if q < min_qty:
                q = _round_step(min_qty + step, step)
        return {"ok": q > 0, "symbol": symbol.upper(), "quantity": q, "filters": f}

    # Private endpoints

    @safe(default={})
    def set_margin_type(self, symbol: str, margin_type: str = "ISOLATED") -> Dict[str, Any]:
        symbol = symbol.upper()
        margin_type = margin_type.upper()
        params = {"symbol": symbol, "marginType": margin_type}
        for path in ["/fapi/v1/marginType", "/api/v1/marginType"]:
            res = self._request("POST", path, params=params, signed=True)
            if res.get("ok"):
                return {"ok": True, "symbol": symbol, "margin_type": margin_type, "raw": res.get("data")}
            # Binance-like: already set can return error but is acceptable
            err = str(res.get("error", "")).lower()
            if "no need to change" in err or "already" in err:
                return {"ok": True, "symbol": symbol, "margin_type": margin_type, "raw": res.get("error")}
        return {"ok": False, "symbol": symbol, "margin_type": margin_type, "error": "set_margin_type_failed"}

    @safe(default={})
    def get_position(self, symbol: str) -> Dict[str, Any]:
        symbol = symbol.upper()
        for path in ["/fapi/v2/positionRisk", "/fapi/v1/positionRisk", "/api/v1/positionRisk"]:
            res = self._request("GET", path, params={"symbol": symbol}, signed=True)
            if not res.get("ok"):
                continue
            data = res.get("data", [])
            rows = data if isinstance(data, list) else data.get("data", data if isinstance(data, dict) else [])
            if isinstance(rows, dict):
                rows = [rows]
            for p in rows:
                if str(p.get("symbol", "")).upper() == symbol:
                    amt = _safe_float(p.get("positionAmt", p.get("positionAmount", p.get("size", 0))))
                    return {"ok": True, "symbol": symbol, "position_amt": amt, "raw": p}
        return {"ok": False, "symbol": symbol, "position_amt": 0.0}

    @safe(default={})
    def create_order(self, symbol: str, side: str, quantity: float, order_type: str = "MARKET", price: Optional[float] = None, reduce_only: bool = False, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        symbol = symbol.upper()
        side = side.upper()
        params = {
            "symbol": symbol,
            "side": side,
            "type": order_type.upper(),
            "quantity": quantity,
        }
        if price is not None and order_type.upper() != "MARKET":
            params["price"] = price
            params["timeInForce"] = "GTC"
        if reduce_only:
            params["reduceOnly"] = "true"
        if extra:
            params.update(extra)
        for path in ["/fapi/v1/order", "/api/v1/order"]:
            res = self._request("POST", path, params=params, signed=True)
            if res.get("ok"):
                return {"ok": True, "symbol": symbol, "side": side, "raw": res.get("data")}
            err = str(res.get("error", ""))
            if "-1202" in err or "quantity too small" in err.lower():
                return {"ok": False, "symbol": symbol, "side": side, "error_code": -1202, "error": err}
        return {"ok": False, "symbol": symbol, "side": side, "error": "create_order_failed"}

    @safe(default={})
    def cancel_order(self, symbol: str, order_id: str) -> Dict[str, Any]:
        return self._request("DELETE", "/fapi/v1/order", params={"symbol": symbol.upper(), "orderId": order_id}, signed=True)

    @safe(default=[])
    def closed_pnl_history(self, symbol: str, start_time: Optional[int] = None, limit: int = 20) -> List[Dict[str, Any]]:
        params = {"symbol": symbol.upper(), "limit": limit}
        if start_time:
            params["startTime"] = start_time
        for path in ["/fapi/v1/income", "/api/v1/income"]:
            res = self._request("GET", path, params=params, signed=True)
            if not res.get("ok"):
                continue
            data = res.get("data", [])
            if isinstance(data, dict):
                data = data.get("data", [])
            return data if isinstance(data, list) else []
        return []


_default_client: Optional[ToobitClient] = None


def client() -> ToobitClient:
    global _default_client
    if _default_client is None:
        _default_client = ToobitClient()
    return _default_client
