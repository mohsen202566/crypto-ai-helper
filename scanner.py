# -*- coding: utf-8 -*-
"""
AI Direct Market Scanner

وظیفه:
- اسکن لیست کوین‌ها
- گرفتن خروجی analyze_symbol
- انتخاب بهترین سیگنال‌ها
- رعایت Slot Manager
- اگر Slot پر بود: ذخیره Ghost Signal برای یادگیری
- خروجی ساده برای bot.py
"""

from typing import Dict, List, Optional, Any
import time

from analysis import analyze_symbol

try:
    from config import SCAN_SYMBOLS, AUTO_DIRECT_SCORE_MIN
except Exception:
    SCAN_SYMBOLS = [
        "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT",
        "XRPUSDT", "DOGEUSDT", "ADAUSDT", "AVAXUSDT",
    ]
    AUTO_DIRECT_SCORE_MIN = 82


try:
    from slot_manager import (
        can_open_new_position,
        get_free_slots,
        is_symbol_direction_active,
        select_best_candidates,
    )
except Exception:
    can_open_new_position = None
    get_free_slots = None
    is_symbol_direction_active = None
    select_best_candidates = None


try:
    from ghost_signals import create_ghost_signal
except Exception:
    create_ghost_signal = None


try:
    from coin_rotation import sort_symbols_by_rotation
except Exception:
    sort_symbols_by_rotation = None


SCAN_DELAY_SECONDS = 0.20
MAX_SCAN_RESULTS = 10
MIN_SCANNER_SCORE = AUTO_DIRECT_SCORE_MIN


def normalize_symbol(symbol: str) -> str:
    symbol = str(symbol).upper().strip()

    if not symbol.endswith("USDT"):
        symbol = f"{symbol}USDT"

    return symbol


def get_scan_symbols() -> List[str]:
    symbols = list(SCAN_SYMBOLS)

    symbols = [
        normalize_symbol(x)
        for x in symbols
        if str(x).strip()
    ]

    symbols = list(dict.fromkeys(symbols))

    if sort_symbols_by_rotation:
        try:
            sorted_symbols = sort_symbols_by_rotation(symbols)
            if isinstance(sorted_symbols, list) and sorted_symbols:
                return sorted_symbols
        except Exception:
            pass

    return symbols


def is_valid_signal(result: Dict[str, Any]) -> bool:
    if not isinstance(result, dict):
        return False

    if result.get("status") != "ACTIVE":
        return False

    if not result.get("entry_confirmed", False):
        return False

    if result.get("direction") not in ["LONG", "SHORT"]:
        return False

    if int(result.get("score", 0) or 0) < MIN_SCANNER_SCORE:
        return False

    if result.get("entry") is None:
        return False

    if result.get("stop_loss") is None:
        return False

    if result.get("tp1") is None:
        return False

    return True


def signal_rank_value(result: Dict[str, Any]) -> float:
    score = float(result.get("score", 0) or 0)
    confirmations = float(result.get("confirmations", 0) or 0)
    rr = float(result.get("risk_reward", 0) or 0)

    risk_level = result.get("risk_level", "HIGH")

    risk_bonus = 0
    if risk_level == "LOW":
        risk_bonus = 4
    elif risk_level == "MEDIUM":
        risk_bonus = 2

    freshness = result.get("freshness", "LOW")

    freshness_bonus = 0
    if freshness == "HIGH":
        freshness_bonus = 3
    elif freshness == "MEDIUM":
        freshness_bonus = 1

    return score + confirmations * 1.5 + rr * 2 + risk_bonus + freshness_bonus


def should_skip_duplicate(result: Dict[str, Any]) -> bool:
    if is_symbol_direction_active is None:
        return False

    try:
        symbol = result.get("symbol")
        direction = result.get("direction")

        return bool(
            is_symbol_direction_active(
                symbol=symbol,
                direction=direction,
            )
        )
    except Exception:
        return False


def scan_market(
    symbols: Optional[List[str]] = None,
    max_results: int = MAX_SCAN_RESULTS,
    allow_ghost: bool = True,
) -> Dict[str, Any]:
    symbols = symbols or get_scan_symbols()

    valid_signals: List[Dict[str, Any]] = []
    no_trade_count = 0
    error_count = 0
    ghost_count = 0

    for symbol in symbols:
        symbol = normalize_symbol(symbol)

        try:
            result = analyze_symbol(symbol)

if not is_valid_signal(result):
                no_trade_count += 1
                continue

            if should_skip_duplicate(result):
                continue

            valid_signals.append(result)

        except Exception:
            error_count += 1

        time.sleep(SCAN_DELAY_SECONDS)

    valid_signals.sort(
        key=signal_rank_value,
        reverse=True,
    )

    selected = valid_signals[:max_results]

    return {
        "signals": selected,
        "all_valid_signals": valid_signals,
        "scanned": len(symbols),
        "no_trade_count": no_trade_count,
        "error_count": error_count,
        "ghost_count": ghost_count,
        "timestamp": int(time.time()),
    }

# ============================================================
# Slot-aware scanner
# ============================================================

def get_available_slots() -> int:
    if get_free_slots is None:
        return 1

    try:
        free = get_free_slots()
        return max(0, int(free))
    except Exception:
        return 1


def save_as_ghost(result: Dict[str, Any], reason: str = "SLOT_FULL") -> bool:
    if create_ghost_signal is None:
        return False

    try:
        create_ghost_signal(
            symbol=result.get("symbol"),
            direction=result.get("direction"),
            entry=result.get("entry"),
            stop_loss=result.get("stop_loss"),
            tp1=result.get("tp1"),
            tp2=result.get("tp2"),
            score=result.get("score"),
            snapshot=result.get("snapshot", {}),
            source="scanner",
            reason=reason,
        )
        return True
    except Exception:
        return False


