from __future__ import annotations
from dataclasses import dataclass, asdict
from typing import Dict, Any, Optional
import time
import requests
from config import SETTINGS

try:
    from market_data import get_market_mode
except Exception:  # keep startup/import safe if public data layer is unavailable
    get_market_mode = None


@dataclass(frozen=True)
class MarketContext:
    fear_greed: float
    altseason_score: float
    btc_dominance: float
    market_breadth: float
    btc_trend: str
    market_state: str
    timestamp: int
    source: str = "OKX_LIGHT"
    strength: float = 0.0
    bullish_count: int = 0
    bearish_count: int = 0
    neutral_count: int = 0
    choppy_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class MarketContextProvider:
    """Lightweight Level-1 market context.

    Locked Level-1 rule: market_state must be a fast OKX mode
    (BULLISH / BEARISH / NEUTRAL / CHOPPY), not a Fear & Greed phase.
    Fear & Greed stays as a small informational sensor only.
    """

    def __init__(self):
        self.cache: Optional[MarketContext] = None
        self.cache_time = 0

    def _get_fear_greed(self) -> float:
        try:
            r = requests.get("https://api.alternative.me/fng/", timeout=5)
            return float(r.json()["data"][0]["value"])
        except Exception:
            return 50.0

    def _fallback_market_mode(self) -> Dict[str, Any]:
        return {
            "mode": "NEUTRAL",
            "strength": 0.0,
            "bullish_count": 0,
            "bearish_count": 0,
            "neutral_count": 0,
            "choppy_count": 0,
            "source": "FALLBACK",
        }

    def _get_light_market_mode(self) -> Dict[str, Any]:
        if get_market_mode is None:
            return self._fallback_market_mode()
        try:
            mode = get_market_mode()
            if hasattr(mode, "to_dict"):
                mode = mode.to_dict()
            if not isinstance(mode, dict):
                return self._fallback_market_mode()
            value = str(mode.get("mode", "NEUTRAL")).upper().strip()
            if value not in {"BULLISH", "BEARISH", "NEUTRAL", "CHOPPY"}:
                value = "NEUTRAL"
            mode["mode"] = value
            return mode
        except Exception:
            return self._fallback_market_mode()

    def build(self) -> MarketContext:
        now = int(time.time())
        ttl = SETTINGS.market_context.cache_ttl_seconds

        if self.cache and now - self.cache_time < ttl:
            return self.cache

        fg = self._get_fear_greed()
        mode = self._get_light_market_mode()
        market_state = str(mode.get("mode", "NEUTRAL")).upper()

        ctx = MarketContext(
            fear_greed=fg,
            altseason_score=50.0,
            btc_dominance=50.0,
            market_breadth=float(mode.get("strength", 0.0) or 0.0),
            btc_trend=market_state if market_state in {"BULLISH", "BEARISH"} else "NEUTRAL",
            market_state=market_state,
            timestamp=now,
            source=str(mode.get("source", "OKX_LIGHT")),
            strength=float(mode.get("strength", 0.0) or 0.0),
            bullish_count=int(mode.get("bullish_count", 0) or 0),
            bearish_count=int(mode.get("bearish_count", 0) or 0),
            neutral_count=int(mode.get("neutral_count", 0) or 0),
            choppy_count=int(mode.get("choppy_count", 0) or 0),
        )

        self.cache = ctx
        self.cache_time = now
        return ctx


_provider: Optional[MarketContextProvider] = None


def provider() -> MarketContextProvider:
    global _provider
    if _provider is None:
        _provider = MarketContextProvider()
    return _provider


def get_market_context() -> MarketContext:
    return provider().build()
