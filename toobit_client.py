"""کلاینت فیوچرز USDT-M توبیت با ایزوله، لوریج، TP/SL و نتیجه دقیق."""
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


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def api_number(value: float, digits: int = 10) -> str:
    quant = Decimal("1." + "0" * digits)
    return format(Decimal(str(value)).quantize(quant, rounding=ROUND_DOWN).normalize(), "f")


class ToobitFuturesClient:
    def __init__(self) -> None:
        self.base_url = config.TOOBIT_BASE_URL
        self.timeout = config.TOOBIT_REQUEST_TIMEOUT
        self.api_key = config.TOOBIT_API_KEY
        self.api_secret = config.TOOBIT_API_SECRET
        self.session = requests.Session()
        self._rules: dict[str, dict[str, Any]] = {}

    @property
    def has_credentials(self) -> bool:
        return bool(self.api_key and self.api_secret)

    def _sign(self, params: dict[str, Any]) -> str:
        query = urlencode(params, doseq=True)
        return hmac.new(self.api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        signed: bool = False,
    ) -> Any:
        data = dict(params or {})
        headers: dict[str, str] = {}
        if signed:
            if not self.has_credentials:
                raise ToobitError("کلید API توبیت تنظیم نشده است")
            data.setdefault("timestamp", int(time.time() * 1000))
            data.setdefault("recvWindow", config.TOOBIT_RECV_WINDOW)
            data["signature"] = self._sign(data)
            headers["X-BB-APIKEY"] = self.api_key
        url = f"{self.base_url}{path}"
        try:
            method_u = method.upper()
            if method_u == "GET":
                response = self.session.get(url, params=data, headers=headers, timeout=self.timeout)
            elif method_u == "POST":
                response = self.session.post(url, data=data, headers=headers, timeout=self.timeout)
            elif method_u == "DELETE":
                response = self.session.delete(url, data=data, headers=headers, timeout=self.timeout)
            else:
                raise ToobitError(f"متد پشتیبانی نمی‌شود: {method}")
            if response.status_code >= 400:
                raise ToobitError(f"HTTP {response.status_code}: {response.text[:400]}")
            payload = response.json()
        except ToobitError:
            raise
        except Exception as exc:
            raise ToobitError(f"خطا در ارتباط با توبیت: {exc}") from exc
        if isinstance(payload, dict):
            code = payload.get("code")
            if code not in (None, 0, 200, "0", "200"):
                raise ToobitError(str(payload.get("msg") or payload.get("message") or payload))
        return payload

    @staticmethod
    def _rows(payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return [row for row in payload if isinstance(row, dict)]
        if not isinstance(payload, dict):
            return []
        for key in ("data", "result", "rows", "list", "positions", "orders", "assets"):
            value = payload.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
            if isinstance(value, dict):
                nested = ToobitFuturesClient._rows(value)
                if nested:
                    return nested
        return [payload]

    def list_usdt_contracts(self) -> dict[str, str]:
        payload = self._request("GET", config.TOOBIT_PATH_EXCHANGE_INFO)
        contracts: list[dict[str, Any]] = []
        if isinstance(payload, dict) and isinstance(payload.get("contracts"), list):
            contracts = [row for row in payload["contracts"] if isinstance(row, dict)]
        else:
            for row in self._rows(payload):
                if "underlying" in row or "marginToken" in row:
                    contracts.append(row)
        out: dict[str, str] = {}
        for row in contracts:
            base = str(row.get("underlying") or "").upper()
            symbol = str(row.get("symbol") or row.get("symbolName") or "").upper()
            margin = str(row.get("marginToken") or row.get("quoteAsset") or "").upper()
            status = str(row.get("status") or "").upper()
            inverse = bool(row.get("inverse", False))
            if base and symbol and margin == "USDT" and not inverse and status == "TRADING":
                out[base] = symbol
                self._rules[symbol] = self._parse_rule(row)
        return out

    @staticmethod
    def _parse_rule(row: dict[str, Any]) -> dict[str, Any]:
        rule: dict[str, Any] = {
            "step": "0.000001",
            "tick": "0.000001",
            "min_qty": 0.0,
            "min_notional": 0.0,
        }
        for item in row.get("filters") or []:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("filterType") or "").upper()
            if kind in ("LOT_SIZE", "MARKET_LOT_SIZE"):
                rule["step"] = str(item.get("stepSize") or rule["step"])
                rule["min_qty"] = safe_float(item.get("minQty"), rule["min_qty"])
            elif kind == "PRICE_FILTER":
                rule["tick"] = str(item.get("tickSize") or rule["tick"])
            elif kind in ("MIN_NOTIONAL", "NOTIONAL"):
                rule["min_notional"] = safe_float(item.get("minNotional"), rule["min_notional"])
        return rule

    def get_symbol_rules(self, symbol: str) -> dict[str, Any]:
        symbol_u = symbol.upper()
        if symbol_u not in self._rules:
            self.list_usdt_contracts()
        return self._rules.get(
            symbol_u,
            {"step": "0.000001", "tick": "0.000001", "min_qty": 0.0, "min_notional": 0.0},
        )

    @staticmethod
    def _round_down(value: float, step: str) -> str:
        number = Decimal(str(value))
        unit = Decimal(str(step))
        if unit <= 0:
            return api_number(value)
        return format(((number / unit).to_integral_value(rounding=ROUND_DOWN) * unit).normalize(), "f")

    def get_balance(self) -> dict[str, float]:
        payload = self._request("GET", config.TOOBIT_PATH_BALANCE, signed=True)
        result = {"available": 0.0, "total": 0.0, "margin": 0.0}
        for row in self._rows(payload):
            asset = str(row.get("asset") or row.get("coin") or row.get("currency") or "USDT").upper()
            if asset != "USDT":
                continue
            result["available"] = max(
                result["available"], safe_float(row.get("availableBalance") or row.get("available") or row.get("free"))
            )
            result["total"] = max(
                result["total"], safe_float(row.get("balance") or row.get("total") or row.get("walletBalance"))
            )
            result["margin"] = max(
                result["margin"],
                safe_float(
                    row.get("positionMargin")
                    or row.get("marginBalance")
                    or row.get("equity")
                    or row.get("total")
                ),
            )
        if result["margin"] <= 0:
            result["margin"] = result["total"] or result["available"]
        return result

    def set_isolated(self, symbol: str) -> None:
        try:
            self._request(
                "POST", config.TOOBIT_PATH_MARGIN_TYPE,
                {"symbol": symbol.upper(), "marginType": "ISOLATED"}, signed=True,
            )
        except ToobitError as exc:
            # Exchanges often return an error when the requested mode is already active.
            text = str(exc).lower()
            if "same" not in text and "already" not in text and "no need" not in text:
                raise

    def set_leverage(self, symbol: str, leverage: int) -> None:
        self._request(
            "POST", config.TOOBIT_PATH_LEVERAGE,
            {"symbol": symbol.upper(), "leverage": int(leverage)}, signed=True,
        )

    def get_open_positions(self, symbol: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if symbol:
            params["symbol"] = symbol.upper()
        payload = self._request("GET", config.TOOBIT_PATH_POSITIONS, params, signed=True)
        out: list[dict[str, Any]] = []
        for row in self._rows(payload):
            sym = str(row.get("symbol") or "").upper()
            if symbol and sym != symbol.upper():
                continue
            qty = safe_float(row.get("position") or row.get("positionAmt") or row.get("size") or row.get("qty"))
            if abs(qty) > 0:
                out.append(row)
        return out

    def is_position_open(self, symbol: str) -> bool:
        return bool(self.get_open_positions(symbol))

    def set_trading_stop(self, symbol: str, side: str, tp: float, sl: float, quantity: str) -> Any:
        return self._request(
            "POST",
            config.TOOBIT_PATH_TRADING_STOP,
            {
                "symbol": symbol.upper(),
                "side": side.upper(),
                "takeProfit": api_number(tp),
                "stopLoss": api_number(sl),
                "tpTriggerBy": "CONTRACT_PRICE",
                "slTriggerBy": "CONTRACT_PRICE",
                "tpSize": quantity,
                "slSize": quantity,
                "stopType": "FIXED_STOP",
            },
            signed=True,
        )

    def open_position(
        self,
        symbol: str,
        side: str,
        margin_usdt: float,
        leverage: int,
        reference_price: float,
        tp: float,
        sl: float,
        client_order_id: str,
    ) -> dict[str, Any]:
        symbol_u = symbol.upper()
        side_u = side.upper()
        self.set_isolated(symbol_u)
        self.set_leverage(symbol_u, leverage)
        notional = float(margin_usdt) * int(leverage)
        rules = self.get_symbol_rules(symbol_u)
        raw_qty = notional / reference_price
        quantity = self._round_down(raw_qty, str(rules["step"]))
        qty_value = safe_float(quantity)
        if qty_value <= 0 or qty_value < float(rules.get("min_qty") or 0.0):
            raise ToobitError("حجم سفارش پس از گردکردن کمتر از حد مجاز است")
        if float(rules.get("min_notional") or 0.0) > qty_value * reference_price:
            raise ToobitError("ارزش سفارش کمتر از حداقل مجاز توبیت است")

        params = {
            "symbol": symbol_u,
            "side": "BUY_OPEN" if side_u == "LONG" else "SELL_OPEN",
            "type": "LIMIT",
            "priceType": "MARKET",
            "quantity": quantity,
            "newClientOrderId": client_order_id,
            "takeProfit": api_number(tp),
            "stopLoss": api_number(sl),
            "tpTriggerBy": "CONTRACT_PRICE",
            "slTriggerBy": "CONTRACT_PRICE",
            "tpOrderType": "MARKET",
            "slOrderType": "MARKET",
        }
        raw = self._request("POST", config.TOOBIT_PATH_ORDER, params, signed=True)
        order_id = ""
        for row in self._rows(raw):
            value = row.get("orderId") or row.get("id")
            if value not in (None, ""):
                order_id = str(value)
                break
        return {"order_id": order_id, "quantity": quantity, "raw": raw}

    def wait_and_protect(
        self, symbol: str, side: str, tp: float, sl: float, quantity: str, timeout_seconds: int
    ) -> bool:
        deadline = time.time() + max(1, timeout_seconds)
        while time.time() < deadline:
            if self.is_position_open(symbol):
                self.set_trading_stop(symbol, side, tp, sl, quantity)
                return True
            time.sleep(4)
        return False

    def get_closed_position(self, symbol: str, side: str, opened_at_ms: int) -> dict[str, Any] | None:
        payload = self._request(
            "GET",
            config.TOOBIT_PATH_HISTORY_POSITIONS,
            {"symbol": symbol.upper(), "startTime": max(0, int(opened_at_ms) - 120_000), "limit": 100},
            signed=True,
        )
        candidates: list[dict[str, Any]] = []
        for row in self._rows(payload):
            if str(row.get("symbol") or "").upper() != symbol.upper():
                continue
            if str(row.get("side") or "").upper() != side.upper():
                continue
            if str(row.get("status") or "CLOSED").upper() != "CLOSED":
                continue
            open_time = int(safe_float(row.get("openTime"), 0.0))
            close_time = int(safe_float(row.get("closeTime"), 0.0))
            if open_time and open_time < opened_at_ms - 120_000:
                continue
            if close_time <= 0 or close_time < opened_at_ms:
                continue
            candidates.append(row)
        if not candidates:
            return None
        row = max(candidates, key=lambda item: int(safe_float(item.get("closeTime"), 0.0)))
        entry = safe_float(row.get("openAvgPrice"))
        close = safe_float(row.get("closeAvgPrice"))
        net = safe_float(row.get("realizedPnL"))
        gross = safe_float(row.get("realizedPnlWithoutFee"), float("nan"))
        open_fee = abs(safe_float(row.get("openFee")))
        close_fee = abs(safe_float(row.get("closeFee")))
        fees = open_fee + close_fee
        if gross != gross:  # NaN
            gross = net + fees
        if fees <= 0:
            fees = max(0.0, gross - net)
        if entry <= 0 or close <= 0:
            return None
        close_reason = ""
        for key in (
            "closeType", "closeReason", "positionCloseType", "triggerType",
            "orderType", "type", "execType", "source",
        ):
            value = row.get(key)
            if value not in (None, ""):
                close_reason = str(value)
                break
        return {
            "entry_price": entry,
            "close_price": close,
            "gross_pnl": gross,
            "fees": fees,
            "net_pnl": net,
            "open_time": int(safe_float(row.get("openTime"), opened_at_ms)),
            "close_time": int(safe_float(row.get("closeTime"), 0.0)),
            "close_reason": close_reason,
            "raw": row,
        }
