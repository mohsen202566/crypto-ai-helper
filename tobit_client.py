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

# Toobit USDT-M futures uses SWAP symbols on the working v1 endpoints.
# Market data can still come from OKX elsewhere; this client is only for real Toobit account/trade routes.
TOBIT_FUTURES_SYMBOL_MAP = {
    "SHIBUSDT": "1000SHIBUSDT",
    "PEPEUSDT": "1000PEPEUSDT",
    "BONKUSDT": "1000BONKUSDT",
    "FLOKIUSDT": "1000FLOKIUSDT",
}
TOBIT_REVERSE_SYMBOL_MAP = {v: k for k, v in TOBIT_FUTURES_SYMBOL_MAP.items()}


def normalize_toobit_plain_symbol(symbol: str) -> str:
    raw = str(symbol or "").upper().strip()
    if not raw:
        return raw
    raw = raw.replace("/", "").replace("_", "-")
    if raw.endswith("-SWAP-USDT"):
        plain = raw.replace("-SWAP-USDT", "USDT").replace("-", "")
    elif raw.endswith("-SWAP-USDC"):
        plain = raw.replace("-SWAP-USDC", "USDC").replace("-", "")
    else:
        plain = raw.replace("-", "").replace("SWAP", "")
    return TOBIT_FUTURES_SYMBOL_MAP.get(plain, plain)


