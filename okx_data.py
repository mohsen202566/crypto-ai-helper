from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests

from config import MIN_ENTRY_CANDLES, MIN_HTF_CANDLES, OKX_BASE_URL, OKX_CANDLE_LIMIT, OKX_TIMEOUT_SECONDS, TIMEFRAMES


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
    def __init__(self, base_url: str = OKX_BASE_URL, timeout_seconds: int = OKX_TIMEOUT_SECONDS) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.session = requests.Session()

    def get_candles(self, inst_id: str, timeframe: str, limit: int = OKX_CANDLE_LIMIT) -> list[Candle]:
        payload = self._get("/api/v5/market/candles", {"instId": inst_id, "bar": timeframe, "limit": str(limit)})
        candles = self._parse_candles(payload, inst_id, timeframe)
        minimum = MIN_ENTRY_CANDLES if timeframe == "5m" else MIN_HTF_CANDLES
        if len(candles) < minimum:
            raise RuntimeError(f"کندل کافی برای {inst_id} تایم {timeframe} نیست: {len(candles)} / حداقل {minimum}")
        return candles

    def get_historical_candles(self, inst_id: str, timeframe: str, limit: int = 2000) -> list[Candle]:
        candles: list[Candle] = []
        before: str | None = None
        while len(candles) < limit:
            batch_limit = min(100, limit - len(candles))
            params = {"instId": inst_id, "bar": timeframe, "limit": str(batch_limit)}
            if before:
                params["before"] = before
            payload = self._get("/api/v5/market/history-candles", params)
            batch = self._parse_candles(payload, inst_id, timeframe, keep_unconfirmed=False)
            if not batch:
                break
            candles.extend(batch)
            before = str(min(c.ts for c in batch))
            if len(batch) < batch_limit:
                break
        unique = {c.ts: c for c in candles if c.confirmed}
        return [unique[k] for k in sorted(unique)]

    def get_multi_timeframe(self, inst_id: str, timeframes: tuple[str, ...] = TIMEFRAMES) -> dict[str, list[Candle]]:
        return {tf: self.get_candles(inst_id, tf) for tf in timeframes}

    def get_last_price(self, inst_id: str) -> float:
        payload = self._get("/api/v5/market/ticker", {"instId": inst_id})
        rows = payload.get("data")
        if not isinstance(rows, list) or not rows:
            raise RuntimeError(f"قیمت لحظه‌ای OKX برای {inst_id} قابل خواندن نیست.")
        value = float(rows[0].get("last"))
        if value <= 0:
            raise RuntimeError(f"قیمت لحظه‌ای OKX برای {inst_id} نامعتبر است.")
        return value

    def _parse_candles(self, payload: dict[str, Any], inst_id: str, timeframe: str, keep_unconfirmed: bool = False) -> list[Candle]:
        raw_rows = payload.get("data")
        if not isinstance(raw_rows, list):
            raise RuntimeError(f"کندل‌های OKX برای {inst_id} تایم {timeframe} قابل خواندن نیست.")
        candles: list[Candle] = []
        for row in raw_rows:
            if not isinstance(row, list) or len(row) < 6:
                continue
            confirmed = str(row[8]) == "1" if len(row) >= 9 else True
            if not keep_unconfirmed and not confirmed:
                continue
            candles.append(Candle(ts=int(row[0]), open=float(row[1]), high=float(row[2]), low=float(row[3]), close=float(row[4]), volume=float(row[5] or 0), confirmed=confirmed))
        candles.sort(key=lambda item: item.ts)
        return candles

    def _get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        response = self.session.get(f"{self.base_url}{path}", params=params, timeout=self.timeout_seconds)
        if response.status_code >= 400:
            raise RuntimeError(f"OKX HTTP {response.status_code}: {response.text[:300]}")
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("پاسخ OKX JSON معتبر نیست.")
        code = str(payload.get("code", "0"))
        if code != "0":
            msg = str(payload.get("msg") or payload)
            inst_id = params.get("instId", "")
            if code == "51001" or "Instrument ID" in msg:
                raise RuntimeError(f"نماد OKX نامعتبر یا غیرفعال است: {inst_id}")
            raise RuntimeError(f"OKX error {code}: {msg}")
        return payload
