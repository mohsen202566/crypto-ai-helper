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


class OKXDataError(RuntimeError):
    def __init__(self, reason: str, *, inst_id: str = "", bar: str = "", raw: object | None = None) -> None:
        self.reason = reason
        self.inst_id = inst_id
        self.bar = bar
        self.raw = raw
        suffix = f" | {inst_id}" if inst_id else ""
        if bar:
            suffix += f" | {bar}"
        super().__init__(reason + suffix)


class OKXClient:
    def __init__(self, base_url: str = "https://www.okx.com", timeout: int = 12) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()

    def get_candles(self, inst_id: str, bar: OKX_BAR, limit: int = 220) -> list[Candle]:
        url = f"{self.base_url}/api/v5/market/candles"
        params = {"instId": inst_id, "bar": bar, "limit": str(max(1, min(int(limit), 300)))}
        payload = self._get(url, params=params, inst_id=inst_id, bar=bar)
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
            raise OKXDataError("داده کندل کافی نیست", inst_id=inst_id, bar=bar)
        return candles

    def get_last_price(self, inst_id: str) -> float:
        url = f"{self.base_url}/api/v5/market/ticker"
        payload = self._get(url, params={"instId": inst_id}, inst_id=inst_id)
        rows = payload.get("data") or []
        if not rows:
            raise OKXDataError("قیمت از اوکی‌اکس خوانده نشد", inst_id=inst_id)
        try:
            price = float(rows[0].get("last") or rows[0].get("markPx") or 0)
        except Exception as exc:
            raise OKXDataError("قیمت اوکی‌اکس معتبر نیست", inst_id=inst_id, raw=exc) from exc
        if price <= 0:
            raise OKXDataError("قیمت اوکی‌اکس معتبر نیست", inst_id=inst_id)
        return price

    def _get(self, url: str, params: dict[str, str], inst_id: str = "", bar: str = "") -> dict:
        last_error: Exception | None = None
        for _ in range(3):
            try:
                response = self.session.get(url, params=params, timeout=self.timeout)
                response.raise_for_status()
                payload = response.json()
                code = str(payload.get("code", "0"))
                if code != "0":
                    reason = self._payload_reason(payload)
                    # خطاهای قطعی مثل نماد نامعتبر را تکرار نکن؛ همان نماد باید رد شود.
                    if code in {"51000", "51001", "51008", "51011"}:
                        raise OKXDataError(reason, inst_id=inst_id, bar=bar, raw=payload)
                    last_error = OKXDataError(reason, inst_id=inst_id, bar=bar, raw=payload)
                    time.sleep(0.5)
                    continue
                return payload
            except OKXDataError:
                raise
            except Exception as exc:
                last_error = exc
                time.sleep(0.5)
        raise OKXDataError("خطا در دریافت داده از اوکی‌اکس", inst_id=inst_id, bar=bar, raw=last_error)

    @staticmethod
    def _payload_reason(payload: dict) -> str:
        code = str(payload.get("code", ""))
        msg = str(payload.get("msg", ""))
        if code == "51001" or "Instrument ID" in msg:
            return "نماد اوکی‌اکس نامعتبر است"
        if code:
            return f"خطای اوکی‌اکس: {code}"
        return "خطای اوکی‌اکس"
