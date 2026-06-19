# -*- coding: utf-8 -*-
"""
market_sentiment.py

Safe market sentiment helper for the crypto futures bot.

Purpose:
- Read Fear & Greed and BTC dominance safely.
- Never break analysis/scanner if external APIs fail.
- Cache results to avoid 429/rate-limit errors.
- Provide a small soft sentiment/bias profile for AI/scanner.

This module is intentionally a soft layer. It must not hard-block signals.
"""

import json
import os
import time
from typing import Any, Dict, Optional

import requests

try:
    from config import MARKET_SENTIMENT_CACHE_SECONDS as _CFG_CACHE_SECONDS  # type: ignore
except Exception:
    _CFG_CACHE_SECONDS = int(os.getenv("MARKET_SENTIMENT_CACHE_SECONDS", "1800") or 1800)

try:
    from data_store import load_json as _ds_load_json, save_json as _ds_save_json  # type: ignore
except Exception:
    _ds_load_json = None
    _ds_save_json = None


CACHE_FILE = "market_sentiment_cache.json"
DATA_DIR = os.getenv("BOT_DATA_DIR", "data")

DEFAULT_SENTIMENT = {
    "fear_value": None,
    "fear_text": "نامشخص",
    "btc_dominance": None,
    "dominance_status": "نامشخص",
    "altseason_status": "نامشخص",
    "sentiment_bias": "NEUTRAL",
    "sentiment_score": 0,
    "source": "market_sentiment",
}

_CACHE = {"ts": 0, "data": None}
_LAST_DOMINANCE = {
    "btc_dominance": None,
    "dominance_status": "نامشخص",
    "altseason_status": "نامشخص",
}


def now_ts() -> int:
    return int(time.time())


def safe_int(value: Any, default: int) -> int:
    try:
        return int(float(value))
    except Exception:
        return int(default)


def safe_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if value is None or str(value).strip() == "":
            return default
        return float(value)
    except Exception:
        return default


def cache_seconds() -> int:
    # حداقل ۳۰ دقیقه کش اجباری برای جلوگیری از 429.
    return max(safe_int(_CFG_CACHE_SECONDS, 1800), 1800)


def _cache_path() -> str:
    return CACHE_FILE if os.path.isabs(CACHE_FILE) else os.path.join(DATA_DIR, CACHE_FILE)


def load_disk_cache() -> Optional[Dict[str, Any]]:
    if _ds_load_json:
        data = _ds_load_json(CACHE_FILE, None)
        return data if isinstance(data, dict) else None

    path = _cache_path()
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def save_disk_cache(data: Dict[str, Any]) -> None:
    payload = {"ts": now_ts(), "data": normalize_sentiment(data)}
    try:
        if _ds_save_json:
            _ds_save_json(CACHE_FILE, payload)
            return
        path = _cache_path()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        pass


def normalize_sentiment(data: Any) -> Dict[str, Any]:
    if not isinstance(data, dict):
        return dict(DEFAULT_SENTIMENT)

    result = dict(DEFAULT_SENTIMENT)
    for key in result:
        if key in data:
            result[key] = data[key]

    # Normalize numeric fields.
    result["fear_value"] = safe_int(result.get("fear_value"), None) if result.get("fear_value") is not None else None
    result["btc_dominance"] = safe_float(result.get("btc_dominance"), None)

    bias = _sentiment_bias(result.get("fear_value"), result.get("btc_dominance"))
    result["sentiment_bias"] = bias["sentiment_bias"]
    result["sentiment_score"] = bias["sentiment_score"]
    result["source"] = "market_sentiment"
    return result


def get_cached_sentiment(allow_old: bool = True) -> Dict[str, Any]:
    global _CACHE

    current = now_ts()
    if _CACHE.get("data") is not None:
        if allow_old or current - int(_CACHE.get("ts", 0) or 0) < cache_seconds():
            return normalize_sentiment(_CACHE["data"])

    disk = load_disk_cache()
    if disk and disk.get("data"):
        data = normalize_sentiment(disk.get("data"))
        ts = safe_int(disk.get("ts"), 0)
        _CACHE = {"ts": ts, "data": data}
        if allow_old or current - ts < cache_seconds():
            return data

    return dict(DEFAULT_SENTIMENT)


def update_memory_cache(data: Dict[str, Any]) -> Dict[str, Any]:
    global _CACHE
    data = normalize_sentiment(data)
    _CACHE = {"ts": now_ts(), "data": data}
    save_disk_cache(data)
    return data


def safe_get_json(url: str, timeout: int = 10) -> Optional[Dict[str, Any]]:
    headers = {
        "accept": "application/json",
        "user-agent": "Mozilla/5.0 CryptoAIHelperBot/2.0",
        "cache-control": "no-cache",
    }
    try:
        response = requests.get(url, headers=headers, timeout=timeout)
        if response.status_code in {401, 403, 418, 429, 500, 502, 503, 504}:
            return None
        if response.status_code < 200 or response.status_code >= 300:
            return None
        data = response.json()
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def get_fear_greed() -> Dict[str, Any]:
    data = safe_get_json("https://api.alternative.me/fng/", timeout=10)
    try:
        item = data["data"][0]  # type: ignore[index]
        return {
            "value": int(item.get("value")),
            "text": item.get("value_classification", "نامشخص"),
        }
    except Exception:
        cached = get_cached_sentiment(allow_old=True)
        return {
            "value": cached.get("fear_value"),
            "text": cached.get("fear_text", "نامشخص"),
        }


