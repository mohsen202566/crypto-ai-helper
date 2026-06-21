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


def _okx_bar(interval: str) -> str:
    """Convert bot/Binance-style interval to OKX candle bar."""
    tf = str(interval or "5m").strip()
    mapping = {
        "1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m",
        "1h": "1H", "2h": "2H", "4h": "4H", "6h": "6H", "12h": "12H",
        "1d": "1D",
        "1H": "1H", "2H": "2H", "4H": "4H", "6H": "6H", "12H": "12H", "1D": "1D",
    }
    return mapping.get(tf, tf)


def _okx_inst_id(symbol: str) -> str:
    """Convert BTCUSDT / BTC-USDT to OKX perpetual swap instId: BTC-USDT-SWAP."""
    s = str(symbol or "").upper().replace("/", "-").replace("_", "-")
    if s.endswith("-SWAP"):
        return s
    if "-" in s:
        parts = [x for x in s.split("-") if x]
        if len(parts) >= 2:
            return f"{parts[0]}-{parts[1]}-SWAP"
    for quote in ("USDT", "USDC", "USD"):
        if s.endswith(quote) and len(s) > len(quote):
            base = s[:-len(quote)]
            return f"{base}-{quote}-SWAP"
    return s



def _is_success_payload(data: Any) -> bool:
    """Toobit often returns HTTP 200 with a business code field."""
    if not isinstance(data, dict):
        return True
    code = data.get("code")
    if code is None:
        return True
    try:
        return int(code) == 200
    except Exception:
        return str(code).lower() in {"200", "success", "0"}

def _toobit_symbol(symbol: str) -> str:
    """Convert bot symbol BTCUSDT / BTC-USDT to Toobit USDT-M futures symbol BTC-SWAP-USDT."""
    s = str(symbol or "").upper().replace("/", "-").replace("_", "-")
    if "-SWAP-" in s:
        return s
    if s.endswith("-SWAP"):
        # OKX style BTC-USDT-SWAP -> Toobit style BTC-SWAP-USDT
        parts = s.split("-")
        if len(parts) >= 3:
            return f"{parts[0]}-SWAP-{parts[1]}"
    if "-" in s:
        parts = [x for x in s.split("-") if x]
        if len(parts) >= 2:
            return f"{parts[0]}-SWAP-{parts[1]}"
    for quote in ("USDT", "USDC", "USD"):
        if s.endswith(quote) and len(s) > len(quote):
            return f"{s[:-len(quote)]}-SWAP-{quote}"
    return s

def _bot_symbol(symbol: str) -> str:
    """Convert Toobit futures symbol BTC-SWAP-USDT back to bot symbol BTCUSDT."""
    s = str(symbol or "").upper()
    if "-SWAP-" in s:
        base, quote = s.split("-SWAP-", 1)
        return f"{base}{quote}"
    return s.replace("-", "")


def _parse_okx_candles(raw: Any) -> List[Dict[str, Any]]:
    rows = []
    if isinstance(raw, dict):
        rows = raw.get("data", [])
    elif isinstance(raw, list):
        rows = raw
    candles: List[Dict[str, Any]] = []
    for row in rows or []:
        if not isinstance(row, list) or len(row) < 6:
            continue
        candles.append({
            "timestamp": int(_safe_float(row[0])),
            "open": _safe_float(row[1]),
            "high": _safe_float(row[2]),
            "low": _safe_float(row[3]),
            "close": _safe_float(row[4]),
            "volume": _safe_float(row[5]),
        })
    # OKX returns newest first; scanner/analysis expect oldest -> newest.
    candles.sort(key=lambda x: x.get("timestamp", 0))
    return candles


