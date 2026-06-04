# -*- coding: utf-8 -*-
import time
import requests
from config import MARKET_SENTIMENT_CACHE_SECONDS

_CACHE = {"ts": 0, "data": None}
_LAST_DOMINANCE = {
    "btc_dominance": None,
    "dominance_status": "نامشخص",
    "altseason_status": "نامشخص",
}


def safe_get_json(url, timeout=12):
    headers = {
        "accept": "application/json",
        "user-agent": "CryptoAIHelperBot/2.0"
    }
    r = requests.get(url, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.json()


def get_fear_greed():
    try:
        data = safe_get_json("https://api.alternative.me/fng/")
        item = data["data"][0]
        return {"value": int(item["value"]), "text": item["value_classification"]}
    except Exception:
        return {"value": None, "text": "نامشخص"}


def _dominance_to_status(dominance):
    if dominance is None:
        return {
            "btc_dominance": None,
            "dominance_status": "نامشخص",
            "altseason_status": "نامشخص",
        }
    dominance = float(dominance)
    if dominance >= 55:
        status = "دامیننس بیتکوین بالا است"
        altseason = "ضعیف"
    elif dominance <= 45:
        status = "دامیننس بیتکوین پایین است"
        altseason = "قوی"
    else:
        status = "دامیننس بیتکوین خنثی است"
        altseason = "متوسط"
    return {
        "btc_dominance": round(dominance, 2),
        "dominance_status": status,
        "altseason_status": altseason,
    }


def get_btc_dominance():
    global _LAST_DOMINANCE
    try:
        data = safe_get_json("https://api.coingecko.com/api/v3/global")
        dominance = data.get("data", {}).get("market_cap_percentage", {}).get("btc")
        if dominance is None:
            raise Exception("BTC dominance not found")
        _LAST_DOMINANCE = _dominance_to_status(dominance)
        return _LAST_DOMINANCE
    except Exception:
        # اگر CoinGecko محدود کرد، مقدار قبلی را برمی‌گردانیم تا لاگ و تحلیل خراب نشود.
        return _LAST_DOMINANCE


def get_market_sentiment():
    now = int(time.time())
    if _CACHE["data"] is not None and now - _CACHE["ts"] < MARKET_SENTIMENT_CACHE_SECONDS:
        return _CACHE["data"]

    fear = get_fear_greed()
    dominance = get_btc_dominance()
    data = {
        "fear_value": fear["value"],
        "fear_text": fear["text"],
        "btc_dominance": dominance["btc_dominance"],
        "dominance_status": dominance["dominance_status"],
        "altseason_status": dominance["altseason_status"],
    }
    _CACHE["ts"] = now
    _CACHE["data"] = data
    return data
