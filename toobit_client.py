"""کلاینت Toobit برای اجرای واقعی و وضعیت حساب."""
from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any
from urllib.parse import urlencode

import requests

from . import config
from .utils import (
    decimal_round_down,
    extract_filter,
    logger,
    safe_float,
    safe_int,
    side_to_toobit_open,
    side_to_toobit_position,
    toobit_symbol_candidates,
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

    @property
    def has_credentials(self) -> bool:
        return bool(self.api_key and self.api_secret)

    def _sign(self, params: dict[str, Any]) -> str:
        query = urlencode(params)
        return hmac.new(self.api_secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        signed: bool = False,
    ) -> Any:
        params = dict(params or {})
        headers = {}
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
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            raise ToobitError(f"خطا در ارتباط با Toobit: {exc}") from exc

        if isinstance(payload, dict):
            code = payload.get("code")
            if code not in (None, 0, 200, "0", "200"):
                raise ToobitError(f"پاسخ ناموفق Toobit: {payload.get('msg') or payload.get('message') or payload}")
        return payload

    def get_exchange_info(self) -> dict[str, Any]:
        return self._request("GET", "/api/v1/exchangeInfo", signed=False)

    def get_exchange_symbols(self) -> dict[str, dict[str, Any]]:
        payload = self.get_exchange_info()
        raw_symbols = []
        if isinstance(payload, dict):
            if isinstance(payload.get("symbols"), list):
                raw_symbols = payload["symbols"]
            elif isinstance(payload.get("data"), dict) and isinstance(payload["data"].get("symbols"), list):
                raw_symbols = payload["data"]["symbols"]
            elif isinstance(payload.get("data"), list):
                raw_symbols = payload["data"]
        result: dict[str, dict[str, Any]] = {}
        for item in raw_symbols:
            if not isinstance(item, dict):
                continue
            names = [
                item.get("symbol"),
                item.get("symbolId"),
                item.get("symbolName"),
                item.get("s"),
            ]
            for name in names:
                if name:
                    result[str(name).upper()] = item
        return result

    def validate_symbol(self, internal_symbol: str, exchange_symbols: dict[str, dict[str, Any]] | None = None) -> tuple[str, dict[str, Any]]:
        if exchange_symbols is None:
            exchange_symbols = self.get_exchange_symbols()
        for candidate in toobit_symbol_candidates(internal_symbol):
            key = candidate.upper()
            if key in exchange_symbols:
                return candidate, exchange_symbols[key]
        raise ToobitError(f"نماد {internal_symbol} در Toobit پیدا نشد")

    def get_balance(self) -> list[dict[str, Any]]:
        payload = self._request("GET", "/api/v1/futures/balance", signed=True)
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict) and isinstance(payload.get("data"), list):
            return payload["data"]
        return []

    def get_usdt_balance_summary(self) -> dict[str, float]:
        balances = self.get_balance()
        usdt = next((b for b in balances if str(b.get("coin") or b.get("asset") or b.get("currency") or "").upper() == "USDT"), {})

        def first_float(*keys: str) -> float:
            for key in keys:
                if key in usdt and usdt.get(key) not in (None, ""):
                    return safe_float(usdt.get(key))
            return 0.0

        return {
            "balance": first_float("balance", "walletBalance", "totalWalletBalance", "equity", "total"),
            "available": first_float("availableBalance", "available", "free", "maxWithdrawAmount"),
            "position_margin": first_float("positionMargin", "positionInitialMargin", "maintMargin"),
            "order_margin": first_float("orderMargin", "openOrderInitialMargin"),
            "unrealized_pnl": first_float("crossUnRealizedPnl", "unrealizedPnl", "unRealizedPnl"),
            "coupon": first_float("coupon"),
        }

    def get_today_pnl(self) -> float:
        payload = self._request("GET", "/api/v1/futures/todayPnl", signed=True)
        data = payload.get("data") if isinstance(payload, dict) else payload
        if isinstance(data, dict):
            for key in ("todayPnl", "pnl", "profit", "realizedPnl", "totalPnl", "income"):
                if key in data:
                    return safe_float(data.get(key))
        if isinstance(data, list):
            total = 0.0
            for item in data:
                if isinstance(item, dict):
                    for key in ("todayPnl", "pnl", "profit", "realizedPnl", "income"):
                        if key in item:
                            total += safe_float(item.get(key))
                            break
            return total
        if isinstance(payload, dict):
            for key in ("todayPnl", "pnl", "profit", "realizedPnl", "totalPnl", "income"):
                if key in payload:
                    return safe_float(payload.get(key))
        return 0.0

    def get_positions(self, symbol: str | None = None, side: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if symbol:
            params["symbol"] = symbol
        if side:
            params["side"] = side
        payload = self._request("GET", "/api/v1/futures/positions", params=params, signed=True)
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict) and isinstance(payload.get("data"), list):
            return payload["data"]
        return []

    def get_mark_price(self, symbol: str) -> float:
        payload = self._request("GET", "/quote/v1/markPrice", {"symbol": symbol}, signed=False)
        if isinstance(payload, dict):
            if "price" in payload:
                return safe_float(payload.get("price"))
            data = payload.get("data")
            if isinstance(data, dict) and "price" in data:
                return safe_float(data.get("price"))
        raise ToobitError(f"قیمت مارک Toobit برای {symbol} دریافت نشد")

    def set_leverage(self, symbol: str, leverage: int) -> Any:
        return self._request("POST", "/api/v1/futures/leverage", {"symbol": symbol, "leverage": leverage}, signed=True)

    def set_margin_type(self, symbol: str, margin_type: str) -> Any:
        return self._request("POST", "/api/v1/futures/marginType", {"symbol": symbol, "marginType": margin_type}, signed=True)

    def place_market_order(
        self,
        symbol: str,
        side: str,
        entry_price: float,
        trade_amount_usdt: float,
        leverage: int,
        tp_price: float,
        sl_price: float,
        client_order_id: str,
        symbol_info: dict[str, Any] | None = None,
    ) -> Any:
        notional = trade_amount_usdt * leverage
        base_qty = notional / entry_price if entry_price > 0 else trade_amount_usdt
        lot = extract_filter(symbol_info or {}, "LOT_SIZE")
        step = str(lot.get("stepSize", "0.0001"))
        min_qty = safe_float(lot.get("minQty"), 0.0)
        qty = max(base_qty, min_qty) if min_qty > 0 else base_qty
        quantity = decimal_round_down(qty, step=step, digits=6)

        params = {
            "symbol": symbol,
            "side": side_to_toobit_open(side),
            "type": "LIMIT",
            "priceType": "MARKET",
            "quantity": quantity,
            "valueQuantity": decimal_round_down(trade_amount_usdt, digits=2),
            "newClientOrderId": client_order_id,
            "takeProfit": decimal_round_down(tp_price, digits=8),
            "tpOrderType": "MARKET",
            "tpTriggerBy": "CONTRACT_PRICE",
            "stopLoss": decimal_round_down(sl_price, digits=8),
            "slOrderType": "MARKET",
            "slTriggerBy": "CONTRACT_PRICE",
        }
        return self._request("POST", "/api/v1/futures/order", params=params, signed=True)

    def set_trading_stop(self, symbol: str, side: str, tp_price: float, sl_price: float, size: str | None = None) -> Any:
        params = {
            "symbol": symbol,
            "side": side_to_toobit_position(side),
            "takeProfit": decimal_round_down(tp_price, digits=8),
            "stopLoss": decimal_round_down(sl_price, digits=8),
            "tpTriggerBy": "CONTRACT_PRICE",
            "slTriggerBy": "CONTRACT_PRICE",
            "stopType": "FIXED_STOP",
        }
        if size:
            params["tpSize"] = size
            params["slSize"] = size
        return self._request("POST", "/api/v1/futures/position/trading-stop", params=params, signed=True)

    def flash_close(self, symbol: str, side: str) -> Any:
        return self._request(
            "POST",
            "/api/v1/futures/flashClose",
            {"symbol": symbol, "side": side_to_toobit_position(side)},
            signed=True,
        )
