"""ابزارهای عمومی؛ بدون وابستگی به ساختار پوشه‌ای."""
from __future__ import annotations

import json
import logging
import math
import re
import time
from decimal import Decimal, ROUND_DOWN
from typing import Any, Iterable

import config

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(threadName)s | %(message)s",
)
logger = logging.getLogger("toobit_pump_bot")


def now_ms() -> int:
    return int(time.time() * 1000)


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
        return out if math.isfinite(out) else default
    except (TypeError, ValueError, OverflowError):
        return default


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return default


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def json_loads(value: str | bytes | None, default: Any = None) -> Any:
    if value in (None, ""):
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def canonical_base(symbol: str) -> str:
    text = re.sub(r"[^A-Z0-9]", "", str(symbol).upper())
    for suffix in ("SWAPUSDT", "USDT", "USDTPERP", "PERP"):
        if text.endswith(suffix):
            return text[: -len(suffix)]
    return text


def canonical_symbol(symbol: str) -> str:
    return f"{canonical_base(symbol)}USDT"


def toobit_contract_symbol(symbol: str) -> str:
    return f"{canonical_base(symbol)}-SWAP-USDT"


def side_to_open(side: str) -> str:
    return "BUY_OPEN" if str(side).upper() == "LONG" else "SELL_OPEN"


def side_to_position(side: str) -> str:
    return "LONG" if str(side).upper() in {"LONG", "BUY", "BUY_OPEN"} else "SHORT"


def decimal_round_down(value: float | Decimal, step: str | float = "0.00000001", digits: int = 8) -> str:
    val = Decimal(str(value))
    step_dec = Decimal(str(step or 0))
    if step_dec > 0:
        val = (val / step_dec).to_integral_value(rounding=ROUND_DOWN) * step_dec
    quant = Decimal(1).scaleb(-digits)
    val = val.quantize(quant, rounding=ROUND_DOWN)
    return format(val.normalize(), "f")


def extract_filter(info: dict[str, Any], filter_type: str) -> dict[str, Any]:
    for key in ("filters", "filter", "rules"):
        rows = info.get(key)
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict) and str(row.get("filterType") or row.get("type") or "").upper() == filter_type.upper():
                    return row
    direct = info.get(filter_type) or info.get(filter_type.lower())
    return direct if isinstance(direct, dict) else {}


def percent_change(new: float, old: float) -> float:
    return ((new / old) - 1.0) * 100.0 if old > 0 else 0.0


def ema(values: list[float], period: int) -> float:
    if not values:
        return 0.0
    period = max(1, min(period, len(values)))
    k = 2.0 / (period + 1.0)
    out = values[0]
    for value in values[1:]:
        out = value * k + out * (1.0 - k)
    return out


def rsi(values: list[float], period: int = 14) -> float:
    if len(values) < 2:
        return 50.0
    deltas = [values[i] - values[i - 1] for i in range(1, len(values))]
    tail = deltas[-period:]
    gains = sum(max(0.0, x) for x in tail) / max(1, len(tail))
    losses = sum(max(0.0, -x) for x in tail) / max(1, len(tail))
    if losses <= 1e-15:
        return 100.0 if gains > 0 else 50.0
    rs = gains / losses
    return 100.0 - 100.0 / (1.0 + rs)


def atr(candles: list[dict[str, float]], period: int = 14) -> float:
    if len(candles) < 2:
        return 0.0
    trs: list[float] = []
    prev_close = candles[0]["close"]
    for candle in candles[1:]:
        tr = max(
            candle["high"] - candle["low"],
            abs(candle["high"] - prev_close),
            abs(candle["low"] - prev_close),
        )
        trs.append(tr)
        prev_close = candle["close"]
    tail = trs[-period:]
    return sum(tail) / max(1, len(tail))


def median(values: Iterable[float]) -> float:
    rows = sorted(float(x) for x in values)
    if not rows:
        return 0.0
    mid = len(rows) // 2
    return rows[mid] if len(rows) % 2 else (rows[mid - 1] + rows[mid]) / 2.0


_FA_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def normalize_command(text: str) -> str:
    return " ".join(str(text).translate(_FA_DIGITS).strip().lower().split())


def parse_number(text: str) -> float:
    normalized = str(text).translate(_FA_DIGITS).replace(",", "").strip()
    return float(normalized)
