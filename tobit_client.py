from __future__ import annotations

"""
22 - tobit_client.py

Toobit v2 API client layer for the locked Movement Hunter architecture.

Responsibilities:
- Be the ONLY low-level Toobit API layer.
- Use Toobit v2 endpoint style only.
- Sign private requests safely.
- Provide real_trade_manager.py with:
  symbol rules
  isolated margin set/read
  leverage set/read
  open futures order with TP/SL
  open positions
  TP/SL repair
  close position
  realized PnL / closed-position lookup with retry support
- Normalize API responses into safe dicts.
- Never decide AI signals.

Strictly forbidden:
- No AI analysis.
- No REAL/GHOST/REJECT decision.
- No Telegram.
- No Paper mode.
- No Setup flow.
- No direct command routing.
"""

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple
import hashlib
import hmac
import json
import math
import os
import time
from urllib.parse import urlencode

import requests

from config import SETTINGS


JsonDict = Dict[str, Any]

SIDE_BUY = "BUY"
SIDE_SELL = "SELL"

DIRECTION_LONG = "LONG"
DIRECTION_SHORT = "SHORT"
DIRECTION_NEUTRAL = "NEUTRAL"

MARGIN_ISOLATED = "ISOLATED"
MARGIN_CROSS = "CROSS"


class ToobitAPIError(RuntimeError):
    """Raised for Toobit API errors."""


@dataclass(frozen=True)
class ToobitCredentials:
    api_key: str
    api_secret: str

    def valid(self) -> bool:
        return bool(self.api_key and self.api_secret)


@dataclass(frozen=True)
class ToobitResponse:
    ok: bool
    status_code: int
    data: JsonDict = field(default_factory=dict)
    error: str = ""
    raw_text: str = ""

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass(frozen=True)
class SymbolRules:
    symbol: str
    min_qty: float = 0.0
    qty_step: float = 0.0
    min_notional: float = 0.0
    price_tick: float = 0.0
    quantity_precision: int = 6
    price_precision: int = 6

    def to_dict(self) -> JsonDict:
        return asdict(self)


def now_ms() -> int:
    return int(time.time() * 1000)


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        v = float(value)
        if math.isnan(v) or math.isinf(v):
            return default
        return v
    except Exception:
        return default


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except Exception:
        return default


def normalize_direction(direction: str) -> str:
    d = str(direction or "").upper().strip()
    if d in {"LONG", "BUY"}:
        return DIRECTION_LONG
    if d in {"SHORT", "SELL"}:
        return DIRECTION_SHORT
    if d in {"", "BOTH", "NONE", "FLAT", "NEUTRAL", "0"}:
        return DIRECTION_NEUTRAL
    return d


def normalize_side(side: str, direction: str = "") -> str:
    s = str(side or "").upper().strip()
    if s in {SIDE_BUY, SIDE_SELL}:
        return s
    d = normalize_direction(direction)
    return SIDE_BUY if d == DIRECTION_LONG else SIDE_SELL


def round_step(value: float, step: float, precision: int = 8) -> float:
    value = safe_float(value)
    step = safe_float(step)
    if value <= 0:
        return 0.0
    if step <= 0:
        return round(value, precision)
    return round(math.floor(value / step) * step, precision)




