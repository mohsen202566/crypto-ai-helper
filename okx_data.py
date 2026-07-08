from __future__ import annotations

import time
from dataclasses import dataclass

import requests

import config
from utils import normalize_symbol, okx_swap_symbol, safe_float

try:
    import symbols_config
except Exception:
    symbols_config = None  # type: ignore


@dataclass(frozen=True)
class Candle:
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float


class OkxDataClient:
    """OKX public market data only. No real execution here."""

    def __init__(self, base_url: str = config.OKX_BASE_URL, timeout: int = config.OKX_REQUEST_TIMEOUT) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self._symbol_map = self._build_symbol_map()

    def _build_symbol_map(self) -> dict[str, str]:
        out: dict[str, str] = {}
        if symbols_config is not None and hasattr(symbols_config, "enabled_symbols"):
            try:
                for item in symbols_config.enabled_symbols():
                    name = normalize_symbol(str(item.get("name") or ""))
                    if name and not name.endswith("USDT"):
                        name = f"{name}USDT"
                    okx_symbol = str(item.get("okx_symbol") or "").upper().strip()
                    if name and okx_symbol:
                        out[name] = okx_symbol
            except Exception:
                pass
        return out

    def _okx_symbol(self, symbol: str) -> str:
        s = normalize_symbol(symbol)
        return self._symbol_map.get(s) or okx_swap_symbol(s)

    def get_candles(self, symbol: str, bar: str, limit: int = config.OKX_CANDLE_LIMIT) -> list[Candle]:
        inst_id = self._okx_symbol(symbol)
        params = {"instId": inst_id, "bar": bar, "limit": str(max(50, min(int(limit), 300)))}
        url = f"{self.base_url}/api/v5/market/candles"
        last_error: Exception | None = None
        for _ in range(3):
            try:
                response = self.session.get(url, params=params, timeout=self.timeout)
                response.raise_for_status()
                payload = response.json()
                if str(payload.get("code")) not in {"0", "200"}:
                    raise RuntimeError(f"OKX candles error for {symbol} {bar}: {payload}")
                data = payload.get("data") or []
                candles: list[Candle] = []
                # OKX returns newest first. Strategy needs oldest first.
                for row in reversed(data):
                    if len(row) < 6:
                        continue
                    candles.append(
                        Candle(
                            ts=int(float(row[0])),
                            open=safe_float(row[1]),
                            high=safe_float(row[2]),
                            low=safe_float(row[3]),
                            close=safe_float(row[4]),
                            volume=safe_float(row[5]),
                        )
                    )
                if len(candles) < 220:
                    raise RuntimeError(f"OKX candles not enough for {symbol} {bar}: {len(candles)}")
                return candles
            except Exception as exc:
                last_error = exc
                time.sleep(0.35)
        raise RuntimeError(f"OKX candles failed for {symbol} {bar}: {last_error}")

    def get_last_price(self, symbol: str) -> float:
        inst_id = self._okx_symbol(symbol)
        url = f"{self.base_url}/api/v5/market/ticker"
        last_error: Exception | None = None
        for _ in range(3):
            try:
                response = self.session.get(url, params={"instId": inst_id}, timeout=self.timeout)
                response.raise_for_status()
                payload = response.json()
                if str(payload.get("code")) not in {"0", "200"}:
                    raise RuntimeError(f"OKX ticker error for {symbol}: {payload}")
                data = payload.get("data") or []
                if not data:
                    raise RuntimeError(f"OKX ticker empty for {symbol}")
                price = safe_float(data[0].get("last"))
                if price <= 0:
                    raise RuntimeError(f"OKX invalid price for {symbol}: {data[0]}")
                return price
            except Exception as exc:
                last_error = exc
                time.sleep(0.35)
        raise RuntimeError(f"OKX ticker failed for {symbol}: {last_error}")
