# tobit_client.py
# Safe Toobit USDT-M Futures REST client
# Real orders are blocked unless REAL_TRADING_ENABLED=true
#
# Notes:
# - Keep filename as tobit_client.py because real_trade_manager.py imports it.
# - Provides backward-compatible aliases:
#     ToobitClient, ToBitClient, toobit_client
#     get_account_balance(), get_balance()
#     get_position(), get_positions()

import os
import time
import hmac
import hashlib
import uuid
from decimal import Decimal, ROUND_DOWN, InvalidOperation
from urllib.parse import urlencode

import requests


TOBIT_BASE_URL = os.getenv("TOBIT_BASE_URL", "https://api.toobit.com").rstrip("/")
TOBIT_API_KEY = os.getenv("TOBIT_API_KEY", "").strip()
TOBIT_SECRET_KEY = os.getenv("TOBIT_SECRET_KEY", "").strip()

REAL_TRADING_ENABLED = os.getenv("REAL_TRADING_ENABLED", "false").strip().lower() == "true"
RECV_WINDOW = int(os.getenv("TOBIT_RECV_WINDOW", "5000") or "5000")
REQUEST_TIMEOUT = int(os.getenv("TOBIT_REQUEST_TIMEOUT", "15") or "15")


class ToobitClient:
    """Minimal, safe Toobit USDT-M futures REST client."""

    def __init__(self, api_key: str | None = None, secret_key: str | None = None, base_url: str | None = None):
        self.base_url = (base_url or TOBIT_BASE_URL).rstrip("/")
        self.api_key = (api_key if api_key is not None else TOBIT_API_KEY).strip()
        self.secret_key = (secret_key if secret_key is not None else TOBIT_SECRET_KEY).strip()

    def _now_ms(self) -> int:
        return int(time.time() * 1000)

    def _headers(self, json_body: bool = False) -> dict:
        # Toobit official docs use X-BB-APIKEY for signed routes.
        headers = {
            "X-BB-APIKEY": self.api_key,
            "User-Agent": "crypto-ai-helper/1.0",
        }
        headers["Content-Type"] = "application/json" if json_body else "application/x-www-form-urlencoded"
        return headers

    def _sign(self, params: dict) -> str:
        # Official examples build the query string from params excluding signature,
        # then HMAC-SHA256 with the Secret Key.
        clean_params = {k: v for k, v in params.items() if k != "signature" and v is not None}
        query = urlencode(clean_params)
        return hmac.new(
            self.secret_key.encode("utf-8"),
            query.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _is_success_payload(self, data) -> bool:
        # Some Toobit endpoints return list/dict directly on success.
        # Error payloads commonly return {"code": -2015, "msg": "..."}.
        if isinstance(data, dict) and "code" in data:
            try:
                return int(data.get("code")) == 200
            except Exception:
                return False
        return True

    def _normalize_error(self, status_code: int | None, data, text: str = "") -> str:
        if isinstance(data, dict):
            code = data.get("code")
            msg = data.get("msg") or data.get("message") or data.get("error")
            if code is not None or msg:
                return f"Toobit error code={code}, msg={msg}"
        if text:
            return text[:500]
        return f"HTTP status {status_code}"

    def _signed_request(self, method: str, path: str, params: dict | None = None, *, json_body: bool = False):
        if not self.api_key or not self.secret_key:
            return {
                "ok": False,
                "error": "TOBIT_API_KEY یا TOBIT_SECRET_KEY تنظیم نشده است",
                "hint": "کلیدها باید در systemd service یا .env تنظیم شوند.",
            }

        method = method.upper().strip()
        signed_params = dict(params or {})
        signed_params["recvWindow"] = RECV_WINDOW
        signed_params["timestamp"] = self._now_ms()
        signed_params["signature"] = self._sign(signed_params)

        url = f"{self.base_url}{path}"

        try:
            if method == "GET":
                response = requests.get(
                    url,
                    headers=self._headers(json_body=False),
                    params=signed_params,
                    timeout=REQUEST_TIMEOUT,
                )
            elif method == "POST":
                if json_body:
                    # Signature remains in query string; JSON body contains the original params without signature.
                    query_params = {
                        "recvWindow": signed_params["recvWindow"],
                        "timestamp": signed_params["timestamp"],
                        "signature": signed_params["signature"],
                    }
                    body = {k: v for k, v in (params or {}).items() if v is not None}
                    response = requests.post(
                        url,
                        headers=self._headers(json_body=True),
                        params=query_params,
                        json=body,
                        timeout=REQUEST_TIMEOUT,
                    )
                else:
                    response = requests.post(
                        url,
                        headers=self._headers(json_body=False),
                        data=signed_params,
                        timeout=REQUEST_TIMEOUT,
                    )
            elif method == "DELETE":
                response = requests.delete(
                    url,
                    headers=self._headers(json_body=False),
                    params=signed_params,
                    timeout=REQUEST_TIMEOUT,
                )
            else:
                return {"ok": False, "error": f"Unsupported method: {method}"}

            raw_text = response.text
            try:
                data = response.json()
            except Exception:
                data = {"raw": raw_text}

            ok = response.status_code == 200 and self._is_success_payload(data)

            result = {
                "ok": ok,
                "status_code": response.status_code,
                "data": data,
                "path": path,
            }

            if not ok:
                result["error"] = self._normalize_error(response.status_code, data, raw_text)
                if isinstance(data, dict) and data.get("code") == -2015:
                    result["hint"] = (
                        "Access Key / Secret Key / IP whitelist / permission را چک کن. "
                        "اگر همه درست است، API جدید بساز چون Secret Key فقط زمان ساخت قابل مشاهده است."
                    )

            return result

        except requests.RequestException as e:
            return {"ok": False, "error": f"Network error: {e}", "path": path}
        except Exception as e:
            return {"ok": False, "error": f"Unexpected error: {e}", "path": path}

    def ping(self):
        try:
            r = requests.get(f"{self.base_url}/api/v1/time", timeout=REQUEST_TIMEOUT)
            try:
                data = r.json()
            except Exception:
                data = {"raw": r.text}
            return {"ok": r.status_code == 200, "status_code": r.status_code, "data": data}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def debug_env_masked(self):
        def mask(v: str) -> str:
            if not v:
                return ""
            if len(v) <= 10:
                return v[:2] + "***" + v[-2:]
            return v[:6] + "***" + v[-6:]

        return {
            "base_url": self.base_url,
            "api_key": mask(self.api_key),
            "secret_key": mask(self.secret_key),
            "recv_window": RECV_WINDOW,
            "real_trading_enabled": REAL_TRADING_ENABLED,
        }


    def debug_balance(self):
        result = self.get_balance()
        print("\n" + "=" * 80)
        print("TOOBIT BALANCE DEBUG")
        print("ENV:", self.debug_env_masked())
        print("RESULT:", result)
        print("=" * 80 + "\n")
        return result

    def normalize_futures_symbol(self, symbol: str) -> str:
        raw = str(symbol or "").upper().strip()
        if not raw:
            return raw

        if raw.endswith("-SWAP-USDT") or raw.endswith("-SWAP-USDC"):
            return raw

        s = raw.replace("/", "").replace("_", "").replace("-", "")
        if s.endswith("USDT"):
            return f"{s[:-4]}-SWAP-USDT"
        if s.endswith("USDC"):
            return f"{s[:-4]}-SWAP-USDC"
        return raw

    def safe_decimal(self, value, precision: int = 6) -> str:
        try:
            q = Decimal("1." + ("0" * int(precision)))
            return str(Decimal(str(value)).quantize(q, rounding=ROUND_DOWN))
        except (InvalidOperation, ValueError, TypeError):
            return "0"

    # ---------- Account / position ----------
    def get_account_balance(self, category: str | None = None):
        params = {}
        if category:
            params["category"] = category
        return self._signed_request("GET", "/api/v1/futures/balance", params)

    def get_balance(self, category: str | None = None):
        return self.get_account_balance(category=category)

    def get_position(self, symbol: str | None = None, category: str | None = None):
        params = {}
        if symbol:
            params["symbol"] = self.normalize_futures_symbol(symbol)
        if category:
            params["category"] = category
        return self._signed_request("GET", "/api/v1/futures/positions", params)

    def get_positions(self, symbol: str | None = None, category: str | None = None):
        return self.get_position(symbol=symbol, category=category)

    def _flatten_position_items(self, result):
        """Return a flat list of position dicts from Toobit response shapes."""
        data = (result or {}).get("data")
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        if isinstance(data, dict):
            for key in ("data", "result", "list", "positions"):
                v = data.get(key)
                if isinstance(v, list):
                    return [x for x in v if isinstance(x, dict)]
            return [data]
        return []

    def _position_qty(self, item: dict) -> float:
        """Best-effort quantity extractor for Toobit futures position rows."""
        for key in ("positionAmt", "positionSize", "size", "qty", "quantity", "positionQuantity", "availablePosition"):
            try:
                v = item.get(key)
                if v is not None and str(v).strip() != "":
                    return abs(float(v))
            except Exception:
                pass
        return 0.0

    def _position_side_matches(self, item: dict, direction: str) -> bool:
        """Best-effort side matcher; if side is absent but qty > 0, accept it."""
        direction = str(direction or "").upper().strip()
        raw = " ".join(str(item.get(k, "")) for k in ("side", "positionSide", "direction", "positionType")).upper()
        if direction == "LONG":
            return ("LONG" in raw) or ("BUY" in raw) or (raw.strip() == "" and self._position_qty(item) > 0)
        if direction == "SHORT":
            return ("SHORT" in raw) or ("SELL" in raw) or (raw.strip() == "" and self._position_qty(item) > 0)
        return False

    def _has_open_position(self, symbol: str, direction: str, min_qty: float = 0.0):
        """
        Verify if an exchange position is actually open.
        Used as a safety recovery when Toobit returns an error after creating a position.
        """
        result = self.get_position(symbol=symbol)
        if not result.get("ok"):
            return False, result

        items = self._flatten_position_items(result)
        for item in items:
            qty = self._position_qty(item)
            if qty > max(float(min_qty or 0.0) * 0.25, 0.0) and self._position_side_matches(item, direction):
                return True, {"ok": True, "position": item, "raw": result}

        return False, result


    # ---------- Leverage ----------
    def _extract_leverage_value(self, data) -> float:
        """Best-effort leverage extractor from Toobit response shapes."""
        def walk(value):
            if isinstance(value, dict):
                yield value
                for v in value.values():
                    yield from walk(v)
            elif isinstance(value, list):
                for item in value:
                    yield from walk(item)

        for item in walk(data):
            if not isinstance(item, dict):
                continue
            for key in ("leverage", "lever", "leverageValue", "longLeverage", "shortLeverage"):
                if key not in item:
                    continue
                try:
                    value = float(item.get(key))
                    if value > 0:
                        return value
                except Exception:
                    pass
        return 0.0

    def set_leverage(self, symbol: str, leverage: float):
        """
        Set futures leverage for a symbol before opening a real position.

        Toobit API versions can expose slightly different leverage paths, so this
        method tries a small safe list of signed POST endpoints. The first
        successful response is returned. If all paths fail, the error includes
        every attempted path for debugging.
        """
        if not REAL_TRADING_ENABLED:
            return {
                "ok": False,
                "blocked": True,
                "error": "ترید واقعی غیرفعال است. REAL_TRADING_ENABLED=false",
            }

        symbol = self.normalize_futures_symbol(symbol)
        try:
            lev = int(float(leverage))
        except Exception:
            return {"ok": False, "error": "leverage نامعتبر است"}

        if lev <= 0:
            return {"ok": False, "error": "leverage باید بیشتر از صفر باشد"}

        request_variants = [
            {"symbol": symbol, "leverage": lev},
            {"symbol": symbol, "leverage": str(lev)},
            {"symbol": symbol, "longLeverage": lev, "shortLeverage": lev},
            {"symbol": symbol, "longLeverage": str(lev), "shortLeverage": str(lev)},
        ]
        candidate_paths = (
            "/api/v1/futures/leverage",
            "/api/v1/futures/position/leverage",
            "/api/v1/futures/set-leverage",
            "/api/v1/futures/setLeverage",
        )

        attempts = []
        for path in candidate_paths:
            for params in request_variants:
                result = self._signed_request("POST", path, params)
                attempts.append({
                    "path": path,
                    "params_keys": list(params.keys()),
                    "ok": bool(result.get("ok")),
                    "error": result.get("error"),
                    "data": result.get("data"),
                })
                if result.get("ok"):
                    self._last_set_leverage = {
                        "symbol": symbol,
                        "leverage": float(lev),
                        "set_result": result,
                        "set_at": self._now_ms(),
                    }
                    if isinstance(result.get("data"), dict):
                        result["data"].setdefault("leverage", lev)
                    else:
                        result["data"] = {"raw": result.get("data"), "leverage": lev}
                    result["actual_leverage"] = float(lev)
                    return result

        return {
            "ok": False,
            "error": "تنظیم لوریج در توبیت ناموفق بود",
            "attempts": attempts[-8:],
        }

    def get_symbol_leverage(self, symbol: str):
        """
        Read current futures leverage for a symbol.

        Priority:
        1) Open-position response if Toobit returns leverage there.
        2) Known leverage GET endpoints.
        3) Last accepted set_leverage result for the same symbol as a fallback.
        """
        symbol = self.normalize_futures_symbol(symbol)

        pos_result = self.get_position(symbol=symbol)
        if pos_result.get("ok"):
            value = self._extract_leverage_value(pos_result.get("data"))
            if value > 0:
                return {"ok": True, "data": {"symbol": symbol, "leverage": value}, "source": "positions", "raw": pos_result}

        candidate_paths = (
            "/api/v1/futures/leverage",
            "/api/v1/futures/position/leverage",
            "/api/v1/futures/position/margin",
            "/api/v1/futures/symbol/config",
        )
        attempts = []
        for path in candidate_paths:
            result = self._signed_request("GET", path, {"symbol": symbol})
            attempts.append({"path": path, "ok": bool(result.get("ok")), "error": result.get("error"), "data": result.get("data")})
            if result.get("ok"):
                value = self._extract_leverage_value(result.get("data"))
                if value > 0:
                    return {"ok": True, "data": {"symbol": symbol, "leverage": value}, "source": path, "raw": result}

        cached = getattr(self, "_last_set_leverage", None)
        if isinstance(cached, dict) and cached.get("symbol") == symbol:
            age_ms = self._now_ms() - int(cached.get("set_at", 0) or 0)
            if 0 <= age_ms <= 60000 and float(cached.get("leverage") or 0) > 0:
                return {
                    "ok": True,
                    "data": {"symbol": symbol, "leverage": float(cached.get("leverage"))},
                    "source": "last_accepted_set_leverage",
                    "warning": "Toobit readback endpoint did not expose leverage; using last accepted set_leverage response.",
                    "set_result": cached.get("set_result"),
                }

        return {
            "ok": False,
            "error": "تایید لوریج از توبیت ممکن نشد",
            "position_read": pos_result,
            "attempts": attempts[-6:],
        }

    def set_symbol_leverage(self, symbol: str, leverage: float):
        """Backward-compatible alias used by real_trade_manager.py."""
        return self.set_leverage(symbol, leverage)

    def change_leverage(self, symbol: str, leverage: float):
        """Backward-compatible alias used by real_trade_manager.py."""
        return self.set_leverage(symbol, leverage)

    def change_symbol_leverage(self, symbol: str, leverage: float):
        """Backward-compatible alias used by real_trade_manager.py."""
        return self.set_leverage(symbol, leverage)

    def set_futures_leverage(self, symbol: str, leverage: float):
        """Backward-compatible alias used by real_trade_manager.py."""
        return self.set_leverage(symbol, leverage)

    def get_leverage(self, symbol: str):
        """Backward-compatible alias used by real_trade_manager.py."""
        return self.get_symbol_leverage(symbol)

    def get_futures_leverage(self, symbol: str):
        """Backward-compatible alias used by real_trade_manager.py."""
        return self.get_symbol_leverage(symbol)

    def get_position_mode_leverage(self, symbol: str):
        """Backward-compatible alias used by real_trade_manager.py."""
        return self.get_symbol_leverage(symbol)


    # ---------- Orders ----------
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
        direction = str(direction or "").upper().strip()

        if direction == "LONG":
            side = "BUY_OPEN"
        elif direction == "SHORT":
            side = "SELL_OPEN"
        else:
            return {"ok": False, "error": "direction باید LONG یا SHORT باشد"}

        if float(quantity or 0) <= 0:
            return {"ok": False, "error": "quantity باید بیشتر از صفر باشد"}

        # Toobit docs use LIMIT orders with priceType=MARKET for market execution.
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

        order_result = self._signed_request("POST", "/api/v1/futures/order", params)

        if order_result.get("ok"):
            return order_result

        # Critical real-trading safety:
        # Some Toobit responses may report an error even though the futures
        # position was opened. Before telling the bot that the order failed,
        # verify the exchange position so the signal can still enter tracking/slots
        # and duplicate entries are avoided.
        try:
            time.sleep(1.2)
            opened, position_result = self._has_open_position(symbol, direction, quantity)
            if opened:
                return {
                    "ok": True,
                    "recovered_after_error": True,
                    "warning": order_result.get("error"),
                    "data": {
                        "order_response": order_result.get("data"),
                        "position_check": position_result,
                        "requested_params": params,
                    },
                    "path": "/api/v1/futures/order",
                }
        except Exception as e:
            order_result["position_check_error"] = str(e)[:300]

        return order_result

    def close_market_position(self, symbol: str, direction: str, quantity: float):
        if not REAL_TRADING_ENABLED:
            return {
                "ok": False,
                "blocked": True,
                "error": "ترید واقعی غیرفعال است. REAL_TRADING_ENABLED=false",
            }

        symbol = self.normalize_futures_symbol(symbol)
        direction = str(direction or "").upper().strip()

        if direction == "LONG":
            side = "SELL_CLOSE"
        elif direction == "SHORT":
            side = "BUY_CLOSE"
        else:
            return {"ok": False, "error": "direction باید LONG یا SHORT باشد"}

        if float(quantity or 0) <= 0:
            return {"ok": False, "error": "quantity باید بیشتر از صفر باشد"}

        params = {
            "symbol": symbol,
            "side": side,
            "type": "LIMIT",
            "priceType": "MARKET",
            "quantity": self.safe_decimal(quantity, 6),
            "newClientOrderId": f"bot_close_{uuid.uuid4().hex[:18]}",
        }

        return self._signed_request("POST", "/api/v1/futures/order", params)


# Backward compatibility: both spellings work.
ToBitClient = ToobitClient

def debug_toobit():
    client = ToobitClient()

    print("\n===== TOOBIT ENV =====")
    print(client.debug_env_masked())

    print("\n===== TOOBIT BALANCE TEST =====")
    print(client.debug_balance())

# Shared singleton used by real_trade_manager.py
toobit_client = ToobitClient()
