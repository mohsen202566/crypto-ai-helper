"""کلاینت Toobit Futures/Contract.
بر اساس سبک امضا و request فایل Spot موجود، اما مخصوص فیوچرز، ایزوله، لوریج و TP/SL همزمان.
نکته: مسیرهای API در config قابل تنظیم هستند تا با نسخه واقعی Toobit هماهنگ شوند.
"""
from __future__ import annotations

import hashlib
import hmac
import time
from decimal import Decimal, ROUND_DOWN
from typing import Any
from urllib.parse import urlencode

import requests

import config

class ToobitError(RuntimeError):
    pass

def safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return default
        return float(v)
    except Exception:
        return default

def api_num(value: float, digits: int = 8) -> str:
    d = Decimal(str(value)).quantize(Decimal("1." + "0" * digits), rounding=ROUND_DOWN)
    return format(d.normalize(), "f")

class ToobitFuturesClient:
    def __init__(self, base_url: str = config.TOOBIT_BASE_URL, timeout: int = config.REQUEST_TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.api_key = config.TOOBIT_API_KEY.strip()
        self.api_secret = config.TOOBIT_API_SECRET.strip()
        self.session = requests.Session()
        self._rules_cache: dict[str, dict[str, float | str]] = {}

    @property
    def has_credentials(self) -> bool:
        return bool(self.api_key and self.api_secret)

    def _sign(self, params: dict[str, Any]) -> str:
        query = urlencode(params, doseq=True)
        return hmac.new(self.api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()

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
            m = method.upper()
            if m == "GET":
                r = self.session.get(url, params=params, headers=headers, timeout=self.timeout)
            elif m == "POST":
                r = self.session.post(url, data=params, headers=headers, timeout=self.timeout)
            elif m == "DELETE":
                r = self.session.delete(url, data=params, headers=headers, timeout=self.timeout)
            else:
                raise ToobitError(f"متد پشتیبانی نمی‌شود: {method}")
            if r.status_code >= 400:
                raise ToobitError(f"HTTP {r.status_code}: {r.text[:500]}")
            payload = r.json()
        except Exception as exc:
            if isinstance(exc, ToobitError):
                raise
            raise ToobitError(f"خطا در ارتباط با Toobit Futures: {exc}") from exc
        if isinstance(payload, dict):
            code = payload.get("code") or payload.get("retCode") or payload.get("status")
            if code not in (None, 0, 200, "0", "200", "OK", "ok", "success", "SUCCESS", True):
                raise ToobitError(f"پاسخ ناموفق Toobit: {payload.get('msg') or payload.get('message') or payload}")
        return payload

    @staticmethod
    def _extract_dicts(payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return [x for x in payload if isinstance(x, dict)]
        if not isinstance(payload, dict):
            return []
        out: list[dict[str, Any]] = []
        for k in ("data", "result", "rows", "list", "positions", "orders", "assets"):
            v = payload.get(k)
            if isinstance(v, dict):
                out.append(v)
                out.extend(ToobitFuturesClient._extract_dicts(v))
            elif isinstance(v, list):
                out.extend(x for x in v if isinstance(x, dict))
        if not out:
            out.append(payload)
        return out

    @staticmethod
    def _extract_order_id(payload: Any) -> str | None:
        for item in ToobitFuturesClient._extract_dicts(payload):
            for key in ("orderId", "order_id", "id", "clientOrderId", "newClientOrderId"):
                v = item.get(key)
                if v not in (None, ""):
                    return str(v)
        return None

    def get_futures_exchange_info(self) -> Any:
        return self._request("GET", config.TOOBIT_FUTURES_PATH_EXCHANGE_INFO, signed=False)

    def get_symbol_rules(self, symbol: str) -> dict[str, float | str]:
        symbol = symbol.upper()
        cached = self._rules_cache.get(symbol)
        if cached:
            return cached
        payload = self.get_futures_exchange_info()
        rule: dict[str, float | str] = {"step": "0.000001", "tick": "0.000001", "min_qty": 0.0, "min_notional": 0.0}
        for item in self._extract_dicts(payload):
            item_symbol = str(item.get("symbol") or item.get("symbolName") or item.get("s") or "").upper()
            if item_symbol != symbol:
                continue
            filters = item.get("filters") if isinstance(item.get("filters"), list) else []
            for f in filters:
                if not isinstance(f, dict):
                    continue
                ft = str(f.get("filterType") or "").upper()
                if ft in ("LOT_SIZE", "MARKET_LOT_SIZE"):
                    rule["step"] = str(f.get("stepSize") or f.get("qtyStep") or rule["step"])
                    rule["min_qty"] = safe_float(f.get("minQty") or f.get("minQuantity"), float(rule["min_qty"]))
                elif ft == "PRICE_FILTER":
                    rule["tick"] = str(f.get("tickSize") or f.get("priceTick") or rule["tick"])
                elif ft in ("MIN_NOTIONAL", "NOTIONAL"):
                    rule["min_notional"] = safe_float(f.get("minNotional") or f.get("notional"), float(rule["min_notional"]))
            rule["step"] = str(item.get("quantityStep") or item.get("qtyStep") or rule["step"])
            rule["tick"] = str(item.get("tickSize") or item.get("priceTick") or rule["tick"])
            rule["min_qty"] = safe_float(item.get("minQty") or item.get("minQuantity"), float(rule["min_qty"]))
            rule["min_notional"] = safe_float(item.get("minNotional") or item.get("minOrderValue"), float(rule["min_notional"]))
            break
        self._rules_cache[symbol] = rule
        return rule

    @staticmethod
    def _round_down_step(value: float, step: str) -> str:
        d = Decimal(str(value))
        st = Decimal(str(step))
        if st <= 0:
            return api_num(value)
        units = (d / st).to_integral_value(rounding=ROUND_DOWN)
        return format((units * st).normalize(), "f")

    def get_futures_balance(self) -> dict[str, float]:
        payload = self._request("GET", config.TOOBIT_FUTURES_PATH_BALANCE, signed=True)
        best = {"available": 0.0, "total": 0.0, "margin": 0.0}
        for item in self._extract_dicts(payload):
            coin = str(item.get("asset") or item.get("coin") or item.get("currency") or "USDT").upper()
            if coin and coin != "USDT":
                continue
            best["available"] = max(best["available"], safe_float(item.get("available") or item.get("availableBalance") or item.get("free")))
            best["total"] = max(best["total"], safe_float(item.get("balance") or item.get("total") or item.get("walletBalance")))
            best["margin"] = max(best["margin"], safe_float(item.get("marginBalance") or item.get("equity") or item.get("total")))
        if best["margin"] <= 0 and best["available"] > 0:
            best["margin"] = best["available"]
        return best

    def set_isolated_margin(self, symbol: str) -> Any:
        params = {"symbol": symbol.upper(), "marginType": "ISOLATED"}
        return self._request("POST", config.TOOBIT_FUTURES_PATH_MARGIN_TYPE, params=params, signed=True)

    def set_leverage(self, symbol: str, leverage: int) -> Any:
        lev = max(config.LEVERAGE_MIN, min(int(leverage), config.LEVERAGE_MAX))
        params = {"symbol": symbol.upper(), "leverage": lev}
        return self._request("POST", config.TOOBIT_FUTURES_PATH_LEVERAGE, params=params, signed=True)

    def get_open_positions(self, symbol: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if symbol:
            params["symbol"] = symbol.upper()
        payload = self._request("GET", config.TOOBIT_FUTURES_PATH_POSITIONS, params=params, signed=True)
        rows: list[dict[str, Any]] = []
        for item in self._extract_dicts(payload):
            sym = str(item.get("symbol") or item.get("symbolName") or "").upper()
            if symbol and sym not in ("", symbol.upper()):
                continue
            size = safe_float(item.get("positionAmt") or item.get("size") or item.get("qty") or item.get("quantity"))
            if abs(size) > 0:
                rows.append(item)
        return rows

    def check_position_opened(self, symbol: str) -> bool:
        return bool(self.get_open_positions(symbol))

    def open_futures_position_with_tpsl(
        self,
        symbol: str,
        side: str,
        usdt_amount: float,
        leverage: int,
        entry_price: float,
        tp_price: float,
        sl_price: float,
        client_order_id: str,
    ) -> dict[str, Any]:
        """ارسال پوزیشن با TP/SL همراه.
        اگر Toobit نام پارامتر متفاوتی داشته باشد، فقط همین متد/params تنظیم می‌شود.
        """
        symbol = symbol.upper()
        side_u = side.upper()
        if config.ISOLATED_MARGIN_REQUIRED:
            self.set_isolated_margin(symbol)
        self.set_leverage(symbol, leverage)
        notional = float(usdt_amount) * float(leverage)
        raw_qty = notional / float(entry_price) if entry_price > 0 else 0.0
        rules = self.get_symbol_rules(symbol)
        qty_str = self._round_down_step(raw_qty, str(rules.get("step") or "0.000001"))
        qty = safe_float(qty_str)
        if qty <= 0 or qty < float(rules.get("min_qty") or 0.0):
            raise ToobitError("حجم سفارش پس از گردکردن کمتر از حد مجاز توبیت است")
        if float(rules.get("min_notional") or 0.0) > 0 and qty * entry_price < float(rules["min_notional"]):
            raise ToobitError("ارزش سفارش کمتر از حداقل مجاز توبیت است")
        order_side = "BUY" if side_u == "LONG" else "SELL"
        params = {
            "symbol": symbol,
            "side": order_side,
            "type": "MARKET",
            "quantity": qty_str,
            "newClientOrderId": client_order_id,
            "positionSide": "LONG" if side_u == "LONG" else "SHORT",
            "marginType": "ISOLATED",
            "leverage": int(leverage),
            "takeProfit": api_num(tp_price),
            "stopLoss": api_num(sl_price),
            "tpPrice": api_num(tp_price),
            "slPrice": api_num(sl_price),
        }
        raw = self._request("POST", config.TOOBIT_FUTURES_PATH_ORDER, params=params, signed=True)
        return {"order_id": self._extract_order_id(raw), "qty": qty, "raw": raw if isinstance(raw, dict) else {"response": raw}}

    def wait_position_opened(self, symbol: str, timeout_seconds: int = config.ORDER_OPEN_CHECK_SECONDS, poll_seconds: int = 5) -> bool:
        end = time.time() + max(1, int(timeout_seconds))
        while time.time() <= end:
            if self.check_position_opened(symbol):
                return True
            time.sleep(max(1, int(poll_seconds)))
        return self.check_position_opened(symbol)

    def get_order_history(self, symbol: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": max(1, min(int(limit), 1000))}
        if symbol:
            params["symbol"] = symbol.upper()
        payload = self._request("GET", config.TOOBIT_FUTURES_PATH_ORDER_HISTORY, params=params, signed=True)
        return self._extract_dicts(payload)

    @staticmethod
    def _order_symbol(item: dict[str, Any]) -> str:
        return str(item.get("symbol") or item.get("symbolName") or item.get("s") or "").upper()

    @staticmethod
    def _order_time_ms(item: dict[str, Any]) -> int:
        for key in ("updateTime", "transactTime", "time", "createdTime", "closeTime", "executedTime"):
            value = item.get(key)
            try:
                iv = int(value)
                return iv if iv > 10_000_000_000 else iv * 1000
            except (TypeError, ValueError):
                continue
        return 0

    def get_closed_trade_result(self, symbol: str, side: str, opened_at_ms: int) -> dict[str, Any] | None:
        """نتیجه واقعی را فقط از تاریخچه توبیت استخراج می‌کند.

        اگر پاسخ API اطلاعات قطعی خروج/PnL نداشته باشد None برمی‌گرداند تا مانیتور
        حدس نزند و در دور بعد دوباره بررسی کند.
        """
        rows = self.get_order_history(symbol=symbol, limit=200)
        candidates: list[dict[str, Any]] = []
        wanted_close_side = "SELL" if side.upper() == "LONG" else "BUY"
        for item in rows:
            if self._order_symbol(item) not in ("", symbol.upper()):
                continue
            ts = self._order_time_ms(item)
            if ts and ts < int(opened_at_ms):
                continue
            item_side = str(item.get("side") or item.get("orderSide") or "").upper()
            reduce_only = str(item.get("reduceOnly") or item.get("closePosition") or "").lower() in ("true", "1", "yes")
            status = str(item.get("status") or item.get("orderStatus") or item.get("state") or "").upper()
            filled = status in ("FILLED", "CLOSED", "DONE", "SUCCESS") or safe_float(item.get("executedQty") or item.get("filledQty") or item.get("cumQty")) > 0
            if not filled:
                continue
            if item_side and item_side != wanted_close_side and not reduce_only:
                continue
            candidates.append(item)
        if not candidates:
            return None
        item = max(candidates, key=self._order_time_ms)
        exit_price = safe_float(item.get("avgPrice") or item.get("averagePrice") or item.get("executedPrice") or item.get("price") or item.get("dealPrice"))
        realized = safe_float(item.get("realizedPnl") or item.get("realisedPnl") or item.get("closedPnl") or item.get("profit"), float("nan"))
        fee = safe_float(item.get("fee") or item.get("commission") or item.get("execFee") or item.get("tradeFee"))
        if exit_price <= 0:
            return None
        return {"exit_price": exit_price, "realized_pnl": realized, "fee": fee, "time_ms": self._order_time_ms(item), "raw": item}

