"""کلاینت Toobit Spot-only.

این فایل عمداً فقط برای Spot ساخته شده است:
- خرید Spot
- فروش Limit Spot
- موجودی Spot
- سفارش‌های باز Spot
- تاریخچه سفارش‌های Spot

هیچ منطق شورت، لوریج، مارجین یا استاپ داخل این فایل وجود ندارد.
"""
from __future__ import annotations

import hashlib
import hmac
import time
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

import requests

import config
from utils import (
    decimal_round_down,
    decimal_to_api,
    extract_filter,
    logger,
    round_price_to_tick,
    safe_float,
    safe_int,
)


class ToobitError(RuntimeError):
    pass


class ToobitClient:
    def __init__(self, base_url: str = config.TOOBIT_BASE_URL, timeout: int = config.REQUEST_TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.api_key = config.TOOBIT_API_KEY
        self.api_secret = config.TOOBIT_API_SECRET
        self.session = requests.Session()

        self.path_exchange_info = config.TOOBIT_SPOT_PATH_EXCHANGE_INFO
        self.path_balance = config.TOOBIT_SPOT_PATH_BALANCE
        self.path_order = config.TOOBIT_SPOT_PATH_ORDER
        self.path_open_orders = config.TOOBIT_SPOT_PATH_OPEN_ORDERS
        self.path_order_history = config.TOOBIT_SPOT_PATH_ORDER_HISTORY
        self.path_order_history_alt = config.TOOBIT_SPOT_PATH_ORDER_HISTORY_ALT

    @property
    def has_credentials(self) -> bool:
        return bool(self.api_key and self.api_secret)

    def _sign(self, params: dict[str, Any]) -> str:
        query = urlencode(params, doseq=True)
        return hmac.new(self.api_secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()

    def _request(self, method: str, path: str, params: dict[str, Any] | None = None, signed: bool = False) -> Any:
        params = dict(params or {})
        headers: dict[str, str] = {}
        if signed:
            if not self.has_credentials:
                raise ToobitError("کلید API توبیت تنظیم نشده است")
            params.setdefault("timestamp", int(time.time() * 1000))
            params.setdefault("recvWindow", config.RECV_WINDOW)
            params["signature"] = self._sign(params)
            headers["X-BB-APIKEY"] = self.api_key

        url = f"{self.base_url}{path}"
        try:
            method = method.upper()
            if method == "GET":
                response = self.session.get(url, params=params, headers=headers, timeout=self.timeout)
            elif method == "POST":
                response = self.session.post(url, data=params, headers=headers, timeout=self.timeout)
            elif method == "DELETE":
                response = self.session.delete(url, data=params, headers=headers, timeout=self.timeout)
            else:
                raise ToobitError(f"متد پشتیبانی نمی‌شود: {method}")
            if response.status_code >= 400:
                raise ToobitError(f"HTTP {response.status_code}: {response.text[:500]}")
            payload = response.json()
        except Exception as exc:
            if isinstance(exc, ToobitError):
                raise
            raise ToobitError(f"خطا در ارتباط با Toobit: {exc}") from exc

        if isinstance(payload, dict):
            code = payload.get("code") or payload.get("retCode") or payload.get("status")
            if code not in (None, 0, 200, "0", "200", "OK", "ok", "success", "SUCCESS", True):
                raise ToobitError(f"پاسخ ناموفق Toobit: {payload.get('msg') or payload.get('message') or payload.get('error') or payload}")
        return payload

    # -----------------------------
    # استخراج پاسخ
    # -----------------------------
    @staticmethod
    def _extract_dicts(payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if not isinstance(payload, dict):
            return []
        result: list[dict[str, Any]] = []
        for key in ("data", "result", "balances", "assets", "rows", "list", "orders"):
            value = payload.get(key)
            if isinstance(value, dict):
                result.append(value)
                result.extend(ToobitClient._extract_dicts(value))
            elif isinstance(value, list):
                result.extend(item for item in value if isinstance(item, dict))
        if not result:
            result.append(payload)
        return result

    @staticmethod
    def _symbol_from_item(item: dict[str, Any]) -> str:
        return str(item.get("symbol") or item.get("symbolId") or item.get("symbolName") or item.get("s") or "").upper()

    @staticmethod
    def _extract_order_id(payload: Any) -> str | None:
        for item in ToobitClient._extract_dicts(payload):
            for key in ("orderId", "order_id", "id", "clientOrderId", "newClientOrderId"):
                value = item.get(key)
                if value not in (None, ""):
                    return str(value)
        return None

    @staticmethod
    def _order_status(item: dict[str, Any]) -> str:
        return str(item.get("status") or item.get("orderStatus") or item.get("state") or "").upper()

    @staticmethod
    def _is_filled(item: dict[str, Any]) -> bool:
        status = ToobitClient._order_status(item)
        if status in {"FILLED", "ORDER_FILLED", "DONE", "CLOSED", "SUCCESS"}:
            return True
        qty = safe_float(item.get("executedQty") or item.get("filledQty") or item.get("dealQuantity") or item.get("cumQty"))
        orig = safe_float(item.get("origQty") or item.get("quantity") or item.get("qty"))
        return qty > 0 and orig > 0 and qty >= orig * 0.999

    @staticmethod
    def _filled_qty(item: dict[str, Any]) -> float:
        return safe_float(
            item.get("executedQty") or item.get("filledQty") or item.get("dealQuantity") or
            item.get("cumQty") or item.get("quantity") or item.get("qty")
        )

    @staticmethod
    def _avg_price(item: dict[str, Any]) -> float:
        price = safe_float(
            item.get("avgPrice") or item.get("averagePrice") or item.get("executedPrice") or
            item.get("price") or item.get("dealPrice")
        )
        if price > 0:
            return price
        qty = ToobitClient._filled_qty(item)
        quote = safe_float(item.get("cummulativeQuoteQty") or item.get("cumQuote") or item.get("dealAmount") or item.get("quoteQty"))
        if qty > 0 and quote > 0:
            return quote / qty
        return 0.0

    @staticmethod
    def _fee_usdt(item: dict[str, Any], fallback_value_usdt: float = 0.0, fallback_fee_pct: float = 0.0) -> float:
        fee = safe_float(
            item.get("fee") or item.get("commission") or item.get("tradeFee") or
            item.get("feeAmount") or item.get("execFee")
        )
        if fee > 0:
            return fee
        return float(fallback_value_usdt) * float(fallback_fee_pct) / 100.0

    # -----------------------------
    # نمادها و قوانین Spot
    # -----------------------------
    def get_spot_exchange_info(self) -> dict[str, Any]:
        payload = self._request("GET", self.path_exchange_info, signed=False)
        return payload if isinstance(payload, dict) else {"data": payload}

    def get_spot_symbols(self) -> dict[str, dict[str, Any]]:
        payload = self.get_spot_exchange_info()
        raw_symbols: list[Any] = []
        if isinstance(payload.get("symbols"), list):
            raw_symbols = payload["symbols"]
        elif isinstance(payload.get("data"), dict) and isinstance(payload["data"].get("symbols"), list):
            raw_symbols = payload["data"]["symbols"]
        elif isinstance(payload.get("data"), list):
            raw_symbols = payload["data"]
        elif isinstance(payload.get("result"), list):
            raw_symbols = payload["result"]

        result: dict[str, dict[str, Any]] = {}
        for item in raw_symbols:
            if not isinstance(item, dict):
                continue
            for name in (item.get("symbol"), item.get("symbolId"), item.get("symbolName"), item.get("s")):
                if name:
                    result[str(name).upper()] = item
        return result

    def validate_spot_symbol(self, symbol: str, exchange_symbols: dict[str, dict[str, Any]] | None = None) -> tuple[str, dict[str, Any]]:
        symbols = exchange_symbols or self.get_spot_symbols()
        candidates = [symbol.upper(), symbol.replace("/", "").replace("-", "").upper()]
        if "USDT" not in candidates[-1]:
            candidates.append(f"{candidates[-1]}USDT")
        for candidate in candidates:
            if candidate in symbols:
                return candidate, symbols[candidate]
        raise ToobitError(f"نماد Spot {symbol} در Toobit پیدا نشد")

    def get_symbol_rules(self, symbol: str, symbol_info: dict[str, Any] | None = None) -> tuple[str, str, float, float]:
        info = symbol_info or {}
        lot = extract_filter(info, "LOT_SIZE")
        price_filter = extract_filter(info, "PRICE_FILTER")
        step = str(lot.get("stepSize") or lot.get("quantityStep") or lot.get("qtyStep") or info.get("quantityStep") or "0.000001")
        tick = str(price_filter.get("tickSize") or info.get("tickSize") or info.get("priceTick") or "0.000001")
        min_qty = safe_float(lot.get("minQty") or info.get("minQty") or info.get("minQuantity"), 0.0)
        min_notional = safe_float(info.get("minNotional") or info.get("minOrderValue") or info.get("minQuoteAmount"), 0.0)
        return step, tick, min_qty, min_notional

    # -----------------------------
    # موجودی Spot
    # -----------------------------
    def get_spot_balances(self) -> list[dict[str, Any]]:
        payload = self._request("GET", self.path_balance, signed=True)
        return self._extract_dicts(payload)

    def get_asset_balance(self, asset: str) -> dict[str, float]:
        target = asset.upper()
        for item in self.get_spot_balances():
            coin = str(item.get("asset") or item.get("coin") or item.get("currency") or item.get("token") or "").upper()
            if coin != target:
                continue
            free = safe_float(item.get("free") or item.get("available") or item.get("availableBalance"))
            locked = safe_float(item.get("locked") or item.get("freeze") or item.get("frozen") or item.get("orderMargin"))
            total = safe_float(item.get("total") or item.get("balance") or item.get("walletBalance"), free + locked)
            return {"free": free, "locked": locked, "total": total}
        return {"free": 0.0, "locked": 0.0, "total": 0.0}

    def get_spot_usdt_balance(self) -> dict[str, float]:
        return self.get_asset_balance("USDT")

    # -----------------------------
    # سفارش‌های Spot
    # -----------------------------
    def get_spot_open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if symbol:
            params["symbol"] = symbol.upper()
        payload = self._request("GET", self.path_open_orders, params=params, signed=True)
        out: list[dict[str, Any]] = []
        for item in self._extract_dicts(payload):
            status = self._order_status(item)
            if status in {"FILLED", "ORDER_FILLED", "CANCELED", "CANCELLED", "REJECTED", "EXPIRED", "DONE", "CLOSED"}:
                continue
            if symbol and self._symbol_from_item(item) not in ("", symbol.upper()):
                continue
            out.append(item)
        return out

    def get_spot_order(self, symbol: str, order_id: str | None = None, client_order_id: str | None = None) -> dict[str, Any] | None:
        params: dict[str, Any] = {"symbol": symbol.upper()}
        if order_id:
            params["orderId"] = order_id
        if client_order_id:
            params["origClientOrderId"] = client_order_id
        try:
            payload = self._request("GET", self.path_order, params=params, signed=True)
            for item in self._extract_dicts(payload):
                return item
        except Exception as exc:
            logger.warning("خواندن سفارش Spot ناموفق بود %s %s: %s", symbol, order_id, exc)
        return None

    def get_spot_order_history(self, symbol: str | None = None, start_ms: int | None = None, end_ms: int | None = None, limit: int = 100) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": max(1, min(int(limit), 1000))}
        if symbol:
            params["symbol"] = symbol.upper()
        if start_ms is not None:
            params["startTime"] = int(start_ms)
        if end_ms is not None:
            params["endTime"] = int(end_ms)

        rows: list[dict[str, Any]] = []
        last_error: Exception | None = None
        for path in (self.path_order_history, self.path_order_history_alt):
            if not path:
                continue
            try:
                payload = self._request("GET", path, params=params, signed=True)
                for item in self._extract_dicts(payload):
                    if symbol and self._symbol_from_item(item) not in ("", symbol.upper()):
                        continue
                    rows.append(item)
                if rows:
                    return rows
            except Exception as exc:
                last_error = exc
                logger.warning("خواندن order history اسپات توبیت از %s ناموفق بود: %s", path, exc)
        if last_error and not rows:
            return []
        return rows

    def place_spot_market_buy(self, symbol: str, quote_amount_usdt: float, client_order_id: str) -> dict[str, Any]:
        params = {
            "symbol": symbol.upper(),
            "side": "BUY",
            "type": "MARKET",
            "quoteOrderQty": decimal_to_api(quote_amount_usdt),
            "newClientOrderId": client_order_id,
        }
        raw = self._request("POST", self.path_order, params=params, signed=True)
        return {"order_id": self._extract_order_id(raw), "raw": raw if isinstance(raw, dict) else {"response": raw}}

    def place_spot_limit_sell(self, symbol: str, quantity: float, price: float, client_order_id: str, symbol_info: dict[str, Any] | None = None) -> dict[str, Any]:
        step, tick, min_qty, _ = self.get_symbol_rules(symbol, symbol_info)
        qty_str = decimal_round_down(quantity, step=step, digits=8)
        price_float = round_price_to_tick(price, tick, direction="up")
        params = {
            "symbol": symbol.upper(),
            "side": "SELL",
            "type": "LIMIT",
            "timeInForce": "GTC",
            "quantity": qty_str,
            "price": decimal_to_api(price_float),
            "newClientOrderId": client_order_id,
        }
        raw = self._request("POST", self.path_order, params=params, signed=True)
        return {
            "order_id": self._extract_order_id(raw),
            "quantity": safe_float(qty_str),
            "price": price_float,
            "raw": raw if isinstance(raw, dict) else {"response": raw},
        }

    def cancel_spot_order(self, symbol: str, order_id: str | None = None, client_order_id: str | None = None) -> Any:
        params: dict[str, Any] = {"symbol": symbol.upper()}
        if order_id:
            params["orderId"] = order_id
        if client_order_id:
            params["origClientOrderId"] = client_order_id
        return self._request("DELETE", self.path_order, params=params, signed=True)

    def wait_spot_order_fill(self, symbol: str, order_id: str | None, timeout_seconds: int, poll_seconds: int = 5) -> dict[str, Any] | None:
        end_time = time.time() + max(1, int(timeout_seconds))
        last_item: dict[str, Any] | None = None
        while time.time() <= end_time:
            if order_id:
                item = self.get_spot_order(symbol, order_id=order_id)
                if item:
                    last_item = item
                    if self._is_filled(item) or self._filled_qty(item) > 0:
                        return item
            rows = self.get_spot_order_history(symbol=symbol, start_ms=int((time.time() - 3600) * 1000), limit=50)
            for item in rows:
                if order_id and str(item.get("orderId") or item.get("id") or "") != str(order_id):
                    continue
                last_item = item
                if self._is_filled(item) or self._filled_qty(item) > 0:
                    return item
            time.sleep(max(1, int(poll_seconds)))
        return last_item

    def find_filled_order(self, symbol: str, order_id: str | None, side: str, start_ms: int | None = None, end_ms: int | None = None) -> dict[str, Any] | None:
        rows = self.get_spot_order_history(symbol=symbol, start_ms=start_ms, end_ms=end_ms, limit=200)
        side_u = side.upper()
        for item in rows:
            item_side = str(item.get("side") or "").upper()
            if item_side and item_side != side_u:
                continue
            if order_id and str(item.get("orderId") or item.get("id") or "") != str(order_id):
                continue
            if self._is_filled(item) or self._filled_qty(item) > 0:
                return item
        return None

    def parse_order_fill(self, item: dict[str, Any], fallback_fee_pct: float = 0.0) -> dict[str, float]:
        qty = self._filled_qty(item)
        avg = self._avg_price(item)
        value = qty * avg if qty > 0 and avg > 0 else safe_float(item.get("dealAmount") or item.get("cummulativeQuoteQty"))
        fee = self._fee_usdt(item, fallback_value_usdt=value, fallback_fee_pct=fallback_fee_pct)
        ts = safe_int(item.get("updateTime") or item.get("transactTime") or item.get("time") or item.get("createdTime"), 0)
        return {"qty": qty, "avg_price": avg, "value_usdt": value, "fee_usdt": fee, "time_ms": ts}
