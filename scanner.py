# -*- coding: utf-8 -*-
import time

from analysis import analyze_symbol, exchange, to_okx_symbol
from config import AUTO_SIGNAL_SCORE, AUTO_SIGNAL_COOLDOWN_MINUTES, AUTO_SCAN_MAX_SYMBOLS, MIN_AUTO_CONFIRMATIONS
from coins_fa import COINS_FA

RAW_SCAN_SYMBOLS = sorted(list(set(COINS_FA.values())))
_MARKETS_CACHE = None
_SUPPORTED_SYMBOLS_CACHE = None
last_alerts = {}


def _load_okx_markets():
    global _MARKETS_CACHE
    if _MARKETS_CACHE is not None:
        return _MARKETS_CACHE
    try:
        _MARKETS_CACHE = exchange.load_markets() or {}
    except Exception as e:
        print("OKX MARKET LOAD ERROR:", str(e))
        _MARKETS_CACHE = {}
    return _MARKETS_CACHE


def symbol_supported(symbol):
    markets = _load_okx_markets()
    if not markets:
        return True
    return to_okx_symbol(symbol) in markets


def build_scan_symbols():
    global _SUPPORTED_SYMBOLS_CACHE
    if _SUPPORTED_SYMBOLS_CACHE is not None:
        return _SUPPORTED_SYMBOLS_CACHE
    supported = []
    for symbol in RAW_SCAN_SYMBOLS:
        try:
            if symbol_supported(symbol):
                supported.append(symbol)
        except Exception:
            continue
    if AUTO_SCAN_MAX_SYMBOLS and len(supported) > AUTO_SCAN_MAX_SYMBOLS:
        supported = supported[:AUTO_SCAN_MAX_SYMBOLS]
    _SUPPORTED_SYMBOLS_CACHE = supported
    return supported


SCAN_SYMBOLS = build_scan_symbols()


def refresh_scan_symbols():
    global _MARKETS_CACHE, _SUPPORTED_SYMBOLS_CACHE, SCAN_SYMBOLS
    _MARKETS_CACHE = None
    _SUPPORTED_SYMBOLS_CACHE = None
    SCAN_SYMBOLS = build_scan_symbols()
    return SCAN_SYMBOLS


def is_high_quality_signal(result):
    if not result or result.get("direction") not in ["LONG", "SHORT"]:
        return False
    if result.get("entry_mode") != "CLASSIC_TECHNICAL":
        return False
    if not result.get("entry_confirmed"):
        return False
    try:
        score = int(result.get("score") or 0)
        confirmations = int(result.get("confirmations") or 0)
    except Exception:
        return False

    try:
        adx = float(result.get("adx") or 0)
    except Exception:
        adx = 0.0

    # ADX balance for auto signals:
    # below 18 is too weak; 18-20 is allowed only with a score penalty.
    if adx < 20:
        return False
    if adx < 22:
        score -= 8

    if score < int(AUTO_SIGNAL_SCORE):
        return False
    if confirmations < int(MIN_AUTO_CONFIRMATIONS):
        return False
    if result.get("stop_loss") is None or result.get("tp1") is None:
        return False
    return True


def should_send_auto_signal(result):
    if not is_high_quality_signal(result):
        return False
    symbol = result.get("symbol")
    direction = result.get("direction")
    key = f"{symbol}_{direction}"
    now = int(time.time())
    last = last_alerts.get(key)
    if last and now - last < AUTO_SIGNAL_COOLDOWN_MINUTES * 60:
        return False
    last_alerts[key] = now
    return True


def analyze_symbol_safe(symbol):
    try:
        result = analyze_symbol(symbol)
        if result and result.get("direction") in ["LONG", "SHORT"]:
            return result
    except Exception as e:
        print("SCAN ANALYZE ERROR:", symbol, str(e)[:180])
    return None


def get_best_signals(limit=5, very_safe_only=False):
    results = []
    for symbol in SCAN_SYMBOLS:
        result = analyze_symbol_safe(symbol)
        if not result:
            continue
        if very_safe_only:
            if not is_high_quality_signal(result):
                continue
            if result.get("risk_level") != "LOW":
                continue
        elif not is_high_quality_signal(result) and int(result.get("score") or 0) < max(70, int(AUTO_SIGNAL_SCORE) - 5):
            continue
        results.append(result)
    results.sort(key=lambda r: (int(r.get("score") or 0), int(r.get("confirmations") or 0)), reverse=True)
    return results[:limit]
