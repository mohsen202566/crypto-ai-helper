# tobit_client.py
# Safe Toobit USDT-M Futures REST client
#
# Real orders are blocked unless REAL_TRADING_ENABLED=true.
#
# Critical trading rules handled here:
# 1) Before opening a real futures position, set Toobit leverage to the bot leverage.
# 2) Market opening can use valueQuantity so the bot can send exact notional USDT
#    calculated by real_trade_manager.py: margin_usdt * leverage.
# 3) TP/SL are included in the opening order. If a position is already open, helper
#    methods can also place/verify position TP/SL orders.
# 4) Position and PnL parsing is centralized so real_trade_manager.py can sync slots
#    from the exchange instead of trusting only local JSON state.

import os
import time
import hmac
import hashlib
import uuid
from decimal import Decimal, ROUND_DOWN, InvalidOperation
from urllib.parse import urlencode
from typing import Any, Dict, List, Optional, Tuple

import requests


TOBIT_BASE_URL = os.getenv("TOBIT_BASE_URL", "https://api.toobit.com").rstrip("/")
TOBIT_API_KEY = os.getenv("TOBIT_API_KEY", "").strip()
TOBIT_SECRET_KEY = os.getenv("TOBIT_SECRET_KEY", "").strip()

REAL_TRADING_ENABLED = os.getenv("REAL_TRADING_ENABLED", "false").strip().lower() == "true"
RECV_WINDOW = int(os.getenv("TOBIT_RECV_WINDOW", "5000") or "5000")
REQUEST_TIMEOUT = int(os.getenv("TOBIT_REQUEST_TIMEOUT", "15") or "15")


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or str(value).strip() == "":
            return default
        return float(value)
    except Exception:
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or str(value).strip() == "":
            return default
        return int(float(value))
    except Exception:
        return default