def _dominance_to_status(dominance: Any) -> Dict[str, Any]:
    value = safe_float(dominance, None)
    if value is None:
        return {
            "btc_dominance": None,
            "dominance_status": "نامشخص",
            "altseason_status": "نامشخص",
        }

    if value >= 55:
        status = "دامیننس بیتکوین بالا است"
        altseason = "ضعیف"
    elif value <= 45:
        status = "دامیننس بیتکوین پایین است"
        altseason = "قوی"
    else:
        status = "دامیننس بیتکوین خنثی است"
        altseason = "متوسط"

    return {
        "btc_dominance": round(value, 2),
        "dominance_status": status,
        "altseason_status": altseason,
    }


def extract_dominance_from_coingecko(data: Any) -> Optional[float]:
    try:
        value = data.get("data", {}).get("market_cap_percentage", {}).get("btc")
        return safe_float(value, None)
    except Exception:
        return None


def get_btc_dominance() -> Dict[str, Any]:
    global _LAST_DOMINANCE

    data = safe_get_json("https://api.coingecko.com/api/v3/global", timeout=10)
    dominance = extract_dominance_from_coingecko(data) if data else None

    if dominance is not None:
        _LAST_DOMINANCE = _dominance_to_status(dominance)
        return _LAST_DOMINANCE

    if _LAST_DOMINANCE.get("btc_dominance") is not None:
        return _LAST_DOMINANCE

    cached = get_cached_sentiment(allow_old=True)
    if cached.get("btc_dominance") is not None:
        _LAST_DOMINANCE = {
            "btc_dominance": cached.get("btc_dominance"),
            "dominance_status": cached.get("dominance_status", "نامشخص"),
            "altseason_status": cached.get("altseason_status", "نامشخص"),
        }
        return _LAST_DOMINANCE

    _LAST_DOMINANCE = {
        "btc_dominance": None,
        "dominance_status": "نامشخص",
        "altseason_status": "نامشخص",
    }
    return _LAST_DOMINANCE


def _sentiment_bias(fear_value: Any, btc_dominance: Any) -> Dict[str, Any]:
    """Small soft bias, used only as context. Positive = more risk-on/alt friendly."""
    score = 0
    fv = safe_int(fear_value, None) if fear_value is not None else None
    dom = safe_float(btc_dominance, None)

    if fv is not None:
        if fv >= 75:
            score += 1       # greed can support risk-on, but not too much
        elif fv >= 55:
            score += 2
        elif fv <= 20:
            score -= 2
        elif fv <= 35:
            score -= 1

    if dom is not None:
        if dom >= 55:
            score -= 2       # altcoins usually weaker when BTC dominance is high
        elif dom <= 45:
            score += 2

    score = max(-4, min(4, int(score)))
    if score >= 2:
        bias = "RISK_ON"
    elif score <= -2:
        bias = "RISK_OFF"
    else:
        bias = "NEUTRAL"
    return {"sentiment_bias": bias, "sentiment_score": score}


def get_market_sentiment() -> Dict[str, Any]:
    current = now_ts()

    if _CACHE.get("data") is not None and current - int(_CACHE.get("ts", 0) or 0) < cache_seconds():
        return normalize_sentiment(_CACHE["data"])

    disk = load_disk_cache()
    if disk and disk.get("data") and current - safe_int(disk.get("ts"), 0) < cache_seconds():
        data = normalize_sentiment(disk["data"])
        _CACHE["ts"] = safe_int(disk.get("ts"), current)
        _CACHE["data"] = data
        return data

    fear = get_fear_greed()
    dominance = get_btc_dominance()

    data = {
        "fear_value": fear.get("value"),
        "fear_text": fear.get("text", "نامشخص"),
        "btc_dominance": dominance.get("btc_dominance"),
        "dominance_status": dominance.get("dominance_status", "نامشخص"),
        "altseason_status": dominance.get("altseason_status", "نامشخص"),
    }

    if data["fear_value"] is None and data["btc_dominance"] is None:
        cached = get_cached_sentiment(allow_old=True)
        if cached != DEFAULT_SENTIMENT:
            return normalize_sentiment(cached)

    return update_memory_cache(data)


def get_market_sentiment_profile() -> Dict[str, Any]:
    """Backward-safe richer profile for analysis/scanner. Soft layer only."""
    data = get_market_sentiment()
    return {
        **data,
        "available": data.get("fear_value") is not None or data.get("btc_dominance") is not None,
        "soft_layer": True,
    }


def format_market_sentiment() -> str:
    data = get_market_sentiment()
    return (
        "🌐 احساسات بازار\n"
        f"Fear & Greed: {data.get('fear_value') if data.get('fear_value') is not None else '-'} | {data.get('fear_text', 'نامشخص')}\n"
        f"BTC Dominance: {data.get('btc_dominance') if data.get('btc_dominance') is not None else '-'} | {data.get('dominance_status', 'نامشخص')}\n"
        f"Altseason: {data.get('altseason_status', 'نامشخص')}\n"
        f"Bias: {data.get('sentiment_bias', 'NEUTRAL')} | Score: {data.get('sentiment_score', 0)}"
    )


# Backward-compatible aliases.
get_sentiment = get_market_sentiment
get_sentiment_profile = get_market_sentiment_profile