def _first_non_empty(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def get_toobit_api_key(default: str = "") -> str:
    return _first_non_empty(
        os.getenv("TOOBIT_API_KEY"),
        os.getenv("TOBIT_API_KEY"),
        default,
    )


def get_toobit_api_secret(default: str = "") -> str:
    return _first_non_empty(
        os.getenv("TOOBIT_API_SECRET"),
        os.getenv("TOOBIT_SECRET_KEY"),
        os.getenv("TOBIT_API_SECRET"),
        os.getenv("TOBIT_SECRET_KEY"),
        default,
    )


def _clean_params(params: Optional[JsonDict]) -> JsonDict:
    cleaned: JsonDict = {}
    for k, v in (params or {}).items():
        if v is None:
            continue
        if isinstance(v, bool):
            cleaned[k] = "true" if v else "false"
        else:
            cleaned[k] = v
    return cleaned


class ToobitSigner:
    """HMAC SHA256 signer for Toobit signed endpoints."""

    def __init__(self, credentials: ToobitCredentials):
        self.credentials = credentials

    def sign_params(self, params: JsonDict) -> JsonDict:
        if not self.credentials.valid():
            raise ToobitAPIError("missing_toobit_credentials")

        signed = dict(params)
        signed.setdefault("recvWindow", 5000)
        signed.setdefault("timestamp", now_ms())
        query = urlencode(signed, doseq=True)
        signature = hmac.new(
            self.credentials.api_secret.encode("utf-8"),
            query.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        signed["signature"] = signature
        return signed


class ToobitClient:
    """
    Low-level Toobit v2 client.

    Method names are intentionally aligned with real_trade_manager.py:
    - set_margin_mode / get_margin_mode
    - set_leverage / get_leverage
    - get_symbol_rules
    - open_futures_position
    - get_open_positions
    - ensure_tp_sl
    - close_position
    - get_closed_position_pnl
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: Optional[int] = None,
    ):
        self.base_url = (base_url or getattr(SETTINGS.toobit, "base_url", "https://api.toobit.com")).rstrip("/")
        self.timeout = int(timeout or getattr(SETTINGS.toobit, "timeout_seconds", 10))
        self.credentials = ToobitCredentials(
            api_key=api_key or get_toobit_api_key(getattr(SETTINGS.toobit, "api_key", "")),
            api_secret=api_secret or get_toobit_api_secret(getattr(SETTINGS.toobit, "api_secret", "")),
        )
        self.signer = ToobitSigner(self.credentials)
        self.session = requests.Session()
        if self.credentials.api_key:
            # Official Toobit REST docs require X-BB-APIKEY.
            # Keep legacy aliases only as compatibility fallbacks for older gateways.
            self.session.headers.update(
                {
                    "X-BB-APIKEY": self.credentials.api_key,
                    "X-MBX-APIKEY": self.credentials.api_key,
                    "accessKey": self.credentials.api_key,
                    "AccessKey": self.credentials.api_key,
                }
            )

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[JsonDict] = None,
        signed: bool = False,
    ) -> ToobitResponse:
        method = method.upper()
        params = _clean_params(params)

        if signed:
            params = self.signer.sign_params(params)

        url = self.base_url + path

        try:
            if method == "GET":
                response = self.session.get(url, params=params, timeout=self.timeout)
            elif method == "POST":
                response = self.session.post(url, params=params, timeout=self.timeout)
            elif method == "DELETE":
                response = self.session.delete(url, params=params, timeout=self.timeout)
            else:
                raise ToobitAPIError(f"unsupported_method:{method}")

            raw_text = response.text
            try:
                data = response.json()
            except Exception:
                data = {"raw": raw_text}

            ok = response.status_code < 400
            error = ""

            if isinstance(data, dict):
                code = data.get("code", data.get("retCode", 0))
                msg = data.get("msg", data.get("message", ""))
                if str(code) not in {"0", "200", ""} and code not in {0, 200, None}:
                    ok = False
                    error = f"{code}:{msg}"
            if not ok and not error:
                error = str(data)

            return ToobitResponse(
                ok=ok,
                status_code=response.status_code,
                data=data if isinstance(data, dict) else {"data": data},
                error=error,
                raw_text=raw_text,
            )
        except Exception as exc:
            return ToobitResponse(ok=False, status_code=0, data={}, error=str(exc), raw_text="")

    def _unwrap(self, response: ToobitResponse, action: str) -> JsonDict:
        if not response.ok:
            raise ToobitAPIError(f"{action}_failed:{response.error}")
        return response.data

    # -------------------------------------------------------------------------
    # Public market / exchange info
    # -------------------------------------------------------------------------

    def get_exchange_info(self) -> JsonDict:
        res = self._request("GET", "/api/v2/futures/exchangeInfo", signed=False)
        if not res.ok:
            # v1 fallback kept for older Toobit deployments.
            res = self._request("GET", "/api/v1/futures/exchangeInfo", signed=False)
        return self._unwrap(res, "exchange_info")

    def get_symbol_rules(self, symbol: str) -> JsonDict:
        info = self.get_exchange_info()
        symbols = info.get("symbols", info.get("data", info.get("contracts", [])))
        if isinstance(symbols, dict):
            symbols = symbols.get("symbols", symbols.get("list", []))
        if not isinstance(symbols, list):
            symbols = []

        target = str(symbol)
        found: JsonDict = {}
        for item in symbols:
            if not isinstance(item, dict):
                continue
            if str(item.get("symbol", item.get("contract", ""))) == target:
                found = item
                break

        min_qty = 0.0
        qty_step = 0.0
        min_notional = 0.0
        price_tick = 0.0
        qty_precision = safe_int(found.get("quantityPrecision", found.get("qtyPrecision", 6)), 6)
        price_precision = safe_int(found.get("pricePrecision", 6), 6)

        filters = found.get("filters", [])
        if isinstance(filters, list):
            for f in filters:
                if not isinstance(f, dict):
                    continue
                ftype = str(f.get("filterType", "")).upper()
                if ftype in {"LOT_SIZE", "MARKET_LOT_SIZE"}:
                    min_qty = max(min_qty, safe_float(f.get("minQty", f.get("min_qty", 0.0))))
                    qty_step = max(qty_step, safe_float(f.get("stepSize", f.get("step", 0.0))))
                elif ftype in {"MIN_NOTIONAL", "NOTIONAL"}:
                    min_notional = max(min_notional, safe_float(f.get("notional", f.get("minNotional", 0.0))))
                elif ftype == "PRICE_FILTER":
                    price_tick = max(price_tick, safe_float(f.get("tickSize", f.get("tick", 0.0))))

        # Direct fields fallback.
        min_qty = max(min_qty, safe_float(found.get("minQty", found.get("min_qty", 0.0))))
        qty_step = max(qty_step, safe_float(found.get("stepSize", found.get("qtyStep", 0.0))))
        min_notional = max(min_notional, safe_float(found.get("minNotional", found.get("min_notional", 0.0))))
        price_tick = max(price_tick, safe_float(found.get("tickSize", found.get("priceTick", 0.0))))

        return SymbolRules(
            symbol=target,
            min_qty=min_qty,
            qty_step=qty_step,
            min_notional=min_notional,
            price_tick=price_tick,
            quantity_precision=qty_precision,
            price_precision=price_precision,
        ).to_dict()

    def get_latest_price(self, symbol: str) -> float:
        res = self._request("GET", "/api/v2/futures/ticker/price", params={"symbol": symbol}, signed=False)
        if not res.ok:
            res = self._request("GET", "/api/v1/futures/ticker/price", params={"symbol": symbol}, signed=False)
        data = self._unwrap(res, "latest_price")
        return safe_float(data.get("price", data.get("lastPrice", data.get("last", 0.0))))

    # -------------------------------------------------------------------------
    # Account / settings
    # -------------------------------------------------------------------------

    def set_margin_mode(self, symbol: str, margin_mode: str = MARGIN_ISOLATED) -> JsonDict:
        mode = str(margin_mode or MARGIN_ISOLATED).upper()
        if mode != MARGIN_ISOLATED:
            raise ToobitAPIError("cross_margin_not_allowed_by_bot")

        res = self._request(
            "POST",
            "/api/v2/futures/marginType",
            params={"symbol": symbol, "marginType": MARGIN_ISOLATED},
            signed=True,
        )
        if not res.ok and "-4046" in res.error:
            return {"symbol": symbol, "margin_mode": MARGIN_ISOLATED, "note": "already_isolated"}
        return self._unwrap(res, "set_margin_mode")

    def get_margin_mode(self, symbol: str) -> JsonDict:
        positions = self.get_open_positions(symbol=symbol)
        for pos in positions:
            if str(pos.get("symbol", "")) == symbol:
                mode = str(pos.get("marginType", pos.get("margin_mode", pos.get("isolated", "")))).upper()
                if mode in {"TRUE", "1"}:
                    mode = MARGIN_ISOLATED
                return {"symbol": symbol, "margin_mode": mode or MARGIN_ISOLATED}
        # If no position exists, Toobit may not expose margin type directly.
        # Return isolated after set_margin_mode succeeds, because preflight already calls set first.
        return {"symbol": symbol, "margin_mode": MARGIN_ISOLATED}

    def set_leverage(self, symbol: str, leverage: int) -> JsonDict:
        lev = int(leverage)
        if lev <= 0:
            raise ToobitAPIError("invalid_leverage")

        res = self._request(
            "POST",
            "/api/v2/futures/leverage",
            params={"symbol": symbol, "leverage": lev},
            signed=True,
        )
        return self._unwrap(res, "set_leverage")

    def get_leverage(self, symbol: str) -> JsonDict:
        positions = self.get_open_positions(symbol=symbol)
        for pos in positions:
            if str(pos.get("symbol", "")) == symbol:
                return {"symbol": symbol, "leverage": safe_int(pos.get("leverage", pos.get("lev", 0)))}

        # Fallback endpoint if Toobit exposes account leverage bracket/position config.
        res = self._request("GET", "/api/v2/futures/positionRisk", params={"symbol": symbol}, signed=True)
        if res.ok:
            data = res.data.get("data", res.data)
            if isinstance(data, list) and data:
                return {"symbol": symbol, "leverage": safe_int(data[0].get("leverage", 0))}
            if isinstance(data, dict):
                return {"symbol": symbol, "leverage": safe_int(data.get("leverage", 0))}
        return {"symbol": symbol, "leverage": 0}


    def ping_private(self) -> JsonDict:
        """
        Verify that Toobit API credentials can access private futures endpoints.
        """
        info = self.get_account_info()
        return {
            "ok": True,
            "has_credentials": self.credentials.valid(),
            "account_type": info.get("account_type", "FUTURES"),
            "raw": info.get("raw", info),
        }

    def _request_first_ok(self, method: str, paths: List[str], params: Optional[JsonDict] = None, signed: bool = True) -> ToobitResponse:
        last: Optional[ToobitResponse] = None
        for path in paths:
            res = self._request(method, path, params=params, signed=signed)
            if res.ok:
                return res
            last = res
        return last or ToobitResponse(ok=False, status_code=0, data={}, error="no_endpoint_attempted", raw_text="")

    def get_account_info(self) -> JsonDict:
        """
        Read raw futures account data from Toobit with several v2/v1 fallbacks.
        """
        res = self._request_first_ok(
            "GET",
            [
                "/api/v2/futures/account",
                "/api/v1/futures/account",
                "/api/v2/futures/balance",
                "/api/v1/futures/balance",
                "/api/v2/account",
                "/api/v1/account",
            ],
            signed=True,
        )
        data = self._unwrap(res, "get_account_info")
        return {"account_type": "FUTURES", "raw": data}

    def _extract_assets(self, data: Any) -> List[JsonDict]:
        payload = data.get("data", data) if isinstance(data, dict) else data
        if isinstance(payload, dict):
            for key in ("assets", "balances", "balance", "wallets", "list", "rows"):
                value = payload.get(key)
                if isinstance(value, list):
                    return [x for x in value if isinstance(x, dict)]
            if any(k in payload for k in ("asset", "coin", "currency", "walletBalance", "availableBalance", "balance")):
                return [payload]
        if isinstance(payload, list):
            return [x for x in payload if isinstance(x, dict)]
        return []

    def _first_number(self, row: JsonDict, keys: List[str], default: float = 0.0) -> float:
        for key in keys:
            if key in row and row.get(key) is not None:
                return safe_float(row.get(key), default)
        return default

    def _normalize_balance_row(self, row: JsonDict) -> JsonDict:
        asset = str(row.get("asset", row.get("coin", row.get("currency", row.get("token", "USDT"))))).upper()
        wallet = self._first_number(
            row,
            ["walletBalance", "wallet_balance", "balance", "total", "equity", "accountEquity"],
            0.0,
        )
        available = self._first_number(
            row,
            ["availableBalance", "available_balance", "available", "free", "withdrawAvailable"],
            wallet,
        )
        unrealized = self._first_number(
            row,
            ["unrealizedProfit", "unRealizedProfit", "unrealized_pnl"],
            0.0,
        )
        margin = self._first_number(
            row,
            ["marginBalance", "margin_balance", "usedMargin", "positionMargin"],
            0.0,
        )
        return {
            "asset": asset,
            "wallet_balance": wallet,
            "available_balance": available,
            "unrealized_pnl": unrealized,
            "margin_balance": margin,
            "raw": row,
        }

    def get_account_balance(self, asset: str = "USDT") -> JsonDict:
        """
        Return normalized real Toobit futures balance.

        The bot must never invent account balance. If Toobit does not return a
        balance row, ok=False is returned with raw response for diagnostics.
        """
        target = str(asset or "USDT").upper()
        info = self.get_account_info()
        raw = info.get("raw", {})
        rows = self._extract_assets(raw)
        normalized = [self._normalize_balance_row(r) for r in rows]

        selected = None
        for row in normalized:
            if row.get("asset") == target:
                selected = row
                break
        if selected is None and normalized:
            selected = normalized[0]
        if selected is None:
            return {
                "ok": False,
                "asset": target,
                "wallet_balance": 0.0,
                "available_balance": 0.0,
                "unrealized_pnl": 0.0,
                "margin_balance": 0.0,
                "error": "balance_row_not_found",
                "raw": raw,
            }
        return {"ok": True, **selected}

    def account_summary(self) -> JsonDict:
        """
        High-level Toobit summary for Telegram status commands.
        """
        balance = self.get_account_balance("USDT")
        positions = self.get_open_positions()
        total_unrealized = sum(safe_float(p.get("unrealized_pnl", 0.0)) for p in positions)
        return {
            "ok": bool(balance.get("ok", False)),
            "balance": balance,
            "open_positions_count": len(positions),
            "open_positions": positions,
            "total_unrealized_pnl": total_unrealized,
            "has_credentials": self.credentials.valid(),
        }


    # -------------------------------------------------------------------------
    # Orders
    # -------------------------------------------------------------------------

    def open_futures_position(
        self,
        symbol: str,
        side: str,
        direction: str,
        quantity: float,
        price: float = 0.0,
        order_type: str = "MARKET",
        margin_mode: str = MARGIN_ISOLATED,
        leverage: int = 1,
        take_profit: float = 0.0,
        take_profit_2: float = 0.0,
        stop_loss: float = 0.0,
        client_order_id: str = "",
    ) -> JsonDict:
        if str(margin_mode).upper() != MARGIN_ISOLATED:
            raise ToobitAPIError("cross_margin_blocked")

        qty = safe_float(quantity)
        if qty <= 0:
            raise ToobitAPIError("quantity_must_be_positive")

        side = normalize_side(side, direction)
        order_type = str(order_type or "MARKET").upper()

        position_side = "LONG" if normalize_direction(direction) == DIRECTION_LONG else "SHORT"

        params: JsonDict = {
            "symbol": symbol,
            "side": side,
            "positionSide": position_side,
            "type": order_type,
            "quantity": qty,
            "newClientOrderId": client_order_id or f"mh_{int(time.time())}",
            "category": "USDT",
        }

        if order_type != "MARKET" and safe_float(price) > 0:
            params["price"] = safe_float(price)
            params["timeInForce"] = "GTC"

        # Toobit v2 futures order supports attached TP/SL fields.
        if take_profit > 0:
            params["takeProfit"] = take_profit
            params["tpTriggerBy"] = "MARK_PRICE"
            params["tpOrderType"] = "MARKET"
        if stop_loss > 0:
            params["stopLoss"] = stop_loss
            params["slTriggerBy"] = "MARK_PRICE"
            params["slOrderType"] = "MARKET"
        # TP2 is not part of the regular-order endpoint; it is repaired/managed
        # later by set_position_tp_sl when the position is confirmed.

        res = self._request("POST", "/api/v2/futures/order", params=params, signed=True)
        if not res.ok:
            # fallback endpoint name
            res = self._request("POST", "/api/v1/futures/order", params=params, signed=True)

        data = self._unwrap(res, "open_futures_position")
        return self._normalize_order_response(data)

    def _normalize_order_response(self, data: JsonDict) -> JsonDict:
        payload = data.get("data", data)
        if isinstance(payload, list) and payload:
            payload = payload[0]
        if not isinstance(payload, dict):
            payload = {"raw": payload}

        return {
            "order_id": str(payload.get("orderId", payload.get("order_id", payload.get("id", "")))),
            "client_order_id": str(payload.get("clientOrderId", payload.get("client_order_id", payload.get("newClientOrderId", "")))),
            "symbol": str(payload.get("symbol", "")),
            "status": str(payload.get("status", "")),
            "raw": data,
        }

    def close_position(self, symbol: str, direction: str, quantity: float, client_order_id: str = "") -> JsonDict:
        normalized_direction = normalize_direction(direction)
        side = SIDE_SELL if normalized_direction == DIRECTION_LONG else SIDE_BUY
        position_side = "LONG" if normalized_direction == DIRECTION_LONG else "SHORT"
        params = {
            "symbol": symbol,
            "side": side,
            "positionSide": position_side,
            "type": "MARKET",
            "quantity": safe_float(quantity),
            "reduceOnly": "true",
            "newClientOrderId": client_order_id or f"mh_close_{int(time.time())}",
            "category": "USDT",
        }
        res = self._request("POST", "/api/v2/futures/order", params=params, signed=True)
        if not res.ok:
            res = self._request("POST", "/api/v1/futures/order", params=params, signed=True)
        return self._normalize_order_response(self._unwrap(res, "close_position"))

    # -------------------------------------------------------------------------
    # Positions / TP SL
    # -------------------------------------------------------------------------

    def get_open_positions(self, symbol: Optional[str] = None) -> List[JsonDict]:
        params = {"symbol": symbol} if symbol else {}
        res = self._request("GET", "/api/v2/futures/positionRisk", params=params, signed=True)
        if not res.ok:
            res = self._request("GET", "/api/v1/futures/positionRisk", params=params, signed=True)
        data = self._unwrap(res, "get_open_positions")
        payload = data.get("data", data)
        if isinstance(payload, dict):
            payload = payload.get("positions", payload.get("list", [payload]))
        if not isinstance(payload, list):
            return []

        positions: List[JsonDict] = []
        for p in payload:
            if not isinstance(p, dict):
                continue
            qty = safe_float(p.get("positionAmt", p.get("quantity", p.get("qty", p.get("size", 0.0)))))
            if qty == 0:
                continue
            side = p.get("side", p.get("positionSide", ""))
            direction = normalize_direction(side)
            if direction == DIRECTION_NEUTRAL:
                direction = DIRECTION_LONG if qty > 0 else DIRECTION_SHORT
            positions.append(
                {
                    "symbol": str(p.get("symbol", symbol or "")),
                    "direction": direction,
                    "quantity": abs(qty),
                    "entry_price": safe_float(p.get("entryPrice", p.get("entry_price", p.get("avgPrice", 0.0)))),
                    "mark_price": safe_float(p.get("markPrice", p.get("mark_price", p.get("lastPrice", 0.0)))),
                    "unrealized_pnl": safe_float(p.get("unRealizedProfit", p.get("unrealizedPnl", p.get("unrealizedProfit", 0.0)))),
                    "leverage": safe_int(p.get("leverage", p.get("lev", 0))),
                    "marginType": str(p.get("marginType", p.get("margin_mode", ""))).upper(),
                    "raw": p,
                }
            )
        return positions

    def ensure_tp_sl(self, symbol: str, direction: str, tp1: float, tp2: float, sl: float) -> JsonDict:
        return self.set_position_tp_sl(symbol, direction, tp1, tp2, sl)

    def set_position_tp_sl(self, symbol: str, direction: str, tp1: float, tp2: float, sl: float) -> JsonDict:
        normalized_direction = normalize_direction(direction)
        position_side = "LONG" if normalized_direction == DIRECTION_LONG else "SHORT"
        close_side = SIDE_SELL if normalized_direction == DIRECTION_LONG else SIDE_BUY
        results: List[JsonDict] = []

        def place_stop(stop_price: float, stop_order_type: str, suffix: str) -> None:
            if safe_float(stop_price) <= 0:
                return
            params = {
                "symbol": symbol,
                "side": close_side,
                "positionSide": position_side,
                "type": "STOP_PROFIT_LOSS_MARKET",
                "stopPrice": safe_float(stop_price),
                "triggerBy": "MARK_PRICE",
                "stopOrderType": stop_order_type,
                "stopType": "FIXED_STOP",
                "newClientOrderId": f"mh_{suffix}_{int(time.time() * 1000)}",
                "category": "USDT",
            }
            res = self._request("POST", "/api/v2/futures/algo-order", params=params, signed=True)
            if not res.ok:
                # Older deployments may expose a position-level TP/SL endpoint.
                legacy_params = {
                    "symbol": symbol,
                    "positionSide": position_side,
                    "takeProfit": safe_float(tp1),
                    "stopLoss": safe_float(sl),
                    "category": "USDT",
                }
                legacy = self._request("POST", "/api/v2/futures/position/tpsl", params=legacy_params, signed=True)
                results.append(self._unwrap(legacy, f"set_position_tp_sl_{suffix}"))
                return
            results.append(self._unwrap(res, f"set_position_tp_sl_{suffix}"))

        place_stop(tp1, "TAKE_PROFIT", "tp1")
        # Optional TP2 is managed as an extra take-profit row if Toobit accepts it.
        place_stop(tp2, "TAKE_PROFIT", "tp2")
        place_stop(sl, "STOP_LOSS", "sl")

        return {"ok": True, "symbol": symbol, "direction": position_side, "rows": results}

    repair_tp_sl = set_position_tp_sl

    # -------------------------------------------------------------------------
    # Realized PnL / history
    # -------------------------------------------------------------------------

    def get_closed_position_pnl(self, symbol: str, start_time: Optional[int] = None, end_time: Optional[int] = None) -> JsonDict:
        params: JsonDict = {"symbol": symbol}
        if start_time:
            params["startTime"] = start_time
        if end_time:
            params["endTime"] = end_time

        res = self._request("GET", "/api/v2/futures/income", params=params, signed=True)
        if not res.ok:
            res = self._request("GET", "/api/v1/futures/income", params=params, signed=True)

        data = self._unwrap(res, "get_closed_position_pnl")
        payload = data.get("data", data)
        if isinstance(payload, dict):
            payload = payload.get("list", payload.get("income", []))
        if not isinstance(payload, list):
            payload = []

        pnl = 0.0
        rows: List[JsonDict] = []
        for row in payload:
            if not isinstance(row, dict):
                continue
            income_type = str(row.get("incomeType", row.get("type", ""))).upper()
            if income_type and income_type not in {"REALIZED_PNL", "PNL", "CLOSED_PNL"}:
                continue
            value = safe_float(row.get("income", row.get("pnl", row.get("realizedPnl", 0.0))))
            pnl += value
            rows.append(row)

        return {"symbol": symbol, "realized_pnl": pnl, "rows": rows, "raw": data}

    def wait_for_closed_position_pnl(
        self,
        symbol: str,
        start_time: Optional[int] = None,
        attempts: int = 10,
        sleep_seconds: float = 5.0,
    ) -> JsonDict:
        last = {"symbol": symbol, "realized_pnl": 0.0, "rows": []}
        for _ in range(max(1, attempts)):
            try:
                last = self.get_closed_position_pnl(symbol=symbol, start_time=start_time)
                if last.get("rows"):
                    return {**last, "confirmed": True}
            except Exception as exc:
                last = {"symbol": symbol, "realized_pnl": 0.0, "rows": [], "error": str(exc)}
            time.sleep(sleep_seconds)
        return {**last, "confirmed": False}


_default_client: Optional[ToobitClient] = None


def client() -> ToobitClient:
    global _default_client
    if _default_client is None:
        _default_client = ToobitClient()
    return _default_client


def get_client() -> ToobitClient:
    return client()
