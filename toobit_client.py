"""کلاینت واحد Toobit برای داده بازار و اجرای واقعی با محدودکننده وزن.

دو مسیر HTTP جدا دارد: market و trade. مسیر سفارش و مانیتور حساب هیچ‌وقت
پشت اسکن سنگین بازار قفل نمی‌شود.
"""
from __future__ import annotations

import hashlib
import hmac
import threading
import time
from collections import deque
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_HALF_UP, ROUND_UP
from typing import Any
from urllib.parse import urlencode

import requests

import config
from utils import (
    canonical_base,
    canonical_symbol,
    decimal_round_down,
    extract_filter,
    logger,
    safe_float,
    safe_int,
    side_to_open,
    side_to_position,
    toobit_contract_symbol,
)


class ToobitError(RuntimeError):
    pass


class RateLimiter:
    """Sliding-window limiter with a protected budget for trading/account requests."""

    def __init__(self) -> None:
        self.lock = threading.Condition(threading.RLock())
        self.events: deque[tuple[float, int, str]] = deque()
        self.blocked_until = 0.0

    def _purge(self, now: float) -> None:
        while self.events and now - self.events[0][0] >= 60.0:
            self.events.popleft()

    def snapshot(self) -> dict[str, int | float]:
        with self.lock:
            now = time.monotonic()
            self._purge(now)
            total = sum(weight for _, weight, _ in self.events)
            market = sum(weight for _, weight, kind in self.events if kind == "market")
            return {
                "total_60s": total,
                "market_60s": market,
                "total_limit": config.INTERNAL_TOTAL_WEIGHT_PER_MINUTE,
                "market_limit": config.INTERNAL_MARKET_WEIGHT_PER_MINUTE,
                "blocked_for_seconds": max(0.0, self.blocked_until - now),
            }

    def acquire(self, weight: int, kind: str = "market", timeout: float = 20.0) -> None:
        weight = max(1, int(weight))
        deadline = time.monotonic() + max(1.0, timeout)
        with self.lock:
            while True:
                now = time.monotonic()
                self._purge(now)
                if now < self.blocked_until:
                    wait_for = min(self.blocked_until - now, max(0.05, deadline - now))
                    if wait_for <= 0:
                        raise ToobitError("محدودیت API هنوز در حالت انتظار است")
                    self.lock.wait(wait_for)
                    continue
                total = sum(w for _, w, _ in self.events)
                market = sum(w for _, w, k in self.events if k == "market")
                total_ok = total + weight <= config.INTERNAL_TOTAL_WEIGHT_PER_MINUTE
                market_ok = kind != "market" or market + weight <= config.INTERNAL_MARKET_WEIGHT_PER_MINUTE
                if total_ok and market_ok:
                    self.events.append((now, weight, kind))
                    return
                if now >= deadline:
                    raise ToobitError(
                        f"بودجه داخلی API پر است: total={total}/{config.INTERNAL_TOTAL_WEIGHT_PER_MINUTE} "
                        f"market={market}/{config.INTERNAL_MARKET_WEIGHT_PER_MINUTE}"
                    )
                oldest = self.events[0][0] if self.events else now
                wait_for = max(0.05, min(60.0 - (now - oldest) + config.RATE_LIMIT_SAFETY_SECONDS, deadline - now))
                self.lock.wait(wait_for)

    def punish_429(self, seconds: float = 60.0) -> None:
        with self.lock:
            self.blocked_until = max(self.blocked_until, time.monotonic() + max(1.0, seconds))
            self.lock.notify_all()


