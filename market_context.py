
from __future__ import annotations
from dataclasses import dataclass, asdict
from typing import Dict, Any, Optional
import time
import requests
from config import SETTINGS

@dataclass(frozen=True)
class MarketContext:
    fear_greed: float
    altseason_score: float
    btc_dominance: float
    market_breadth: float
    btc_trend: str
    market_state: str
    timestamp: int

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

class MarketContextProvider:
    def __init__(self):
        self.cache = None
        self.cache_time = 0

    def _get_fear_greed(self) -> float:
        try:
            r = requests.get("https://api.alternative.me/fng/", timeout=8)
            return float(r.json()["data"][0]["value"])
        except Exception:
            return 50.0

    def _market_state(self, fg: float) -> str:
        if fg > 75:
            return "EXHAUSTION"
        if fg > 60:
            return "MIDDLE"
        if fg < 25:
            return "REVERSAL"
        return "START"

    def build(self) -> MarketContext:
        now = int(time.time())
        ttl = SETTINGS.market_context.cache_ttl_seconds

        if self.cache and now - self.cache_time < ttl:
            return self.cache

        fg = self._get_fear_greed()

        ctx = MarketContext(
            fear_greed=fg,
            altseason_score=50.0,
            btc_dominance=50.0,
            market_breadth=50.0,
            btc_trend="NEUTRAL",
            market_state=self._market_state(fg),
            timestamp=now,
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
