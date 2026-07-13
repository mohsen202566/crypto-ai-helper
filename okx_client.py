"""کلاینت داده عمومی OKX."""
from __future__ import annotations
from typing import Any
import requests
import config
from models import MicroSnapshot

class OKXError(RuntimeError):
    pass

class OKXClient:
    def __init__(self, base_url: str = config.OKX_BASE_URL, timeout: int = config.OKX_REQUEST_TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        try:
            response = self.session.get(f"{self.base_url}{path}", params=params or {}, timeout=self.timeout)
            if response.status_code >= 400:
                raise OKXError(f"HTTP {response.status_code}: {response.text[:300]}")
            payload = response.json()
        except OKXError:
            raise
        except Exception as exc:
            raise OKXError(f"OKX connection error: {exc}") from exc
        if isinstance(payload, dict) and str(payload.get("code", "0")) not in ("0", "200", ""):
            raise OKXError(f"OKX error: {payload.get('msg') or payload}")
        return payload

    def get_ticker(self, inst_id: str) -> dict[str, Any]:
        payload = self._get("/api/v5/market/ticker", {"instId": inst_id})
        rows = payload.get("data") if isinstance(payload, dict) else []
        if not rows:
            raise OKXError(f"ticker empty: {inst_id}")
        return rows[0]

    def get_last_price(self, inst_id: str) -> float:
        row = self.get_ticker(inst_id)
        value = float(row.get("last") or row.get("lastPx") or 0.0)
        if value <= 0:
            raise OKXError(f"last price invalid: {inst_id}")
        return value

    def get_candles(self, inst_id: str, bar: str = config.OKX_PRIMARY_BAR, limit: int = config.OKX_CANDLE_LIMIT) -> list[dict[str, float]]:
        payload = self._get("/api/v5/market/candles", {"instId": inst_id, "bar": bar, "limit": str(limit)})
        rows = payload.get("data") if isinstance(payload, dict) else []
        out: list[dict[str, float]] = []
        for row in reversed(rows or []):
            try:
                out.append({
                    "ts": int(row[0]), "open": float(row[1]), "high": float(row[2]),
                    "low": float(row[3]), "close": float(row[4]), "volume": float(row[5]),
                    "vol_ccy": float(row[6]) if len(row) > 6 else 0.0,
                    "vol_quote": float(row[7]) if len(row) > 7 else 0.0,
                    "confirm": int(row[8]) if len(row) > 8 else 1,
                })
            except (TypeError, ValueError, IndexError):
                continue
        if len(out) < 40:
            raise OKXError(f"candles too few: {inst_id}")
        return out

    def get_recent_trades(self, inst_id: str, limit: int = config.OKX_MICRO_TRADES_LIMIT) -> list[dict[str, float | str]]:
        payload = self._get("/api/v5/market/trades", {"instId": inst_id, "limit": str(max(20, min(int(limit), 500)))})
        rows = payload.get("data") if isinstance(payload, dict) else []
        out: list[dict[str, float | str]] = []
        for item in reversed(rows or []):
            try:
                out.append({"ts": int(item.get("ts") or 0), "price": float(item.get("px") or 0.0), "size": float(item.get("sz") or 0.0), "side": str(item.get("side") or "").lower()})
            except (TypeError, ValueError):
                continue
        return out

    def get_order_book(self, inst_id: str, depth: int = config.OKX_BOOK_DEPTH) -> dict[str, list[list[float]]]:
        payload = self._get("/api/v5/market/books", {"instId": inst_id, "sz": str(max(1, min(int(depth), 400)))})
        rows = payload.get("data") if isinstance(payload, dict) else []
        if not rows:
            raise OKXError(f"order book empty: {inst_id}")
        item = rows[0]
        def parse(levels: Any) -> list[list[float]]:
            out: list[list[float]] = []
            for row in levels or []:
                try:
                    out.append([float(row[0]), float(row[1])])
                except (TypeError, ValueError, IndexError):
                    continue
            return out
        return {"bids": parse(item.get("bids")), "asks": parse(item.get("asks"))}

    def get_micro_snapshot(self, inst_id: str) -> MicroSnapshot:
        ticker = self.get_ticker(inst_id)
        trades = self.get_recent_trades(inst_id)
        book = self.get_order_book(inst_id)
        bids, asks = book["bids"], book["asks"]
        if not bids or not asks:
            raise OKXError(f"book sides empty: {inst_id}")
        bid, ask = bids[0][0], asks[0][0]
        last = float(ticker.get("last") or ticker.get("lastPx") or (bid + ask) / 2.0)
        mid = (bid + ask) / 2.0
        spread_pct = ((ask - bid) / mid * 100.0) if mid > 0 else 999.0
        buy = sum(float(t["size"]) for t in trades if t["side"] == "buy")
        sell = sum(float(t["size"]) for t in trades if t["side"] == "sell")
        trade_imbalance = (buy - sell) / (buy + sell) if buy + sell > 0 else 0.0
        bid_qty = sum(level[1] for level in bids)
        ask_qty = sum(level[1] for level in asks)
        book_imbalance = (bid_qty - ask_qty) / (bid_qty + ask_qty) if bid_qty + ask_qty > 0 else 0.0
        best_bid_qty, best_ask_qty = bids[0][1], asks[0][1]
        microprice = (ask * best_bid_qty + bid * best_ask_qty) / (best_bid_qty + best_ask_qty) if best_bid_qty + best_ask_qty > 0 else mid
        micro_bias = ((microprice - mid) / mid * 100.0) if mid > 0 else 0.0
        return MicroSnapshot(last, bid, ask, spread_pct, trade_imbalance, book_imbalance, microprice, micro_bias, len(trades), {"ticker": ticker})
