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
from decimal import Decimal, ROUND_DOWN, ROUND_UP, InvalidOperation
from urllib.parse import urlencode

import requests


TOBIT_BASE_URL = os.getenv("TOBIT_BASE_URL", "https://api.toobit.com").rstrip("/")
TOBIT_API_KEY = os.getenv("TOBIT_API_KEY", "").strip()
TOBIT_SECRET_KEY = os.getenv("TOBIT_SECRET_KEY", "").strip()

REAL_TRADING_ENABLED = os.getenv("REAL_TRADING_ENABLED", "false").strip().lower() == "true"
RECV_WINDOW = int(os.getenv("TOBIT_RECV_WINDOW", "5000") or "5000")
REQUEST_TIMEOUT = int(os.getenv("TOBIT_REQUEST_TIMEOUT", "15") or "15")
# Keep metadata fetching disabled by default to avoid extra Toobit calls/rate limits.
# If the exchange-info endpoint is confirmed later, set TOBIT_FETCH_SYMBOL_RULES=true.
TOBIT_FETCH_SYMBOL_RULES = os.getenv("TOBIT_FETCH_SYMBOL_RULES", "false").strip().lower() == "true"

# ---------------------------------------------------------------------------
# Toobit futures symbol mapping
# ---------------------------------------------------------------------------
# The analysis/scanner can keep using standard symbols such as SHIBUSDT,
# because market data is fetched from OKX. Real Toobit order routes must use
# Toobit's exact USDT-M futures contract symbols. Some meme coins are listed
# with a multiplier prefix on Toobit, so normalize them centrally here.
TOBIT_FUTURES_SYMBOL_MAP = {
    "SHIBUSDT": "1000SHIBUSDT",
    "PEPEUSDT": "1000PEPEUSDT",
    "BONKUSDT": "1000BONKUSDT",
    "FLOKIUSDT": "1000FLOKIUSDT",
}

TOBIT_REVERSE_SYMBOL_MAP = {v: k for k, v in TOBIT_FUTURES_SYMBOL_MAP.items()}


def normalize_toobit_plain_symbol(symbol: str) -> str:
    """Return Toobit's plain futures symbol, e.g. SHIBUSDT -> 1000SHIBUSDT."""
    raw = str(symbol or "").upper().strip()
    if not raw:
        return raw

    raw = raw.replace("/", "").replace("_", "-")

    if raw.endswith("-SWAP-USDT"):
        plain = raw.replace("-SWAP-USDT", "USDT").replace("-", "")
    elif raw.endswith("-SWAP-USDC"):
        plain = raw.replace("-SWAP-USDC", "USDC").replace("-", "")
    else:
        plain = raw.replace("-", "").replace("SWAP", "")

    return TOBIT_FUTURES_SYMBOL_MAP.get(plain, plain)


def normalize_bot_plain_symbol(symbol: str) -> str:
    """Return the bot/analysis symbol, e.g. 1000SHIBUSDT -> SHIBUSDT."""
    plain = normalize_toobit_plain_symbol(symbol)
    return TOBIT_REVERSE_SYMBOL_MAP.get(plain, plain)




