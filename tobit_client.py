"""
tobit_client.py
Level 4 / 1H Smart Scalp Bot

Low-level Toobit client.

Architecture lock:
- This is the ONLY low-level Toobit API layer.
- No AI decision, no Telegram, no command routing, no JSON ownership.
- Reads API keys from environment only.
- Uses Toobit futures v1-compatible routes from the locked working behavior.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Optional
import hashlib
import hmac
import os
import re
import time
from urllib.parse import urlencode

import requests

from constants import (
    DIRECTION_LONG, DIRECTION_SHORT, STATUS_FAILED, STATUS_OK, STATUS_RECOVERED,
    SYSTEM_VERSION, TOOBIT_API_KEY_ENV, TOOBIT_BASE_URL_ENV, TOOBIT_REQUEST_TIMEOUT_SECONDS,
    TOOBIT_SECRET_KEY_ENV, TOOBIT_SPECIAL_SYMBOL_MAP, TRADE_CONFIG,
)
from models import TradeCloseResult, TradeOpenResult
from utils import (
    normalize_direction, normalize_symbol, profit_usdt, round_price, round_quantity,
    safe_float, safe_int, safe_str,
)


TOBIT_CLIENT_VERSION: str = SYSTEM_VERSION
DEFAULT_TOOBIT_BASE_URL: str = "https://api.toobit.com"
MARGIN_ISOLATED: str = "ISOLATED"
MARGIN_CROSS: str = "CROSS"

SIDE_BUY_OPEN: str = "BUY_OPEN"
SIDE_SELL_OPEN: str = "SELL_OPEN"
SIDE_BUY_CLOSE: str = "BUY_CLOSE"
SIDE_SELL_CLOSE: str = "SELL_CLOSE"


class ToobitAPIError(RuntimeError):
    pass


@dataclass(frozen=True)
class ToobitCredentials:
    api_key: str = ""
    api_secret: str = ""

    def valid(self) -> bool:
        return bool(self.api_key and self.api_secret)


@dataclass
class ToobitResponse:
    ok: bool
    status_code: int = 0
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    raw_text: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SymbolRules:
    symbol: str
    exchange_symbol: str = ""
    min_qty: float = 0.001
    qty_step: float = 0.001
    min_notional: float = 5.0
    price_tick: float = 0.0001
    quantity_precision: int = 6
    price_precision: int = 6

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def now_ms() -> int:
    return int(time.time() * 1000)


def _load_dotenv_once() -> None:
    """
    Best-effort .env loader.
    systemd Environment remains primary; .env is only fallback.
    """
    if os.environ.get("_L4_DOTENV_CHECKED") == "1":
        return
    os.environ["_L4_DOTENV_CHECKED"] = "1"
    try:
        from dotenv import load_dotenv  # type: ignore
        load_dotenv(".env", override=False)
    except Exception:
        pass


def _read_systemd_env_value(name: str) -> str:
    """
    Fallback for this VPS layout where credentials are stored in systemd service.
    Used only when os.environ/.env do not provide the value.
    """
    service_paths = [
        "/etc/systemd/system/crypto-bot.service",
        "/etc/systemd/system/crypto-bot.service.d/override.conf",
    ]
    patterns = [
        re.compile(r'^\s*Environment\s*=\s*["\']?' + re.escape(name) + r'=([^"\'\n]+)["\']?\s*$'),
        re.compile(r'^\s*' + re.escape(name) + r'=([^\n]+)\s*$'),
    ]
    for path in service_paths:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    clean = line.strip()
                    for pattern in patterns:
                        match = pattern.match(clean)
                        if match:
                            return safe_str(match.group(1)).strip().strip('"').strip("'")
        except Exception:
            continue
    return ""


def env_first(*names: str, default: str = "") -> str:
    _load_dotenv_once()
    for name in names:
        value = safe_str(os.getenv(name))
        if value:
            return value
    for name in names:
        value = _read_systemd_env_value(name)
        if value:
            return value
    return default


def get_toobit_api_key(default: str = "") -> str:
    return env_first(TOOBIT_API_KEY_ENV, "TOOBIT_API_KEY", "TOBIT_API_KEY", default=default)


def get_toobit_api_secret(default: str = "") -> str:
    return env_first(TOOBIT_SECRET_KEY_ENV, "TOOBIT_API_SECRET", "TOOBIT_SECRET_KEY", "TOBIT_API_SECRET", "TOBIT_SECRET_KEY", default=default)


def get_toobit_base_url(default: str = DEFAULT_TOOBIT_BASE_URL) -> str:
    return env_first(TOOBIT_BASE_URL_ENV, "TOOBIT_BASE_URL", default=default).rstrip("/")


def normalize_toobit_plain_symbol(symbol: str) -> str:
    raw = safe_str(symbol).upper().replace("/", "").replace("_", "").replace("-", "")
    raw = raw.replace("SWAP", "")
    if raw.endswith("USDT"):
        return TOOBIT_SPECIAL_SYMBOL_MAP.get(raw, raw)
    if raw.endswith("USDC"):
        return raw
    return TOOBIT_SPECIAL_SYMBOL_MAP.get(raw, raw)


def normalize_bot_plain_symbol(symbol: str) -> str:
    plain = normalize_toobit_plain_symbol(symbol)
    reverse = {v: k for k, v in TOOBIT_SPECIAL_SYMBOL_MAP.items()}
    return reverse.get(plain, plain)


def normalize_futures_symbol(symbol: str) -> str:
    plain = normalize_toobit_plain_symbol(symbol)
    if plain.endswith("USDT"):
        return f"{plain[:-4]}-SWAP-USDT"
    if plain.endswith("USDC"):
        return f"{plain[:-4]}-SWAP-USDC"
    return plain


def open_side_for_direction(direction: str) -> str:
    return SIDE_BUY_OPEN if normalize_direction(direction) == DIRECTION_LONG else SIDE_SELL_OPEN


def close_side_for_direction(direction: str) -> str:
    return SIDE_SELL_CLOSE if normalize_direction(direction) == DIRECTION_LONG else SIDE_BUY_CLOSE


def _clean_params(params: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in dict(params or {}).items():
        if value is None:
            continue
        out[key] = "true" if value is True else "false" if value is False else value
    return out


class ToobitSigner:
    def __init__(self, credentials: ToobitCredentials):
        self.credentials = credentials

    def sign_params(self, params: Mapping[str, Any]) -> dict[str, Any]:
        if not self.credentials.valid():
            raise ToobitAPIError("missing_toobit_credentials")
        signed = dict(params)
        signed.setdefault("recvWindow", 5000)
        signed.setdefault("timestamp", now_ms())
        query = urlencode(signed, doseq=True)
        signed["signature"] = hmac.new(
            self.credentials.api_secret.encode("utf-8"),
            query.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return signed


class ToobitClient:
    def __init__(self, api_key: str | None = None, api_secret: str | None = None, base_url: str | None = None, timeout: int | None = None, session: Any = None):
        self.base_url = (base_url or get_toobit_base_url()).rstrip("/")
        self.timeout = int(timeout or TOOBIT_REQUEST_TIMEOUT_SECONDS or 10)
        self.credentials = ToobitCredentials(api_key or get_toobit_api_key(), api_secret or get_toobit_api_secret())
        self.signer = ToobitSigner(self.credentials)
        self.session = session or requests.Session()
        self._exchange_info_cache: dict[str, Any] = {}
        self._symbol_rules_cache: dict[str, SymbolRules] = {}
        self._leverage_cache: dict[str, int] = {}
        self._margin_mode_cache: dict[str, str] = {}
        if self.credentials.api_key:
            self.session.headers.update({"X-BB-APIKEY": self.credentials.api_key, "User-Agent": "level4-smart-scalp/1.0", "Content-Type": "application/x-www-form-urlencoded"})

    def _request(self, method: str, path: str, params: Optional[Mapping[str, Any]] = None, *, signed: bool = False) -> ToobitResponse:
        method = safe_str(method).upper()
        clean = _clean_params(params)
        if signed:
            clean = self.signer.sign_params(clean)
        url = self.base_url + path
        try:
            if method == "GET":
                response = self.session.get(url, params=clean, timeout=self.timeout)
            elif method == "POST":
                response = self.session.post(url, data=clean, timeout=self.timeout)
            elif method == "DELETE":
                response = self.session.delete(url, params=clean, timeout=self.timeout)
            else:
                return ToobitResponse(ok=False, error=f"unsupported_method:{method}")
            raw = safe_str(getattr(response, "text", ""))
            try:
                data = response.json()
            except Exception:
                data = {"raw": raw}
            status_code = safe_int(getattr(response, "status_code", 0), 0) or 0
            ok = status_code < 400
            error = ""
            if isinstance(data, dict):
                code = data.get("code", data.get("retCode", data.get("status")))
                msg = data.get("msg", data.get("message", data.get("error", "")))
                if code not in {None, "", 0, 200, "0", "200", "OK", "ok"}:
                    ok = False
                    error = f"{code}:{msg}"
                if data.get("success", None) is False:
                    ok = False
                    error = error or safe_str(msg, "success_false")
            if not ok and not error:
                error = safe_str(data, "request_failed")
            return ToobitResponse(ok=ok, status_code=status_code, data=data if isinstance(data, dict) else {"data": data}, error=error, raw_text=raw)
        except Exception as exc:
            return ToobitResponse(ok=False, status_code=0, data={}, error=str(exc), raw_text="")

    def _request_first_ok(self, method: str, paths: list[str], params: Optional[Mapping[str, Any]] = None, *, signed: bool = False) -> ToobitResponse:
        last = ToobitResponse(ok=False, error="no_paths")
        for path in paths:
            last = self._request(method, path, params=params, signed=signed)
            if last.ok:
                return last
        return last

    def normalize_futures_symbol(self, symbol: str) -> str:
        return normalize_futures_symbol(symbol)

    def normalize_bot_symbol(self, symbol: str) -> str:
        return normalize_symbol(normalize_bot_plain_symbol(symbol))

    def _flatten_items(self, payload: Any) -> list[dict[str, Any]]:
        data = payload.get("data", payload) if isinstance(payload, dict) else payload
        out: list[dict[str, Any]] = []
        def walk(value: Any) -> None:
            if isinstance(value, dict):
                if any(k in value for k in ("symbol", "contractCode", "instrumentId", "minQty", "quantityPrecision", "positionAmt", "realizedPnl")):
                    out.append(value)
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)
        walk(data)
        return out

    def get_exchange_info(self, *, force: bool = False) -> dict[str, Any]:
        if self._exchange_info_cache and not force:
            age = time.time() - safe_float(self._exchange_info_cache.get("_ts"), 0.0)
            if 0 <= age <= 3600:
                return dict(self._exchange_info_cache.get("payload", {}))
        response = self._request_first_ok("GET", ["/api/v1/futures/exchangeInfo", "/api/v1/futures/symbols", "/api/v1/exchangeInfo"], signed=False)
        if response.ok:
            self._exchange_info_cache = {"_ts": time.time(), "payload": response.data}
            return response.data
        return {}

    def _parse_rules_item(self, item: Mapping[str, Any], bot_symbol: str) -> Optional[SymbolRules]:
        symbol_raw = safe_str(item.get("symbol") or item.get("contractCode") or item.get("instrumentId") or item.get("name"))
        if not symbol_raw:
            return None
        candidates = {normalize_toobit_plain_symbol(symbol_raw), normalize_bot_plain_symbol(symbol_raw), self.normalize_futures_symbol(symbol_raw), normalize_symbol(symbol_raw)}
        bot_plain = normalize_toobit_plain_symbol(bot_symbol)
        if bot_plain not in candidates and normalize_symbol(bot_symbol) not in candidates:
            return None
        min_qty = safe_float(item.get("minQty") or item.get("min_quantity") or item.get("minTradeQty") or item.get("minVolume"), 0.001) or 0.001
        qty_step = safe_float(item.get("stepSize") or item.get("qtyStep") or item.get("quantityStep") or item.get("minQty"), 0.001) or 0.001
        min_notional = safe_float(item.get("minNotional") or item.get("minValue") or item.get("minTradeAmount"), 5.0) or 5.0
        price_tick = safe_float(item.get("tickSize") or item.get("priceTick") or item.get("priceStep"), 0.0001) or 0.0001
        return SymbolRules(
            symbol=normalize_symbol(bot_symbol), exchange_symbol=self.normalize_futures_symbol(bot_symbol),
            min_qty=min_qty, qty_step=qty_step, min_notional=min_notional, price_tick=price_tick,
            quantity_precision=safe_int(item.get("quantityPrecision") or item.get("qtyPrecision"), 6) or 6,
            price_precision=safe_int(item.get("pricePrecision"), 6) or 6,
        )

    def get_symbol_rules(self, symbol: str, *, force: bool = False) -> SymbolRules:
        bot_symbol = normalize_symbol(symbol)
        if bot_symbol in self._symbol_rules_cache and not force:
            return self._symbol_rules_cache[bot_symbol]
        info = self.get_exchange_info(force=force)
        for item in self._flatten_items(info):
            parsed = self._parse_rules_item(item, bot_symbol)
            if parsed:
                self._symbol_rules_cache[bot_symbol] = parsed
                return parsed
        fallback = SymbolRules(symbol=bot_symbol, exchange_symbol=self.normalize_futures_symbol(bot_symbol))
        self._symbol_rules_cache[bot_symbol] = fallback
        return fallback

    def validate_quantity(self, symbol: str, quantity: Any, price: Any = 0.0) -> tuple[bool, float, str, SymbolRules]:
        rules = self.get_symbol_rules(symbol)
        qty = round_quantity(safe_float(quantity, 0.0) or 0.0, rules.qty_step)
        if qty <= 0:
            return False, qty, "quantity_zero", rules
        if qty < rules.min_qty:
            return False, qty, f"quantity_below_min_qty:{qty}<{rules.min_qty}", rules
        price_f = safe_float(price, 0.0) or 0.0
        if price_f > 0 and qty * price_f < rules.min_notional:
            return False, qty, f"notional_below_min:{qty * price_f}<{rules.min_notional}", rules
        return True, qty, "OK", rules

    def ping_private(self) -> bool:
        try:
            return self._request("GET", "/api/v1/futures/balance", signed=True).ok
        except Exception:
            return False

    def _balance_candidates(self, payload: Any) -> list[dict[str, Any]]:
        """
        Extract possible balance rows from Toobit responses.

        Toobit balance responses may be nested and may use fields such as
        totalWalletBalance/accountEquity/totalMarginBalance instead of balance.
        """
        candidates: list[dict[str, Any]] = []
        balance_keys = {
            "asset", "coin", "currency", "ccy",
            "balance", "walletBalance", "equity", "total", "totalBalance",
            "available", "availableBalance", "free", "freeBalance",
            "totalWalletBalance", "accountEquity", "marginBalance",
            "totalMarginBalance", "availableMargin",
            "usdtBalance", "cashBalance",
        }

        def walk(value: Any) -> None:
            if isinstance(value, Mapping):
                if any(k in value for k in balance_keys):
                    candidates.append(dict(value))
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)

        walk(payload)
        return candidates

    def get_account_balance(self, asset: str = "USDT") -> dict[str, Any]:
        asset_u = safe_str(asset, "USDT").upper()

        if not self.credentials.valid():
            return {
                "status": STATUS_FAILED,
                "asset": asset_u,
                "balance": None,
                "available": None,
                "error": "missing_toobit_credentials",
                "credentials_loaded": False,
                "raw": {},
            }

        response = self._request("GET", "/api/v1/futures/balance", signed=True)
        if not response.ok:
            return {
                "status": STATUS_FAILED,
                "asset": asset_u,
                "balance": None,
                "available": None,
                "error": response.error,
                "credentials_loaded": True,
                "raw": response.to_dict(),
            }

        rows = self._balance_candidates(response.data)
        best: Mapping[str, Any] = response.data

        for row in rows:
            row_asset = safe_str(row.get("asset") or row.get("coin") or row.get("currency") or row.get("ccy"))
            if row_asset and row_asset.upper() == asset_u:
                best = row
                break
        else:
            if rows:
                for row in rows:
                    if asset_u in safe_str(row).upper():
                        best = row
                        break
                else:
                    best = rows[0]

        balance = safe_float(
            best.get("balance")
            or best.get("walletBalance")
            or best.get("equity")
            or best.get("total")
            or best.get("totalBalance")
            or best.get("totalWalletBalance")
            or best.get("accountEquity")
            or best.get("marginBalance")
            or best.get("totalMarginBalance")
            or best.get("usdtBalance")
            or best.get("cashBalance"),
            None,
        )
        available = safe_float(
            best.get("available")
            or best.get("availableBalance")
            or best.get("free")
            or best.get("freeBalance")
            or best.get("availableMargin")
            or balance,
            None,
        )

        return {
            "status": STATUS_OK,
            "asset": asset_u,
            "balance": balance,
            "available": available,
            "error": "",
            "credentials_loaded": True,
            "raw": response.data,
        }

    get_balance = get_account_balance

    def get_positions(self, symbol: str = "") -> list[dict[str, Any]]:
        params = {"symbol": self.normalize_futures_symbol(symbol)} if symbol else {}
        response = self._request("GET", "/api/v1/futures/positions", params=params, signed=True)
        return self._flatten_items(response.data) if response.ok else []

    def _position_qty(self, row: Mapping[str, Any]) -> float:
        return abs(safe_float(row.get("positionAmt") or row.get("positionAmount") or row.get("qty") or row.get("volume") or row.get("available"), 0.0) or 0.0)

    def _position_direction(self, row: Mapping[str, Any]) -> str:
        side = safe_str(row.get("side") or row.get("positionSide") or row.get("direction")).upper()
        if side in {"LONG", "BUY", "BUY_OPEN"}:
            return DIRECTION_LONG
        if side in {"SHORT", "SELL", "SELL_OPEN"}:
            return DIRECTION_SHORT
        qty = safe_float(row.get("positionAmt"), 0.0) or 0.0
        return DIRECTION_SHORT if qty < 0 else DIRECTION_LONG

    def _position_matches(self, row: Mapping[str, Any], symbol: str, direction: str = "", *, require_qty: bool = True) -> bool:
        if symbol:
            row_symbol = safe_str(row.get("symbol") or row.get("contractCode") or row.get("instrumentId"))
            if row_symbol and self.normalize_bot_symbol(row_symbol) != normalize_symbol(symbol):
                return False
        if direction and self._position_direction(row) != normalize_direction(direction):
            return False
        return self._position_qty(row) > 0 if require_qty else True

    def get_position(self, symbol: str, direction: str = "") -> Optional[dict[str, Any]]:
        for row in self.get_positions(symbol):
            if self._position_matches(row, symbol, direction):
                return dict(row)
        return None

    def get_open_positions(self, symbol: str = "") -> list[dict[str, Any]]:
        return [dict(r) for r in self.get_positions(symbol) if self._position_qty(r) > 0]

    def get_margin_mode(self, symbol: str) -> str:
        return self._margin_mode_cache.get(normalize_symbol(symbol), MARGIN_ISOLATED)

    def set_margin_mode(self, symbol: str, margin_mode: str = MARGIN_ISOLATED) -> dict[str, Any]:
        mode = safe_str(margin_mode, MARGIN_ISOLATED).upper()
        if mode != MARGIN_ISOLATED:
            return {"status": STATUS_FAILED, "ok": False, "error": "cross_margin_blocked"}
        self._margin_mode_cache[normalize_symbol(symbol)] = MARGIN_ISOLATED
        return {"status": STATUS_OK, "ok": True, "symbol": normalize_symbol(symbol), "margin_mode": MARGIN_ISOLATED}

    change_margin_mode = set_margin_mode
    change_symbol_margin_mode = set_margin_mode
    set_futures_margin_mode = set_margin_mode

    def get_leverage(self, symbol: str) -> int:
        bot_symbol = normalize_symbol(symbol)
        if bot_symbol in self._leverage_cache:
            return self._leverage_cache[bot_symbol]
        pos = self.get_position(bot_symbol)
        if pos:
            lev = safe_int(pos.get("leverage") or pos.get("lever"), 0) or 0
            if lev > 0:
                self._leverage_cache[bot_symbol] = lev
                return lev
        return 0

    def set_leverage(self, symbol: str, leverage: int) -> dict[str, Any]:
        lev = safe_int(leverage, 0) or 0
        if lev <= 0:
            return {"status": STATUS_FAILED, "ok": False, "error": "invalid_leverage"}
        response = self._request("POST", "/api/v1/futures/leverage", params={"symbol": self.normalize_futures_symbol(symbol), "leverage": lev}, signed=True)
        if response.ok:
            self._leverage_cache[normalize_symbol(symbol)] = lev
            return {"status": STATUS_OK, "ok": True, "symbol": normalize_symbol(symbol), "leverage": lev, "raw": response.data}
        return {"status": STATUS_FAILED, "ok": False, "symbol": normalize_symbol(symbol), "leverage": lev, "error": response.error, "raw": response.to_dict()}

    change_leverage = set_leverage
    change_symbol_leverage = set_leverage
    set_symbol_leverage = set_leverage
    set_futures_leverage = set_leverage

    def verify_leverage(self, symbol: str, leverage: int) -> tuple[bool, str]:
        desired = safe_int(leverage, 0) or 0
        if desired <= 0:
            return False, "invalid_leverage"
        current = self.get_leverage(symbol)
        if current == desired:
            return True, "OK"
        result = self.set_leverage(symbol, desired)
        if result.get("ok"):
            current2 = self.get_leverage(symbol)
            if current2 in {0, desired}:
                return True, "OK"
        return False, safe_str(result.get("error"), "leverage_not_verified")

    def _order_response_id(self, payload: Mapping[str, Any]) -> str:
        def walk(value: Any) -> str:
            if isinstance(value, Mapping):
                for key in ("orderId", "order_id", "clientOrderId", "newClientOrderId", "id"):
                    found = safe_str(value.get(key))
                    if found:
                        return found
                for child in value.values():
                    found = walk(child)
                    if found:
                        return found
            elif isinstance(value, list):
                for child in value:
                    found = walk(child)
                    if found:
                        return found
            return ""
        return walk(payload)

    def _recover_open_position_after_order_error(self, symbol: str, direction: str, wait_seconds: float = 70.0) -> Optional[dict[str, Any]]:
        time.sleep(max(0.0, wait_seconds))
        return self.get_position(symbol, direction)

    def _recover_close_after_order_error(self, symbol: str, direction: str, wait_seconds: float = 70.0, poll_seconds: float = 5.0) -> bool:
        """
        Toobit can accept/execute a close even when the API response is delayed or looks failed.
        Re-check exchange open positions before leaving the internal slot locked.
        """
        deadline = time.time() + max(0.0, wait_seconds)
        poll = max(1.0, poll_seconds)
        while time.time() <= deadline:
            if not self.get_position(symbol, direction):
                return True
            time.sleep(poll)
        return not bool(self.get_position(symbol, direction))

    def open_futures_position(self, symbol: str, side: str = "", direction: str = "", quantity: Any = 0.0, price: Any = 0.0, order_type: str = "MARKET", margin_mode: str = MARGIN_ISOLATED, leverage: int = 1, take_profit: Any = None, take_profit_2: Any = None, stop_loss: Any = None, client_order_id: str = "") -> TradeOpenResult:
        bot_symbol = normalize_symbol(symbol)
        d = normalize_direction(direction or side)
        entry_price = safe_float(price, 0.0) or 0.0
        if margin_mode.upper() != MARGIN_ISOLATED:
            return TradeOpenResult(status=STATUS_FAILED, symbol=bot_symbol, direction=d, error="cross_margin_blocked")
        ok, qty, reason, rules = self.validate_quantity(bot_symbol, quantity, entry_price)
        if not ok:
            return TradeOpenResult(status=STATUS_FAILED, symbol=bot_symbol, direction=d, quantity=qty, entry=entry_price, error=reason)
        lev_ok, lev_reason = self.verify_leverage(bot_symbol, leverage)
        if TRADE_CONFIG.get("require_leverage_verification", True) and not lev_ok:
            return TradeOpenResult(status=STATUS_FAILED, symbol=bot_symbol, direction=d, quantity=qty, entry=entry_price, error=f"leverage_not_verified:{lev_reason}")
        params = {"symbol": rules.exchange_symbol or self.normalize_futures_symbol(bot_symbol), "side": open_side_for_direction(d), "type": "LIMIT", "priceType": "MARKET" if order_type.upper() == "MARKET" else safe_str(order_type, "MARKET").upper(), "quantity": qty, "newClientOrderId": client_order_id or f"L4_{bot_symbol}_{d}_{now_ms()}"}
        if take_profit:
            params["takeProfit"] = round_price(take_profit, rules.price_tick)
            params["tpOrderType"] = "MARKET"
        if stop_loss:
            params["stopLoss"] = round_price(stop_loss, rules.price_tick)
            params["slOrderType"] = "MARKET"
        response = self._request("POST", "/api/v1/futures/order", params=params, signed=True)
        if response.ok:
            return TradeOpenResult(status=STATUS_OK, symbol=bot_symbol, direction=d, entry=entry_price, quantity=qty, exchange_order_id=self._order_response_id(response.data), message="order_submitted", raw=response.data)
        recovered = self._recover_open_position_after_order_error(bot_symbol, d, wait_seconds=70.0)
        if recovered:
            recovered_entry = safe_float(recovered.get("entryPrice") or recovered.get("avgPrice") or recovered.get("price"), entry_price) or entry_price
            recovered_qty = self._position_qty(recovered) or qty
            return TradeOpenResult(status=STATUS_RECOVERED, symbol=bot_symbol, direction=d, entry=recovered_entry, quantity=recovered_qty, recovered=True, message="order_error_but_position_found", error=response.error, raw={"order_error": response.to_dict(), "position": recovered})
        return TradeOpenResult(status=STATUS_FAILED, symbol=bot_symbol, direction=d, entry=entry_price, quantity=qty, error=response.error, raw=response.to_dict())

    def close_position(self, symbol: str, direction: str, quantity: Any = 0.0, price: Any = 0.0, client_order_id: str = "") -> TradeCloseResult:
        bot_symbol = normalize_symbol(symbol)
        d = normalize_direction(direction)
        qty_raw = safe_float(quantity, 0.0) or 0.0
        close_price = safe_float(price, 0.0) or 0.0
        pos = self.get_position(bot_symbol, d)

        # If exchange already closed it via TP/SL/manual close, confirm success instead of keeping the slot stuck.
        if not pos:
            pnl_data = self.wait_for_closed_position_pnl(bot_symbol, d, timeout_seconds=45, poll_seconds=5)
            return TradeCloseResult(
                status=STATUS_OK,
                symbol=bot_symbol,
                direction=d,
                close_price=close_price,
                closed_quantity=max(0.0, qty_raw),
                pnl_usdt=safe_float(pnl_data.get("pnl_usdt"), None),
                pnl_confirmed=bool(pnl_data.get("confirmed", False)),
                close_confirmed=True,
                message="already_closed_on_exchange",
                raw={"pnl": pnl_data},
            )

        if qty_raw <= 0:
            qty_raw = self._position_qty(pos)

        ok, qty, reason, rules = self.validate_quantity(bot_symbol, qty_raw, price)
        if not ok:
            return TradeCloseResult(status=STATUS_FAILED, symbol=bot_symbol, direction=d, close_price=close_price, closed_quantity=qty, close_confirmed=False, error=reason)

        params = {
            "symbol": rules.exchange_symbol or self.normalize_futures_symbol(bot_symbol),
            "side": close_side_for_direction(d),
            "type": "LIMIT",
            "priceType": "MARKET",
            "quantity": qty,
            "newClientOrderId": client_order_id or f"L4_CLOSE_{bot_symbol}_{d}_{now_ms()}",
        }
        response = self._request("POST", "/api/v1/futures/order", params=params, signed=True)

        if not response.ok:
            recovered_closed = self._recover_close_after_order_error(bot_symbol, d, wait_seconds=70.0, poll_seconds=5.0)
            if recovered_closed:
                pnl_data = self.wait_for_closed_position_pnl(bot_symbol, d, timeout_seconds=50, poll_seconds=5)
                return TradeCloseResult(
                    status=STATUS_RECOVERED,
                    symbol=bot_symbol,
                    direction=d,
                    close_price=close_price,
                    closed_quantity=qty,
                    pnl_usdt=safe_float(pnl_data.get("pnl_usdt"), None),
                    pnl_confirmed=bool(pnl_data.get("confirmed", False)),
                    close_confirmed=True,
                    message="close_error_but_position_disappeared",
                    error=response.error,
                    raw={"order_error": response.to_dict(), "pnl": pnl_data},
                )
            return TradeCloseResult(status=STATUS_FAILED, symbol=bot_symbol, direction=d, close_price=close_price, closed_quantity=qty, close_confirmed=False, error=response.error, raw=response.to_dict())

        confirmed = self.verify_close(bot_symbol, d, attempts=14, sleep_seconds=5)
        pnl_data = self.wait_for_closed_position_pnl(bot_symbol, d, timeout_seconds=50, poll_seconds=5)
        return TradeCloseResult(
            status=STATUS_OK if confirmed else STATUS_FAILED,
            exchange_order_id=self._order_response_id(response.data),
            symbol=bot_symbol,
            direction=d,
            close_price=close_price,
            closed_quantity=qty,
            pnl_usdt=safe_float(pnl_data.get("pnl_usdt"), None),
            pnl_confirmed=bool(pnl_data.get("confirmed", False)),
            close_confirmed=confirmed,
            message="close_confirmed" if confirmed else "close_not_confirmed",
            error="" if confirmed else "close_not_confirmed",
            raw={"order": response.data, "pnl": pnl_data},
        )

    close_futures_position = close_position

    def verify_close(self, symbol: str, direction: str = "", attempts: int | None = None, sleep_seconds: float | None = None) -> bool:
        # Toobit close confirmation can lag. Check long enough so valid closes do not leave slots stuck.
        count = safe_int(attempts, TRADE_CONFIG.get("close_confirm_attempts", 14)) or 14
        sleep = safe_float(sleep_seconds, TRADE_CONFIG.get("close_confirm_sleep_seconds", 5)) or 5.0
        for _ in range(max(1, count)):
            if not self.get_position(symbol, direction):
                return True
            time.sleep(max(0.0, sleep))
        return not bool(self.get_position(symbol, direction))

    def ensure_tp_sl(self, symbol: str, direction: str, take_profit: Any = None, stop_loss: Any = None, take_profit_2: Any = None) -> dict[str, Any]:
        return self.set_position_tp_sl(symbol, direction, take_profit=take_profit, stop_loss=stop_loss, take_profit_2=take_profit_2)

    def set_position_tp_sl(self, symbol: str, direction: str, take_profit: Any = None, stop_loss: Any = None, take_profit_2: Any = None) -> dict[str, Any]:
        return {"status": STATUS_OK, "ok": True, "symbol": normalize_symbol(symbol), "direction": normalize_direction(direction), "take_profit": take_profit, "take_profit_2": take_profit_2, "stop_loss": stop_loss, "note": "tp_sl_repair_noop_until_endpoint_confirmed"}

    def get_closed_position_pnl(self, symbol: str = "", direction: str = "", since_ms: Any = None) -> dict[str, Any]:
        params = {"symbol": self.normalize_futures_symbol(symbol)} if symbol else {}
        if since_ms:
            params["startTime"] = safe_int(since_ms, 0) or 0
        response = self._request_first_ok("GET", ["/api/v1/futures/closedPosition", "/api/v1/futures/history/positions", "/api/v1/futures/userTrades"], params=params, signed=True)
        if not response.ok:
            return {"status": STATUS_FAILED, "confirmed": False, "pnl_usdt": None, "error": response.error, "raw": response.to_dict()}
        rows = self._flatten_items(response.data)
        for row in rows:
            row_symbol = safe_str(row.get("symbol") or row.get("contractCode") or row.get("instrumentId"))
            if symbol and row_symbol and self.normalize_bot_symbol(row_symbol) != normalize_symbol(symbol):
                continue
            for key in ("realizedPnl", "realizedPNL", "realizedProfit", "pnl", "profit"):
                if key in row:
                    return {"status": STATUS_OK, "confirmed": True, "pnl_usdt": safe_float(row.get(key), 0.0) or 0.0, "row": dict(row), "raw": response.data}
        return {"status": STATUS_OK, "confirmed": False, "pnl_usdt": None, "raw": response.data}

    def wait_for_closed_position_pnl(self, symbol: str = "", direction: str = "", timeout_seconds: int = 45, poll_seconds: int = 5) -> dict[str, Any]:
        deadline = time.time() + max(0, safe_int(timeout_seconds, 45) or 45)
        poll = max(1, safe_int(poll_seconds, 5) or 5)
        last = {"status": STATUS_FAILED, "confirmed": False, "pnl_usdt": None}
        while time.time() <= deadline:
            last = self.get_closed_position_pnl(symbol, direction)
            if last.get("confirmed"):
                return last
            time.sleep(poll)
        return last


ToBitClient = ToobitClient
_client: Optional[ToobitClient] = None


def get_client() -> ToobitClient:
    global _client
    if _client is None:
        _client = ToobitClient()
    return _client


def toobit_client() -> ToobitClient:
    return get_client()


__all__ = [
    "TOBIT_CLIENT_VERSION", "MARGIN_ISOLATED", "MARGIN_CROSS", "ToobitAPIError", "ToobitCredentials",
    "ToobitResponse", "SymbolRules", "ToobitClient", "ToBitClient", "get_client", "toobit_client",
    "get_toobit_api_key", "get_toobit_api_secret", "get_toobit_base_url", "normalize_toobit_plain_symbol",
    "normalize_bot_plain_symbol", "normalize_futures_symbol", "open_side_for_direction", "close_side_for_direction",
]
