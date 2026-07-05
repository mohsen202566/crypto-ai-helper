from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Literal

import requests


OKX_BAR = Literal["5m", "15m", "1H", "4H", "1D"]


@dataclass(frozen=True)
class Candle:
    timestamp_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float


class OKXClient:
    def __init__(self, base_url: str = "https://www.okx.com", timeout: int = 12) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()

    def get_candles(self, inst_id: str, bar: OKX_BAR, limit: int = 220) -> list[Candle]:
        url = f"{self.base_url}/api/v5/market/candles"
        params = {"instId": inst_id, "bar": bar, "limit": str(max(1, min(int(limit), 300)))}
        payload = self._get(url, params=params)
        rows = payload.get("data") or []
        candles: list[Candle] = []
        for row in rows:
            try:
                candles.append(
                    Candle(
                        timestamp_ms=int(row[0]),
                        open=float(row[1]),
                        high=float(row[2]),
                        low=float(row[3]),
                        close=float(row[4]),
                        volume=float(row[5]),
                    )
                )
            except (IndexError, TypeError, ValueError):
                continue
        candles.sort(key=lambda item: item.timestamp_ms)
        if len(candles) < 80:
            raise RuntimeError(f"داده کندل {inst_id} در تایم {bar} کافی نیست.")
        return candles

    def get_last_price(self, inst_id: str) -> float:
        url = f"{self.base_url}/api/v5/market/ticker"
        payload = self._get(url, params={"instId": inst_id})
        rows = payload.get("data") or []
        if not rows:
            raise RuntimeError(f"قیمت {inst_id} از اوکی‌اکس خوانده نشد.")
        price = float(rows[0].get("last") or rows[0].get("markPx") or 0)
        if price <= 0:
            raise RuntimeError(f"قیمت {inst_id} معتبر نیست.")
        return price

    def _get(self, url: str, params: dict[str, str]) -> dict:
        last_error: Exception | None = None
        for _ in range(3):
            try:
                response = self.session.get(url, params=params, timeout=self.timeout)
                response.raise_for_status()
                payload = response.json()
                if str(payload.get("code", "0")) != "0":
                    raise RuntimeError(str(payload))
                return payload
            except Exception as exc:
                last_error = exc
                time.sleep(0.5)
        raise RuntimeError(f"خطا در دریافت داده از اوکی‌اکس: {last_error}")