def _parse_binance_like_candles(raw: Any) -> List[Dict[str, Any]]:
    if isinstance(raw, dict):
        raw = raw.get("data", raw.get("rows", raw.get("result", [])))
    candles: List[Dict[str, Any]] = []
    for row in raw or []:
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
    candles = [c for c in candles if c.get("timestamp") and c.get("close")]
    candles.sort(key=lambda x: x.get("timestamp", 0))
    return candles


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
        h = {"Content-Type": "application/x-www-form-urlencoded"}
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
                # Toobit signed examples use form body (-d). Query string also works for some endpoints,
                # but body is more compatible for futures trade endpoints.
                r = self.session.post(url, data=params, headers=self._headers(), timeout=self.timeout)
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
            if not _is_success_payload(data):
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
        """
        Return candles in the bot's standard format, sorted oldest -> newest.

        Market analysis previously used OKX data, while this client is also used for
        Toobit real trading/private endpoints. To keep the rest of the bot unchanged,
        public candles are fetched from OKX first and Toobit is kept only as fallback.
        """
        symbol = str(symbol or "").upper()
        limit = max(1, min(int(limit or 120), 300))

        # Primary public data source: OKX perpetual swap candles.
        try:
            okx_res = self.session.get(
                "https://www.okx.com/api/v5/market/candles",
                params={"instId": _okx_inst_id(symbol), "bar": _okx_bar(interval), "limit": limit},
                timeout=self.timeout,
            )
            okx_data = okx_res.json()
            if okx_res.status_code < 400 and str(okx_data.get("code", "0")) == "0":
                candles = _parse_okx_candles(okx_data)
                if candles:
                    return candles[-limit:]
        except Exception as e:
            record_error(e, module="tobit_client", function="klines_okx", context={"symbol": symbol, "interval": interval})

        # Fallback: Toobit/Binance-compatible public endpoints.
        for path in ["/api/v1/klines", "/fapi/v1/klines"]:
            res = self._request("GET", path, params={"symbol": symbol, "interval": interval, "limit": limit})
            if not res.get("ok"):
                continue
            candles = _parse_binance_like_candles(res.get("data", []))
            if candles:
                return candles[-limit:]
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
    def account_leverage(self, symbol: Optional[str] = None) -> Dict[str, Any]:
        """Read leverage/margin mode from Toobit accountLeverage endpoint."""
        params: Dict[str, Any] = {}
        if symbol:
            params["symbol"] = _toobit_symbol(symbol)
        res = self._request("GET", "/api/v1/futures/accountLeverage", params=params, signed=True)
        if not res.get("ok"):
            return {"ok": False, "symbol": symbol.upper() if symbol else "", "error": res}
        data = res.get("data", [])
        rows = data if isinstance(data, list) else data.get("data", data if isinstance(data, dict) else [])
        if isinstance(rows, dict):
            rows = [rows]
        target = None
        if symbol:
            tb = _toobit_symbol(symbol)
            for r in rows or []:
                if str(r.get("symbolId", r.get("symbol", ""))).upper() == tb:
                    target = r
                    break
        return {"ok": True, "symbol": symbol.upper() if symbol else "", "raw": target if target is not None else rows}

    @safe(default={})
    def set_margin_type(self, symbol: str, margin_type: str = "ISOLATED") -> Dict[str, Any]:
        symbol_bot = symbol.upper()
        tb_symbol = _toobit_symbol(symbol_bot)
        margin_type = margin_type.upper()
        params = {"symbol": tb_symbol, "marginType": margin_type}
        res = self._request("POST", "/api/v1/futures/marginType", params=params, signed=True)
        if res.get("ok"):
            return {"ok": True, "symbol": symbol_bot, "exchange_symbol": tb_symbol, "margin_type": margin_type, "raw": res.get("data")}
        # Some exchanges return an error when already set. Treat clearly matching messages as OK.
        err = str(res.get("error", "")).lower()
        if "no need to change" in err or "already" in err:
            return {"ok": True, "symbol": symbol_bot, "exchange_symbol": tb_symbol, "margin_type": margin_type, "raw": res.get("error")}
        return {"ok": False, "symbol": symbol_bot, "exchange_symbol": tb_symbol, "margin_type": margin_type, "error": "set_margin_type_failed", "raw": res}

    @safe(default={})
    def set_leverage(self, symbol: str, leverage: int) -> Dict[str, Any]:
        """Set Toobit USDT-M leverage using the official v2 futures endpoint."""
        symbol_bot = symbol.upper()
        tb_symbol = _toobit_symbol(symbol_bot)
        lev = max(1, min(125, int(leverage or 1)))
        params = {"symbol": tb_symbol, "leverage": str(lev), "category": "USDT"}
        res = self._request("POST", "/api/v2/futures/leverage", params=params, signed=True)
        if res.get("ok"):
            return {"ok": True, "symbol": symbol_bot, "exchange_symbol": tb_symbol, "leverage": lev, "raw": res.get("data"), "params": params}
        return {"ok": False, "symbol": symbol_bot, "exchange_symbol": tb_symbol, "leverage": lev, "error": "set_leverage_failed", "raw": res, "params": params}

    @safe(default={})
    def get_position(self, symbol: str, side: Optional[str] = None) -> Dict[str, Any]:
        symbol_bot = symbol.upper()
        tb_symbol = _toobit_symbol(symbol_bot)
        params: Dict[str, Any] = {"symbol": tb_symbol}
        if side:
            params["side"] = side.upper()
        res = self._request("GET", "/api/v1/futures/positions", params=params, signed=True)
        if not res.get("ok"):
            return {"ok": False, "symbol": symbol_bot, "exchange_symbol": tb_symbol, "position_amt": 0.0, "raw": res}
        data = res.get("data", [])
        rows = data if isinstance(data, list) else data.get("data", data if isinstance(data, dict) else [])
        if isinstance(rows, dict):
            rows = [rows]
        for p in rows or []:
            if str(p.get("symbol", "")).upper() != tb_symbol:
                continue
            if side and str(p.get("side", "")).upper() != side.upper():
                continue
            amt = _safe_float(p.get("position", p.get("available", p.get("positionAmt", p.get("size", 0)))))
            return {"ok": True, "symbol": symbol_bot, "exchange_symbol": tb_symbol, "position_amt": amt, "side": p.get("side"), "margin_type": p.get("marginType"), "leverage": p.get("leverage"), "raw": p}
        return {"ok": False, "symbol": symbol_bot, "exchange_symbol": tb_symbol, "position_amt": 0.0, "raw": rows}

    def _resolve_v2_order_side(self, side: str, reduce_only: bool = False) -> Dict[str, str]:
        """
        Convert internal side names to Toobit v2 order fields.
        v2 requires side=BUY/SELL and positionSide=LONG/SHORT.
        """
        side_u = str(side or "").upper()
        if side_u in {"BUY_OPEN", "LONG", "BUY"} and not reduce_only:
            return {"side": "BUY", "positionSide": "LONG"}
        if side_u in {"SELL_OPEN", "SHORT", "SELL"} and not reduce_only:
            return {"side": "SELL", "positionSide": "SHORT"}
        if side_u in {"SELL_CLOSE", "CLOSE_LONG"}:
            return {"side": "SELL", "positionSide": "LONG"}
        if side_u in {"BUY_CLOSE", "CLOSE_SHORT"}:
            return {"side": "BUY", "positionSide": "SHORT"}
        # Safe fallback: preserve previous BUY/SELL behavior for opens.
        if side_u == "BUY":
            return {"side": "BUY", "positionSide": "LONG"}
        if side_u == "SELL":
            return {"side": "SELL", "positionSide": "SHORT"}
        return {"side": side_u, "positionSide": ""}

    @safe(default={})
    def create_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        order_type: str = "MARKET",
        price: Optional[float] = None,
        reduce_only: bool = False,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Place a Toobit USDT-M futures order using official v2 fields.

        Important:
        - v2 order endpoint is /api/v2/futures/order.
        - side must be BUY/SELL, not BUY_OPEN/SELL_OPEN.
        - positionSide must be LONG/SHORT.
        - type supports MARKET directly; do NOT send priceType.
        - takeProfit/stopLoss are supported on the same order request.
        """
        symbol_bot = symbol.upper()
        tb_symbol = _toobit_symbol(symbol_bot)
        resolved = self._resolve_v2_order_side(side, reduce_only=reduce_only)
        v2_side = resolved.get("side", "")
        position_side = resolved.get("positionSide", "")
        typ = str(order_type or "MARKET").upper()
        if typ not in {"MARKET", "LIMIT"}:
            typ = "MARKET"

        params: Dict[str, Any] = {
            "symbol": tb_symbol,
            "side": v2_side,
            "positionSide": position_side,
            "type": typ,
            "quantity": str(quantity),
            "category": "USDT",
            "newClientOrderId": f"ai_{int(time.time()*1000)}",
        }
        if typ == "LIMIT":
            if price is None:
                return {"ok": False, "symbol": symbol_bot, "exchange_symbol": tb_symbol, "side": v2_side, "positionSide": position_side, "error": "limit_order_requires_price", "params": params}
            params["price"] = str(price)
            params["timeInForce"] = "GTC"

        if extra:
            # Official v2 order supports attached TP/SL on the same request.
            allowed_extra = {
                "takeProfit", "tpTriggerBy", "tpLimitPrice", "tpOrderType",
                "stopLoss", "slTriggerBy", "slLimitPrice", "slOrderType",
                "valueQuantity", "recvWindow",
            }
            for k, v in extra.items():
                if k in allowed_extra and v is not None and v != "":
                    params[k] = str(v)

        # For market TP/SL we should not send limit prices.
        if params.get("tpOrderType") == "MARKET":
            params.pop("tpLimitPrice", None)
        if params.get("slOrderType") == "MARKET":
            params.pop("slLimitPrice", None)

        res = self._request("POST", "/api/v2/futures/order", params=params, signed=True)
        if res.get("ok"):
            return {
                "ok": True,
                "symbol": symbol_bot,
                "exchange_symbol": tb_symbol,
                "side": v2_side,
                "positionSide": position_side,
                "raw": res.get("data"),
                "params": params,
            }
        err = str(res.get("error", ""))
        if "-1202" in err or "quantity too small" in err.lower():
            return {"ok": False, "symbol": symbol_bot, "exchange_symbol": tb_symbol, "side": v2_side, "positionSide": position_side, "error_code": -1202, "error": err, "raw": res, "params": params}
        return {"ok": False, "symbol": symbol_bot, "exchange_symbol": tb_symbol, "side": v2_side, "positionSide": position_side, "error": "create_order_failed", "raw": res, "params": params}

    @safe(default={})
    def set_trading_stop(self, symbol: str, direction: str, take_profit: Optional[float] = None, stop_loss: Optional[float] = None, quantity: Optional[float] = None) -> Dict[str, Any]:
        """
        Fallback only: create separate v2 STOP_PROFIT_LOSS orders.
        Primary protection should be attached in create_order().
        """
        symbol_bot = symbol.upper()
        tb_symbol = _toobit_symbol(symbol_bot)
        direction = str(direction).upper()
        if direction == "LONG":
            close_side = "SELL"
            position_side = "LONG"
        elif direction == "SHORT":
            close_side = "BUY"
            position_side = "SHORT"
        else:
            return {"ok": False, "symbol": symbol_bot, "error": "invalid_direction"}

        results = []
        ok = True
        base = {
            "symbol": tb_symbol,
            "side": close_side,
            "positionSide": position_side,
            "category": "USDT",
            "quantity": str(quantity) if quantity else "",
            "triggerBy": "CONTRACT_PRICE",
        }
        if take_profit:
            p = dict(base)
            p.update({
                "type": "STOP_PROFIT_LOSS_MARKET",
                "stopPrice": str(take_profit),
                "stopOrderType": "TAKE_PROFIT",
                "stopType": "FIXED_STOP",
                "newClientOrderId": f"tp_{int(time.time()*1000)}",
            })
            if not quantity:
                p.pop("quantity", None)
            r = self._request("POST", "/api/v2/futures/algo-order", params=p, signed=True)
            results.append({"kind": "TP", "ok": r.get("ok"), "raw": r, "params": p})
            ok = ok and bool(r.get("ok"))
        if stop_loss:
            p = dict(base)
            p.update({
                "type": "STOP_PROFIT_LOSS_MARKET",
                "stopPrice": str(stop_loss),
                "stopOrderType": "STOP_LOSS",
                "stopType": "FIXED_STOP",
                "newClientOrderId": f"sl_{int(time.time()*1000)}",
            })
            if not quantity:
                p.pop("quantity", None)
            r = self._request("POST", "/api/v2/futures/algo-order", params=p, signed=True)
            results.append({"kind": "SL", "ok": r.get("ok"), "raw": r, "params": p})
            ok = ok and bool(r.get("ok"))
        return {"ok": ok, "symbol": symbol_bot, "exchange_symbol": tb_symbol, "direction": direction, "results": results}


    @safe(default={})
    def cancel_order(self, symbol: str, order_id: str) -> Dict[str, Any]:
        return self._request("DELETE", "/api/v2/futures/order", params={"orderId": order_id, "category": "USDT"}, signed=True)

    @safe(default=[])
    def closed_pnl_history(self, symbol: str, start_time: Optional[int] = None, limit: int = 20) -> List[Dict[str, Any]]:
        params = {"symbol": _toobit_symbol(symbol), "limit": limit, "category": "USDT"}
        if start_time:
            params["startTime"] = start_time
        for path in ["/api/v1/futures/historyPositions", "/fapi/v1/income", "/api/v1/income"]:
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