def normalize_bot_plain_symbol(symbol: str) -> str:
    plain = normalize_toobit_plain_symbol(symbol)
    return TOBIT_REVERSE_SYMBOL_MAP.get(plain, plain)


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
        self._leverage_cache: Dict[str, Dict[str, Any]] = {}
        self._margin_mode_cache: Dict[str, Dict[str, Any]] = {}
        if self.credentials.api_key:
            # Working Toobit futures v1 routes require X-BB-APIKEY.
            # X-MBX/accessKey are kept only as harmless compatibility aliases.
            self.session.headers.update(
                {
                    "X-BB-APIKEY": self.credentials.api_key,
                    "X-MBX-APIKEY": self.credentials.api_key,
                    "accessKey": self.credentials.api_key,
                    "AccessKey": self.credentials.api_key,
                    "User-Agent": "crypto-ai-helper/1.0",
                    "Content-Type": "application/x-www-form-urlencoded",
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
                # Toobit futures v1 accepts signed form data. Sending signed params
                # in the query string caused account/order route failures on the VPS.
                response = self.session.post(url, data=params, timeout=self.timeout)
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


    def normalize_futures_symbol(self, symbol: str) -> str:
        raw = str(symbol or "").upper().strip()
        if not raw:
            return raw
        plain = normalize_toobit_plain_symbol(raw)
        if plain.endswith("USDT"):
            return f"{plain[:-4]}-SWAP-USDT"
        if plain.endswith("USDC"):
            return f"{plain[:-4]}-SWAP-USDC"
        return plain

    def _symbol_candidates(self, symbol: str) -> List[str]:
        raw = str(symbol or "").upper().strip()
        if not raw:
            return []
        toobit_plain = normalize_toobit_plain_symbol(raw)
        bot_plain = normalize_bot_plain_symbol(raw)
        normalized = self.normalize_futures_symbol(raw)
        out: List[str] = []
        for item in (normalized, toobit_plain, bot_plain, raw.replace("/", "").replace("_", "").replace("-", "").replace("SWAP", ""), raw):
            item = str(item or "").upper().strip()
            if item and item not in out:
                out.append(item)
        return out

    def _cache_get(self, cache: Dict[str, Dict[str, Any]], key: str, max_age_sec: int) -> Any:
        item = cache.get(str(key))
        if not isinstance(item, dict):
            return None
        age = time.time() - safe_float(item.get("ts"), 0.0)
        if 0 <= age <= max_age_sec:
            return item.get("value")
        return None

    def _cache_set(self, cache: Dict[str, Dict[str, Any]], key: str, value: Any) -> None:
        cache[str(key)] = {"value": value, "ts": time.time()}

    def _extract_leverage_value(self, data: Any) -> int:
        def walk(value: Any):
            if isinstance(value, dict):
                yield value
                for v in value.values():
                    yield from walk(v)
            elif isinstance(value, list):
                for item in value:
                    yield from walk(item)
        for item in walk(data):
            for key in ("leverage", "lever", "leverageValue", "longLeverage", "shortLeverage", "lev"):
                if key in item:
                    value = safe_int(item.get(key), 0)
                    if value > 0:
                        return value
        return 0

    def _flatten_items(self, payload: Any) -> List[JsonDict]:
        payload = payload.get("data", payload) if isinstance(payload, dict) else payload
        out: List[JsonDict] = []
        def walk(value: Any) -> None:
            if isinstance(value, dict):
                if any(k in value for k in ("symbol", "contractCode", "positionAmt", "positionSize", "size", "qty", "quantity", "availablePosition", "holdVol", "leverage", "side", "positionSide")):
                    out.append(value)
                for v in value.values():
                    if isinstance(v, (dict, list)):
                        walk(v)
            elif isinstance(value, list):
                for item in value:
                    walk(item)
        walk(payload)
        if not out and isinstance(payload, dict):
            out.append(payload)
        return out

    def _position_qty(self, item: JsonDict) -> float:
        for key in ("positionAmt", "positionSize", "size", "qty", "quantity", "positionQuantity", "availablePosition", "totalPosition", "holdVol", "holdVolume", "volume", "position"):
            qty = safe_float(item.get(key), 0.0)
            if qty != 0:
                return abs(qty)
        return 0.0

    def _position_symbol_matches(self, item: JsonDict, symbol: str) -> bool:
        wanted = set(self._symbol_candidates(symbol))
        fields = ("symbol", "contractCode", "instrument", "instId", "pair", "symbolName", "contract", "contractName")
        present = False
        for key in fields:
            value = item.get(key)
            if value is None or str(value).strip() == "":
                continue
            present = True
            if set(self._symbol_candidates(str(value))) & wanted:
                return True
        return not present

    # -------------------------------------------------------------------------
    # Public market / exchange info
    # -------------------------------------------------------------------------

    def get_exchange_info(self) -> JsonDict:
        # Working Toobit account used v1 public metadata endpoints.
        for path in ("/api/v1/futures/exchangeInfo", "/api/v1/futures/symbols", "/api/v1/exchangeInfo"):
            res = self._request("GET", path, signed=False)
            if res.ok:
                return res.data
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
        normalized = self.normalize_futures_symbol(symbol)
        self._cache_set(self._margin_mode_cache, normalized, MARGIN_ISOLATED)
        # Do not spam Toobit margin-mode endpoints: the working legacy client used
        # manual/global isolated mode because Toobit margin read/set routes were unstable.
        return {"symbol": normalized, "margin_mode": MARGIN_ISOLATED, "source": "manual_global_isolated_no_api_call"}

    def get_margin_mode(self, symbol: str) -> JsonDict:
        normalized = self.normalize_futures_symbol(symbol)
        cached = self._cache_get(self._margin_mode_cache, normalized, 3600)
        if cached == MARGIN_ISOLATED:
            return {"symbol": normalized, "margin_mode": MARGIN_ISOLATED, "source": "recent_margin_mode_cache"}
        self._cache_set(self._margin_mode_cache, normalized, MARGIN_ISOLATED)
        return {"symbol": normalized, "margin_mode": MARGIN_ISOLATED, "source": "manual_global_isolated_no_api_read"}

    def set_leverage(self, symbol: str, leverage: int) -> JsonDict:
        normalized = self.normalize_futures_symbol(symbol)
        lev = int(leverage)
        if lev <= 0:
            raise ToobitAPIError("invalid_leverage")
        cached = self._cache_get(self._leverage_cache, normalized, 600)
        if safe_int(cached, 0) == lev:
            return {"symbol": normalized, "leverage": lev, "source": "recent_leverage_cache"}
        res = self._request("POST", "/api/v1/futures/leverage", params={"symbol": normalized, "leverage": lev}, signed=True)
        if not res.ok:
            raise ToobitAPIError(f"set_leverage_failed:{res.error}")
        self._cache_set(self._leverage_cache, normalized, lev)
        data = res.data.get("data", res.data) if isinstance(res.data, dict) else res.data
        return {"symbol": normalized, "leverage": lev, "data": data, "source": "/api/v1/futures/leverage"}

    def get_leverage(self, symbol: str) -> JsonDict:
        normalized = self.normalize_futures_symbol(symbol)
        cached = self._cache_get(self._leverage_cache, normalized, 600)
        if safe_int(cached, 0) > 0:
            return {"symbol": normalized, "leverage": safe_int(cached), "source": "recent_leverage_cache"}
        try:
            positions = self.get_open_positions(symbol=normalized)
            for pos in positions:
                if self._position_symbol_matches(pos.get("raw", pos), normalized):
                    lev = safe_int(pos.get("leverage"), 0)
                    if lev > 0:
                        self._cache_set(self._leverage_cache, normalized, lev)
                        return {"symbol": normalized, "leverage": lev, "source": "positions"}
        except Exception:
            pass
        return {"symbol": normalized, "leverage": 0, "source": "not_exposed_without_open_position"}

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
        # The working account route is v1 futures balance. v2/account and
        # positionRisk are Binance-like and returned 404 on the VPS.
        res = self._request("GET", "/api/v1/futures/balance", signed=True)
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

        normalized_direction = normalize_direction(direction)
        if normalized_direction == DIRECTION_LONG:
            toobit_side = "BUY_OPEN"
        elif normalized_direction == DIRECTION_SHORT:
            toobit_side = "SELL_OPEN"
        else:
            raise ToobitAPIError("invalid_direction")

        normalized_symbol = self.normalize_futures_symbol(symbol)
        params: JsonDict = {
            "symbol": normalized_symbol,
            "side": toobit_side,
            "type": "LIMIT",
            "priceType": "MARKET",
            "quantity": qty,
            "newClientOrderId": client_order_id or f"mh_{int(time.time() * 1000)}",
        }
        if safe_float(take_profit) > 0:
            params["takeProfit"] = safe_float(take_profit)
            params["tpOrderType"] = "MARKET"
        if safe_float(stop_loss) > 0:
            params["stopLoss"] = safe_float(stop_loss)
            params["slOrderType"] = "MARKET"
        # Toobit regular order endpoint does not reliably support a second TP.
        # TP2 is managed later by monitor/repair if supported.

        res = self._request("POST", "/api/v1/futures/order", params=params, signed=True)
        data = self._unwrap(res, "open_futures_position")
        normalized = self._normalize_order_response(data)
        normalized.setdefault("symbol", normalized_symbol)
        normalized.setdefault("side", toobit_side)
        normalized["normalized_params"] = params
        return normalized

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
        if normalized_direction == DIRECTION_LONG:
            toobit_side = "SELL_CLOSE"
        elif normalized_direction == DIRECTION_SHORT:
            toobit_side = "BUY_CLOSE"
        else:
            raise ToobitAPIError("invalid_direction")
        normalized_symbol = self.normalize_futures_symbol(symbol)
        params = {
            "symbol": normalized_symbol,
            "side": toobit_side,
            "type": "LIMIT",
            "priceType": "MARKET",
            "quantity": safe_float(quantity),
            "newClientOrderId": client_order_id or f"mh_close_{int(time.time() * 1000)}",
        }
        res = self._request("POST", "/api/v1/futures/order", params=params, signed=True)
        return self._normalize_order_response(self._unwrap(res, "close_position"))

    def get_open_positions(self, symbol: Optional[str] = None) -> List[JsonDict]:
        normalized_symbol = self.normalize_futures_symbol(symbol) if symbol else None
        params = {"symbol": normalized_symbol} if normalized_symbol else {}
        res = self._request("GET", "/api/v1/futures/positions", params=params, signed=True)
        data = self._unwrap(res, "get_open_positions")
        items = self._flatten_items(data)

        positions: List[JsonDict] = []
        for p in items:
            if not isinstance(p, dict):
                continue
            qty = self._position_qty(p)
            if qty <= 0:
                continue
            if normalized_symbol and not self._position_symbol_matches(p, normalized_symbol):
                continue
            raw_side = str(p.get("side", p.get("positionSide", p.get("direction", p.get("holdSide", p.get("tradeSide", "")))))).upper()
            direction = normalize_direction(raw_side)
            if direction == DIRECTION_NEUTRAL:
                if "SHORT" in raw_side or "SELL" in raw_side or "空" in raw_side:
                    direction = DIRECTION_SHORT
                else:
                    direction = DIRECTION_LONG
            sym = str(p.get("symbol", p.get("contractCode", p.get("instId", normalized_symbol or ""))))
            positions.append(
                {
                    "symbol": sym or normalized_symbol or "",
                    "direction": direction,
                    "quantity": qty,
                    "entry_price": safe_float(p.get("entryPrice", p.get("entry_price", p.get("avgPrice", p.get("openPrice", p.get("positionAvgPrice", 0.0)))))),
                    "mark_price": safe_float(p.get("markPrice", p.get("mark_price", p.get("lastPrice", p.get("price", 0.0))))),
                    "unrealized_pnl": safe_float(p.get("unRealizedProfit", p.get("unrealizedPnl", p.get("unrealizedProfit", p.get("pnl", 0.0))))),
                    "leverage": safe_int(p.get("leverage", p.get("lev", p.get("lever", 0))), 0),
                    "marginType": str(p.get("marginType", p.get("marginMode", p.get("margin_mode", MARGIN_ISOLATED)))).upper() or MARGIN_ISOLATED,
                    "raw": p,
                }
            )
        return positions

    def ensure_tp_sl(self, symbol: str, direction: str, tp1: float, tp2: float, sl: float) -> JsonDict:
        return self.set_position_tp_sl(symbol, direction, tp1, tp2, sl)

    def set_position_tp_sl(self, symbol: str, direction: str, tp1: float, tp2: float, sl: float) -> JsonDict:
        # Primary TP/SL is attached at order opening on this Toobit account.
        # Keep a conservative v1 repair fallback; if unsupported, return the
        # exchange error instead of pretending success.
        normalized_symbol = self.normalize_futures_symbol(symbol)
        normalized_direction = normalize_direction(direction)
        close_side = "SELL_CLOSE" if normalized_direction == DIRECTION_LONG else "BUY_CLOSE"
        params = {
            "symbol": normalized_symbol,
            "side": close_side,
            "takeProfit": safe_float(tp1),
            "stopLoss": safe_float(sl),
            "tpOrderType": "MARKET",
            "slOrderType": "MARKET",
        }
        res = self._request("POST", "/api/v1/futures/position/tpsl", params=params, signed=True)
        if res.ok:
            return {"ok": True, "symbol": normalized_symbol, "direction": normalized_direction, "raw": res.data}
        return {"ok": False, "symbol": normalized_symbol, "direction": normalized_direction, "error": res.error, "raw": res.data}

    def get_closed_position_pnl(self, symbol: str, start_time: Optional[int] = None, end_time: Optional[int] = None) -> JsonDict:
        normalized_symbol = self.normalize_futures_symbol(symbol)
        params: JsonDict = {"symbol": normalized_symbol}
        if start_time:
            params["startTime"] = start_time
        if end_time:
            params["endTime"] = end_time

        last: Optional[ToobitResponse] = None
        for path in ("/api/v1/futures/income", "/api/v1/futures/incomeHistory", "/api/v1/futures/closedPositions", "/api/v1/futures/positionHistory"):
            res = self._request("GET", path, params=params, signed=True)
            if res.ok:
                data = res.data
                payload = data.get("data", data) if isinstance(data, dict) else data
                if isinstance(payload, dict):
                    payload = payload.get("list", payload.get("rows", payload.get("income", [payload])))
                if not isinstance(payload, list):
                    payload = []
                pnl = 0.0
                rows: List[JsonDict] = []
                for row in payload:
                    if not isinstance(row, dict):
                        continue
                    value = safe_float(row.get("realizedPnl", row.get("closedPnl", row.get("pnl", row.get("income", row.get("profit", 0.0))))))
                    pnl += value
                    rows.append(row)
                return {"symbol": normalized_symbol, "realized_pnl": pnl, "rows": rows, "raw": data, "source": path}
            last = res
        raise ToobitAPIError(f"get_closed_position_pnl_failed:{last.error if last else 'no_endpoint_attempted'}")

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



# Backward-compatible aliases used by older managers/scripts.
ToBitClient = ToobitClient
toobit_client = ToobitClient


_default_client: Optional[ToobitClient] = None


def client() -> ToobitClient:
    global _default_client
    if _default_client is None:
        _default_client = ToobitClient()
    return _default_client


def get_client() -> ToobitClient:
    return client()
