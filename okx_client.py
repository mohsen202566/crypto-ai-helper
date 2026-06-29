"""کلاینت OKX برای دریافت دیتای تحلیل."""
from __future__ import annotations

from typing import Any

import requests

import config
from utils import logger, okx_symbol_candidates, safe_float


class OKXError(RuntimeError):
    pass


class OKXClient:
    def __init__(self, base_url: str = config.OKX_BASE_URL, timeout: int = config.REQUEST_TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}{path}"
        try:
            response = self.session.get(url, params=params, timeout=self.timeout)
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            raise OKXError(f"خطا در ارتباط با OKX: {exc}") from exc

        if isinstance(payload, dict) and payload.get("code") not in (None, "0", 0):
            raise OKXError(f"پاسخ ناموفق OKX: {payload.get('msg') or payload}")
        return payload

    def get_instruments(self, inst_type: str = "SWAP") -> set[str]:
        payload = self._get("/api/v5/public/instruments", {"instType": inst_type})
        data = payload.get("data", []) if isinstance(payload, dict) else []
        return {item.get("instId") for item in data if isinstance(item, dict) and item.get("instId")}

    def validate_symbol(self, internal_symbol: str, instruments: set[str] | None = None) -> str:
        if instruments is None:
            instruments = self.get_instruments("SWAP")
        for candidate in okx_symbol_candidates(internal_symbol):
            if candidate in instruments:
                return candidate
        raise OKXError(f"نماد {internal_symbol} در OKX پیدا نشد")

    def get_candles(self, okx_symbol: str, bar: str = config.TIMEFRAME, limit: int = config.CANDLE_LIMIT) -> list[dict[str, Any]]:
        payload = self._get(
            "/api/v5/market/candles",
            {"instId": okx_symbol, "bar": bar, "limit": str(limit)},
        )
        rows = payload.get("data", []) if isinstance(payload, dict) else []
        candles: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, list) or len(row) < 6:
                continue
            candles.append(
                {
                    "open_time": int(row[0]),
                    "open": safe_float(row[1]),
                    "high": safe_float(row[2]),
                    "low": safe_float(row[3]),
                    "close": safe_float(row[4]),
                    "volume": safe_float(row[5]),
                    "quote_volume": safe_float(row[7] if len(row) > 7 else 0),
                    "confirm": str(row[8]) if len(row) > 8 else "0",
                }
            )
        candles.sort(key=lambda x: x["open_time"])
        if len(candles) < 30:
            raise OKXError(f"کندل کافی برای {okx_symbol} دریافت نشد")
        return candles

    def get_last_price(self, okx_symbol: str) -> float:
        payload = self._get("/api/v5/market/ticker", {"instId": okx_symbol})
        data = payload.get("data", []) if isinstance(payload, dict) else []
        if not data:
            raise OKXError(f"قیمت لحظه‌ای OKX برای {okx_symbol} دریافت نشد")
        return safe_float(data[0].get("last"))