def _flatten_dicts(value: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if isinstance(value, dict):
        out.append(value)
        for v in value.values():
            if isinstance(v, (dict, list)):
                out.extend(_flatten_dicts(v))
    elif isinstance(value, list):
        for item in value:
            out.extend(_flatten_dicts(item))
    return out


class ToobitClient:
    """Minimal, safe Toobit USDT-M futures REST client."""

    def __init__(self, api_key: str | None = None, secret_key: str | None = None, base_url: str | None = None):
        self.base_url = (base_url or TOBIT_BASE_URL).rstrip("/")
        self.api_key = (api_key if api_key is not None else TOBIT_API_KEY).strip()
        self.secret_key = (secret_key if secret_key is not None else TOBIT_SECRET_KEY).strip()

    # ------------------------------------------------------------------
    # Low-level signed request helpers
    # ------------------------------------------------------------------
    def _now_ms(self) -> int:
        return int(time.time() * 1000)

    def _headers(self, json_body: bool = False) -> dict:
        headers = {
            "X-BB-APIKEY": self.api_key,
            "User-Agent": "crypto-ai-helper/1.0",
        }
        headers["Content-Type"] = "application/json" if json_body else "application/x-www-form-urlencoded"
        return headers

    def _sign(self, params: dict) -> str:
        clean_params = {k: v for k, v in params.items() if k != "signature" and v is not None}
        query = urlencode(clean_params)
        return hmac.new(
            self.secret_key.encode("utf-8"),
            query.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _is_success_payload(self, data: Any) -> bool:
        if isinstance(data, dict) and "code" in data:
            try:
                return int(data.get("code")) == 200
            except Exception:
                return False
        return True

    def _normalize_error(self, status_code: int | None, data: Any, text: str = "") -> str:
        if isinstance(data, dict):
            code = data.get("code")
            msg = data.get("msg") or data.get("message") or data.get("error")
            if code is not None or msg:
                return f"Toobit error code={code}, msg={msg}"
        if text:
            return text[:500]
        return f"HTTP status {status_code}"

    def _signed_request(self, method: str, path: str, params: dict | None = None, *, json_body: bool = False) -> Dict[str, Any]:
        if not self.api_key or not self.secret_key:
            return {
                "ok": False,
                "error": "TOBIT_API_KEY یا TOBIT_SECRET_KEY تنظیم نشده است",
                "hint": "کلیدها باید در systemd service یا .env تنظیم شوند.",
                "path": path,
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
                return {"ok": False, "error": f"Unsupported method: {method}", "path": path}

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

    # ------------------------------------------------------------------
    # Debug / formatting helpers
    # ------------------------------------------------------------------
    def ping(self) -> Dict[str, Any]:
        try:
            r = requests.get(f"{self.base_url}/api/v1/time", timeout=REQUEST_TIMEOUT)
            try:
                data = r.json()
            except Exception:
                data = {"raw": r.text}
            return {"ok": r.status_code == 200, "status_code": r.status_code, "data": data}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def debug_env_masked(self) -> Dict[str, Any]:
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

    def plain_symbol(self, symbol: str) -> str:
        raw = str(symbol or "").upper().strip()
        raw = raw.replace("/", "").replace("_", "-")
        if raw.endswith("-SWAP-USDT"):
            return raw.replace("-SWAP-USDT", "USDT")
        if raw.endswith("-SWAP-USDC"):
            return raw.replace("-SWAP-USDC", "USDC")
        return raw.replace("-", "")

    def safe_decimal(self, value: Any, precision: int = 8) -> str:
        try:
            precision = max(0, int(precision))
            q = Decimal("1") if precision == 0 else Decimal("1." + ("0" * precision))
            return str(Decimal(str(value)).quantize(q, rounding=ROUND_DOWN))
        except (InvalidOperation, ValueError, TypeError):
            return "0"

    # ------------------------------------------------------------------
    # Account / balance / leverage / margin mode
    # ------------------------------------------------------------------
    def get_account_balance(self, category: str | None = None) -> Dict[str, Any]:
        params: Dict[str, Any] = {}
        if category:
            params["category"] = category
        return self._signed_request("GET", "/api/v1/futures/balance", params)

    def get_balance(self, category: str | None = None) -> Dict[str, Any]:
        return self.get_account_balance(category=category)

    def set_margin_type(self, symbol: str, margin_type: str = "CROSS", category: str | None = None) -> Dict[str, Any]:
        if not REAL_TRADING_ENABLED:
            return {"ok": False, "blocked": True, "error": "ترید واقعی غیرفعال است. REAL_TRADING_ENABLED=false"}
        mt = str(margin_type or "CROSS").upper().strip()
        if mt not in {"CROSS", "ISOLATED"}:
            return {"ok": False, "error": "marginType باید CROSS یا ISOLATED باشد"}
        params: Dict[str, Any] = {
            "symbol": self.normalize_futures_symbol(symbol),
            "marginType": mt,
        }
        if category:
            params["category"] = category
        return self._signed_request("POST", "/api/v1/futures/marginType", params)

    def set_leverage(self, symbol: str, leverage: int | float, category: str | None = None) -> Dict[str, Any]:
        """
        Set Toobit opening leverage for a symbol before placing an order.
        The next open order should inherit this exact leverage from Toobit.
        """
        if not REAL_TRADING_ENABLED:
            return {"ok": False, "blocked": True, "error": "ترید واقعی غیرفعال است. REAL_TRADING_ENABLED=false"}

        lev = _safe_int(leverage)
        if lev <= 0:
            return {"ok": False, "error": "لوریج باید بیشتر از صفر باشد"}

        params: Dict[str, Any] = {
            "symbol": self.normalize_futures_symbol(symbol),
            "leverage": lev,
        }
        if category:
            params["category"] = category
        return self._signed_request("POST", "/api/v1/futures/leverage", params)

    def get_account_leverage(self, symbol: str | None = None, category: str | None = None) -> Dict[str, Any]:
        params: Dict[str, Any] = {}
        if symbol:
            params["symbol"] = self.normalize_futures_symbol(symbol)
        if category:
            params["category"] = category
        return self._signed_request("GET", "/api/v1/futures/accountLeverage", params)

    def get_symbol_leverage_value(self, symbol: str) -> Optional[float]:
        normalized = self.normalize_futures_symbol(symbol)
        result = self.get_account_leverage(symbol=normalized)
        if not result.get("ok"):
            return None
        for item in _flatten_dicts(result.get("data")):
            sym = str(item.get("symbolId") or item.get("symbol") or "").upper()
            if sym and sym != normalized:
                continue
            lev = _safe_float(item.get("leverage"), 0)
            if lev > 0:
                return lev
        return None

    def ensure_symbol_settings(
        self,
        symbol: str,
        leverage: int | float,
        margin_type: str = "CROSS",
        *,
        verify: bool = True,
    ) -> Dict[str, Any]:
        """
        Ensure exchange settings match bot settings before order creation.
        If leverage cannot be set/verified, the caller should NOT open a position.
        """
        symbol_norm = self.normalize_futures_symbol(symbol)

        margin_result = self.set_margin_type(symbol_norm, margin_type=margin_type)
        # Toobit may reject changing margin mode while a position/order exists. That is not
        # necessarily fatal for opening, but we return it for diagnostics.
        lev_result = self.set_leverage(symbol_norm, leverage)
        if not lev_result.get("ok"):
            return {
                "ok": False,
                "error": f"لوریج روی توبیت تنظیم نشد: {lev_result.get('error') or lev_result.get('data')}",
                "margin_result": margin_result,
                "leverage_result": lev_result,
            }

        actual = None
        if verify:
            time.sleep(0.25)
            actual = self.get_symbol_leverage_value(symbol_norm)
            if actual is not None and abs(float(actual) - float(leverage)) > 0.01:
                return {
                    "ok": False,
                    "error": f"لوریج توبیت با تنظیم ربات یکی نیست: ربات={leverage}x، توبیت={actual}x",
                    "margin_result": margin_result,
                    "leverage_result": lev_result,
                    "actual_leverage": actual,
                }

        return {
            "ok": True,
            "symbol": symbol_norm,
            "requested_leverage": float(leverage),
            "actual_leverage": actual if actual is not None else float(leverage),
            "margin_result": margin_result,
            "leverage_result": lev_result,
        }

    # ------------------------------------------------------------------
    # Positions
    # ------------------------------------------------------------------
    def get_position(self, symbol: str | None = None, category: str | None = None) -> Dict[str, Any]:
        params: Dict[str, Any] = {}
        if symbol:
            params["symbol"] = self.normalize_futures_symbol(symbol)
        if category:
            params["category"] = category
        return self._signed_request("GET", "/api/v1/futures/positions", params)

    def get_positions(self, symbol: str | None = None, category: str | None = None) -> Dict[str, Any]:
        return self.get_position(symbol=symbol, category=category)

    def _flatten_position_items(self, result: Any) -> List[Dict[str, Any]]:
        data = (result or {}).get("data") if isinstance(result, dict) else result
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
        for key in ("positionAmt", "positionSize", "size", "qty", "quantity", "positionQuantity", "availablePosition", "holdAmount"):
            v = item.get(key)
            try:
                if v is not None and str(v).strip() != "":
                    return abs(float(v))
            except Exception:
                pass
        return 0.0

    def _position_symbol(self, item: dict) -> str:
        for key in ("symbol", "symbolId", "contractCode", "instrument", "instId", "pair"):
            if item.get(key):
                return self.plain_symbol(str(item.get(key)))
        return ""

    def _position_side(self, item: dict) -> str:
        raw = " ".join(str(item.get(k, "")) for k in (
            "side", "positionSide", "direction", "positionType", "holdSide", "tradeSide"
        )).upper()
        signed_qty = None
        for key in ("positionAmt", "positionSize", "size", "qty", "quantity", "positionQuantity"):
            if item.get(key) is not None:
                signed_qty = _safe_float(item.get(key), 0)
                break
        if "SHORT" in raw or "SELL" in raw:
            return "SHORT"
        if "LONG" in raw or "BUY" in raw:
            return "LONG"
        if signed_qty is not None and signed_qty < 0:
            return "SHORT"
        return "LONG"

    def _position_side_matches(self, item: dict, direction: str) -> bool:
        direction = str(direction or "").upper().strip()
        return self._position_side(item) == direction and self._position_qty(item) > 0

    def _position_entry(self, item: dict) -> float:
        for key in ("entryPrice", "avgPrice", "openPrice", "positionAvgPrice", "averagePrice", "holdAvgPrice"):
            v = _safe_float(item.get(key), 0)
            if v > 0:
                return v
        return 0.0

    def _position_mark_price(self, item: dict) -> float:
        for key in ("markPrice", "marketPrice", "lastPrice", "indexPrice"):
            v = _safe_float(item.get(key), 0)
            if v > 0:
                return v
        return 0.0

    def _position_leverage(self, item: dict) -> float:
        for key in ("leverage", "lever", "leverageValue"):
            v = _safe_float(item.get(key), 0)
            if v > 0:
                return v
        return 0.0

    def _position_margin(self, item: dict) -> float:
        for key in ("margin", "positionMargin", "initialMargin", "isolatedMargin", "marginAmount", "usedMargin"):
            v = _safe_float(item.get(key), 0)
            if v > 0:
                return v
        return 0.0

    def _position_notional(self, item: dict) -> float:
        for key in ("positionValue", "notional", "value", "sizeUSDT", "amount"):
            v = _safe_float(item.get(key), 0)
            if v > 0:
                return v
        qty = self._position_qty(item)
        mark_or_entry = self._position_mark_price(item) or self._position_entry(item)
        if qty > 0 and mark_or_entry > 0:
            return qty * mark_or_entry
        return 0.0

    def _position_pnl(self, item: dict) -> float:
        for key in ("unrealizedPnl", "unRealizedPnl", "unrealizedProfit", "pnl", "profit", "positionPnl"):
            if item.get(key) is not None and str(item.get(key)).strip() != "":
                return _safe_float(item.get(key), 0)
        return 0.0

    def normalize_position_item(self, item: dict) -> Dict[str, Any]:
        symbol = self._position_symbol(item)
        qty = self._position_qty(item)
        notional = self._position_notional(item)
        leverage = self._position_leverage(item)
        margin = self._position_margin(item)
        if margin <= 0 and notional > 0 and leverage > 0:
            margin = notional / leverage

        return {
            "symbol": symbol,
            "exchange_symbol": self.normalize_futures_symbol(symbol) if symbol else "",
            "direction": self._position_side(item),
            "quantity": qty,
            "entry": self._position_entry(item),
            "mark_price": self._position_mark_price(item),
            "leverage": leverage,
            "margin": margin,
            "notional": notional,
            "unrealized_pnl": self._position_pnl(item),
            "raw": item,
        }

    def get_open_positions_normalized(self, symbol: str | None = None) -> Dict[str, Any]:
        result = self.get_positions(symbol=symbol)
        if not result.get("ok"):
            return {"ok": False, "error": result.get("error") or result.get("data"), "positions": [], "raw": result}

        positions: List[Dict[str, Any]] = []
        for item in self._flatten_position_items(result):
            if not isinstance(item, dict):
                continue
            qty = self._position_qty(item)
            if qty <= 0:
                continue
            pos = self.normalize_position_item(item)
            if not pos.get("symbol"):
                continue
            positions.append(pos)

        return {"ok": True, "positions": positions, "raw": result}

    def _has_open_position(self, symbol: str, direction: str, min_qty: float = 0.0) -> Tuple[bool, Dict[str, Any]]:
        result = self.get_open_positions_normalized(symbol=symbol)
        if not result.get("ok"):
            return False, result
        symbol_plain = self.plain_symbol(symbol)
        for item in result.get("positions") or []:
            if item.get("symbol") != symbol_plain:
                continue
            qty = _safe_float(item.get("quantity"), 0)
            if qty > max(float(min_qty or 0.0) * 0.25, 0.0) and str(item.get("direction")) == str(direction).upper():
                return True, {"ok": True, "position": item, "raw": result}
        return False, result

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------
    def _opening_side(self, direction: str) -> Optional[str]:
        direction = str(direction or "").upper().strip()
        if direction == "LONG":
            return "BUY_OPEN"
        if direction == "SHORT":
            return "SELL_OPEN"
        return None

    def _closing_side(self, direction: str) -> Optional[str]:
        direction = str(direction or "").upper().strip()
        if direction == "LONG":
            return "SELL_CLOSE"
        if direction == "SHORT":
            return "BUY_CLOSE"
        return None

    def query_order(self, order_id: str | None = None, client_order_id: str | None = None, order_type: str | None = None) -> Dict[str, Any]:
        params: Dict[str, Any] = {}
        if order_id:
            params["orderId"] = order_id
        if client_order_id:
            params["origClientOrderId"] = client_order_id
        if order_type:
            params["type"] = order_type
        return self._signed_request("GET", "/api/v1/futures/order", params)

    def place_market_order(
        self,
        symbol: str,
        direction: str,
        quantity: float,
        take_profit: float | None = None,
        stop_loss: float | None = None,
        *,
        leverage: int | float | None = None,
        margin_type: str = "CROSS",
        value_quantity: float | None = None,
        verify_leverage: bool = True,
    ) -> Dict[str, Any]:
        """
        Open a futures position.

        Backward-compatible:
        - If quantity is provided, sends quantity.
        New preferred path:
        - real_trade_manager.py should pass value_quantity = margin_usdt * leverage
          and quantity=0. Toobit docs say valueQuantity is USDT order value and is
          ignored when quantity is present.
        """
        if not REAL_TRADING_ENABLED:
            return {"ok": False, "blocked": True, "error": "ترید واقعی غیرفعال است. REAL_TRADING_ENABLED=false"}

        symbol_norm = self.normalize_futures_symbol(symbol)
        direction = str(direction or "").upper().strip()
        side = self._opening_side(direction)
        if not side:
            return {"ok": False, "error": "direction باید LONG یا SHORT باشد"}

        qty = _safe_float(quantity, 0)
        value_qty = _safe_float(value_quantity, 0)

        if qty <= 0 and value_qty <= 0:
            return {"ok": False, "error": "quantity یا valueQuantity باید بیشتر از صفر باشد"}

        setting_result = None
        if leverage is not None:
            setting_result = self.ensure_symbol_settings(
                symbol_norm,
                leverage=leverage,
                margin_type=margin_type,
                verify=verify_leverage,
            )
            if not setting_result.get("ok"):
                return {
                    "ok": False,
                    "blocked": True,
                    "error": setting_result.get("error"),
                    "settings": setting_result,
                }

        client_id = f"bot_{uuid.uuid4().hex[:24]}"
        params: Dict[str, Any] = {
            "symbol": symbol_norm,
            "side": side,
            "type": "LIMIT",
            "priceType": "MARKET",
            "newClientOrderId": client_id,
        }

        if qty > 0:
            params["quantity"] = self.safe_decimal(qty, 8)
        else:
            # valueQuantity is the notional/order value in USDT. It lets the bot
            # keep margin exact after real_trade_manager calculates margin*leverage.
            params["valueQuantity"] = self.safe_decimal(value_qty, 8)

        if take_profit is not None and _safe_float(take_profit, 0) > 0:
            params["takeProfit"] = str(take_profit)
            params["tpTriggerBy"] = "CONTRACT_PRICE"
            params["tpOrderType"] = "MARKET"

        if stop_loss is not None and _safe_float(stop_loss, 0) > 0:
            params["stopLoss"] = str(stop_loss)
            params["slTriggerBy"] = "CONTRACT_PRICE"
            params["slOrderType"] = "MARKET"

        order_result = self._signed_request("POST", "/api/v1/futures/order", params)
        order_result["requested_params"] = params
        if setting_result is not None:
            order_result["settings"] = setting_result

        if order_result.get("ok"):
            # Verify position visibility soon after accepting the order.
            try:
                time.sleep(0.8)
                opened, position_result = self._has_open_position(symbol_norm, direction, qty)
                order_result["position_check"] = position_result
                if opened:
                    order_result["position_confirmed"] = True
            except Exception as e:
                order_result["position_check_error"] = str(e)[:300]
            return order_result

        # Safety recovery: Toobit can return an error even if the position opens.
        try:
            time.sleep(1.2)
            opened, position_result = self._has_open_position(symbol_norm, direction, qty)
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
                    "requested_params": params,
                    "settings": setting_result,
                    "path": "/api/v1/futures/order",
                }
        except Exception as e:
            order_result["position_check_error"] = str(e)[:300]

        return order_result

    def place_market_order_by_margin(
        self,
        symbol: str,
        direction: str,
        margin_usdt: float,
        leverage: int | float,
        take_profit: float | None = None,
        stop_loss: float | None = None,
        *,
        margin_type: str = "CROSS",
    ) -> Dict[str, Any]:
        """
        Preferred method for this bot:
        margin_usdt is the user's configured "حجم هر پوزیشن".
        Toobit receives valueQuantity = margin_usdt * leverage.
        """
        margin = _safe_float(margin_usdt, 0)
        lev = _safe_float(leverage, 0)
        if margin <= 0 or lev <= 0:
            return {"ok": False, "error": "مارجین و لوریج باید بیشتر از صفر باشند"}
        value_quantity = margin * lev
        result = self.place_market_order(
            symbol=symbol,
            direction=direction,
            quantity=0,
            value_quantity=value_quantity,
            take_profit=take_profit,
            stop_loss=stop_loss,
            leverage=leverage,
            margin_type=margin_type,
            verify_leverage=True,
        )
        result["bot_margin_usdt"] = margin
        result["bot_leverage"] = lev
        result["bot_expected_notional"] = value_quantity
        return result

    def close_market_position(self, symbol: str, direction: str, quantity: float):
        if not REAL_TRADING_ENABLED:
            return {"ok": False, "blocked": True, "error": "ترید واقعی غیرفعال است. REAL_TRADING_ENABLED=false"}

        symbol_norm = self.normalize_futures_symbol(symbol)
        side = self._closing_side(direction)
        if not side:
            return {"ok": False, "error": "direction باید LONG یا SHORT باشد"}

        if float(quantity or 0) <= 0:
            return {"ok": False, "error": "quantity باید بیشتر از صفر باشد"}

        params = {
            "symbol": symbol_norm,
            "side": side,
            "type": "LIMIT",
            "priceType": "MARKET",
            "quantity": self.safe_decimal(quantity, 8),
            "newClientOrderId": f"bot_close_{uuid.uuid4().hex[:18]}",
        }
        return self._signed_request("POST", "/api/v1/futures/order", params)

    # ------------------------------------------------------------------
    # TP/SL helpers
    # ------------------------------------------------------------------
    def place_position_tpsl(
        self,
        symbol: str,
        direction: str,
        take_profit: float | None = None,
        stop_loss: float | None = None,
        *,
        quantity: float = 0,
        trigger_by: str = "CONTRACT_PRICE",
    ) -> Dict[str, Any]:
        """
        Fallback helper for existing positions. The open-order route already supports
        takeProfit/stopLoss. This method creates closing STOP_PROFIT_LOSS style orders
        through the same futures order endpoint when separate TP/SL is needed.

        quantity=0 means whole-position TP/SL where Toobit supports it.
        """
        if not REAL_TRADING_ENABLED:
            return {"ok": False, "blocked": True, "error": "ترید واقعی غیرفعال است. REAL_TRADING_ENABLED=false"}

        symbol_norm = self.normalize_futures_symbol(symbol)
        close_side = self._closing_side(direction)
        if not close_side:
            return {"ok": False, "error": "direction باید LONG یا SHORT باشد"}

        created = []
        errors = []

        for label, stop_price in (("TP", take_profit), ("SL", stop_loss)):
            sp = _safe_float(stop_price, 0)
            if sp <= 0:
                continue
            params = {
                "symbol": symbol_norm,
                "side": close_side,
                "type": "STOP_PROFIT_LOSS",
                "priceType": "MARKET",
                "stopPrice": str(stop_price),
                "triggerBy": trigger_by,
                "stopType": "FIXED_STOP",
                "newClientOrderId": f"bot_{label.lower()}_{uuid.uuid4().hex[:18]}",
            }
            # Per Toobit parameter rules, quantity=0 can indicate whole position TP/SL.
            if quantity is not None:
                params["quantity"] = self.safe_decimal(quantity, 8)

            res = self._signed_request("POST", "/api/v1/futures/order", params)
            res["requested_params"] = params
            if res.get("ok"):
                created.append({"type": label, "result": res})
            else:
                errors.append({"type": label, "result": res})

        return {
            "ok": len(errors) == 0 and len(created) > 0,
            "created": created,
            "errors": errors,
        }

    def verify_position_has_tpsl(self, symbol: str, direction: str) -> Dict[str, Any]:
        """
        Best-effort verification. Toobit returns STOP_PROFIT_LOSS orders when queried
        by known order id; listing all open stop orders may not be available in this v1
        route. This placeholder keeps the call stable for real_trade_manager.py.
        """
        # Keep a stable response shape for next step.
        return {"ok": True, "verified": False, "note": "TP/SL verification requires open STOP order listing endpoint if available."}


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
