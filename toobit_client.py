"""Toobit client for Crypto AI Helper bot.

Locked responsibility:
- Exchange HTTP client only.
- Reads wallet/margin and open positions accurately for the trade panel/state sync.
- Checks open orders and existing positions before any real order to prevent duplicates.
- Ensures isolated margin mode and leverage before any real order, then reads them back
  and verifies the exchange accepted the values.
- Opens one market position using the configured margin amount with TP/SL attached.
- Rounds quantity by quantity step and TP/SL by tick size.
- Verifies the real margin used after quantity rounding is close to configured margin.
- After every order attempt, waits 70 seconds and then verifies whether the position exists.
- Does not analyze markets, render Telegram messages, manage learning, decide AI entries,
  calculate TP/SL, or own slot accounting.

Design lock:
- Small, simple, strong.
- One responsibility only.
- No hidden fallback from real execution to signal mode; callers decide mode before using this client.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_HALF_UP, ROUND_UP
from typing import Any, Literal
from urllib.parse import urlencode

import requests

Direction = Literal["LONG", "SHORT"]
MarginMode = Literal["isolated"]

MARGIN_ISOLATED = "isolated"
_CLIENT: "ToobitClient | None" = None


@dataclass(frozen=True)
class ToobitConfig:
    api_key: str
    secret_key: str
    base_url: str
    recv_window: int = 5000
    timeout_seconds: int = 12
    verify_after_error_seconds: int = 70
    margin_tolerance_pct: float = 1.0

    @classmethod
    def from_env(cls) -> "ToobitConfig":
        api_key = os.getenv("TOBIT_API_KEY", "").strip()
        secret_key = os.getenv("TOBIT_SECRET_KEY", "").strip()
        base_url = os.getenv("TOBIT_BASE_URL", "https://api.toobit.com").strip().rstrip("/")
        if not api_key:
            raise RuntimeError("TOBIT_API_KEY تنظیم نشده است.")
        if not secret_key:
            raise RuntimeError("TOBIT_SECRET_KEY تنظیم نشده است.")
        return cls(
            api_key=api_key,
            secret_key=secret_key,
            base_url=base_url,
            recv_window=int(os.getenv("TOBIT_RECV_WINDOW", "5000")),
            timeout_seconds=int(os.getenv("TOBIT_TIMEOUT_SECONDS", "12")),
            verify_after_error_seconds=int(os.getenv("TOBIT_VERIFY_AFTER_ERROR_SECONDS", "70")),
            margin_tolerance_pct=float(os.getenv("TOBIT_MARGIN_TOLERANCE_PCT", "1.0")),
        )


@dataclass(frozen=True)
class SymbolRules:
    symbol: str
    quantity_step: Decimal
    price_tick: Decimal
    min_quantity: Decimal
    min_notional: Decimal

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "quantity_step": _decimal_to_api(self.quantity_step),
            "price_tick": _decimal_to_api(self.price_tick),
            "min_quantity": _decimal_to_api(self.min_quantity),
            "min_notional": _decimal_to_api(self.min_notional),
        }


@dataclass(frozen=True)
class PositionInfo:
    symbol: str
    side: Direction
    quantity: float
    entry_price: float
    unrealized_pnl: float
    raw: dict[str, Any]


@dataclass(frozen=True)
class OpenOrderInfo:
    symbol: str
    side: Direction | None
    order_id: str | None
    raw: dict[str, Any]


@dataclass(frozen=True)
class OpenOrderResult:
    symbol: str
    direction: Direction
    requested_margin_usdt: float
    actual_margin_usdt: float
    leverage: int
    quantity: float
    entry_price: float
    tp_price: float
    sl_price: float
    opened: bool
    order_id: str | None
    position: PositionInfo | None
    reason: str
    raw: dict[str, Any] | None = None


class ToobitClient:
    """Small Toobit REST client.

    Endpoint paths and TP/SL parameter names are environment-configurable so this
    client can stay stable if the Toobit account/API variant uses different names.
    """

    def __init__(self, config: ToobitConfig | None = None, session: requests.Session | None = None) -> None:
        self.config = config or ToobitConfig.from_env()
        self.session = session or requests.Session()

        self.path_balance = os.getenv("TOBIT_PATH_BALANCE", "/api/v1/futures/balance")
        self.path_positions = os.getenv("TOBIT_PATH_POSITIONS", "/api/v1/futures/positions")
        self.path_open_orders = os.getenv("TOBIT_PATH_OPEN_ORDERS", "/api/v1/futures/openOrders")
        self.path_margin_mode = os.getenv("TOBIT_PATH_MARGIN_MODE", "/api/v1/futures/marginType")
        self.path_leverage = os.getenv("TOBIT_PATH_LEVERAGE", "/api/v1/futures/leverage")
        self.path_position_settings = os.getenv("TOBIT_PATH_POSITION_SETTINGS", "/api/v1/futures/positionRisk")
        self.path_order = os.getenv("TOBIT_PATH_ORDER", "/api/v1/futures/order")
        self.path_mark_price = os.getenv("TOBIT_PATH_MARK_PRICE", "/api/v1/futures/markPrice")
        self.path_exchange_info = os.getenv("TOBIT_PATH_EXCHANGE_INFO", "/api/v1/futures/exchangeInfo")

        self.param_tp = os.getenv("TOBIT_PARAM_TP", "takeProfit")
        self.param_sl = os.getenv("TOBIT_PARAM_SL", "stopLoss")
        self.param_quantity = os.getenv("TOBIT_PARAM_QUANTITY", "quantity")

    def get_wallet_margin_usdt(self) -> float:
        payload = self._request("GET", self.path_balance, signed=True)
        for item in _extract_dicts(payload):
            asset = str(item.get("asset") or item.get("coin") or item.get("currency") or "").upper()
            if asset and asset != "USDT":
                continue
            value = _first_decimal(item, "availableBalance", "available", "balance", "walletBalance", "marginBalance")
            if value is not None and value >= 0:
                return float(value)
        raise RuntimeError("موجودی/مارجین USDT از پاسخ توبیت قابل خواندن نیست.")

    def get_open_positions(self, symbol: str | None = None) -> list[PositionInfo]:
        params: dict[str, Any] = {}
        if symbol:
            params["symbol"] = symbol.upper()
        payload = self._request("GET", self.path_positions, params=params, signed=True)
        positions: list[PositionInfo] = []
        for item in _extract_dicts(payload):
            parsed = self._parse_position(item)
            if parsed is None:
                continue
            if symbol and parsed.symbol != symbol.upper():
                continue
            positions.append(parsed)
        return positions

    def get_open_orders(self, symbol: str | None = None) -> list[OpenOrderInfo]:
        params: dict[str, Any] = {}
        if symbol:
            params["symbol"] = symbol.upper()
        payload = self._request("GET", self.path_open_orders, params=params, signed=True)
        orders: list[OpenOrderInfo] = []
        for item in _extract_dicts(payload):
            parsed = self._parse_open_order(item)
            if parsed is None:
                continue
            if symbol and parsed.symbol != symbol.upper():
                continue
            orders.append(parsed)
        return orders

    def has_open_position(self, symbol: str) -> bool:
        return bool(self.get_open_positions(symbol))

    def has_open_order(self, symbol: str) -> bool:
        return bool(self.get_open_orders(symbol))

    def ensure_no_open_order(self, symbol: str) -> None:
        orders = self.get_open_orders(symbol)
        if orders:
            ids = ", ".join(order.order_id or "unknown" for order in orders)
            raise RuntimeError(f"برای {symbol.upper()} سفارش باز وجود دارد و سفارش جدید بلاک شد: {ids}")

    def ensure_no_open_position(self, symbol: str) -> None:
        positions = self.get_open_positions(symbol)
        if positions:
            raise RuntimeError(f"برای {symbol.upper()} پوزیشن باز وجود دارد و سفارش جدید بلاک شد.")

    def ensure_isolated_margin(self, symbol: str) -> None:
        symbol = symbol.upper()
        params = {"symbol": symbol, "marginType": "ISOLATED"}
        try:
            self._request("POST", self.path_margin_mode, params=params, signed=True)
        except ToobitAPIError as exc:
            message = str(exc).lower()
            already_isolated = "no need" in message or "already" in message or "isolated" in message
            if not already_isolated:
                raise
        verified = self._read_margin_mode(symbol)
        if verified != "isolated":
            raise RuntimeError(f"حالت مارجین {symbol} بعد از تنظیم تایید نشد. actual={verified!r}")

    def ensure_leverage(self, symbol: str, leverage: int) -> None:
        symbol = symbol.upper()
        if leverage <= 0:
            raise ValueError("لوریج باید مثبت باشد.")
        params = {"symbol": symbol, "leverage": int(leverage)}
        self._request("POST", self.path_leverage, params=params, signed=True)
        actual = self._read_leverage(symbol)
        if actual != int(leverage):
            raise RuntimeError(f"لوریج {symbol} تایید نشد. requested={leverage} actual={actual}")

    def prepare_symbol_for_trade(self, symbol: str, leverage: int) -> None:
        self.ensure_no_open_position(symbol)
        self.ensure_no_open_order(symbol)
        self.ensure_isolated_margin(symbol)
        self.ensure_leverage(symbol, leverage)

    def open_position_with_tp_sl(
        self,
        *,
        symbol: str,
        direction: Direction,
        margin_usdt: float,
        leverage: int,
        tp_price: float,
        sl_price: float,
        price: float | None = None,
    ) -> OpenOrderResult:
        symbol = symbol.upper()
        _validate_direction(direction)
        if margin_usdt <= 0:
            raise ValueError("مارجین معامله باید مثبت باشد.")
        if leverage <= 0:
            raise ValueError("لوریج باید مثبت باشد.")

        self.prepare_symbol_for_trade(symbol, leverage)

        entry_price = Decimal(str(price if price is not None else self.get_mark_price(symbol)))
        if entry_price <= 0:
            raise ValueError("قیمت ورود/مارک باید مثبت باشد.")

        rules = self.get_symbol_rules(symbol)
        tp_decimal = _round_price_to_tick(Decimal(str(tp_price)), rules.price_tick, direction, is_tp=True)
        sl_decimal = _round_price_to_tick(Decimal(str(sl_price)), rules.price_tick, direction, is_tp=False)
        self._validate_prices(direction, tp_price=tp_decimal, sl_price=sl_decimal, reference_price=entry_price)

        quantity_decimal = self.quantity_from_margin_decimal(
            symbol=symbol,
            margin_usdt=Decimal(str(margin_usdt)),
            leverage=int(leverage),
            price=entry_price,
            rules=rules,
        )
        actual_margin = self._actual_margin_usdt(quantity_decimal, entry_price, int(leverage))
        self._validate_actual_margin(requested_margin=Decimal(str(margin_usdt)), actual_margin=actual_margin)

        side = "BUY" if direction == "LONG" else "SELL"
        params = {
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            self.param_quantity: _decimal_to_api(quantity_decimal),
            self.param_tp: _decimal_to_api(tp_decimal),
            self.param_sl: _decimal_to_api(sl_decimal),
            "marginType": "ISOLATED",
            "leverage": int(leverage),
        }

        try:
            raw = self._request("POST", self.path_order, params=params, signed=True)
        except Exception as exc:
            position = self._verify_position_after_order(symbol, direction)
            if position is not None:
                return OpenOrderResult(
                    symbol=symbol,
                    direction=direction,
                    requested_margin_usdt=float(margin_usdt),
                    actual_margin_usdt=float(actual_margin),
                    leverage=int(leverage),
                    quantity=float(quantity_decimal),
                    entry_price=float(entry_price),
                    tp_price=float(tp_decimal),
                    sl_price=float(sl_decimal),
                    opened=True,
                    order_id=None,
                    position=position,
                    reason=f"سفارش خطا داد ولی بعد از تایید {self.config.verify_after_error_seconds} ثانیه‌ای پوزیشن باز پیدا شد: {exc}",
                    raw=None,
                )
            return OpenOrderResult(
                symbol=symbol,
                direction=direction,
                requested_margin_usdt=float(margin_usdt),
                actual_margin_usdt=float(actual_margin),
                leverage=int(leverage),
                quantity=float(quantity_decimal),
                entry_price=float(entry_price),
                tp_price=float(tp_decimal),
                sl_price=float(sl_decimal),
                opened=False,
                order_id=None,
                position=None,
                reason=f"سفارش خطا داد و بعد از تایید {self.config.verify_after_error_seconds} ثانیه‌ای پوزیشن باز نشد: {exc}",
                raw=None,
            )

        order_id = _extract_order_id(raw)
        position = self._verify_position_after_order(symbol, direction)
        return OpenOrderResult(
            symbol=symbol,
            direction=direction,
            requested_margin_usdt=float(margin_usdt),
            actual_margin_usdt=float(actual_margin),
            leverage=int(leverage),
            quantity=float(quantity_decimal),
            entry_price=float(entry_price),
            tp_price=float(tp_decimal),
            sl_price=float(sl_decimal),
            opened=position is not None,
            order_id=order_id,
            position=position,
            reason=f"سفارش ارسال شد و بعد از تایید {self.config.verify_after_error_seconds} ثانیه‌ای وضعیت پوزیشن بررسی شد.",
            raw=raw if isinstance(raw, dict) else {"response": raw},
        )
    def get_mark_price(self, symbol: str) -> float:
        payload = self._request("GET", self.path_mark_price, params={"symbol": symbol.upper()}, signed=False)
        for item in _extract_dicts(payload):
            value = _first_decimal(item, "markPrice", "price", "lastPrice", "indexPrice")
            if value is not None and value > 0:
                return float(value)
        raise RuntimeError(f"قیمت مارک برای {symbol} قابل خواندن نیست.")

    def get_symbol_rules(self, symbol: str) -> SymbolRules:
        symbol = symbol.upper()
        fallback_qty = Decimal(os.getenv("TOBIT_DEFAULT_QUANTITY_STEP", "0.0001"))
        fallback_tick = Decimal(os.getenv("TOBIT_DEFAULT_PRICE_TICK", "0.0001"))
        fallback_min_qty = Decimal(os.getenv("TOBIT_DEFAULT_MIN_QTY", "0"))
        fallback_min_notional = Decimal(os.getenv("TOBIT_DEFAULT_MIN_NOTIONAL", "0"))

        try:
            payload = self._request("GET", self.path_exchange_info, params={"symbol": symbol}, signed=False)
        except Exception:
            return SymbolRules(symbol, fallback_qty, fallback_tick, fallback_min_qty, fallback_min_notional)

        qty_step = fallback_qty
        price_tick = fallback_tick
        min_qty = fallback_min_qty
        min_notional = fallback_min_notional

        for item in _extract_dicts(payload):
            item_symbol = str(item.get("symbol") or item.get("contractCode") or "").upper()
            if item_symbol and item_symbol != symbol:
                continue
            qty_step = _first_decimal(item, "stepSize", "quantityStep", "qtyStep", "lotSize") or qty_step
            price_tick = _first_decimal(item, "tickSize", "priceTick", "pricePrecisionStep") or price_tick
            min_qty = _first_decimal(item, "minQty", "minQuantity") or min_qty
            min_notional = _first_decimal(item, "minNotional", "minOrderValue") or min_notional
            filters = item.get("filters", [])
            if isinstance(filters, list):
                for filter_item in filters:
                    if not isinstance(filter_item, dict):
                        continue
                    qty_step = _first_decimal(filter_item, "stepSize", "quantityStep", "qtyStep") or qty_step
                    price_tick = _first_decimal(filter_item, "tickSize", "priceTick") or price_tick
                    min_qty = _first_decimal(filter_item, "minQty", "minQuantity") or min_qty
                    min_notional = _first_decimal(filter_item, "minNotional", "minOrderValue") or min_notional

        return SymbolRules(
            symbol=symbol,
            quantity_step=qty_step if qty_step > 0 else fallback_qty,
            price_tick=price_tick if price_tick > 0 else fallback_tick,
            min_quantity=min_qty if min_qty > 0 else fallback_min_qty,
            min_notional=min_notional if min_notional > 0 else fallback_min_notional,
        )

    def quantity_from_margin(self, *, symbol: str, margin_usdt: float, leverage: int, price: float) -> float:
        quantity = self.quantity_from_margin_decimal(
            symbol=symbol,
            margin_usdt=Decimal(str(margin_usdt)),
            leverage=leverage,
            price=Decimal(str(price)),
            rules=self.get_symbol_rules(symbol),
        )
        return float(quantity)

    def quantity_from_margin_decimal(
        self,
        *,
        symbol: str,
        margin_usdt: Decimal,
        leverage: int,
        price: Decimal,
        rules: SymbolRules | None = None,
    ) -> Decimal:
        if margin_usdt <= 0:
            raise ValueError("مارجین معامله باید مثبت باشد.")
        if leverage <= 0:
            raise ValueError("لوریج باید مثبت باشد.")
        if price <= 0:
            raise ValueError("قیمت برای محاسبه quantity باید مثبت باشد.")

        rules = rules or self.get_symbol_rules(symbol)
        notional = margin_usdt * Decimal(str(leverage))
        quantity = _floor_to_step(notional / price, rules.quantity_step)
        if quantity <= 0:
            raise ValueError("quantity بعد از گرد کردن صفر شد؛ مارجین/لوریج برای این کوین کافی نیست.")
        if rules.min_quantity > 0 and quantity < rules.min_quantity:
            raise ValueError(f"quantity کمتر از حداقل مجاز صرافی است: {quantity} < {rules.min_quantity}")
        actual_notional = quantity * price
        if rules.min_notional > 0 and actual_notional < rules.min_notional:
            raise ValueError(f"notional کمتر از حداقل مجاز صرافی است: {actual_notional} < {rules.min_notional}")
        return quantity

    def _verify_position(self, symbol: str, direction: Direction) -> PositionInfo | None:
        for position in self.get_open_positions(symbol):
            if position.side == direction and position.quantity > 0:
                return position
        return None

    def _verify_position_after_order(self, symbol: str, direction: Direction) -> PositionInfo | None:
        wait_seconds = max(0.0, float(self.config.verify_after_error_seconds))
        if wait_seconds > 0:
            time.sleep(wait_seconds)
        return self._verify_position(symbol, direction)


    def _read_margin_mode(self, symbol: str) -> str:
        payload = self._request("GET", self.path_position_settings, params={"symbol": symbol.upper()}, signed=True)
        for item in _extract_dicts(payload):
            item_symbol = str(item.get("symbol") or item.get("contractCode") or "").upper()
            if item_symbol and item_symbol != symbol.upper():
                continue
            raw_mode = item.get("marginType", item.get("marginMode", None))
            if raw_mode is not None and str(raw_mode).strip():
                mode = str(raw_mode).strip().lower()
                if mode in {"isolated", "isolate", "true", "1"}:
                    return "isolated"
                if mode in {"cross", "crossed", "false", "0"}:
                    return "cross"
            isolated_flag = item.get("isolated", None)
            if isolated_flag is not None:
                return "isolated" if str(isolated_flag).lower() in {"true", "1", "yes"} else "cross"
        raise RuntimeError(f"وضعیت margin mode برای {symbol.upper()} از صرافی قابل خواندن نیست.")

    def _read_leverage(self, symbol: str) -> int:
        payload = self._request("GET", self.path_position_settings, params={"symbol": symbol.upper()}, signed=True)
        for item in _extract_dicts(payload):
            item_symbol = str(item.get("symbol") or item.get("contractCode") or "").upper()
            if item_symbol and item_symbol != symbol.upper():
                continue
            value = _first_decimal(item, "leverage", "isolatedLeverage", "crossLeverage")
            if value is not None and value > 0:
                return int(value)
        raise RuntimeError(f"لوریج {symbol.upper()} از صرافی قابل خواندن نیست.")

    def _parse_position(self, item: dict[str, Any]) -> PositionInfo | None:
        symbol = str(item.get("symbol") or item.get("contractCode") or "").upper()
        if not symbol:
            return None
        quantity = _first_decimal(item, "positionAmt", "positionAmount", "size", "quantity", "qty")
        if quantity is None or quantity == 0:
            return None
        raw_side = str(item.get("side") or item.get("positionSide") or item.get("direction") or "").upper()
        if raw_side in {"LONG", "BUY"}:
            side: Direction = "LONG"
        elif raw_side in {"SHORT", "SELL"}:
            side = "SHORT"
        else:
            side = "LONG" if quantity > 0 else "SHORT"
        entry = _first_decimal(item, "entryPrice", "avgPrice", "averagePrice") or Decimal("0")
        pnl = _first_decimal(item, "unrealizedPnl", "unrealizedProfit", "pnl") or Decimal("0")
        return PositionInfo(
            symbol=symbol,
            side=side,
            quantity=float(abs(quantity)),
            entry_price=float(entry),
            unrealized_pnl=float(pnl),
            raw=item,
        )

    def _parse_open_order(self, item: dict[str, Any]) -> OpenOrderInfo | None:
        symbol = str(item.get("symbol") or item.get("contractCode") or "").upper()
        if not symbol:
            return None
        status = str(item.get("status") or item.get("orderStatus") or "").upper()
        if status in {"FILLED", "CANCELED", "CANCELLED", "REJECTED", "EXPIRED"}:
            return None
        raw_side = str(item.get("side") or item.get("positionSide") or "").upper()
        side: Direction | None
        if raw_side in {"BUY", "LONG"}:
            side = "LONG"
        elif raw_side in {"SELL", "SHORT"}:
            side = "SHORT"
        else:
            side = None
        return OpenOrderInfo(symbol=symbol, side=side, order_id=_extract_order_id(item), raw=item)

    def _validate_prices(
        self,
        direction: Direction,
        *,
        tp_price: Decimal,
        sl_price: Decimal,
        reference_price: Decimal,
    ) -> None:
        if tp_price <= 0 or sl_price <= 0:
            raise ValueError("TP و SL باید مثبت باشند.")
        if reference_price <= 0:
            raise ValueError("قیمت مرجع باید مثبت باشد.")
        if direction == "LONG" and not (tp_price > reference_price > sl_price):
            raise ValueError("برای LONG باید TP بالاتر از ورود و SL پایین‌تر از ورود باشد.")
        if direction == "SHORT" and not (tp_price < reference_price < sl_price):
            raise ValueError("برای SHORT باید TP پایین‌تر از ورود و SL بالاتر از ورود باشد.")

    def _actual_margin_usdt(self, quantity: Decimal, price: Decimal, leverage: int) -> Decimal:
        return (quantity * price) / Decimal(str(leverage))

    def _validate_actual_margin(self, *, requested_margin: Decimal, actual_margin: Decimal) -> None:
        if requested_margin <= 0 or actual_margin <= 0:
            raise ValueError("مارجین واقعی/درخواستی باید مثبت باشد.")
        diff_pct = abs(requested_margin - actual_margin) / requested_margin * Decimal("100")
        max_diff = Decimal(str(self.config.margin_tolerance_pct))
        if diff_pct > max_diff:
            raise ValueError(
                "مارجین واقعی بعد از محاسبه quantity با مقدار تنظیم‌شده نمی‌خواند: "
                f"requested={requested_margin}, actual={actual_margin}, diff={diff_pct:.4f}%"
            )

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        signed: bool,
    ) -> Any:
        params = dict(params or {})
        headers = {"X-BB-APIKEY": self.config.api_key} if signed else {}
        if signed:
            params.setdefault("recvWindow", self.config.recv_window)
            params.setdefault("timestamp", int(time.time() * 1000))
            query = urlencode(params, doseq=True)
            params["signature"] = hmac.new(
                self.config.secret_key.encode("utf-8"),
                query.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()

        url = f"{self.config.base_url}{path}"
        if method.upper() == "GET":
            response = self.session.get(url, params=params, headers=headers, timeout=self.config.timeout_seconds)
        elif method.upper() == "POST":
            response = self.session.post(url, data=params, headers=headers, timeout=self.config.timeout_seconds)
        else:
            raise ValueError(f"HTTP method پشتیبانی نمی‌شود: {method}")

        if response.status_code >= 400:
            raise ToobitAPIError(f"HTTP {response.status_code}: {response.text[:500]}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ToobitAPIError("پاسخ توبیت JSON معتبر نیست.") from exc
        self._raise_if_api_error(payload)
        return payload

    @staticmethod
    def _raise_if_api_error(payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        code = payload.get("code") or payload.get("retCode") or payload.get("status")
        success_values = {None, 0, "0", "OK", "ok", "success", "SUCCESS", True}
        if code in success_values:
            return
        message = payload.get("msg") or payload.get("message") or payload.get("error") or payload
        raise ToobitAPIError(str(message))


def get_client(config: ToobitConfig | None = None, session: requests.Session | None = None) -> ToobitClient:
    """Return a Toobit client compatible with real_trade_manager.py.

    - With explicit config/session: return a fresh client (useful for tests).
    - Without arguments: reuse one process-local client so callers share the same
      configured HTTP session and do not rebuild it on every trade/status call.
    """
    global _CLIENT
    if config is not None or session is not None:
        return ToobitClient(config=config, session=session)
    if _CLIENT is None:
        _CLIENT = ToobitClient()
    return _CLIENT


class ToobitAPIError(RuntimeError):
    """Raised for Toobit HTTP/API errors."""


def _validate_direction(direction: str) -> None:
    if direction not in ("LONG", "SHORT"):
        raise ValueError("direction باید LONG یا SHORT باشد.")


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
            result.extend(_extract_dicts(value))
        elif isinstance(value, list):
            result.extend(item for item in value if isinstance(item, dict))
    if not result:
        result.append(payload)
    return result


def _first_decimal(item: dict[str, Any], *keys: str) -> Decimal | None:
    for key in keys:
        if key not in item or item[key] in (None, ""):
            continue
        try:
            return Decimal(str(item[key]))
        except (InvalidOperation, ValueError):
            continue
    return None


def _floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def _round_price_to_tick(value: Decimal, tick: Decimal, direction: Direction, *, is_tp: bool) -> Decimal:
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


def _decimal_to_api(value: float | Decimal) -> str:
    decimal_value = Decimal(str(value)).normalize()
    return format(decimal_value, "f")


def _extract_order_id(payload: Any) -> str | None:
    if isinstance(payload, dict):
        for key in ("orderId", "order_id", "id", "clientOrderId"):
            value = payload.get(key)
            if value not in (None, ""):
                return str(value)
    for item in _extract_dicts(payload):
        for key in ("orderId", "order_id", "id", "clientOrderId"):
            value = item.get(key)
            if value not in (None, ""):
                return str(value)
    return None


__all__ = [
    "Direction",
    "MarginMode",
    "MARGIN_ISOLATED",
    "OpenOrderInfo",
    "OpenOrderResult",
    "PositionInfo",
    "SymbolRules",
    "ToobitAPIError",
    "ToobitClient",
    "ToobitConfig",
    "get_client",
]
