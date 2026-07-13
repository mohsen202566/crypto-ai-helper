"""کلاینت عمومی OKX برای ابزارها، تیکر، معاملات و تاریخچه کندل."""
from __future__ import annotations

import time
from typing import Any

import requests

import config


class OKXError(RuntimeError):
    pass


class OKXClient:
    def __init__(self) -> None:
        self.base_url = config.OKX_BASE_URL
        self.timeout = config.OKX_REQUEST_TIMEOUT
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "adaptive-start-bot/1.0"})

    def _get(self, path: str, params: dict[str, Any] | None = None, retries: int = 2) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            try:
                response = self.session.get(
                    f"{self.base_url}{path}", params=params or {}, timeout=self.timeout
                )
                if response.status_code >= 400:
                    raise OKXError(f"HTTP {response.status_code}: {response.text[:240]}")
                payload = response.json()
                if not isinstance(payload, dict):
                    raise OKXError("پاسخ OKX دیکشنری نیست")
                if str(payload.get("code", "0")) not in ("0", "200", ""):
                    raise OKXError(str(payload.get("msg") or payload))
                return payload
            except Exception as exc:
                last_error = exc
                if attempt < retries:
                    time.sleep(0.35 * (attempt + 1))
        if isinstance(last_error, OKXError):
            raise last_error
        raise OKXError(f"OKX connection error: {last_error}")

    def list_usdt_swaps(self) -> dict[str, str]:
        payload = self._get("/api/v5/public/instruments", {"instType": "SWAP"})
        out: dict[str, str] = {}
        for row in payload.get("data") or []:
            if not isinstance(row, dict):
                continue
            inst_id = str(row.get("instId") or "").upper()
            base = str(row.get("baseCcy") or "").upper()
            settle = str(row.get("settleCcy") or "").upper()
            state = str(row.get("state") or "live").lower()
            if not base and inst_id.endswith("-USDT-SWAP"):
                base = inst_id.split("-", 1)[0]
            if inst_id and base and settle == "USDT" and state in ("live", "trading"):
                out[base] = inst_id
        return out

    def get_all_swap_tickers(self) -> dict[str, dict[str, Any]]:
        payload = self._get("/api/v5/market/tickers", {"instType": "SWAP"})
        out: dict[str, dict[str, Any]] = {}
        for row in payload.get("data") or []:
            if isinstance(row, dict):
                inst_id = str(row.get("instId") or "").upper()
                if inst_id:
                    out[inst_id] = row
        return out

    def get_last_price(self, inst_id: str) -> float:
        payload = self._get("/api/v5/market/ticker", {"instId": inst_id})
        rows = payload.get("data") or []
        if not rows:
            raise OKXError(f"ticker empty: {inst_id}")
        price = float(rows[0].get("last") or 0.0)
        if price <= 0:
            raise OKXError(f"ticker invalid: {inst_id}")
        return price

    @staticmethod
    def _parse_candle(row: list[Any]) -> dict[str, float | int] | None:
        try:
            return {
                "ts": int(row[0]),
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]),
                "vol_ccy": float(row[6]) if len(row) > 6 else 0.0,
                "vol_quote": float(row[7]) if len(row) > 7 else 0.0,
                "confirm": int(row[8]) if len(row) > 8 else 1,
            }
        except (TypeError, ValueError, IndexError):
            return None

    def get_history_candles(
        self,
        inst_id: str,
        days: int = config.PROFILE_DAYS,
        bar: str = config.OKX_HISTORY_BAR,
    ) -> list[dict[str, float | int]]:
        cutoff = int((time.time() - days * 86400) * 1000)
        cursor: int | None = None
        by_ts: dict[int, dict[str, float | int]] = {}
        max_pages = max(5, config.PROFILE_MAX_CANDLES // config.PROFILE_PAGE_LIMIT + 8)

        for _ in range(max_pages):
            params: dict[str, str] = {
                "instId": inst_id,
                "bar": bar,
                "limit": str(config.PROFILE_PAGE_LIMIT),
            }
            if cursor is not None:
                params["after"] = str(cursor)
            payload = self._get("/api/v5/market/history-candles", params)
            rows = payload.get("data") or []
            if not rows:
                break
            parsed_count = 0
            oldest: int | None = None
            for raw in rows:
                if not isinstance(raw, list):
                    continue
                candle = self._parse_candle(raw)
                if candle is None:
                    continue
                ts = int(candle["ts"])
                oldest = ts if oldest is None else min(oldest, ts)
                if ts >= cutoff:
                    by_ts[ts] = candle
                parsed_count += 1
            if parsed_count == 0 or oldest is None or oldest <= cutoff:
                break
            if cursor == oldest:
                break
            cursor = oldest
            time.sleep(config.PROFILE_REQUEST_PAUSE)

        candles = [by_ts[k] for k in sorted(by_ts)]
        if len(candles) > config.PROFILE_MAX_CANDLES:
            candles = candles[-config.PROFILE_MAX_CANDLES :]
        if len(candles) < config.PROFILE_MIN_CANDLES:
            raise OKXError(
                f"history too short {inst_id}: {len(candles)} < {config.PROFILE_MIN_CANDLES}"
            )
        return candles

    def get_recent_trades(self, inst_id: str, limit: int = config.RECENT_TRADES_LIMIT) -> list[dict[str, Any]]:
        payload = self._get(
            "/api/v5/market/trades",
            {"instId": inst_id, "limit": str(max(20, min(int(limit), 500)))},
        )
        out: list[dict[str, Any]] = []
        for row in payload.get("data") or []:
            if not isinstance(row, dict):
                continue
            try:
                out.append(
                    {
                        "ts": int(row.get("ts") or 0),
                        "price": float(row.get("px") or 0.0),
                        "size": float(row.get("sz") or 0.0),
                        "side": str(row.get("side") or "").lower(),
                    }
                )
            except (TypeError, ValueError):
                continue
        out.sort(key=lambda item: int(item["ts"]))
        return out

    def recent_quote_volume(self, inst_id: str, window_seconds: int) -> float:
        cutoff = int((time.time() - max(1, window_seconds)) * 1000)
        total = 0.0
        for trade in self.get_recent_trades(inst_id):
            if int(trade["ts"]) >= cutoff:
                total += float(trade["price"]) * float(trade["size"])
        return total
