from __future__ import annotations

import time
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


class OkxDataClient:
    """OKX public market data only. No real execution here."""

    def __init__(self, base_url: str = config.OKX_BASE_URL, timeout: int = config.OKX_REQUEST_TIMEOUT) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()

    def get_candles(self, symbol: str, bar: str, limit: int = config.OKX_CANDLE_LIMIT) -> list[Candle]:
        inst_id = okx_swap_symbol(symbol)
        params = {"instId": inst_id, "bar": bar, "limit": str(max(50, min(int(limit), 300)))}
        url = f"{self.base_url}/api/v5/market/candles"
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
        return candles

    def get_last_price(self, symbol: str) -> float:
        inst_id = okx_swap_symbol(symbol)
        url = f"{self.base_url}/api/v5/market/ticker"
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
