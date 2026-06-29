"""ابزارهای عمومی ربات."""
from __future__ import annotations

import hashlib
import logging
import math
import time
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, InvalidOperation
from typing import Any, Optional

from . import config


def setup_logger(name: str = "scalper") -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")

    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    logger.addHandler(stream)

    file_handler = logging.FileHandler(config.LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    return logger


logger = setup_logger()


def now_ms() -> int:
    return int(time.time() * 1000)


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        out = float(value)
        if math.isnan(out) or math.isinf(out):
            return default
        return out
    except (ValueError, TypeError):
        return default


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (ValueError, TypeError):
        return default


def clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def validate_range(value: float, minimum: float, maximum: float, name_fa: str) -> tuple[bool, str]:
    if value < minimum or value > maximum:
        return False, f"❌ مقدار {name_fa} نامعتبر است. محدوده مجاز: {minimum:g} تا {maximum:g}"
    return True, ""


def internal_symbol_to_okx(symbol: str) -> str:
    return config.SYMBOL_MAP[symbol]["okx"]


def internal_symbol_to_toobit(symbol: str) -> str:
    return config.SYMBOL_MAP[symbol]["toobit"]


def normalize_symbol(raw: str) -> str:
    return raw.replace("-", "").replace("_", "").upper()


def toobit_symbol_candidates(internal_symbol: str) -> list[str]:
    base = config.SYMBOL_MAP[internal_symbol]["base"]
    return [
        f"{base}-SWAP-USDT",
        f"{base}USDT",
        f"{base}-USDT",
        f"{base}USDT_PERP",
        f"{base}-PERP-USDT",
    ]


def okx_symbol_candidates(internal_symbol: str) -> list[str]:
    base = config.SYMBOL_MAP[internal_symbol]["base"]
    return [
        f"{base}-USDT-SWAP",
        f"{base}-USDT",
    ]


def candle_age_seconds(open_time_ms: int) -> int:
    age = int((now_ms() - open_time_ms) / 1000)
    return max(0, age)


def is_entry_window(open_time_ms: int) -> bool:
    age = candle_age_seconds(open_time_ms)
    return config.MIN_CANDLE_AGE_SECONDS <= age <= config.MAX_CANDLE_AGE_SECONDS


def price_by_percent(entry: float, percent: float, side: str, target_type: str) -> float:
    """محاسبه TP/SL بر اساس درصد ثابت."""
    side = side.upper()
    target_type = target_type.upper()
    p = percent / 100.0
    if side == "BUY":
        return entry * (1 + p) if target_type == "TP" else entry * (1 - p)
    return entry * (1 - p) if target_type == "TP" else entry * (1 + p)


def hit_tp_sl(side: str, price: float, tp: float, sl: float) -> Optional[str]:
    side = side.upper()
    if side == "BUY":
        if price >= tp:
            return "TP"
        if price <= sl:
            return "SL"
    else:
        if price <= tp:
            return "TP"
        if price >= sl:
            return "SL"
    return None


def build_signal_id(symbol: str, side: str) -> str:
    seed = f"{symbol}-{side}-{now_ms()}".encode("utf-8")
    digest = hashlib.sha1(seed).hexdigest()[:8].upper()
    return f"{symbol}-{side}-{digest}"


def format_num(value: Any, digits: int = 6) -> str:
    v = safe_float(value, 0.0)
    if abs(v) >= 100:
        digits = min(digits, 4)
    if abs(v) >= 1000:
        digits = min(digits, 2)
    text = f"{v:.{digits}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def decimal_round_down(value: float, step: Optional[str] = None, digits: int = 6) -> str:
    try:
        d = Decimal(str(value))
        if step:
            s = Decimal(str(step))
            if s > 0:
                rounded = (d / s).to_integral_value(rounding=ROUND_DOWN) * s
                return format(rounded.normalize(), "f")
        quant = Decimal("1") / (Decimal(10) ** digits)
        return format(d.quantize(quant, rounding=ROUND_DOWN).normalize(), "f")
    except (InvalidOperation, ValueError):
        return format_num(value, digits)


def extract_filter(symbol_info: dict[str, Any], filter_type: str) -> dict[str, Any]:
    filters = symbol_info.get("filters") or []
    if isinstance(filters, list):
        for item in filters:
            if isinstance(item, dict) and item.get("filterType") == filter_type:
                return item
    return {}


def side_to_persian(side: str) -> str:
    return "خرید" if side.upper() == "BUY" else "فروش"


def side_to_toobit_open(side: str) -> str:
    return "BUY_OPEN" if side.upper() == "BUY" else "SELL_OPEN"


def side_to_toobit_position(side: str) -> str:
    return "LONG" if side.upper() == "BUY" else "SHORT"


def safe_sleep(seconds: float) -> None:
    try:
        time.sleep(seconds)
    except KeyboardInterrupt:
        raise
