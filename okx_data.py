from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests

from config import OKX_BASE_URL, OKX_CANDLE_LIMIT


@dataclass(frozen=True)
class Candle:
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    confirmed: bool = True


class OkxDataClient:
    def __init__(self, base_url: str = OKX_BASE_URL, timeout_seconds: int = 12) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.session = requests.Session()

    def get_candles(self, inst_id: str, timeframe: str, limit: int = OKX_CANDLE_LIMIT) -> list[Candle]:
        payload = self._get("/api/v5/market/candles", {"instId": inst_id, "bar": timeframe, "limit": str(limit)})
        raw_rows = payload.get("data")
        if not isinstance(raw_rows, list):
            raise RuntimeError(f"کندل‌های OKX برای {inst_id} تایم {timeframe} قابل خواندن نیست.")
        candles: list[Candle] = []
        for row in raw_rows:
            if not isinstance(row, list) or len(row) < 5:
                continue
            confirmed = str(row[8]) == "1" if len(row) >= 9 else True
            volume = 0.0
            if len(row) >= 6:
                try:
                    volume = float(row[5])
                except (TypeError, ValueError):
                    volume = 0.0
            candles.append(Candle(ts=int(row[0]), open=float(row[1]), high=float(row[2]), low=float(row[3]), close=float(row[4]), volume=volume, confirmed=confirmed))
        candles.sort(key=lambda item: item.ts)
        confirmed_only = [c for c in candles if c.confirmed]
        if len(confirmed_only) >= min(80, max(20, limit // 3)):
            candles = confirmed_only
        if len(candles) < 80:
            raise RuntimeError(f"تعداد کندل‌های OKX برای {inst_id} تایم {timeframe} کافی نیست: {len(candles)}")
        return candles

    def get_multi_timeframe(self, inst_id: str, timeframes: tuple[str, ...]) -> dict[str, list[Candle]]:
        return {tf: self.get_candles(inst_id, tf) for tf in timeframes}

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
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError("پاسخ OKX JSON معتبر نیست.") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("پاسخ OKX JSON معتبر نیست.")
        code = str(payload.get("code", "0"))
        if code != "0":
            raise RuntimeError(f"OKX error: {payload}")
        return payload
