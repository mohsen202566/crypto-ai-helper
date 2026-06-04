# -*- coding: utf-8 -*-
import json
import os
import time
import requests

from config import MARKET_SENTIMENT_CACHE_SECONDS


CACHE_FILE = "market_sentiment_cache.json"

DEFAULT_SENTIMENT = {
    "fear_value": None,
    "fear_text": "نامشخص",
    "btc_dominance": None,
    "dominance_status": "نامشخص",
    "altseason_status": "نامشخص",
}

_CACHE = {"ts": 0, "data": None}
_LAST_DOMINANCE = {
    "btc_dominance": None,
    "dominance_status": "نامشخص",
    "altseason_status": "نامشخص",
}


def now_ts():
    return int(time.time())


def safe_int(value, default):
    try:
        return int(value)
    except Exception:
        return default


def cache_seconds():
    # برای جلوگیری از 429، حداقل 30 دقیقه کش اجباری است.
    return max(safe_int(MARKET_SENTIMENT_CACHE_SECONDS, 1800), 1800)


def load_disk_cache():
    if not os.path.exists(CACHE_FILE):
        return None
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return None
        return data
    except Exception:
        return None


def save_disk_cache(data):
    try:
        payload = {
            "ts": now_ts(),
            "data": data,
        }
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def get_cached_sentiment(allow_old=True):
    global _CACHE

    current = now_ts()
    if _CACHE.get("data") is not None:
        if allow_old or current - _CACHE.get("ts", 0) < cache_seconds():
            return _CACHE["data"]

    disk = load_disk_cache()
    if disk and disk.get("data"):
        data = normalize_sentiment(disk.get("data"))
        _CACHE = {"ts": disk.get("ts", 0), "data": data}
        if allow_old or current - disk.get("ts", 0) < cache_seconds():
            return data

    return dict(DEFAULT_SENTIMENT)


def update_memory_cache(data):
    global _CACHE
    data = normalize_sentiment(data)
    _CACHE = {"ts": now_ts(), "data": data}
    save_disk_cache(data)
    return data


def normalize_sentiment(data):
    if not isinstance(data, dict):
        return dict(DEFAULT_SENTIMENT)

    result = dict(DEFAULT_SENTIMENT)
    for key in result:
        if key in data:
            result[key] = data[key]
    return result


def safe_get_json(url, timeout=10):
    headers = {
        "accept": "application/json",
        "user-agent": "Mozilla/5.0 CryptoAIHelperBot/2.0",
        "cache-control": "no-cache",
    }

    try:
        response = requests.get(url, headers=headers, timeout=timeout)
    except requests.exceptions.Timeout:
        return None
    except requests.exceptions.ConnectionError:
        return None
    except Exception:
        return None

    # مهم: هیچ خطایی raise نمی‌کنیم تا VPS پر از ارور نشود.
    if response.status_code in [401, 403, 418, 429, 500, 502, 503, 504]:
        return None

    if response.status_code < 200 or response.status_code >= 300:
        return None

    try:
        return response.json()
    except Exception:
        return None


def get_fear_greed():
    data = safe_get_json("https://api.alternative.me/fng/")
    try:
        item = data["data"][0]
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


def _dominance_to_status(dominance):
    try:
        dominance = float(dominance)
    except Exception:
        return {
            "btc_dominance": None,
            "dominance_status": "نامشخص",
            "altseason_status": "نامشخص",
        }

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


def extract_dominance_from_coingecko(data):
    try:
        value = data.get("data", {}).get("market_cap_percentage", {}).get("btc")
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def get_btc_dominance():
    global _LAST_DOMINANCE

    # اول از CoinGecko با کنترل کامل خطا
    data = safe_get_json("https://api.coingecko.com/api/v3/global", timeout=10)
    dominance = extract_dominance_from_coingecko(data) if data else None

    if dominance is not None:
        _LAST_DOMINANCE = _dominance_to_status(dominance)
        return _LAST_DOMINANCE

    # اگر API جواب نداد، آخرین دامیننس حافظه را بده
    if _LAST_DOMINANCE.get("btc_dominance") is not None:
        return _LAST_DOMINANCE

    # اگر حافظه خالی بود، از کش دیسک/حافظه قبلی استفاده کن
    cached = get_cached_sentiment(allow_old=True)
    if cached.get("btc_dominance") is not None:
        _LAST_DOMINANCE = {
            "btc_dominance": cached.get("btc_dominance"),
            "dominance_status": cached.get("dominance_status", "نامشخص"),
            "altseason_status": cached.get("altseason_status", "نامشخص"),
        }
        return _LAST_DOMINANCE

    # در نهایت مقدار خنثی برگردان تا تحلیل و Auto Signal نخوابد.
    _LAST_DOMINANCE = {
        "btc_dominance": None,
        "dominance_status": "نامشخص",
        "altseason_status": "نامشخص",
    }
    return _LAST_DOMINANCE


def get_market_sentiment():
    current = now_ts()

    # کش حافظه
    if _CACHE.get("data") is not None and current - _CACHE.get("ts", 0) < cache_seconds():
        return normalize_sentiment(_CACHE["data"])

    # کش دیسک
    disk = load_disk_cache()
    if disk and disk.get("data") and current - disk.get("ts", 0) < cache_seconds():
        data = normalize_sentiment(disk["data"])
        _CACHE["ts"] = disk.get("ts", current)
        _CACHE["data"] = data
        return data

    # دریافت امن
    fear = get_fear_greed()
    dominance = get_btc_dominance()

    data = {
        "fear_value": fear.get("value"),
        "fear_text": fear.get("text", "نامشخص"),
        "btc_dominance": dominance.get("btc_dominance"),
        "dominance_status": dominance.get("dominance_status", "نامشخص"),
        "altseason_status": dominance.get("altseason_status", "نامشخص"),
    }

    # اگر همه چیز نامشخص بود، از کش قدیمی استفاده کن.
    if data["fear_value"] is None and data["btc_dominance"] is None:
        cached = get_cached_sentiment(allow_old=True)
        if cached != DEFAULT_SENTIMENT:
            return cached

    return update_memory_cache(data)
