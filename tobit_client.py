# tobit_client.py
# Safe Toobit USDT-M Futures REST client
# Real orders are blocked unless REAL_TRADING_ENABLED=true

import os
import time
import hmac
import hashlib
import uuid
from decimal import Decimal, ROUND_DOWN
from urllib.parse import urlencode

import requests


TOBIT_BASE_URL = os.getenv("TOBIT_BASE_URL", "https://api.toobit.com")
TOBIT_API_KEY = os.getenv("TOBIT_API_KEY", "")
TOBIT_SECRET_KEY = os.getenv("TOBIT_SECRET_KEY", "")

REAL_TRADING_ENABLED = os.getenv("REAL_TRADING_ENABLED", "false").lower() == "true"
RECV_WINDOW = int(os.getenv("TOBIT_RECV_WINDOW", "5000"))


class ToobitClient:
    def __init__(self):
        self.base_url = TOBIT_BASE_URL.rstrip("/")
        self.api_key = TOBIT_API_KEY
        self.secret_key = TOBIT_SECRET_KEY

    def _now_ms(self):
        return int(time.time() * 1000)

    def _headers(self):
        return {
            "X-BB-APIKEY": self.api_key,
            "Content-Type": "application/x-www-form-urlencoded",
        }

    def _sign(self, params: dict) -> str:
        query = urlencode(params)
        return hmac.new(
            self.secret_key.encode("utf-8"),
            query.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _signed_request(self, method: str, path: str, params: dict | None = None):
        if not self.api_key or not self.secret_key:
            return {
                "ok": False,
                "error": "TOBIT_API_KEY یا TOBIT_SECRET_KEY تنظیم نشده است",
            }

        params = params or {}
        params["recvWindow"] = RECV_WINDOW
        params["timestamp"] = self._now_ms()
        params["signature"] = self._sign(params)

        url = f"{self.base_url}{path}"

        try:
            if method.upper() == "GET":
                r = requests.get(url, headers=self._headers(), params=params, timeout=15)
            elif method.upper() == "POST":
                r = requests.post(url, headers=self._headers(), data=params, timeout=15)
            elif method.upper() == "DELETE":
                r = requests.delete(url, headers=self._headers(), data=params, timeout=15)
            else:
                return {"ok": False, "error": f"Unsupported method: {method}"}

            try:
                data = r.json()
            except Exception:
                data = {"raw": r.text}

            return {
                "ok": r.status_code == 200,
                "status_code": r.status_code,
                "data": data,
            }

        except Exception as e:
            return {"ok": False, "error": str(e)}

    def normalize_futures_symbol(self, symbol: str) -> str:
        raw = str(symbol or "").upper().strip()

        if "-SWAP-USDT" in raw:
            return raw

        s = raw.replace("/", "").replace("-", "").replace("_", "")

        if s.endswith("USDT"):
            base = s[:-4]
            return f"{base}-SWAP-USDT"

        return raw

    def safe_decimal(self, value, precision: int = 6) -> str:
        q = Decimal("1." + ("0" * precision))
        return str(Decimal(str(value)).quantize(q, rounding=ROUND_DOWN))

    def get_account_balance(self):
        return self._signed_request("GET", "/api/v1/futures/balance")

    def get_position(self, symbol: str | None = None):
        params = {}
        if symbol:
            params["symbol"] = self.normalize_futures_symbol(symbol)
        return self._signed_request(
            "GET",
            "/api/v1/futures/positions",
            params,
        )

    def place_market_order(
        self,
        symbol: str,
        direction: str,
        quantity: float,
        take_profit: float | None = None,
        stop_loss: float | None = None,
    ):
        if not REAL_TRADING_ENABLED:
            return {
                "ok": False,
                "blocked": True,
                "error": "ترید واقعی غیرفعال است. REAL_TRADING_ENABLED=false",
            }

        symbol = self.normalize_futures_symbol(symbol)
        direction = direction.upper()

        if direction == "LONG":
            side = "BUY_OPEN"
        elif direction == "SHORT":
            side = "SELL_OPEN"
        else:
            return {"ok": False, "error": "direction باید LONG یا SHORT باشد"}

        if quantity <= 0:
            return {"ok": False, "error": "quantity باید بیشتر از صفر باشد"}

        params = {
            "symbol": symbol,
            "side": side,
            "type": "LIMIT",
            "priceType": "MARKET",
            "quantity": self.safe_decimal(quantity, 6),
            "newClientOrderId": f"bot_{uuid.uuid4().hex[:24]}",
        }

        if take_profit:
            params["takeProfit"] = str(take_profit)
            params["tpOrderType"] = "MARKET"

        if stop_loss:
            params["stopLoss"] = str(stop_loss)
            params["slOrderType"] = "MARKET"

        return self._signed_request("POST", "/api/v1/futures/order", params)

    def close_market_position(self, symbol: str, direction: str, quantity: float):
        if not REAL_TRADING_ENABLED:
            return {
                "ok": False,
                "blocked": True,
                "error": "ترید واقعی غیرفعال است. REAL_TRADING_ENABLED=false",
            }

        symbol = self.normalize_futures_symbol(symbol)
        direction = direction.upper()

        if direction == "LONG":
            side = "SELL_CLOSE"
        elif direction == "SHORT":
            side = "BUY_CLOSE"
        else:
            return {"ok": False, "error": "direction باید LONG یا SHORT باشد"}

        params = {
            "symbol": symbol,
            "side": side,
            "type": "LIMIT",
            "priceType": "MARKET",
            "quantity": self.safe_decimal(quantity, 6),
            "newClientOrderId": f"bot_close_{uuid.uuid4().hex[:18]}",
        }

        return self._signed_request("POST", "/api/v1/futures/order", params)


toobit_client = ToobitClient()
