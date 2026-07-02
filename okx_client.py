"""کلاینت عمومی OKX برای تحلیل بازار و تعقیب سیگنال‌های عادی."""
from __future__ import annotations

from typing import Any

import requests

import config
from indicators import add_indicators, candles_to_df
from utils import logger, okx_inst_id, safe_float


class OkxError(RuntimeError):
    pass


class OkxClient:
    def __init__(self, base_url: str = config.OKX_BASE_URL, timeout: int = config.REQUEST_TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()

    def _request(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}{path}"
        try:
            response = self.session.get(url, params=params or {}, timeout=self.timeout)
            if response.status_code >= 400:
                raise OkxError(f"HTTP {response.status_code}: {response.text[:300]}")
            payload = response.json()
        except Exception as exc:
            if isinstance(exc, OkxError):
                raise
            raise OkxError(f"خطا در ارتباط با OKX: {exc}") from exc

        if isinstance(payload, dict) and str(payload.get("code", "0")) not in ("0", "200"):
            raise OkxError(f"پاسخ ناموفق OKX: {payload}")
        return payload

    def get_candles(self, inst_id: str, bar: str, limit: int = 200) -> list[list[Any]]:
        payload = self._request(
            "/api/v5/market/candles",
            {"instId": inst_id, "bar": bar, "limit": max(1, min(int(limit), 300))},
        )
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            return []
        return data


    def validate_symbol(self, base_symbol: str) -> tuple[bool, str]:
        """بررسی زنده اینکه نماد Spot در OKX قابل خواندن است یا نه."""
        inst_id = okx_inst_id(base_symbol)
        try:
            payload = self._request("/api/v5/market/ticker", {"instId": inst_id})
            data = payload.get("data") if isinstance(payload, dict) else None
            if isinstance(data, list) and data:
                got = str(data[0].get("instId") or inst_id).upper()
                if got == inst_id.upper():
                    return True, inst_id
                return True, got
            return False, f"{inst_id} بدون دیتا برگشت"
        except Exception as exc:
            return False, f"{inst_id} خطا: {exc}"

    def get_ticker_price(self, base_symbol: str) -> float:
        inst_id = okx_inst_id(base_symbol)
        payload = self._request("/api/v5/market/ticker", {"instId": inst_id})
        data = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(data, list) and data:
            item = data[0]
            return safe_float(item.get("last") or item.get("askPx") or item.get("bidPx"))
        return 0.0

    def get_market_pack(self, base_symbol: str) -> dict[str, Any]:
        inst_id = okx_inst_id(base_symbol)
        bars = {
            "1D": "1D",
            "4H": "4H",
            "1H": "1H",
            "15M": "15m",
            "5M": "5m",
        }
        out: dict[str, Any] = {"base_symbol": base_symbol.upper(), "okx_symbol": inst_id, "frames": {}}
        for name, bar in bars.items():
            try:
                candles = self.get_candles(inst_id, bar, 200)
                df = add_indicators(candles_to_df(candles))
                out["frames"][name] = df
            except Exception as exc:
                logger.warning("دریافت کندل OKX برای %s %s ناموفق بود: %s", inst_id, name, exc)
                out["frames"][name] = None
        return out
