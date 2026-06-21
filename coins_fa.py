from __future__ import annotations

"""
Persian display names and symbol normalization.
"""

from typing import Dict


COIN_FA: Dict[str, str] = {
    "BTCUSDT": "بیت‌کوین",
    "ETHUSDT": "اتریوم",
    "SOLUSDT": "سولانا",
    "BNBUSDT": "بی‌ان‌بی",
    "XRPUSDT": "ریپل",
    "DOGEUSDT": "دوج‌کوین",
    "ADAUSDT": "کاردانو",
    "AVAXUSDT": "آوالانچ",
    "LINKUSDT": "چین‌لینک",
    "TRXUSDT": "ترون",
    "DOTUSDT": "پولکادات",
    "MATICUSDT": "متیک",
    "LTCUSDT": "لایت‌کوین",
    "BCHUSDT": "بیت‌کوین‌کش",
    "ATOMUSDT": "اتم",
    "NEARUSDT": "نیر",
    "INJUSDT": "اینجکتیو",
    "APTUSDT": "اپتوس",
    "ARBUSDT": "آربیتروم",
    "OPUSDT": "آپتیمیزم",
    "SUIUSDT": "سویی",
    "FILUSDT": "فایل‌کوین",
    "ETCUSDT": "اتریوم کلاسیک",
    "UNIUSDT": "یونی‌سواپ",
    "AAVEUSDT": "آوه",
    "PEPEUSDT": "په‌په",
    "SHIBUSDT": "شیبا",
    "WIFUSDT": "ویف",
    "TIAUSDT": "تیا",
    "SEIUSDT": "سی",
}

ALIASES = {
    "بیت": "BTCUSDT",
    "بیتکوین": "BTCUSDT",
    "بیت‌کوین": "BTCUSDT",
    "btc": "BTCUSDT",
    "اتریوم": "ETHUSDT",
    "eth": "ETHUSDT",
    "سول": "SOLUSDT",
    "سولانا": "SOLUSDT",
    "sol": "SOLUSDT",
    "دوج": "DOGEUSDT",
    "doge": "DOGEUSDT",
    "ریپل": "XRPUSDT",
    "xrp": "XRPUSDT",
    "اینجکتیو": "INJUSDT",
    "inj": "INJUSDT",
    "شیبا": "SHIBUSDT",
    "shib": "SHIBUSDT",
    "په‌په": "PEPEUSDT",
    "pepe": "PEPEUSDT",
}


def display_symbol(symbol: str) -> str:
    s = str(symbol or "").upper()
    return f"{COIN_FA.get(s, s)} ({s})" if s else ""


def normalize_symbol(text: str) -> str:
    raw = str(text or "").strip()
    low = raw.lower().replace("/", "").replace("-", "").replace(" ", "")
    if not low:
        return ""
    if low.upper().endswith("USDT"):
        return low.upper()
    if low.upper() in {k.replace("USDT", "") for k in COIN_FA}:
        return low.upper() + "USDT"
    if low in ALIASES:
        return ALIASES[low]
    for alias, sym in ALIASES.items():
        if alias in low:
            return sym
    return low.upper() + "USDT" if low.isascii() and len(low) <= 8 else ""
