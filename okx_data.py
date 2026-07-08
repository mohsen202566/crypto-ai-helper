from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests

import config
from utils import okx_swap_symbol, safe_float


@dataclass(frozen=True)
class Candle:
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    volume_ccy: float = 0.0


class OkxDataClient:
    def __init__(self, base_url: str = config.OKX_BASE_URL, timeout: int = config.OKX_REQUEST_TIMEOUT) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()

    def _get(self, path: str, params: dict[str, Any]) -> Any:
        r = self.session.get(self.base_url + path, params=params, timeout=self.timeout)
        r.raise_for_status()
        payload = r.json()
        if str(payload.get("code", "0")) != "0":
            raise RuntimeError(f"OKX error {payload}")
        return payload.get("data") or []

    def get_candles(self, symbol: str, bar: str = "5m", limit: int = config.OKX_CANDLE_LIMIT) -> list[Candle]:
        data = self._get("/api/v5/market/candles", {"instId": okx_swap_symbol(symbol), "bar": bar, "limit": int(limit)})
        candles: list[Candle] = []
        for row in reversed(data):
            try:
                candles.append(Candle(
                    ts=int(float(row[0])),
                    open=safe_float(row[1]),
                    high=safe_float(row[2]),
                    low=safe_float(row[3]),
                    close=safe_float(row[4]),
                    volume=safe_float(row[5]),
                    volume_ccy=safe_float(row[6]) if len(row) > 6 else 0.0,
                ))
            except Exception:
                continue
        return candles

    def get_last_price(self, symbol: str) -> float:
        data = self._get("/api/v5/market/ticker", {"instId": okx_swap_symbol(symbol)})
        if not data:
            return 0.0
        return safe_float(data[0].get("last") or data[0].get("markPx") or data[0].get("idxPx"))

    def get_order_book(self, symbol: str, depth: int = config.ORDERBOOK_DEPTH_LEVELS) -> dict[str, Any]:
        data = self._get("/api/v5/market/books", {"instId": okx_swap_symbol(symbol), "sz": int(depth)})
        if not data:
            return {"bids": [], "asks": []}
        return data[0]

    def get_trades(self, symbol: str, limit: int = 100) -> list[dict[str, Any]]:
        try:
            data = self._get("/api/v5/market/trades", {"instId": okx_swap_symbol(symbol), "limit": int(limit)})
            return list(reversed(data))
        except Exception:
            return []

    def get_open_interest(self, symbol: str) -> float | None:
        try:
            data = self._get("/api/v5/public/open-interest", {"instType": "SWAP", "instId": okx_swap_symbol(symbol)})
            if not data:
                return None
            return safe_float(data[0].get("oi") or data[0].get("oiCcy"), 0.0)
        except Exception:
            return None