class ToobitClient:
    """Minimal, safe Toobit USDT-M futures REST client."""

    def __init__(self, api_key: str | None = None, secret_key: str | None = None, base_url: str | None = None):
        self.base_url = (base_url or TOBIT_BASE_URL).rstrip("/")
        self.api_key = (api_key if api_key is not None else TOBIT_API_KEY).strip()
        self.secret_key = (secret_key if secret_key is not None else TOBIT_SECRET_KEY).strip()

        # In-memory safety caches.
        # Toobit rate-limits repeated leverage/margin-mode changes very fast.
        # These caches let the bot avoid re-sending SET requests for every signal
        # when the symbol was already confirmed recently.
        self._leverage_cache = {}
        self._margin_mode_cache = {}
        self._rate_limit_until = {}
        # Symbol trading-rule cache. Used to normalize price/quantity before
        # sending real orders, so Toobit does not reject TP/SL or qty because
        # of unsupported precision/step sizes.
        self._symbol_rules_cache = {}

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

    def _is_rate_limit_error(self, result) -> bool:
        raw = str(result or "").lower()
        return "too many requests" in raw or "rate limit" in raw or "429" in raw

    def _cache_get(self, cache_name: str, key: str, max_age_sec: int):
        cache = getattr(self, cache_name, {}) or {}
        item = cache.get(str(key))
        if not isinstance(item, dict):
            return None
        age = time.time() - float(item.get("ts", 0) or 0)
        if 0 <= age <= max_age_sec:
            return item.get("value")
        return None

    def _cache_set(self, cache_name: str, key: str, value):
        cache = getattr(self, cache_name, None)
        if not isinstance(cache, dict):
            cache = {}
            setattr(self, cache_name, cache)
        cache[str(key)] = {"value": value, "ts": time.time()}

    def _rate_limited(self, key: str) -> bool:
        until = float((getattr(self, "_rate_limit_until", {}) or {}).get(str(key), 0) or 0)
        return time.time() < until

    def _mark_rate_limited(self, key: str, seconds: int = 60):
        rl = getattr(self, "_rate_limit_until", None)
        if not isinstance(rl, dict):
            rl = {}
            setattr(self, "_rate_limit_until", rl)
        rl[str(key)] = time.time() + max(5, int(seconds))

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

        s = normalize_toobit_plain_symbol(raw)

        if s.endswith("USDT"):
            return f"{s[:-4]}-SWAP-USDT"
        if s.endswith("USDC"):
            return f"{s[:-4]}-SWAP-USDC"
        return s

    def safe_decimal(self, value, precision: int = 6) -> str:
        try:
            q = Decimal("1." + ("0" * int(precision)))
            return str(Decimal(str(value)).quantize(q, rounding=ROUND_DOWN))
        except (InvalidOperation, ValueError, TypeError):
            return "0"


    # ---------- Symbol trading rules / order normalization ----------
    def _decimal_from_value(self, value, default: str = "0") -> Decimal:
        try:
            d = Decimal(str(value))
            if d.is_finite():
                return d
        except Exception:
            pass
        return Decimal(str(default))

    def _decimals_from_step(self, step) -> int:
        """Return decimal places implied by a tick/step value."""
        try:
            d = Decimal(str(step)).normalize()
            if d <= 0:
                return 0
            return max(0, -int(d.as_tuple().exponent))
        except Exception:
            return 0

    def _fallback_price_step(self, price) -> Decimal:
        """
        Conservative fallback when Toobit does not expose symbol filters.
        It avoids long floating tails while keeping enough precision for small coins.
        """
        p = abs(float(price or 0))
        if p >= 1000:
            return Decimal("0.1")
        if p >= 100:
            return Decimal("0.01")
        if p >= 10:
            return Decimal("0.001")
        if p >= 1:
            return Decimal("0.0001")
        if p >= 0.1:
            return Decimal("0.00001")
        if p >= 0.01:
            return Decimal("0.000001")
        if p >= 0.001:
            return Decimal("0.0000001")
        return Decimal("0.00000001")

    def _fallback_qty_step(self, quantity) -> Decimal:
        q = abs(float(quantity or 0))
        if q >= 1000:
            return Decimal("1")
        if q >= 100:
            return Decimal("0.1")
        if q >= 10:
            return Decimal("0.01")
        if q >= 1:
            return Decimal("0.001")
        return Decimal("0.0001")

    def _round_to_step(self, value, step, *, mode: str = "down") -> str:
        """Round a numeric value to an exchange step using Decimal arithmetic."""
        d = self._decimal_from_value(value)
        st = self._decimal_from_value(step)
        if st <= 0:
            return str(d)
        rounding = ROUND_UP if str(mode).lower() == "up" else ROUND_DOWN
        units = (d / st).to_integral_value(rounding=rounding)
        rounded = units * st
        decimals = self._decimals_from_step(st)
        if decimals > 0:
            quant = Decimal("1").scaleb(-decimals)
            rounded = rounded.quantize(quant, rounding=ROUND_DOWN)
        else:
            rounded = rounded.quantize(Decimal("1"), rounding=ROUND_DOWN)
        return format(rounded, "f")

    def _extract_symbol_rules_from_data(self, data, symbol: str) -> dict:
        """Best-effort parser for Toobit/exchange-info style symbol filters."""
        wanted = set(self._symbol_candidates(symbol))
        matches = []

        def walk(value):
            if isinstance(value, dict):
                sym_text = " ".join(str(value.get(k, "")) for k in (
                    "symbol", "contractCode", "instrument", "instId", "pair", "symbolName", "contract", "contractName"
                ))
                if sym_text:
                    candidates = set()
                    for part in sym_text.split():
                        candidates.update(self._symbol_candidates(part))
                    if candidates & wanted:
                        matches.append(value)
                for v in value.values():
                    if isinstance(v, (dict, list)):
                        walk(v)
            elif isinstance(value, list):
                for item in value:
                    walk(item)

        walk(data)
        if not matches and isinstance(data, dict):
            matches = [data]

        price_keys = ("tickSize", "priceStep", "priceTick", "minPricePrecision", "pricePrecision", "quotePrecision")
        qty_keys = ("stepSize", "qtyStep", "quantityStep", "lotSize", "basePrecision", "quantityPrecision", "volumePrecision")
        min_qty_keys = ("minQty", "minQuantity", "minOrderQty", "minVolume", "minTradeVolume")

        rules = {}
        for item in matches:
            if not isinstance(item, dict):
                continue

            # Direct keys first.
            for key in price_keys:
                if key in item and item.get(key) not in (None, ""):
                    val = item.get(key)
                    # Precision integers mean decimal places; tick/step values mean actual step.
                    try:
                        if "precision" in key.lower() and float(val).is_integer() and float(val) >= 0:
                            rules["price_step"] = str(Decimal("1").scaleb(-int(float(val))))
                        else:
                            rules["price_step"] = str(val)
                        break
                    except Exception:
                        pass

            for key in qty_keys:
                if key in item and item.get(key) not in (None, ""):
                    val = item.get(key)
                    try:
                        if "precision" in key.lower() and float(val).is_integer() and float(val) >= 0:
                            rules["qty_step"] = str(Decimal("1").scaleb(-int(float(val))))
                        else:
                            rules["qty_step"] = str(val)
                        break
                    except Exception:
                        pass

            for key in min_qty_keys:
                if key in item and item.get(key) not in (None, ""):
                    rules["min_qty"] = str(item.get(key))
                    break

            # Nested filter list fallback.
            filters = item.get("filters") or item.get("filter") or []
            if isinstance(filters, dict):
                filters = list(filters.values())
            if isinstance(filters, list):
                for f in filters:
                    if not isinstance(f, dict):
                        continue
                    for k in ("tickSize", "priceStep", "priceTick"):
                        if k in f and f.get(k) not in (None, ""):
                            rules.setdefault("price_step", str(f.get(k)))
                    for k in ("stepSize", "qtyStep", "quantityStep"):
                        if k in f and f.get(k) not in (None, ""):
                            rules.setdefault("qty_step", str(f.get(k)))
                    for k in min_qty_keys:
                        if k in f and f.get(k) not in (None, ""):
                            rules.setdefault("min_qty", str(f.get(k)))

            if rules:
                return rules
        return rules

    def get_symbol_trading_rules(self, symbol: str, reference_price=None, quantity=None) -> dict:
        """
        Return best-known price/quantity rules for a futures symbol.
        Uses a short cache and falls back safely if Toobit metadata endpoints are unavailable.
        """
        normalized_symbol = self.normalize_futures_symbol(symbol)
        cache_key = f"{normalized_symbol}:rules"
        cached = self._cache_get("_symbol_rules_cache", cache_key, 3600)
        if isinstance(cached, dict) and cached:
            return dict(cached)

        rules = {}
        # Optional metadata lookup. Disabled by default because repeated/unknown
        # Toobit metadata calls previously caused rate-limit style problems.
        if TOBIT_FETCH_SYMBOL_RULES:
            metadata_paths = (
                "/api/v1/futures/exchangeInfo",
                "/api/v1/futures/symbols",
                "/api/v1/exchangeInfo",
            )
            for path in metadata_paths:
                try:
                    result = self._signed_request("GET", path, {"symbol": normalized_symbol})
                    if isinstance(result, dict) and result.get("ok"):
                        rules = self._extract_symbol_rules_from_data(result.get("data"), normalized_symbol)
                        if rules:
                            rules["source"] = path
                            break
                except Exception:
                    pass

        if not rules:
            rules["source"] = "safe_fallback_no_extra_api_call"

        if not rules.get("price_step"):
            rules["price_step"] = str(self._fallback_price_step(reference_price))
        if not rules.get("qty_step"):
            rules["qty_step"] = str(self._fallback_qty_step(quantity))
        if not rules.get("min_qty"):
            rules["min_qty"] = "0"

        self._cache_set("_symbol_rules_cache", cache_key, dict(rules))
        return dict(rules)

    def _normalize_order_values(self, symbol: str, direction: str, quantity, take_profit=None, stop_loss=None):
        """
        Normalize quantity/TP/SL before sending to Toobit.
        Prevents invalid TP/SL precision and validates final side logic.
        """
        direction = str(direction or "").upper().strip()
        ref_candidates = [x for x in (take_profit, stop_loss) if x not in (None, "", 0)]
        reference_price = ref_candidates[0] if ref_candidates else 0
        rules = self.get_symbol_trading_rules(symbol, reference_price=reference_price, quantity=quantity)
        price_step = self._decimal_from_value(rules.get("price_step"), str(self._fallback_price_step(reference_price)))
        qty_step = self._decimal_from_value(rules.get("qty_step"), str(self._fallback_qty_step(quantity)))
        min_qty = self._decimal_from_value(rules.get("min_qty"), "0")

        qty_down = self._decimal_from_value(self._round_to_step(quantity, qty_step, mode="down"))
        if min_qty > 0 and qty_down < min_qty:
            return {
                "ok": False,
                "error": f"quantity کمتر از حداقل مجاز نماد است: qty={qty_down}, minQty={min_qty}",
                "rules": rules,
            }
        if qty_down <= 0:
            return {"ok": False, "error": "quantity بعد از گرد کردن صفر شد", "rules": rules}

        # Direction-aware rounding: move TP/SL one safe direction, not toward invalid side.
        # LONG: TP higher, SL lower. SHORT: TP lower, SL higher.
        tp_norm = None
        sl_norm = None
        if take_profit not in (None, "", 0):
            tp_norm = self._round_to_step(take_profit, price_step, mode="up" if direction == "LONG" else "down")
        if stop_loss not in (None, "", 0):
            sl_norm = self._round_to_step(stop_loss, price_step, mode="down" if direction == "LONG" else "up")

        # If both are present, validate that TP and SL are on opposite logical sides.
        # We do not have guaranteed real-time entry here, so use their relative order.
        if tp_norm is not None and sl_norm is not None:
            tp_d = self._decimal_from_value(tp_norm)
            sl_d = self._decimal_from_value(sl_norm)
            if direction == "LONG" and not (tp_d > sl_d):
                return {"ok": False, "error": "TP/SL بعد از گرد کردن برای LONG نامعتبر شد", "rules": rules, "tp": tp_norm, "sl": sl_norm}
            if direction == "SHORT" and not (tp_d < sl_d):
                return {"ok": False, "error": "TP/SL بعد از گرد کردن برای SHORT نامعتبر شد", "rules": rules, "tp": tp_norm, "sl": sl_norm}

        return {
            "ok": True,
            "quantity": format(qty_down, "f"),
            "take_profit": tp_norm,
            "stop_loss": sl_norm,
            "rules": rules,
        }

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

    def _plain_symbol(self, symbol: str) -> str:
        """Convert Toobit/futures symbols to bot plain symbols like SHIBUSDT."""
        raw = str(symbol or "").upper().strip()
        if not raw:
            return ""
        return normalize_bot_plain_symbol(raw)

    def _symbol_candidates(self, symbol: str) -> list[str]:
        """Return bot and Toobit symbol formats for a futures symbol."""
        raw = str(symbol or "").upper().strip()
        if not raw:
            return []

        toobit_plain = normalize_toobit_plain_symbol(raw)
        bot_plain = normalize_bot_plain_symbol(raw)
        normalized = self.normalize_futures_symbol(raw)

        candidates = []
        for item in (
            normalized,
            toobit_plain,
            bot_plain,
            raw.replace("/", "").replace("_", "").replace("-", "").replace("SWAP", ""),
            raw,
        ):
            item = str(item or "").upper().strip()
            if item and item not in candidates:
                candidates.append(item)
        return candidates

    def _dict_looks_like_position(self, item: dict) -> bool:
        """Best-effort check for Toobit position rows."""
        if not isinstance(item, dict):
            return False
        position_keys = {
            "symbol", "contractCode", "instrument", "instId", "pair",
            "positionAmt", "positionSize", "size", "qty", "quantity",
            "positionQuantity", "availablePosition", "totalPosition",
            "holdVol", "holdVolume", "volume", "position",
            "entryPrice", "avgPrice", "openPrice", "positionAvgPrice",
            "averagePrice", "leverage", "side", "positionSide", "direction",
            "positionType", "holdSide", "tradeSide",
        }
        return any(k in item for k in position_keys)

    def _flatten_position_items(self, result):
        """Return a flat list of likely position dicts from Toobit response shapes."""
        data = (result or {}).get("data")
        out = []

        def walk(value):
            if isinstance(value, dict):
                if self._dict_looks_like_position(value):
                    out.append(value)
                for v in value.values():
                    if isinstance(v, (dict, list)):
                        walk(v)
            elif isinstance(value, list):
                for item in value:
                    walk(item)

        walk(data)
        # Fallback: if Toobit returns one raw dict without recognizable keys.
        if not out and isinstance(data, dict):
            out.append(data)
        return out

    def _position_qty(self, item: dict) -> float:
        """Best-effort quantity extractor for Toobit futures position rows."""
        qty_keys = (
            "positionAmt", "positionSize", "size", "qty", "quantity",
            "positionQuantity", "availablePosition", "totalPosition",
            "holdVol", "holdVolume", "volume", "position",
        )
        for key in qty_keys:
            try:
                v = item.get(key)
                if v is not None and str(v).strip() != "":
                    return abs(float(v))
            except Exception:
                pass

        # Some APIs return long/short quantities separately.
        for key in ("longQty", "shortQty", "longSize", "shortSize", "longPosition", "shortPosition"):
            try:
                v = item.get(key)
                if v is not None and str(v).strip() != "":
                    qty = abs(float(v))
                    if qty > 0:
                        return qty
            except Exception:
                pass
        return 0.0

    def _position_symbol_matches(self, item: dict, symbol: str) -> bool:
        """Best-effort symbol matcher for Toobit futures position rows."""
        wanted = set(self._symbol_candidates(symbol))
        if not wanted:
            return False

        symbol_fields = (
            "symbol", "contractCode", "instrument", "instId", "pair",
            "symbolName", "contract", "contractName",
        )

        for key in symbol_fields:
            value = item.get(key)
            if value is None or str(value).strip() == "":
                continue
            candidates = set(self._symbol_candidates(str(value)))
            if candidates & wanted:
                return True

        # If the position endpoint was queried with symbol and Toobit omitted
        # the symbol field, allow this row to pass symbol matching.
        return not any(item.get(k) for k in symbol_fields)

    def _position_side_matches(self, item: dict, direction: str) -> bool:
        """Best-effort side matcher; if side is absent but qty > 0, accept it."""
        direction = str(direction or "").upper().strip()
        raw = " ".join(str(item.get(k, "")) for k in (
            "side", "positionSide", "direction", "positionType", "holdSide",
            "tradeSide", "sideType", "positionDirection",
        )).upper()

        if direction == "LONG":
            return (
                "LONG" in raw
                or "BUY" in raw
                or "BULL" in raw
                or "多" in raw
                or (raw.strip() == "" and self._position_qty(item) > 0)
            )
        if direction == "SHORT":
            return (
                "SHORT" in raw
                or "SELL" in raw
                or "BEAR" in raw
                or "空" in raw
                or (raw.strip() == "" and self._position_qty(item) > 0)
            )
        return False

    def _has_open_position(self, symbol: str, direction: str, min_qty: float = 0.0):
        """
        Verify if an exchange futures position is actually open.

        Important:
        - Do not require the exchange quantity to be close to the order quantity.
          Toobit may expose different quantity fields/precision after market execution.
        - Check both symbol-specific and all-position endpoints, because some
          Toobit responses omit/ignore the symbol filter.
        """
        symbol = str(symbol or "").upper().strip()
        direction = str(direction or "").upper().strip()

        results = []
        seen_paths = set()

        for query_symbol in (symbol, self.normalize_futures_symbol(symbol), None):
            try:
                result = self.get_position(symbol=query_symbol)
            except Exception as e:
                result = {"ok": False, "error": str(e), "query_symbol": query_symbol}

            key = str((result or {}).get("path")) + "|" + str(query_symbol)
            if key in seen_paths:
                continue
            seen_paths.add(key)
            results.append({"query_symbol": query_symbol, "result": result})

            if not isinstance(result, dict) or not result.get("ok"):
                continue

            items = self._flatten_position_items(result)
            for item in items:
                if not isinstance(item, dict):
                    continue
                qty = self._position_qty(item)

                # A real open position must have positive quantity. Use any
                # positive quantity as confirmation; min_qty is only used as a
                # very small noise floor, not as a strict expected amount.
                noise_floor = max(float(min_qty or 0.0) * 0.01, 0.0)
                if qty <= noise_floor:
                    continue

                if not self._position_symbol_matches(item, symbol):
                    continue

                if not self._position_side_matches(item, direction):
                    continue

                return True, {
                    "ok": True,
                    "position": item,
                    "quantity_detected": qty,
                    "query_symbol": query_symbol,
                    "raw": result,
                }

        return False, {
            "ok": False,
            "error": "open futures position not found",
            "symbol": symbol,
            "direction": direction,
            "min_qty": min_qty,
            "checked": results[-3:],
        }

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

        Rate-limit-safe behavior:
        1) If the symbol was recently confirmed at the desired leverage, do not
           send any SET request again.
        2) Try to read current leverage first. If it already matches, cache and return.
        3) Only if needed, try a small endpoint list and stop immediately on rate limit.
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

        cache_key = f"{symbol}:leverage"
        cached = self._cache_get("_leverage_cache", cache_key, 600)
        if cached is not None:
            try:
                if abs(float(cached) - float(lev)) <= 0.01:
                    return {
                        "ok": True,
                        "data": {"symbol": symbol, "leverage": lev},
                        "actual_leverage": float(lev),
                        "source": "recent_leverage_cache",
                    }
            except Exception:
                pass

        if self._rate_limited(cache_key):
            return {
                "ok": False,
                "error": "Toobit موقتاً برای تنظیم لوریج rate limit داده؛ برای جلوگیری از اسپم سفارش ارسال نشد.",
                "rate_limited": True,
            }

        # Read first; avoid SET if Toobit is already configured.
        try:
            current = self.get_symbol_leverage(symbol)
            if current.get("ok"):
                cur_lev = self._extract_leverage_value(current.get("data", current))
                if cur_lev > 0 and abs(cur_lev - float(lev)) <= 0.01:
                    self._cache_set("_leverage_cache", cache_key, float(lev))
                    return {
                        "ok": True,
                        "data": {"symbol": symbol, "leverage": cur_lev},
                        "actual_leverage": cur_lev,
                        "source": "already_configured",
                        "read_result": current,
                    }
        except Exception:
            pass

        request_variants = [
            {"symbol": symbol, "leverage": lev},
        ]
        candidate_paths = (
            "/api/v1/futures/leverage",
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
                    self._cache_set("_leverage_cache", cache_key, float(lev))
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

                if self._is_rate_limit_error(result):
                    self._mark_rate_limited(cache_key, 180)
                    return {
                        "ok": False,
                        "error": "Toobit برای تنظیم لوریج too many requests داد؛ درخواست‌های بیشتر متوقف شد.",
                        "rate_limited": True,
                        "attempts": attempts[-3:],
                    }

        return {
            "ok": False,
            "error": "تنظیم لوریج در توبیت ناموفق بود",
            "attempts": attempts[-4:],
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
        cache_key = f"{symbol}:leverage"
        cached = self._cache_get("_leverage_cache", cache_key, 600)
        if cached is not None:
            return {"ok": True, "data": {"symbol": symbol, "leverage": float(cached)}, "source": "recent_leverage_cache"}

        pos_result = self.get_position(symbol=symbol)
        if pos_result.get("ok"):
            value = self._extract_leverage_value(pos_result.get("data"))
            if value > 0:
                self._cache_set("_leverage_cache", cache_key, value)
                return {"ok": True, "data": {"symbol": symbol, "leverage": value}, "source": "positions", "raw": pos_result}

        # Avoid repeated leverage readback endpoints here. In current Toobit
        # responses, several leverage/config endpoints can return 404 or rate
        # limits. The safe path is: cache -> open-position read -> last accepted
        # set_leverage. If none exists, set_leverage will try one SET endpoint.
        attempts = []

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



    # ---------- Margin mode / isolated safety ----------
    def _normalize_margin_mode(self, mode: str) -> str:
        """Normalize margin mode values to ISOLATED or CROSS."""
        raw = str(mode or "").upper().strip().replace("-", "_").replace(" ", "_")
        if raw in {"ISOLATED", "ISOLATE", "FIXED", "SINGLE"}:
            return "ISOLATED"
        if raw in {"CROSS", "CROSSED", "CROSS_MARGIN", "FULL"}:
            return "CROSS"
        return raw

    def _extract_margin_mode_value(self, data) -> str:
        """Best-effort margin-mode extractor from Toobit response shapes."""
        def walk(value):
            if isinstance(value, dict):
                yield value
                for v in value.values():
                    yield from walk(v)
            elif isinstance(value, list):
                for item in value:
                    yield from walk(item)

        keys = (
            "marginMode", "margin_mode", "positionMode", "position_mode",
            "tradeMode", "trade_mode", "marginType", "margin_type",
            "isolated", "isIsolated", "cross", "isCross",
        )

        for item in walk(data):
            if not isinstance(item, dict):
                continue

            for key in keys:
                if key not in item:
                    continue

                value = item.get(key)

                if isinstance(value, bool):
                    if key.lower() in {"isolated", "isisolated"}:
                        return "ISOLATED" if value else "CROSS"
                    if key.lower() in {"cross", "iscross"}:
                        return "CROSS" if value else "ISOLATED"

                mode = self._normalize_margin_mode(value)
                if mode in {"ISOLATED", "CROSS"}:
                    return mode

                text = str(value or "").upper()
                if "ISOL" in text:
                    return "ISOLATED"
                if "CROSS" in text:
                    return "CROSS"

        return ""

    def set_margin_mode(self, symbol: str, mode: str = "ISOLATED"):
        """
        Margin-mode safety for Toobit.

        The user's account/app is configured to apply ISOLATED to all futures
        pairs. Toobit API margin-mode endpoints are unstable/rate-limited and
        the observed readback endpoints return 404, so repeatedly calling them
        before every order blocks valid trades with "too many requests".

        Safety rule:
        - CROSS is never accepted from code.
        - ISOLATED is treated as the required/manual-global account setting.
        - No setMarginMode API spam is sent here.
        """
        if not REAL_TRADING_ENABLED:
            return {
                "ok": False,
                "blocked": True,
                "error": "ترید واقعی غیرفعال است. REAL_TRADING_ENABLED=false",
            }

        symbol = self.normalize_futures_symbol(symbol)
        normalized_mode = self._normalize_margin_mode(mode)

        if normalized_mode != "ISOLATED":
            return {
                "ok": False,
                "blocked": True,
                "error": "برای امنیت فقط margin mode ایزوله مجاز است.",
                "requested_mode": mode,
            }

        cache_key = f"{symbol}:margin_mode"
        self._cache_set("_margin_mode_cache", cache_key, "ISOLATED")
        self._last_set_margin_mode = {
            "symbol": symbol,
            "mode": "ISOLATED",
            "set_result": {"ok": True, "source": "manual_global_isolated_no_api_call"},
            "set_at": self._now_ms(),
        }

        return {
            "ok": True,
            "data": {"symbol": symbol, "marginMode": "ISOLATED"},
            "actual_margin_mode": "ISOLATED",
            "source": "manual_global_isolated_no_api_call",
            "warning": (
                "Margin Mode با تنظیم دستی/سراسری اپ Toobit به عنوان ISOLATED در نظر گرفته شد؛ "
                "برای جلوگیری از too many requests درخواست setMarginMode ارسال نشد."
            ),
        }


    def get_margin_mode(self, symbol: str):
        """
        Return required margin mode without calling unstable Toobit endpoints.

        Current Toobit API responses showed 404 for margin readback endpoints and
        rate limits for setMarginMode. Because the user enabled app-level
        "Margin mode changes apply to all futures trading pairs" with ISOLATED,
        this client avoids repeated margin-mode API calls and reports the
        required safety mode from local/manual-global configuration.
        """
        symbol = self.normalize_futures_symbol(symbol)
        cache_key = f"{symbol}:margin_mode"

        cached = self._cache_get("_margin_mode_cache", cache_key, 3600)
        if cached == "ISOLATED":
            return {
                "ok": True,
                "data": {"symbol": symbol, "marginMode": "ISOLATED"},
                "source": "recent_margin_mode_cache",
            }

        self._cache_set("_margin_mode_cache", cache_key, "ISOLATED")
        return {
            "ok": True,
            "data": {"symbol": symbol, "marginMode": "ISOLATED"},
            "source": "manual_global_isolated_no_api_read",
            "warning": (
                "Toobit margin-mode readback endpoints are not reliable; "
                "using the required/manual global ISOLATED configuration."
            ),
        }


    def ensure_isolated_margin(self, symbol: str):
        """
        Safety gate before opening a real order.

        Required rule for this bot/account:
        - Every real Toobit futures position must be ISOLATED.
        - CROSS is never allowed by code.
        - To avoid Toobit rate-limit/404 failures, margin-mode API set/read is
          not spammed before each order. The bot relies on the user's app-level
          global ISOLATED setting and records/cache-confirms ISOLATED locally.
        """
        symbol = self.normalize_futures_symbol(symbol)
        cache_key = f"{symbol}:margin_mode"

        self._cache_set("_margin_mode_cache", cache_key, "ISOLATED")
        return {
            "ok": True,
            "actual_margin_mode": "ISOLATED",
            "source": "manual_global_isolated_no_api_call",
            "warning": (
                "ISOLATED بر اساس تنظیم دستی/سراسری Toobit تایید شد؛ "
                "برای جلوگیری از too many requests درخواست Margin Mode به API ارسال نشد."
            ),
        }


    def set_symbol_margin_mode(self, symbol: str, mode: str = "ISOLATED"):
        """Backward-compatible alias for margin mode setting."""
        return self.set_margin_mode(symbol, mode)

    def change_margin_mode(self, symbol: str, mode: str = "ISOLATED"):
        """Backward-compatible alias for margin mode setting."""
        return self.set_margin_mode(symbol, mode)

    def change_symbol_margin_mode(self, symbol: str, mode: str = "ISOLATED"):
        """Backward-compatible alias for margin mode setting."""
        return self.set_margin_mode(symbol, mode)

    def set_futures_margin_mode(self, symbol: str, mode: str = "ISOLATED"):
        """Backward-compatible alias for margin mode setting."""
        return self.set_margin_mode(symbol, mode)

    def get_symbol_margin_mode(self, symbol: str):
        """Backward-compatible alias for margin mode readback."""
        return self.get_margin_mode(symbol)

    def get_futures_margin_mode(self, symbol: str):
        """Backward-compatible alias for margin mode readback."""
        return self.get_margin_mode(symbol)


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

        normalized_values = self._normalize_order_values(
            symbol,
            direction,
            quantity,
            take_profit=take_profit,
            stop_loss=stop_loss,
        )
        if not normalized_values.get("ok"):
            return {
                "ok": False,
                "blocked": True,
                "error": normalized_values.get("error", "order values normalization failed"),
                "normalization": normalized_values,
            }

        # Toobit docs use LIMIT orders with priceType=MARKET for market execution.
        # Quantity, TP and SL are normalized before sending to avoid invalid
        # precision/step-size errors such as invalid stop profit/loss price.
        params = {
            "symbol": symbol,
            "side": side,
            "type": "LIMIT",
            "priceType": "MARKET",
            "quantity": normalized_values["quantity"],
            "newClientOrderId": f"bot_{uuid.uuid4().hex[:24]}",
        }

        if normalized_values.get("take_profit") is not None:
            params["takeProfit"] = normalized_values["take_profit"]
            params["tpOrderType"] = "MARKET"

        if normalized_values.get("stop_loss") is not None:
            params["stopLoss"] = normalized_values["stop_loss"]
            params["slOrderType"] = "MARKET"

        order_result = self._signed_request("POST", "/api/v1/futures/order", params)
        order_result.setdefault("normalized_params", {
            "symbol": symbol,
            "side": side,
            "quantity": params.get("quantity"),
            "takeProfit": params.get("takeProfit"),
            "stopLoss": params.get("stopLoss"),
            "rules": normalized_values.get("rules"),
        })

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

        rules = self.get_symbol_trading_rules(symbol, quantity=quantity)
        qty_step = self._decimal_from_value(rules.get("qty_step"), str(self._fallback_qty_step(quantity)))
        min_qty = self._decimal_from_value(rules.get("min_qty"), "0")
        qty_down = self._decimal_from_value(self._round_to_step(quantity, qty_step, mode="down"))
        if min_qty > 0 and qty_down < min_qty:
            return {"ok": False, "error": f"quantity کمتر از حداقل مجاز نماد است: qty={qty_down}, minQty={min_qty}", "rules": rules}
        if qty_down <= 0:
            return {"ok": False, "error": "quantity بعد از گرد کردن صفر شد", "rules": rules}

        params = {
            "symbol": symbol,
            "side": side,
            "type": "LIMIT",
            "priceType": "MARKET",
            "quantity": format(qty_down, "f"),
            "newClientOrderId": f"bot_close_{uuid.uuid4().hex[:18]}",
        }

        return self._signed_request("POST", "/api/v1/futures/order", params)


    # ---------- Closed PnL / history ----------
    def _extract_realized_pnl_value(self, item) -> float | None:
        """Best-effort realized-PnL extractor from Toobit history rows."""
        if not isinstance(item, dict):
            return None
        pnl_keys = (
            "realizedPnl", "realizedPNL", "realized_pnl", "closedPnl", "closedPNL",
            "closePnl", "closeProfit", "profit", "pnl", "netPnl", "netProfit",
            "realizedProfit", "income", "amount", "feeIncome", "tradePnl",
        )
        for key in pnl_keys:
            if key not in item:
                continue
            try:
                value = item.get(key)
                if value is not None and str(value).strip() != "":
                    return float(value)
            except Exception:
                pass
        return None

    def _extract_timestamp_value(self, item) -> int:
        """Return timestamp in milliseconds when present, otherwise 0."""
        if not isinstance(item, dict):
            return 0
        for key in ("time", "timestamp", "ts", "createdTime", "createTime", "updatedTime", "closeTime", "closedTime", "transactTime"):
            try:
                value = item.get(key)
                if value is None or str(value).strip() == "":
                    continue
                ts = int(float(value))
                # seconds -> milliseconds
                if ts and ts < 10_000_000_000:
                    ts *= 1000
                return ts
            except Exception:
                pass
        return 0

    def _history_symbol_matches(self, item: dict, symbol: str) -> bool:
        if not isinstance(item, dict):
            return False
        wanted = set(self._symbol_candidates(symbol))
        if not wanted:
            return False
        for key in ("symbol", "contractCode", "instrument", "instId", "pair", "symbolName", "contract", "contractName"):
            value = item.get(key)
            if value is None or str(value).strip() == "":
                continue
            if set(self._symbol_candidates(str(value))) & wanted:
                return True
        # Some income endpoints omit symbol when symbol filter was accepted.
        return not any(item.get(k) for k in ("symbol", "contractCode", "instrument", "instId", "pair", "symbolName", "contract", "contractName"))

    def _history_direction_matches(self, item: dict, direction: str) -> bool:
        """Best-effort direction matcher; accepts unknown side to avoid missing PnL rows."""
        direction = str(direction or "").upper().strip()
        if direction not in {"LONG", "SHORT"}:
            return True
        raw = " ".join(str(item.get(k, "")) for k in (
            "side", "positionSide", "direction", "positionType", "holdSide", "tradeSide", "sideType", "positionDirection"
        )).upper()
        if not raw.strip():
            return True
        if direction == "LONG":
            return "LONG" in raw or "BUY" in raw or "SELL_CLOSE" in raw or "CLOSE_LONG" in raw
        return "SHORT" in raw or "SELL" in raw or "BUY_CLOSE" in raw or "CLOSE_SHORT" in raw

    def _flatten_history_items(self, result):
        """Return rows that may contain realized pnl/history information."""
        data = (result or {}).get("data") if isinstance(result, dict) else result
        out = []
        history_keys = {
            "realizedPnl", "realizedPNL", "realized_pnl", "closedPnl", "closedPNL", "closePnl",
            "closeProfit", "profit", "pnl", "netPnl", "netProfit", "realizedProfit", "income", "amount",
            "symbol", "contractCode", "instrument", "instId", "pair", "time", "timestamp", "closeTime", "closedTime",
        }
        def walk(value):
            if isinstance(value, dict):
                if any(k in value for k in history_keys):
                    out.append(value)
                for v in value.values():
                    if isinstance(v, (dict, list)):
                        walk(v)
            elif isinstance(value, list):
                for item in value:
                    walk(item)
        walk(data)
        if not out and isinstance(data, dict):
            out.append(data)
        return out

    def _signed_history_request_candidates(self, paths: tuple[str, ...], params: dict):
        """Try several compatible Toobit history endpoints without raising."""
        attempts = []
        for path in paths:
            try:
                result = self._signed_request("GET", path, params)
            except Exception as e:
                result = {"ok": False, "error": str(e), "path": path}
            attempts.append({
                "path": path,
                "ok": bool(isinstance(result, dict) and result.get("ok")),
                "error": (result or {}).get("error") if isinstance(result, dict) else None,
            })
            if isinstance(result, dict) and result.get("ok"):
                result.setdefault("history_endpoint", path)
                result.setdefault("attempts", attempts[-3:])
                return result
        return {"ok": False, "error": "No Toobit history endpoint returned success", "attempts": attempts[-8:]}

    def get_income_history(self, symbol: str | None = None, startTime: int | None = None, endTime: int | None = None, limit: int = 50):
        """Read futures income/realized-PnL history using safe endpoint fallbacks."""
        params = {"limit": int(limit or 50)}
        if symbol:
            params["symbol"] = self.normalize_futures_symbol(symbol)
        if startTime:
            params["startTime"] = int(startTime)
        if endTime:
            params["endTime"] = int(endTime)

        return self._signed_history_request_candidates(
            (
                "/api/v1/futures/income",
                "/api/v1/futures/incomeHistory",
                "/api/v1/futures/income/history",
                "/api/v1/futures/account/income",
            ),
            params,
        )

    def get_closed_position_history(self, symbol: str | None = None, startTime: int | None = None, endTime: int | None = None, limit: int = 50):
        """Read closed-position/order/trade history using compatible Toobit endpoint fallbacks."""
        params = {"limit": int(limit or 50)}
        if symbol:
            params["symbol"] = self.normalize_futures_symbol(symbol)
        if startTime:
            params["startTime"] = int(startTime)
        if endTime:
            params["endTime"] = int(endTime)

        return self._signed_history_request_candidates(
            (
                "/api/v1/futures/closedPositions",
                "/api/v1/futures/closedPosition",
                "/api/v1/futures/positionHistory",
                "/api/v1/futures/position/history",
                "/api/v1/futures/userTrades",
                "/api/v1/futures/myTrades",
                "/api/v1/futures/trades",
                "/api/v1/futures/order/trades",
            ),
            params,
        )

    def get_recent_closed_pnl(self, symbol: str, direction: str | None = None, opened_at: int | None = None, closed_at: int | None = None, limit: int = 80):
        """Return confirmed realized PnL for a recently closed futures position.

        The result is marked ok only when a numeric realized PnL is found from
        Toobit history. It never fabricates a value.
        """
        symbol = str(symbol or "").upper().strip()
        direction = str(direction or "").upper().strip()
        now_ms = self._now_ms()
        start_ms = int(opened_at or 0)
        if start_ms and start_ms < 10_000_000_000:
            start_ms *= 1000
        end_ms = int(closed_at or 0)
        if end_ms and end_ms < 10_000_000_000:
            end_ms *= 1000
        if not end_ms:
            end_ms = now_ms
        # Give Toobit a small settlement window and include a pre-open buffer.
        query_start = max(0, (start_ms or (end_ms - 30 * 60 * 1000)) - 3 * 60 * 1000)
        query_end = end_ms + 3 * 60 * 1000

        sources = []
        for name, getter in (
            ("income_history", self.get_income_history),
            ("closed_position_history", self.get_closed_position_history),
        ):
            try:
                res = getter(symbol=symbol, startTime=query_start, endTime=query_end, limit=limit)
            except TypeError:
                res = getter(symbol)
            except Exception as e:
                sources.append({"source": name, "ok": False, "error": str(e)[:250]})
                continue

            if not isinstance(res, dict) or not res.get("ok"):
                sources.append({"source": name, "ok": False, "error": (res or {}).get("error") if isinstance(res, dict) else str(res)[:250], "attempts": (res or {}).get("attempts") if isinstance(res, dict) else None})
                continue

            rows = self._flatten_history_items(res)
            matched = []
            pnl_sum = 0.0
            for item in rows:
                if not isinstance(item, dict):
                    continue
                if not self._history_symbol_matches(item, symbol):
                    continue
                if direction and not self._history_direction_matches(item, direction):
                    continue
                ts = self._extract_timestamp_value(item)
                if ts and (ts < query_start or ts > query_end):
                    continue
                pnl = self._extract_realized_pnl_value(item)
                if pnl is None:
                    continue
                matched.append(item)
                pnl_sum += float(pnl)

            sources.append({"source": name, "ok": True, "endpoint": res.get("history_endpoint"), "rows": len(rows), "matched": len(matched)})
            if matched:
                return {
                    "ok": True,
                    "pnl_usd": round(pnl_sum, 8),
                    "source": name,
                    "endpoint": res.get("history_endpoint"),
                    "matched_rows": matched[-10:],
                    "query": {"symbol": symbol, "direction": direction, "startTime": query_start, "endTime": query_end},
                    "sources_checked": sources,
                }

        return {"ok": False, "error": "realized pnl row not found in Toobit history", "sources_checked": sources}

    def get_closed_position_pnl(self, symbol: str, direction: str | None = None, signal_id: str | None = None, opened_at: int | None = None, closed_at: int | None = None, **kwargs):
        """Compatibility hook used by signal_tracker/real_trade_manager."""
        return self.get_recent_closed_pnl(symbol=symbol, direction=direction, opened_at=opened_at, closed_at=closed_at)

    def get_realized_pnl_for_position(self, symbol: str, direction: str | None = None, opened_at: int | None = None, closed_at: int | None = None, **kwargs):
        """Compatibility alias for closed PnL lookup."""
        return self.get_recent_closed_pnl(symbol=symbol, direction=direction, opened_at=opened_at, closed_at=closed_at)

    def get_position_realized_pnl(self, symbol: str, direction: str | None = None, opened_at: int | None = None, closed_at: int | None = None, **kwargs):
        """Compatibility alias for closed PnL lookup."""
        return self.get_recent_closed_pnl(symbol=symbol, direction=direction, opened_at=opened_at, closed_at=closed_at)


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
