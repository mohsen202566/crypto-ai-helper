"""کلاینت OKX برای دیتای عمومی و مانیتورینگ سیگنال‌های عادی.
تمام تحلیل‌ها فقط از OKX تغذیه می‌شوند.
"""
from __future__ import annotations

import time
from typing import Any

import requests

import config

class OKXError(RuntimeError):
    pass

class OKXClient:
    def __init__(self, base_url: str = config.OKX_BASE_URL, timeout: int = config.OKX_REQUEST_TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}{path}"
        try:
            r = self.session.get(url, params=params or {}, timeout=self.timeout)
            if r.status_code >= 400:
                raise OKXError(f"HTTP {r.status_code}: {r.text[:300]}")
            payload = r.json()
        except Exception as exc:
            if isinstance(exc, OKXError):
                raise
            raise OKXError(f"OKX connection error: {exc}") from exc
        if isinstance(payload, dict) and str(payload.get("code", "0")) not in ("0", "200", ""):
            raise OKXError(f"OKX error: {payload.get('msg') or payload}")
        return payload

    def get_ticker(self, inst_id: str) -> dict[str, Any]:
        payload = self._get("/api/v5/market/ticker", {"instId": inst_id})
        data = payload.get("data") if isinstance(payload, dict) else None
        if not data:
            raise OKXError(f"ticker empty: {inst_id}")
        return data[0]

    def get_last_price(self, inst_id: str) -> float:
        item = self.get_ticker(inst_id)
        return float(item.get("last") or item.get("lastPx") or 0.0)

    def get_candles(self, inst_id: str, bar: str = config.OKX_BAR, limit: int = config.OKX_CANDLE_LIMIT) -> list[dict[str, float]]:
        payload = self._get("/api/v5/market/candles", {"instId": inst_id, "bar": bar, "limit": str(limit)})
        rows = payload.get("data") if isinstance(payload, dict) else []
        out: list[dict[str, float]] = []
        # OKX: [ts,o,h,l,c,vol,volCcy,volCcyQuote,confirm]
        for r in reversed(rows or []):
            try:
                out.append({
                    "ts": int(r[0]),
                    "open": float(r[1]),
                    "high": float(r[2]),
                    "low": float(r[3]),
                    "close": float(r[4]),
                    "volume": float(r[5]),
                    "vol_ccy": float(r[6]) if len(r) > 6 else 0.0,
                    "vol_quote": float(r[7]) if len(r) > 7 else 0.0,
                    "confirm": int(r[8]) if len(r) > 8 else 1,
                })
            except Exception:
                continue
        if len(out) < 20:
            raise OKXError(f"candles too few: {inst_id}")
        return out

    def get_recent_trades(self, inst_id: str, limit: int = config.OKX_MICRO_TRADES_LIMIT) -> list[dict[str, float | str]]:
        payload = self._get("/api/v5/market/trades", {"instId": inst_id, "limit": str(max(20, min(int(limit), 500)))})
        rows = payload.get("data") if isinstance(payload, dict) else []
        out: list[dict[str, float | str]] = []
        for item in reversed(rows or []):
            try:
                out.append({
                    "ts": int(item.get("ts") or 0),
                    "price": float(item.get("px") or 0.0),
                    "size": float(item.get("sz") or 0.0),
                    "side": str(item.get("side") or "").lower(),
                })
            except Exception:
                continue
        return out

    def get_order_book(self, inst_id: str, depth: int = config.OKX_BOOK_DEPTH) -> dict[str, list[list[float]]]:
        payload = self._get("/api/v5/market/books", {"instId": inst_id, "sz": str(max(1, min(int(depth), 400)))})
        data = payload.get("data") if isinstance(payload, dict) else []
        if not data:
            raise OKXError(f"order book empty: {inst_id}")
        item = data[0]
        def parse(rows):
            out=[]
            for r in rows or []:
                try:
                    out.append([float(r[0]), float(r[1])])
                except Exception:
                    continue
            return out
        return {"bids": parse(item.get("bids")), "asks": parse(item.get("asks"))}

    def get_micro_snapshot(self, inst_id: str) -> dict[str, float]:
        """دو درخواست سبک فقط برای ارزهای داخل واچ‌لیست."""
        trades = self.get_recent_trades(inst_id)
        book = self.get_order_book(inst_id)
        buy = sum(float(t["size"]) for t in trades if t.get("side") == "buy")
        sell = sum(float(t["size"]) for t in trades if t.get("side") == "sell")
        total = buy + sell or 1e-12
        trade_imbalance = (buy - sell) / total

        half = max(1, len(trades) // 2)
        older = sum(float(t["size"]) for t in trades[:half]) or 1e-12
        newer = sum(float(t["size"]) for t in trades[half:])
        intensity_acceleration = max(-1.0, min(3.0, newer / older - 1.0))

        bid_qty = sum(x[1] for x in book["bids"])
        ask_qty = sum(x[1] for x in book["asks"])
        book_total = bid_qty + ask_qty or 1e-12
        book_imbalance = (bid_qty - ask_qty) / book_total
        best_bid = book["bids"][0][0] if book["bids"] else 0.0
        best_ask = book["asks"][0][0] if book["asks"] else 0.0
        mid = (best_bid + best_ask) / 2.0 if best_bid > 0 and best_ask > 0 else (float(trades[-1]["price"]) if trades else 0.0)
        return {
            "trade_imbalance": trade_imbalance,
            "book_imbalance": book_imbalance,
            "intensity_acceleration": intensity_acceleration,
            "mid_price": mid,
            "last_price": float(trades[-1]["price"]) if trades else mid,
            "trade_count": float(len(trades)),
        }

    @staticmethod
    def reached_tp_or_sl(candles: list[dict[str, float]], side: str, tp: float, sl: float, after_ts_ms: int) -> tuple[str | None, float | None, int | None]:
        side = side.upper()
        for c in candles:
            if int(c["ts"]) <= int(after_ts_ms):
                continue
            h, l = float(c["high"]), float(c["low"])
            if side == "LONG":
                if l <= sl:
                    return "SL", sl, int(c["ts"])
                if h >= tp:
                    return "TP", tp, int(c["ts"])
            else:
                if h >= sl:
                    return "SL", sl, int(c["ts"])
                if l <= tp:
                    return "TP", tp, int(c["ts"])
        return None, None, None

    @staticmethod
    def max_favorable_adverse(candles: list[dict[str, float]], side: str, entry: float, after_ts_ms: int) -> tuple[float, float]:
        mfe = 0.0
        mae = 0.0
        side = side.upper()
        for c in candles:
            if int(c["ts"]) <= int(after_ts_ms):
                continue
            if side == "LONG":
                mfe = max(mfe, (float(c["high"]) - entry) / entry * 100.0)
                mae = min(mae, (float(c["low"]) - entry) / entry * 100.0)
            else:
                mfe = max(mfe, (entry - float(c["low"])) / entry * 100.0)
                mae = min(mae, (entry - float(c["high"])) / entry * 100.0)
        return mfe, mae
