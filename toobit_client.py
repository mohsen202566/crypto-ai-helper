"""کلاینت Toobit برای اجرای واقعی، وضعیت حساب، اسلات واقعی و PnL تاریخچه.

نسخه ریشه‌ای: همه چیز در ریشه پروژه است، بدون پکیج bot.
"""
from __future__ import annotations

import hashlib
import hmac
import time
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_HALF_UP, ROUND_UP
from typing import Any
from urllib.parse import urlencode

import requests

import config
from utils import (
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

        self.path_balance = getattr(config, "TOOBIT_PATH_BALANCE", "/api/v1/futures/balance")
        self.path_positions = getattr(config, "TOOBIT_PATH_POSITIONS", "/api/v1/futures/positions")
        self.path_open_orders = getattr(config, "TOOBIT_PATH_OPEN_ORDERS", "/api/v1/futures/openOrders")
        self.path_margin_mode = getattr(config, "TOOBIT_PATH_MARGIN_MODE", "/api/v1/futures/marginType")
        self.path_leverage = getattr(config, "TOOBIT_PATH_LEVERAGE", "/api/v1/futures/leverage")
        self.path_position_settings = getattr(config, "TOOBIT_PATH_POSITION_SETTINGS", "/api/v1/futures/accountLeverage")
        self.path_order = getattr(config, "TOOBIT_PATH_ORDER", "/api/v1/futures/order")
        self.path_mark_price = getattr(config, "TOOBIT_PATH_MARK_PRICE", "/api/v1/futures/markPrice")
        self.path_exchange_info = getattr(config, "TOOBIT_PATH_EXCHANGE_INFO", "/api/v1/futures/exchangeInfo")
        self.path_history_positions = getattr(config, "TOOBIT_PATH_HISTORY_POSITIONS", "/api/v1/futures/historyPositions")
        self.path_order_history = getattr(config, "TOOBIT_PATH_ORDER_HISTORY", "/api/v1/futures/historyOrders")
        self.path_order_history_alt = getattr(config, "TOOBIT_PATH_ORDER_HISTORY_ALT", "/api/v1/futures/order/history")
        self.path_today_pnl = getattr(config, "TOOBIT_PATH_TODAY_PNL", "/api/v1/futures/todayPnl")
        self.path_close_order = getattr(config, "TOOBIT_PATH_CLOSE_ORDER", self.path_order)
        self.param_tp = getattr(config, "TOOBIT_PARAM_TP", "takeProfit")
        self.param_sl = getattr(config, "TOOBIT_PARAM_SL", "stopLoss")

    @property
    def has_credentials(self) -> bool:
        return bool(self.api_key and self.api_secret)

    def _sign(self, params: dict[str, Any]) -> str:
        query = urlencode(params, doseq=True)
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
    # عمومی / استخراج پاسخ
    # -----------------------------
    @staticmethod
    def _extract_dicts(payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if not isinstance(payload, dict):
            return []
        result: list[dict[str, Any]] = []
        for key in ("data", "result", "balances", "positions", "rows", "list"):
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
    def _first_decimal(item: dict[str, Any], *keys: str) -> Decimal | None:
        for key in keys:
            if key not in item or item[key] in (None, ""):
                continue
            try:
                return Decimal(str(item[key]))
            except (InvalidOperation, ValueError):
                continue
        return None

    @staticmethod
    def _symbol_from_item(item: dict[str, Any]) -> str:
        return str(item.get("symbol") or item.get("symbolId") or item.get("symbolName") or item.get("contractCode") or item.get("s") or "").upper()

    @staticmethod
    def _extract_order_id(payload: Any) -> str | None:
        for item in ToobitClient._extract_dicts(payload):
            for key in ("orderId", "order_id", "id", "clientOrderId", "newClientOrderId"):
                value = item.get(key)
                if value not in (None, ""):
                    return str(value)
        return None

    # -----------------------------
    # نمادها و حساب
    # -----------------------------
    def get_exchange_info(self) -> dict[str, Any]:
        return self._request("GET", self.path_exchange_info, signed=False)

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
            names = [item.get("symbol"), item.get("symbolId"), item.get("symbolName"), item.get("s")]
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
        payload = self._request("GET", self.path_balance, signed=True)
        return self._extract_dicts(payload)

    def get_usdt_balance_summary(self) -> dict[str, float]:
        balances = self.get_balance()
        usdt = next((b for b in balances if str(b.get("coin") or b.get("asset") or b.get("currency") or "").upper() in ("", "USDT")), {})
        return {
            "balance": safe_float(usdt.get("balance") or usdt.get("walletBalance") or usdt.get("totalWalletBalance") or usdt.get("equity") or usdt.get("accountEquity")),
            "available": safe_float(usdt.get("availableBalance") or usdt.get("availableMargin") or usdt.get("available") or usdt.get("free")),
            "position_margin": safe_float(usdt.get("positionMargin") or usdt.get("positionInitialMargin")),
            "order_margin": safe_float(usdt.get("orderMargin") or usdt.get("openOrderInitialMargin")),
            "unrealized_pnl": safe_float(usdt.get("crossUnRealizedPnl") or usdt.get("unrealizedPnL") or usdt.get("unrealizedPnl") or usdt.get("unrealizedProfit") or usdt.get("pnl")),
            "coupon": safe_float(usdt.get("coupon")),
        }

    def get_today_pnl(self) -> float:
        payload = self._request("GET", self.path_today_pnl, signed=True)
        total = 0.0
        found = False
        for item in self._extract_dicts(payload):
            value = self._first_decimal(item, "todayPnl", "dayProfit", "profit", "pnl", "realizedPnL", "realizedPnl", "totalPnl")
            if value is not None:
                total += float(value)
                found = True
        return total if found else 0.0

    # -----------------------------
    # پوزیشن / سفارش / آماده‌سازی قبل ترید
    # -----------------------------
    def get_positions(self, symbol: str | None = None, side: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if symbol:
            params["symbol"] = symbol.upper()
        if side:
            params["side"] = side
        payload = self._request("GET", self.path_positions, params=params, signed=True)
        return self._extract_dicts(payload)

    def get_open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if symbol:
            params["symbol"] = symbol.upper()
        payload = self._request("GET", self.path_open_orders, params=params, signed=True)
        orders = []
        for item in self._extract_dicts(payload):
            status = str(item.get("status") or item.get("orderStatus") or "").upper()
            if status in {"FILLED", "ORDER_FILLED", "CANCELED", "CANCELLED", "ORDER_CANCELED", "REJECTED", "EXPIRED"}:
                continue
            if symbol and self._symbol_from_item(item) not in ("", symbol.upper()):
                continue
            orders.append(item)
        return orders

    def _position_qty(self, item: dict[str, Any]) -> float:
        return abs(safe_float(item.get("position") or item.get("positionAmt") or item.get("positionAmount") or item.get("size") or item.get("quantity") or item.get("qty")))

    def _position_side(self, item: dict[str, Any]) -> str:
        raw_side = str(item.get("side") or item.get("positionSide") or item.get("direction") or "").upper()
        qty = safe_float(item.get("position") or item.get("positionAmt") or item.get("positionAmount") or item.get("size") or item.get("quantity") or item.get("qty"))
        if raw_side in {"LONG", "BUY", "BUY_OPEN"}:
            return "LONG"
        if raw_side in {"SHORT", "SELL", "SELL_OPEN"}:
            return "SHORT"
        return "LONG" if qty >= 0 else "SHORT"

    def get_open_position(self, symbol: str, side: str | None = None) -> dict[str, Any] | None:
        target_side = side_to_toobit_position(side) if side in ("BUY", "SELL") else (str(side).upper() if side else None)
        for item in self.get_positions(symbol):
            if self._position_qty(item) <= 0:
                continue
            if target_side and self._position_side(item) != target_side:
                continue
            return item
        return None

    def has_open_position(self, symbol: str) -> bool:
        return self.get_open_position(symbol) is not None

    def has_open_order(self, symbol: str) -> bool:
        return bool(self.get_open_orders(symbol))

    def _read_position_settings(self, symbol: str) -> list[dict[str, Any]]:
        payload = self._request("GET", self.path_position_settings, params={"symbol": symbol.upper()}, signed=True)
        return self._extract_dicts(payload)

    def _read_margin_mode(self, symbol: str) -> str | None:
        try:
            for item in self._read_position_settings(symbol):
                item_symbol = self._symbol_from_item(item)
                if item_symbol and item_symbol != symbol.upper():
                    continue
                raw = item.get("marginType") or item.get("marginMode")
                if raw is not None and str(raw).strip():
                    mode = str(raw).strip().lower()
                    if mode in {"isolated", "isolate", "true", "1"}:
                        return "ISOLATED"
                    if mode in {"cross", "crossed", "false", "0"}:
                        return "CROSS"
        except Exception as exc:
            logger.warning("خواندن margin mode ناموفق بود: %s", exc)
        return None

    def _read_leverage(self, symbol: str) -> int | None:
        try:
            for item in self._read_position_settings(symbol):
                item_symbol = self._symbol_from_item(item)
                if item_symbol and item_symbol != symbol.upper():
                    continue
                value = self._first_decimal(item, "leverage", "isolatedLeverage", "crossLeverage")
                if value is not None and value > 0:
                    return int(value)
        except Exception as exc:
            logger.warning("خواندن leverage ناموفق بود: %s", exc)
        return None

    def set_leverage(self, symbol: str, leverage: int) -> Any:
        return self._request("POST", self.path_leverage, {"symbol": symbol.upper(), "leverage": int(leverage)}, signed=True)

    def set_margin_type(self, symbol: str, margin_type: str) -> Any:
        return self._request("POST", self.path_margin_mode, {"symbol": symbol.upper(), "marginType": str(margin_type).upper()}, signed=True)

    def prepare_symbol_for_trade(self, symbol: str, leverage: int, margin_type: str = "ISOLATED") -> None:
        symbol = symbol.upper()
        if self.has_open_position(symbol):
            raise ToobitError(f"برای {symbol} پوزیشن باز وجود دارد و سفارش جدید بلاک شد")
        if self.has_open_order(symbol):
            raise ToobitError(f"برای {symbol} سفارش باز وجود دارد و سفارش جدید بلاک شد")

        desired_margin = str(margin_type or "ISOLATED").upper()
        current_margin = self._read_margin_mode(symbol)
        if current_margin != desired_margin:
            try:
                self.set_margin_type(symbol, desired_margin)
            except Exception as exc:
                msg = str(exc).lower()
                if "already" not in msg and "no need" not in msg and "isolated" not in msg:
                    raise

        current_lev = self._read_leverage(symbol)
        if current_lev != int(leverage):
            self.set_leverage(symbol, int(leverage))

    def get_mark_price(self, symbol: str) -> float:
        # بعضی endpointها /quote/v1/markPrice هستند و بعضی /api/v1/futures/markPrice؛ هر دو را امتحان می‌کنیم.
        last_error: Exception | None = None
        for path in (self.path_mark_price, "/quote/v1/markPrice"):
            try:
                payload = self._request("GET", path, {"symbol": symbol.upper()}, signed=False)
                for item in self._extract_dicts(payload):
                    value = self._first_decimal(item, "markPrice", "price", "lastPrice", "indexPrice")
                    if value is not None and value > 0:
                        return float(value)
            except Exception as exc:
                last_error = exc
        raise ToobitError(f"قیمت مارک Toobit برای {symbol} دریافت نشد: {last_error}")

    # -----------------------------
    # سفارش واقعی با TP/SL همراه سفارش
    # -----------------------------
    @staticmethod
    def _round_price_to_tick(value: Decimal, tick: Decimal, direction: str, *, is_tp: bool) -> Decimal:
        if tick <= 0:
            return value
        units = value / tick
        if direction == "LONG":
            rounding = ROUND_DOWN if is_tp else ROUND_UP
        else:
            rounding = ROUND_UP if is_tp else ROUND_DOWN
        rounded = units.to_integral_value(rounding=rounding) * tick
        if rounded <= 0:
            rounded = units.to_integral_value(rounding=ROUND_HALF_UP) * tick
        return rounded

    @staticmethod
    def _decimal_to_api(value: float | Decimal) -> str:
        return format(Decimal(str(value)).normalize(), "f")

    def get_symbol_rules(self, symbol: str, symbol_info: dict[str, Any] | None = None) -> tuple[str, str, float, float]:
        # خروجی: quantity_step, price_tick, min_qty, min_notional
        info = symbol_info or {}
        lot = extract_filter(info, "LOT_SIZE")
        price_filter = extract_filter(info, "PRICE_FILTER")
        step = str(lot.get("stepSize") or lot.get("quantityStep") or lot.get("qtyStep") or "0.0001")
        tick = str(price_filter.get("tickSize") or info.get("tickSize") or info.get("priceTick") or "0.0001")
        min_qty = safe_float(lot.get("minQty") or info.get("minQty") or info.get("minQuantity"), 0.0)
        min_notional = safe_float(info.get("minNotional") or info.get("minOrderValue"), 0.0)
        return step, tick, min_qty, min_notional

    def _verify_position_after_order(self, symbol: str, side: str) -> dict[str, Any] | None:
        wait_seconds = max(0.0, float(getattr(config, "TOOBIT_VERIFY_AFTER_ERROR_SECONDS", 70)))
        if wait_seconds > 0:
            time.sleep(wait_seconds)
        return self.get_open_position(symbol, side)

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
    ) -> dict[str, Any]:
        symbol = symbol.upper()
        direction = side_to_toobit_position(side)
        self.prepare_symbol_for_trade(symbol, int(leverage), config.DEFAULT_MARGIN_TYPE)

        entry = Decimal(str(entry_price if entry_price > 0 else self.get_mark_price(symbol)))
        step, tick, min_qty, min_notional = self.get_symbol_rules(symbol, symbol_info)
        tick_dec = Decimal(str(tick))
        tp_dec = self._round_price_to_tick(Decimal(str(tp_price)), tick_dec, direction, is_tp=True)
        sl_dec = self._round_price_to_tick(Decimal(str(sl_price)), tick_dec, direction, is_tp=False)

        notional = Decimal(str(trade_amount_usdt)) * Decimal(str(leverage))
        base_qty = notional / entry if entry > 0 else Decimal(str(trade_amount_usdt))
        if min_qty > 0 and base_qty < Decimal(str(min_qty)):
            base_qty = Decimal(str(min_qty))
        quantity = decimal_round_down(float(base_qty), step=step, digits=8)

        params = {
            "symbol": symbol,
            "side": side_to_toobit_open(side),
            "type": "LIMIT",
            "priceType": "MARKET",
            "quantity": quantity,
            # valueQuantity را برابر ارزش کل معامله می‌گذاریم، نه مارجین خام.
            "valueQuantity": self._decimal_to_api(notional),
            "newClientOrderId": client_order_id,
            self.param_tp: self._decimal_to_api(tp_dec),
            "tpOrderType": "MARKET",
            "tpTriggerBy": "CONTRACT_PRICE",
            self.param_sl: self._decimal_to_api(sl_dec),
            "slOrderType": "MARKET",
            "slTriggerBy": "CONTRACT_PRICE",
        }

        raw: Any | None = None
        try:
            raw = self._request("POST", self.path_order, params=params, signed=True)
        except Exception as exc:
            position = self._verify_position_after_order(symbol, side)
            return {
                "opened": position is not None,
                "symbol": symbol,
                "side": side,
                "direction": direction,
                "order_id": None,
                "entry_price": float(entry),
                "tp_price": float(tp_dec),
                "sl_price": float(sl_dec),
                "quantity": self._position_qty(position or {}) if position else None,
                "requested_margin_usdt": float(trade_amount_usdt),
                "notional_usdt": float(notional),
                "leverage": int(leverage),
                "reason": (
                    f"سفارش خطا داد ولی بعد از تایید {config.TOOBIT_VERIFY_AFTER_ERROR_SECONDS} ثانیه‌ای پوزیشن باز پیدا شد: {exc}"
                    if position is not None
                    else f"سفارش خطا داد و بعد از تایید {config.TOOBIT_VERIFY_AFTER_ERROR_SECONDS} ثانیه‌ای پوزیشن باز نشد: {exc}"
                ),
                "raw": None,
                "position": position,
            }

        position = self._verify_position_after_order(symbol, side)
        return {
            "opened": position is not None,
            "symbol": symbol,
            "side": side,
            "direction": direction,
            "order_id": self._extract_order_id(raw),
            "entry_price": float(entry),
            "tp_price": float(tp_dec),
            "sl_price": float(sl_dec),
            "quantity": self._position_qty(position or {}) if position else None,
            "requested_margin_usdt": float(trade_amount_usdt),
            "notional_usdt": float(notional),
            "leverage": int(leverage),
            "reason": f"سفارش ارسال شد؛ TP و SL همراه سفارش اصلی ثبت شدند؛ بعد از تایید {config.TOOBIT_VERIFY_AFTER_ERROR_SECONDS} ثانیه‌ای وضعیت پوزیشن بررسی شد.",
            "raw": raw if isinstance(raw, dict) else {"response": raw},
            "position": position,
        }

    def set_trading_stop(self, symbol: str, side: str, tp_price: float, sl_price: float, size: str | None = None) -> Any:
        # فقط برای سازگاری نگه داشته شده؛ ربات اصلی TP/SL را همراه سفارش باز می‌فرستد.
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
        return self._request("POST", "/api/v1/futures/flashClose", {"symbol": symbol, "side": side_to_toobit_position(side)}, signed=True)

    # -----------------------------
    # تاریخچه / PnL واقعی بعد بسته شدن
    # -----------------------------
    def get_history_positions(self, symbol: str | None = None, start_ms: int | None = None, end_ms: int | None = None, limit: int = 50) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": max(1, min(int(limit), 1000))}
        if symbol:
            params["symbol"] = symbol.upper()
        if start_ms is not None:
            params["startTime"] = int(start_ms)
        if end_ms is not None:
            params["endTime"] = int(end_ms)
        payload = self._request("GET", self.path_history_positions, params=params, signed=True)
        out = []
        for item in self._extract_dicts(payload):
            if symbol and self._symbol_from_item(item) not in ("", symbol.upper()):
                continue
            out.append(item)
        return out

    def get_order_history(self, symbol: str | None = None, start_ms: int | None = None, end_ms: int | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """خواندن تاریخچه سفارش‌ها با دو endpoint قابل تنظیم.

        بعضی نسخه‌های Toobit اسم endpoint تاریخچه را متفاوت برمی‌گردانند. برای همین اول مسیر اصلی و بعد مسیر جایگزین تست می‌شود.
        """
        params: dict[str, Any] = {"limit": max(1, min(int(limit), 1000))}
        if symbol:
            params["symbol"] = symbol.upper()
        if start_ms is not None:
            params["startTime"] = int(start_ms)
        if end_ms is not None:
            params["endTime"] = int(end_ms)

        last_error: Exception | None = None
        rows: list[dict[str, Any]] = []
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
                logger.warning("خواندن order history توبیت از %s ناموفق بود: %s", path, exc)
        if last_error and not rows:
            # خطا را بالا نمی‌بریم تا مانیتور بتواند historyPositions یا fallback را هم امتحان کند.
            return []
        return rows

    def find_realized_result(self, symbol: str, side: str, start_ms: int, end_ms: int | None = None) -> dict[str, Any] | None:
        direction = side_to_toobit_position(side)
        end_ms = end_ms or int(time.time() * 1000)
        start_window = max(0, int(start_ms) - 120_000)
        end_window = int(end_ms) + 120_000

        rows: list[dict[str, Any]] = []
        try:
            rows.extend(self.get_history_positions(symbol=symbol, start_ms=start_window, end_ms=end_window, limit=100))
        except Exception as exc:
            logger.warning("خواندن historyPositions توبیت برای %s ناموفق بود: %s", symbol, exc)
        try:
            rows.extend(self.get_order_history(symbol=symbol, start_ms=start_window, end_ms=end_window, limit=100))
        except Exception as exc:
            logger.warning("خواندن orderHistory توبیت برای %s ناموفق بود: %s", symbol, exc)

        candidates: list[dict[str, Any]] = []
        for item in rows:
            raw_side = str(item.get("side") or item.get("positionSide") or item.get("direction") or "").upper()
            if raw_side and raw_side not in {direction, "LONG" if side == "BUY" else "SHORT", "SELL_CLOSE" if side == "BUY" else "BUY_CLOSE"}:
                if raw_side in {"LONG", "SHORT", "BUY_OPEN", "SELL_OPEN", "BUY_CLOSE", "SELL_CLOSE"}:
                    continue

            status = str(item.get("status") or item.get("orderStatus") or item.get("state") or "").upper()
            if status and status in {"NEW", "PARTIALLY_FILLED", "OPEN", "ORDER_NEW"}:
                continue

            pnl = self._first_decimal(
                item,
                "realizedPnL", "realizedPnl", "realizedPnlWithoutFee", "closedPnl",
                "profit", "pnl", "cumRealizedPnl", "realProfit", "income",
            )
            close_price = self._first_decimal(
                item,
                "closePrice", "avgClosePrice", "exitPrice", "closeAvgPrice",
                "avgPrice", "price", "triggerPrice", "stopPrice", "executedPrice",
            )
            close_time = safe_int(
                item.get("closeTime") or item.get("updatedTime") or item.get("updateTime") or
                item.get("time") or item.get("transactTime") or item.get("createdTime"),
                0,
            )

            # بعضی order history ها فقط سفارش TP/SL را می‌دهند و PnL ندارند. اینها برای تشخیص TP/SL مفیدند،
            # اما برای PnL واقعی فقط وقتی pnl موجود است استفاده می‌شود.
            if pnl is None:
                continue

            candidates.append({
                "pnl": float(pnl),
                "close_time_ms": close_time,
                "close_price": float(close_price) if close_price is not None else None,
                "raw": item,
            })
        if not candidates:
            return None
        candidates.sort(key=lambda x: int(x.get("close_time_ms") or 0), reverse=True)
        return candidates[0]