class ToobitClient:
    def __init__(self, base_url: str = config.TOOBIT_BASE_URL, timeout: float = config.REQUEST_TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.api_key = config.TOOBIT_API_KEY
        self.api_secret = config.TOOBIT_API_SECRET
        self.market_session = requests.Session()
        self.trade_session = requests.Session()
        self.market_lock = threading.RLock()
        self.trade_lock = threading.RLock()
        self.rate = RateLimiter()

    @property
    def has_credentials(self) -> bool:
        return bool(self.api_key and self.api_secret)

    def close(self) -> None:
        self.market_session.close()
        self.trade_session.close()

    def _sign(self, params: dict[str, Any]) -> str:
        return hmac.new(
            self.api_secret.encode("utf-8"),
            urlencode(params, doseq=True).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    @staticmethod
    def _extract_dicts(payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return [x for x in payload if isinstance(x, dict)]
        if not isinstance(payload, dict):
            return []
        out: list[dict[str, Any]] = []
        for key in ("data", "result", "rows", "list", "positions", "balances", "openInterestList"):
            value = payload.get(key)
            if isinstance(value, list):
                out.extend(x for x in value if isinstance(x, dict))
            elif isinstance(value, dict):
                out.append(value)
                out.extend(ToobitClient._extract_dicts(value))
        if not out:
            out.append(payload)
        return out

    @staticmethod
    def _first_decimal(item: dict[str, Any], *keys: str) -> Decimal | None:
        for key in keys:
            if item.get(key) in (None, ""):
                continue
            try:
                return Decimal(str(item[key]))
            except (InvalidOperation, ValueError):
                pass
        return None

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        signed: bool = False,
        weight: int | None = None,
        kind: str = "market",
        timeout: float | None = None,
    ) -> Any:
        params = dict(params or {})
        if signed:
            if not self.has_credentials:
                raise ToobitError("کلید API توبیت تنظیم نشده است")
            params.setdefault("timestamp", int(time.time() * 1000))
            params.setdefault("recvWindow", config.TOOBIT_RECV_WINDOW)
            params["signature"] = self._sign(params)
        request_weight = int(weight or config.ENDPOINT_WEIGHTS.get(path, 1))
        self.rate.acquire(request_weight, kind=kind, timeout=max(20.0, self.timeout * 2))
        headers = {"X-BB-APIKEY": self.api_key} if signed else {}
        url = f"{self.base_url}{path}"
        session = self.trade_session if kind == "trade" else self.market_session
        lock = self.trade_lock if kind == "trade" else self.market_lock
        last: Exception | None = None
        for attempt in range(config.HTTP_RETRIES + 1):
            try:
                with lock:
                    if method.upper() == "GET":
                        response = session.get(url, params=params, headers=headers, timeout=timeout or self.timeout)
                    elif method.upper() == "POST":
                        response = session.post(url, data=params, headers=headers, timeout=timeout or self.timeout)
                    elif method.upper() == "DELETE":
                        response = session.delete(url, data=params, headers=headers, timeout=timeout or self.timeout)
                    else:
                        raise ToobitError(f"متد پشتیبانی نمی‌شود: {method}")
                if response.status_code == 429:
                    reset_ms = safe_int(
                        response.headers.get("X-Api-Limit-Reset-Timestamp")
                        or response.headers.get("X-RateLimit-Reset"),
                        0,
                    )
                    pause = 60.0
                    if reset_ms > 10_000_000_000:
                        pause = max(1.0, reset_ms / 1000 - time.time())
                    elif reset_ms > int(time.time()):
                        pause = max(1.0, reset_ms - time.time())
                    self.rate.punish_429(pause)
                    raise ToobitError(f"Toobit rate limit 429؛ توقف {pause:.1f} ثانیه")
                if response.status_code >= 400:
                    raise ToobitError(f"HTTP {response.status_code}: {response.text[:500]}")
                payload = response.json()
                if isinstance(payload, dict):
                    code = payload.get("code") or payload.get("retCode") or payload.get("status")
                    if code not in (None, 0, 200, "0", "200", "OK", "ok", "success", "SUCCESS", True):
                        raise ToobitError(
                            f"پاسخ ناموفق Toobit: {payload.get('msg') or payload.get('message') or payload.get('error') or payload}"
                        )
                return payload
            except Exception as exc:
                last = exc
                if isinstance(exc, ToobitError) and "429" in str(exc):
                    raise
                if attempt < config.HTTP_RETRIES:
                    time.sleep(config.HTTP_BACKOFF_SECONDS * (attempt + 1))
        if isinstance(last, ToobitError):
            raise last
        raise ToobitError(f"خطا در ارتباط با Toobit: {last}")

    # -------------------- داده بازار --------------------
    def get_exchange_info(self) -> dict[str, Any]:
        payload = self._request("GET", config.PATH_EXCHANGE_INFO, weight=1)
        return payload if isinstance(payload, dict) else {}

    def get_contracts(self) -> dict[str, dict[str, Any]]:
        payload = self.get_exchange_info()
        containers = [payload]
        if isinstance(payload.get("data"), dict):
            containers.insert(0, payload["data"])
        rows: list[dict[str, Any]] = []
        for container in containers:
            contracts = container.get("contracts")
            if isinstance(contracts, list):
                rows = [x for x in contracts if isinstance(x, dict)]
                break
        if not rows and isinstance(payload.get("data"), list):
            rows = [x for x in payload["data"] if isinstance(x, dict)]
        result: dict[str, dict[str, Any]] = {}
        for item in rows:
            status = str(item.get("status") or "TRADING").upper()
            if status != "TRADING" or bool(item.get("inverse", False)):
                continue
            margin_token = str(item.get("marginToken") or item.get("quoteAsset") or "USDT").upper()
            if margin_token != "USDT":
                continue
            raw_symbol = str(item.get("symbol") or item.get("symbolId") or item.get("symbolName") or "").upper()
            if not raw_symbol:
                continue
            contract = toobit_contract_symbol(raw_symbol)
            item = dict(item)
            item["exchange_symbol"] = contract
            item["canonical"] = canonical_symbol(raw_symbol)
            result[contract] = item
        return result

    def get_24h_tickers(self) -> list[dict[str, Any]]:
        payload = self._request("GET", config.PATH_TICKER_24H, params={}, weight=40)
        return self._extract_dicts(payload)

    def get_all_prices(self) -> dict[str, float]:
        payload = self._request("GET", config.PATH_PRICE_TICKER, params={}, weight=1)
        out: dict[str, float] = {}
        for item in self._extract_dicts(payload):
            symbol = str(item.get("s") or item.get("symbol") or item.get("symbolId") or "").upper()
            price = safe_float(item.get("p") or item.get("price") or item.get("lastPrice"))
            if symbol and price > 0:
                out[canonical_symbol(symbol)] = price
        return out

    def get_all_book_tickers(self) -> dict[str, dict[str, float]]:
        payload = self._request("GET", config.PATH_BOOK_TICKER, params={}, weight=1)
        out: dict[str, dict[str, float]] = {}
        for item in self._extract_dicts(payload):
            symbol = str(item.get("s") or item.get("symbol") or "").upper()
            if not symbol:
                continue
            out[canonical_symbol(symbol)] = {
                "bid": safe_float(item.get("b") or item.get("bidPrice")),
                "bid_qty": safe_float(item.get("bq") or item.get("bidQty")),
                "ask": safe_float(item.get("a") or item.get("askPrice")),
                "ask_qty": safe_float(item.get("aq") or item.get("askQty")),
                "time": safe_int(item.get("t") or item.get("time")),
            }
        return out

    def get_klines(self, symbol: str, interval: str = "1m", limit: int = 120) -> list[dict[str, float]]:
        payload = self._request(
            "GET", config.PATH_KLINES,
            {"symbol": toobit_contract_symbol(symbol), "interval": interval, "limit": max(2, min(int(limit), 1000))},
            weight=1,
        )
        rows = payload if isinstance(payload, list) else (payload.get("data") if isinstance(payload, dict) else [])
        out: list[dict[str, float]] = []
        for row in rows or []:
            if isinstance(row, list) and len(row) >= 6:
                out.append({
                    "ts": safe_int(row[0]), "open": safe_float(row[1]), "high": safe_float(row[2]),
                    "low": safe_float(row[3]), "close": safe_float(row[4]), "volume": safe_float(row[5]),
                    "quote_volume": safe_float(row[7] if len(row) > 7 else 0),
                    "trades": safe_int(row[8] if len(row) > 8 else 0),
                    "taker_buy_volume": safe_float(row[9] if len(row) > 9 else 0),
                    "taker_buy_quote": safe_float(row[10] if len(row) > 10 else 0),
                })
            elif isinstance(row, dict):
                out.append({
                    "ts": safe_int(row.get("t") or row.get("time")),
                    "open": safe_float(row.get("o") or row.get("open")),
                    "high": safe_float(row.get("h") or row.get("high")),
                    "low": safe_float(row.get("l") or row.get("low")),
                    "close": safe_float(row.get("c") or row.get("close")),
                    "volume": safe_float(row.get("v") or row.get("volume")),
                    "quote_volume": safe_float(row.get("qv") or row.get("quoteVolume")),
                    "trades": safe_int(row.get("n") or row.get("trades")),
                    "taker_buy_volume": safe_float(row.get("tbv") or row.get("takerBuyVolume")),
                    "taker_buy_quote": safe_float(row.get("tbq") or row.get("takerBuyQuote")),
                })
        return sorted((x for x in out if x["close"] > 0 and x["high"] >= x["low"] > 0), key=lambda x: x["ts"])[-limit:]

    def get_recent_trades(self, symbol: str, limit: int = 60) -> list[dict[str, Any]]:
        payload = self._request(
            "GET", config.PATH_TRADES,
            {"symbol": toobit_contract_symbol(symbol), "limit": max(1, min(int(limit), 60))},
            weight=1,
        )
        return self._extract_dicts(payload)

    def get_depth(self, symbol: str, limit: int = 20) -> dict[str, Any]:
        limit = max(5, min(int(limit), 100))
        payload = self._request(
            "GET", config.PATH_DEPTH,
            {"symbol": toobit_contract_symbol(symbol), "limit": limit},
            weight=1,
        )
        return payload if isinstance(payload, dict) else {}

    def get_mark_price(self, symbol: str) -> float:
        payload = self._request("GET", config.PATH_MARK_PRICE, {"symbol": toobit_contract_symbol(symbol)}, weight=1)
        for item in self._extract_dicts(payload):
            value = self._first_decimal(item, "price", "markPrice", "lastPrice", "indexPrice")
            if value is not None and value > 0:
                return float(value)
        raise ToobitError(f"Mark Price برای {symbol} دریافت نشد")

    def get_funding_rate(self, symbol: str) -> dict[str, Any]:
        payload = self._request("GET", config.PATH_FUNDING, {"symbol": toobit_contract_symbol(symbol)}, weight=1)
        rows = self._extract_dicts(payload)
        return rows[0] if rows else {}

    def get_open_interest(self, symbol: str) -> float:
        payload = self._request("GET", config.PATH_OPEN_INTEREST, {"symbol": toobit_contract_symbol(symbol)}, weight=1)
        for item in self._extract_dicts(payload):
            value = self._first_decimal(item, "size", "openInterest", "oi")
            if value is not None:
                return float(value)
        return 0.0

    def get_long_short_ratio(self, symbol: str, period: str = "5m") -> dict[str, Any]:
        payload = self._request(
            "GET", config.PATH_LONG_SHORT,
            {"symbol": toobit_contract_symbol(symbol), "period": period, "limit": 2},
            weight=1,
        )
        rows = self._extract_dicts(payload)
        return rows[-1] if rows else {}

    # -------------------- حساب و اجرا --------------------
    def get_balance(self) -> list[dict[str, Any]]:
        return self._extract_dicts(self._request("GET", config.PATH_BALANCE, signed=True, kind="trade"))

    def get_usdt_balance_summary(self) -> dict[str, float]:
        rows = self.get_balance()
        row = next((x for x in rows if str(x.get("coin") or x.get("asset") or x.get("currency") or "").upper() == "USDT"), None)
        if row is None:
            row = next((x for x in rows if not str(x.get("coin") or x.get("asset") or x.get("currency") or "").strip()), {})
        return {
            "balance": safe_float(row.get("balance") or row.get("walletBalance") or row.get("equity") or row.get("accountEquity")),
            "available": safe_float(row.get("availableBalance") or row.get("availableMargin") or row.get("available") or row.get("free")),
            "position_margin": safe_float(row.get("positionMargin") or row.get("positionInitialMargin")),
            "order_margin": safe_float(row.get("orderMargin") or row.get("openOrderInitialMargin")),
            "unrealized_pnl": safe_float(row.get("crossUnRealizedPnl") or row.get("unrealizedPnL") or row.get("unrealizedPnl") or row.get("unrealizedProfit")),
        }

    def get_positions(self, symbol: str | None = None) -> list[dict[str, Any]]:
        params = {"symbol": toobit_contract_symbol(symbol)} if symbol else {}
        return self._extract_dicts(self._request("GET", config.PATH_POSITIONS, params, signed=True, kind="trade"))

    def get_open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        params = {"symbol": toobit_contract_symbol(symbol)} if symbol else {}
        rows = self._extract_dicts(self._request("GET", config.PATH_OPEN_ORDERS, params, signed=True, kind="trade"))
        out = []
        for item in rows:
            status = str(item.get("status") or item.get("orderStatus") or "").upper()
            if status in {"FILLED", "ORDER_FILLED", "CANCELED", "CANCELLED", "REJECTED", "EXPIRED"}:
                continue
            out.append(item)
        return out

    @staticmethod
    def position_qty(item: dict[str, Any]) -> float:
        return abs(safe_float(item.get("position") or item.get("positionAmt") or item.get("positionAmount") or item.get("size") or item.get("quantity") or item.get("qty")))

    @staticmethod
    def position_side(item: dict[str, Any]) -> str:
        raw = str(item.get("side") or item.get("positionSide") or item.get("direction") or "").upper()
        qty = safe_float(item.get("position") or item.get("positionAmt") or item.get("size") or item.get("quantity"))
        if raw in {"LONG", "BUY", "BUY_OPEN"}:
            return "LONG"
        if raw in {"SHORT", "SELL", "SELL_OPEN"}:
            return "SHORT"
        return "LONG" if qty >= 0 else "SHORT"

    @staticmethod
    def item_symbol(item: dict[str, Any]) -> str:
        return str(item.get("symbol") or item.get("symbolId") or item.get("symbolName") or item.get("contractCode") or item.get("s") or "").upper()

    def has_open_position(self, symbol: str) -> bool:
        base = canonical_base(symbol)
        return any(self.position_qty(x) > 0 and canonical_base(self.item_symbol(x)) == base for x in self.get_positions(symbol))

    def has_open_order(self, symbol: str) -> bool:
        base = canonical_base(symbol)
        return any(canonical_base(self.item_symbol(x)) in {"", base} for x in self.get_open_orders(symbol))

    def _read_position_settings(self, symbol: str) -> list[dict[str, Any]]:
        return self._extract_dicts(self._request(
            "GET", config.PATH_POSITION_SETTINGS,
            {"symbol": toobit_contract_symbol(symbol)}, signed=True, kind="trade",
        ))

    def set_margin_type(self, symbol: str, margin_type: str = "ISOLATED") -> Any:
        return self._request(
            "POST", config.PATH_MARGIN_MODE,
            {"symbol": toobit_contract_symbol(symbol), "marginType": margin_type.upper()},
            signed=True, kind="trade",
        )

    def set_leverage(self, symbol: str, leverage: int) -> Any:
        return self._request(
            "POST", config.PATH_LEVERAGE,
            {"symbol": toobit_contract_symbol(symbol), "leverage": int(leverage)},
            signed=True, kind="trade",
        )

    def prepare_symbol_for_trade(self, symbol: str, leverage: int) -> None:
        if self.has_open_position(symbol):
            raise ToobitError(f"برای {symbol} پوزیشن باز وجود دارد")
        if self.has_open_order(symbol):
            raise ToobitError(f"برای {symbol} سفارش باز وجود دارد")
        current_margin = None
        current_leverage = None
        try:
            for item in self._read_position_settings(symbol):
                raw_margin = str(item.get("marginType") or item.get("marginMode") or "").upper()
                if raw_margin:
                    current_margin = "ISOLATED" if raw_margin in {"ISOLATED", "ISOLATE", "TRUE", "1"} else raw_margin
                lev = self._first_decimal(item, "leverage", "isolatedLeverage", "crossLeverage")
                if lev is not None and lev > 0:
                    current_leverage = int(lev)
        except Exception as exc:
            logger.warning("خواندن تنظیمات پوزیشن ناموفق بود: %s", exc)
        if current_margin != "ISOLATED":
            try:
                self.set_margin_type(symbol, "ISOLATED")
            except Exception as exc:
                text = str(exc).lower()
                if "already" not in text and "no need" not in text and "isolated" not in text:
                    raise
        if current_leverage != int(leverage):
            self.set_leverage(symbol, int(leverage))

    @staticmethod
    def _round_price(value: Decimal, tick: Decimal, direction: str, is_tp: bool) -> Decimal:
        if tick <= 0:
            return value
        units = value / tick
        if direction == "LONG":
            mode = ROUND_UP if is_tp else ROUND_DOWN
        else:
            mode = ROUND_DOWN if is_tp else ROUND_UP
        out = units.to_integral_value(rounding=mode) * tick
        return out if out > 0 else units.to_integral_value(rounding=ROUND_HALF_UP) * tick

    @staticmethod
    def _api_decimal(value: Decimal | float) -> str:
        return format(Decimal(str(value)).normalize(), "f")

    def get_symbol_rules(self, info: dict[str, Any]) -> tuple[str, str, float, float]:
        lot = extract_filter(info, "LOT_SIZE")
        price = extract_filter(info, "PRICE_FILTER")
        notional = extract_filter(info, "MIN_NOTIONAL")
        step = str(lot.get("stepSize") or lot.get("quantityStep") or info.get("stepSize") or info.get("quantityStep") or "0.0001")
        tick = str(price.get("tickSize") or info.get("tickSize") or info.get("priceTick") or "0.0001")
        min_qty = safe_float(lot.get("minQty") or info.get("minQty") or info.get("minQuantity"))
        min_notional = safe_float(info.get("minNotional") or info.get("minOrderValue") or notional.get("minNotional") or notional.get("notional"))
        return step, tick, min_qty, min_notional

    def place_market_order(
        self,
        *,
        symbol: str,
        side: str,
        entry_price: float,
        margin_usdt: float,
        leverage: int,
        tp_price: float,
        sl_price: float,
        client_order_id: str,
        symbol_info: dict[str, Any],
    ) -> dict[str, Any]:
        contract = toobit_contract_symbol(symbol)
        direction = side_to_position(side)
        self.prepare_symbol_for_trade(symbol, leverage)
        entry = Decimal(str(entry_price if entry_price > 0 else self.get_mark_price(symbol)))
        step, tick, min_qty, min_notional = self.get_symbol_rules(symbol_info)
        requested_notional = Decimal(str(margin_usdt)) * Decimal(str(leverage))
        if min_notional > 0 and requested_notional < Decimal(str(min_notional)):
            raise ToobitError(f"ارزش پوزیشن {requested_notional} کمتر از حداقل {min_notional} است")
        quantity = Decimal(decimal_round_down(requested_notional / entry, step=step, digits=8))
        if quantity <= 0 or (min_qty > 0 and quantity < Decimal(str(min_qty))):
            raise ToobitError(f"حجم سفارش بعد از گردکردن کمتر از حداقل است: {quantity}")
        actual_notional = quantity * entry
        if min_notional > 0 and actual_notional < Decimal(str(min_notional)):
            raise ToobitError(f"Notional قابل اجرا {actual_notional} کمتر از حداقل {min_notional} است")
        tick_dec = Decimal(str(tick))
        tp = self._round_price(Decimal(str(tp_price)), tick_dec, direction, True)
        sl = self._round_price(Decimal(str(sl_price)), tick_dec, direction, False)
        params = {
            "symbol": contract,
            "side": side_to_open(side),
            "type": "LIMIT",
            "priceType": "MARKET",
            "quantity": self._api_decimal(quantity),
            "newClientOrderId": client_order_id,
            "takeProfit": self._api_decimal(tp),
            "tpOrderType": "MARKET",
            "tpTriggerBy": "CONTRACT_PRICE",
            "stopLoss": self._api_decimal(sl),
            "slOrderType": "MARKET",
            "slTriggerBy": "CONTRACT_PRICE",
        }
        raw = self._request("POST", config.PATH_ORDER, params, signed=True, kind="trade")
        order_id = None
        for item in self._extract_dicts(raw):
            for key in ("orderId", "order_id", "id", "clientOrderId", "newClientOrderId"):
                if item.get(key) not in (None, ""):
                    order_id = str(item[key])
                    break
            if order_id:
                break
        return {
            "submitted": True,
            "symbol": contract,
            "side": side,
            "order_id": order_id,
            "quantity": float(quantity),
            "entry_price_requested": float(entry),
            "tp_price": float(tp),
            "sl_price": float(sl),
            "requested_margin_usdt": float(margin_usdt),
            "actual_margin_usdt": float(actual_notional / Decimal(str(leverage))),
            "notional_usdt": float(actual_notional),
            "leverage": int(leverage),
            "raw": raw if isinstance(raw, dict) else {"response": raw},
        }

    def set_trading_stop(self, symbol: str, side: str, tp_price: float, sl_price: float, size: str | None = None) -> Any:
        params = {
            "symbol": toobit_contract_symbol(symbol),
            "side": side_to_position(side),
            "takeProfit": decimal_round_down(tp_price, digits=8),
            "stopLoss": decimal_round_down(sl_price, digits=8),
            "tpTriggerBy": "CONTRACT_PRICE",
            "slTriggerBy": "CONTRACT_PRICE",
            "stopType": "FIXED_STOP",
        }
        if size:
            params["tpSize"] = size
            params["slSize"] = size
        return self._request("POST", config.PATH_TRADING_STOP, params, signed=True, kind="trade")

    def flash_close(self, symbol: str, side: str) -> Any:
        return self._request(
            "POST", config.PATH_FLASH_CLOSE,
            {"symbol": toobit_contract_symbol(symbol), "side": side_to_position(side)},
            signed=True, kind="trade",
        )

    def get_history_positions(self, symbol: str, start_ms: int, end_ms: int, limit: int = 100) -> list[dict[str, Any]]:
        return self._extract_dicts(self._request(
            "GET", config.PATH_HISTORY_POSITIONS,
            {"symbol": toobit_contract_symbol(symbol), "startTime": int(start_ms), "endTime": int(end_ms), "limit": max(1, min(limit, 1000))},
            signed=True, kind="trade",
        ))

    def get_order_history(self, symbol: str, start_ms: int, end_ms: int, limit: int = 100) -> list[dict[str, Any]]:
        params = {"symbol": toobit_contract_symbol(symbol), "startTime": int(start_ms), "endTime": int(end_ms), "limit": max(1, min(limit, 1000))}
        for path in (config.PATH_ORDER_HISTORY, config.PATH_ORDER_HISTORY_ALT):
            try:
                rows = self._extract_dicts(self._request("GET", path, params, signed=True, kind="trade"))
                if rows:
                    return rows
            except Exception as exc:
                logger.warning("order history %s ناموفق: %s", path, exc)
        return []

    def find_realized_result(
        self,
        *,
        symbol: str,
        side: str,
        start_ms: int,
        end_ms: int | None = None,
        order_id: str | None = None,
        client_order_id: str | None = None,
    ) -> dict[str, Any] | None:
        end_ms = int(end_ms or time.time() * 1000)
        start_window = max(0, int(start_ms) - 5000)
        end_window = end_ms + 120_000
        requested_ids = {str(x) for x in (order_id, client_order_id) if x}
        rows: list[dict[str, Any]] = []
        try:
            rows.extend(self.get_history_positions(symbol, start_window, end_window))
        except Exception as exc:
            logger.warning("historyPositions ناموفق: %s", exc)
        try:
            rows.extend(self.get_order_history(symbol, start_window, end_window))
        except Exception as exc:
            logger.warning("historyOrders ناموفق: %s", exc)
        target = side_to_position(side)
        candidates = []
        for item in rows:
            raw_side = str(item.get("side") or item.get("positionSide") or item.get("direction") or "").upper()
            allowed = {"LONG", "BUY", "BUY_OPEN", "SELL_CLOSE"} if target == "LONG" else {"SHORT", "SELL", "SELL_OPEN", "BUY_CLOSE"}
            if raw_side and raw_side not in allowed:
                continue
            status = str(item.get("status") or item.get("orderStatus") or item.get("state") or "").upper()
            if status in {"NEW", "PARTIALLY_FILLED", "OPEN", "ORDER_NEW"}:
                continue
            pnl = self._first_decimal(item, "realizedPnL", "realizedPnl", "closedPnl", "profit", "pnl", "realProfit", "income")
            if pnl is None:
                continue
            close_time = safe_int(item.get("closeTime") or item.get("updatedTime") or item.get("updateTime") or item.get("time") or item.get("transactTime"))
            if close_time and close_time < start_window:
                continue
            close_price = self._first_decimal(item, "closePrice", "avgClosePrice", "exitPrice", "avgPrice", "price", "triggerPrice")
            ids = {str(item.get(k)) for k in ("orderId", "order_id", "clientOrderId", "newClientOrderId", "origClientOrderId", "positionId") if item.get(k)}
            candidates.append({
                "pnl": float(pnl),
                "close_time_ms": close_time,
                "close_price": float(close_price) if close_price is not None else None,
                "identifier_match": bool(requested_ids.intersection(ids)),
                "raw": item,
            })
        if not candidates:
            return None
        candidates.sort(key=lambda x: (x["identifier_match"], x["close_time_ms"]), reverse=True)
        return candidates[0]
