from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests

from config import OKX_BASE_URL, OKX_CANDLE_LIMIT, TIMEFRAME


@dataclass(frozen=True)
class Candle:
    ts: int
    open: float
    high: float
    low: float
    close: float


class OkxDataClient:
    def __init__(self, base_url: str = OKX_BASE_URL, timeout_seconds: int = 10) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.session = requests.Session()

    def get_candles(self, inst_id: str, limit: int = OKX_CANDLE_LIMIT) -> list[Candle]:
        payload = self._get(
            "/api/v5/market/candles",
            {"instId": inst_id, "bar": TIMEFRAME, "limit": str(limit)},
        )
        raw_rows = payload.get("data")
        if not isinstance(raw_rows, list):
            raise RuntimeError(f"کندل‌های OKX برای {inst_id} قابل خواندن نیست.")
        candles: list[Candle] = []
        for row in raw_rows:
            if not isinstance(row, list) or len(row) < 5:
                continue
            candles.append(
                Candle(
                    ts=int(row[0]),
                    open=float(row[1]),
                    high=float(row[2]),
                    low=float(row[3]),
                    close=float(row[4]),
                )
            )
        candles.sort(key=lambda item: item.ts)
        if len(candles) < 60:
            raise RuntimeError(f"تعداد کندل‌های OKX برای {inst_id} کافی نیست.")
        return candles

    def get_last_price(self, inst_id: str) -> float:
        payload = self._get("/api/v5/market/ticker", {"instId": inst_id})
        rows = payload.get("data")
        if not isinstance(rows, list) or not rows:
            raise RuntimeError(f"قیمت لحظه‌ای OKX برای {inst_id} قابل خواندن نیست.")
        last = rows[0].get("last") if isinstance(rows[0], dict) else None
        if last is None:
            raise RuntimeError(f"قیمت لحظه‌ای OKX برای {inst_id} ناقص است.")
        value = float(last)
        if value <= 0:
            raise RuntimeError(f"قیمت لحظه‌ای OKX برای {inst_id} نامعتبر است.")
        return value

    def _get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        response = self.session.get(f"{self.base_url}{path}", params=params, timeout=self.timeout_seconds)
        if response.status_code >= 400:
            raise RuntimeError(f"OKX HTTP {response.status_code}: {response.text[:300]}")
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("پاسخ OKX JSON معتبر نیست.")
        code = str(payload.get("code", "0"))
        if code != "0":
            raise RuntimeError(f"OKX error: {payload}")
        return payload