def scan_for_auto_signals(
    symbols: Optional[List[str]] = None,
    max_results: int = MAX_SCAN_RESULTS,
    allow_ghost: bool = True,
) -> Dict[str, Any]:
    """
    خروجی اصلی برای bot.py auto signal loop.

    اگر Slot آزاد باشد:
        بهترین سیگنال‌ها را برمی‌گرداند.

    اگر Slot پر باشد:
        سیگنال‌ها را به عنوان Ghost ذخیره می‌کند
        و چیزی برای ارسال تلگرام برنمی‌گرداند.
    """

    scan_result = scan_market(
        symbols=symbols,
        max_results=max_results,
        allow_ghost=allow_ghost,
    )

    valid_signals = scan_result.get("all_valid_signals", [])

    if not valid_signals:
        scan_result["signals"] = []
        scan_result["mode"] = "NO_SIGNAL"
        return scan_result

    free_slots = get_available_slots()
    scan_result["free_slots"] = free_slots

    if free_slots <= 0:
        ghost_count = 0

        if allow_ghost:
            for signal in valid_signals:
                if save_as_ghost(signal, reason="SLOT_FULL"):
                    ghost_count += 1

        scan_result["signals"] = []
        scan_result["ghost_count"] = ghost_count
        scan_result["mode"] = "GHOST_ONLY"
        return scan_result

    candidates = valid_signals

    if select_best_candidates:
        try:
            selected = select_best_candidates(
                candidates=valid_signals,
                limit=min(max_results, free_slots),
            )

            if isinstance(selected, list):
                candidates = selected

        except Exception:
            candidates = valid_signals

    candidates = sorted(
        candidates,
        key=signal_rank_value,
        reverse=True,
    )

    selected = candidates[:min(max_results, free_slots)]

    scan_result["signals"] = selected
    scan_result["mode"] = "ACTIVE_SIGNALS"

    return scan_result


def get_best_signal(
    symbols: Optional[List[str]] = None,
) -> Optional[Dict[str, Any]]:
    result = scan_for_auto_signals(
        symbols=symbols,
        max_results=1,
        allow_ghost=False,
    )

    signals = result.get("signals", [])

    if not signals:
        return None

    return signals[0]


def get_top_signals(
    symbols: Optional[List[str]] = None,
    limit: int = 5,
) -> List[Dict[str, Any]]:
    result = scan_for_auto_signals(
        symbols=symbols,
        max_results=limit,
        allow_ghost=False,
    )

    return result.get("signals", [])[:limit]


# ============================================================
# Market overview
# ============================================================

def scan_market_overview(
    symbols: Optional[List[str]] = None,
    limit: int = 40,
) -> Dict[str, Any]:
    symbols = symbols or get_scan_symbols()
    symbols = symbols[:limit]

    bullish = 0
    bearish = 0
    neutral = 0
    errors = 0

    details = []

    for symbol in symbols:
        symbol = normalize_symbol(symbol)

        try:
            result = analyze_symbol(symbol)
            trends = result.get("trends", {})

            one_h = trends.get("1H")
            fifteen = trends.get("15M")

if one_h == "bullish" and fifteen == "bullish":
                bullish += 1
                bias = "bullish"
            elif one_h == "bearish" and fifteen == "bearish":
                bearish += 1
                bias = "bearish"
            else:
                neutral += 1
                bias = "neutral"

            details.append({
                "symbol": symbol,
                "bias": bias,
                "direction": result.get("direction"),
                "score": result.get("score"),
            })

        except Exception:
            errors += 1

        time.sleep(SCAN_DELAY_SECONDS)

    total = max(bullish + bearish + neutral, 1)

    bullish_pct = round((bullish / total) * 100, 1)
    bearish_pct = round((bearish / total) * 100, 1)
    neutral_pct = round((neutral / total) * 100, 1)

    if bullish_pct >= 50:
        market_bias = "bullish"
        summary = "بازار بیشتر صعودی است"
    elif bearish_pct >= 50:
        market_bias = "bearish"
        summary = "بازار بیشتر نزولی است"
    elif neutral_pct >= 45:
        market_bias = "neutral"
        summary = "بازار بیشتر رنج یا نامشخص است"
    elif bullish_pct > bearish_pct:
        market_bias = "slightly_bullish"
        summary = "بازار کمی تمایل صعودی دارد"
    elif bearish_pct > bullish_pct:
        market_bias = "slightly_bearish"
        summary = "بازار کمی تمایل نزولی دارد"
    else:
        market_bias = "neutral"
        summary = "بازار جهت مشخصی ندارد"

    return {
        "market_bias": market_bias,
        "summary": summary,
        "bullish": bullish,
        "bearish": bearish,
        "neutral": neutral,
        "errors": errors,
        "bullish_pct": bullish_pct,
        "bearish_pct": bearish_pct,
        "neutral_pct": neutral_pct,
        "details": details,
        "scanned": len(symbols),
        "timestamp": int(time.time()),
    }


# ============================================================
# Backward compatible aliases
# ============================================================

def scan_symbols_for_signals(
    symbols: Optional[List[str]] = None,
    max_results: int = MAX_SCAN_RESULTS,
) -> List[Dict[str, Any]]:
    result = scan_for_auto_signals(
        symbols=symbols,
        max_results=max_results,
        allow_ghost=True,
    )

    return result.get("signals", [])


def find_best_signal(
    symbols: Optional[List[str]] = None,
) -> Optional[Dict[str, Any]]:
    return get_best_signal(symbols=symbols)


def find_top_signals(
    symbols: Optional[List[str]] = None,
    limit: int = 5,
) -> List[Dict[str, Any]]:
    return get_top_signals(
        symbols=symbols,
        limit=limit,
    )
