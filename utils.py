"""ابزارهای کوچک، بدون وابستگی سنگین و قابل استفاده در همه Workerها."""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import shutil
import time
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP, ROUND_UP
from pathlib import Path
from typing import Any, Iterable, Sequence

logger = logging.getLogger("adaptive_bot")

PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٫٬", "0123456789..")


def now_ms() -> int:
    return int(time.time() * 1000)


def utc_iso(ts_ms: int | None = None) -> str:
    dt = datetime.fromtimestamp((ts_ms or now_ms()) / 1000, tz=timezone.utc)
    return dt.isoformat(timespec="seconds")


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


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
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def normalize_command(text: str) -> str:
    return " ".join(str(text or "").translate(PERSIAN_DIGITS).strip().split())


def parse_float_fa(text: str) -> float:
    raw = str(text).translate(PERSIAN_DIGITS).replace(",", ".").strip()
    return float(raw)


def decimal_round_down(value: float | Decimal, step: str | float | Decimal | None = None, digits: int = 8) -> str:
    d = Decimal(str(value))
    if step not in (None, "", 0, "0"):
        s = Decimal(str(step))
        if s > 0:
            d = (d / s).to_integral_value(rounding=ROUND_DOWN) * s
    q = Decimal("1").scaleb(-digits)
    d = d.quantize(q, rounding=ROUND_DOWN)
    return format(d.normalize(), "f")


def round_to_tick(value: float, tick: float, mode: str = "nearest") -> float:
    if tick <= 0:
        return value
    d = Decimal(str(value)) / Decimal(str(tick))
    rounding = {"down": ROUND_DOWN, "up": ROUND_UP}.get(mode, ROUND_HALF_UP)
    return float(d.to_integral_value(rounding=rounding) * Decimal(str(tick)))


def percentile(values: Sequence[float], p: float, default: float = 0.0) -> float:
    vals = sorted(v for v in values if math.isfinite(v))
    if not vals:
        return default
    p = clamp(p, 0.0, 1.0)
    idx = (len(vals) - 1) * p
    lo = int(math.floor(idx))
    hi = int(math.ceil(idx))
    if lo == hi:
        return vals[lo]
    return vals[lo] * (hi - idx) + vals[hi] * (idx - lo)


def mean(values: Iterable[float], default: float = 0.0) -> float:
    vals = [v for v in values if math.isfinite(v)]
    return sum(vals) / len(vals) if vals else default


def stdev(values: Sequence[float], default: float = 0.0) -> float:
    vals = [v for v in values if math.isfinite(v)]
    if len(vals) < 2:
        return default
    m = sum(vals) / len(vals)
    return math.sqrt(sum((v - m) ** 2 for v in vals) / (len(vals) - 1))


def ema(values: Sequence[float], period: int) -> list[float]:
    if not values:
        return []
    alpha = 2.0 / (period + 1.0)
    out = [float(values[0])]
    for v in values[1:]:
        out.append(alpha * float(v) + (1.0 - alpha) * out[-1])
    return out


def sma(values: Sequence[float], period: int) -> list[float]:
    if period <= 0:
        raise ValueError("period must be positive")
    out: list[float] = []
    total = 0.0
    window: list[float] = []
    for v in values:
        fv = float(v)
        window.append(fv)
        total += fv
        if len(window) > period:
            total -= window.pop(0)
        out.append(total / len(window))
    return out


def true_ranges(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float]) -> list[float]:
    out: list[float] = []
    for i, (h, l) in enumerate(zip(highs, lows)):
        prev = closes[i - 1] if i else closes[i]
        out.append(max(h - l, abs(h - prev), abs(l - prev)))
    return out


def normalize_symbol(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def canonical_base_from_symbol(value: str) -> str:
    raw = str(value or "").upper().replace("_", "-")
    if "-SWAP-USDT" in raw:
        return raw.split("-SWAP-USDT", 1)[0]
    if "-USDT-SWAP" in raw:
        return raw.split("-USDT-SWAP", 1)[0]
    compact = normalize_symbol(raw)
    if compact.endswith("USDT"):
        return compact[:-4]
    return compact


def alias_candidates(base: str, exchange: str) -> tuple[str, ...]:
    b = base.upper()
    if exchange == "okx":
        return (f"{b}-USDT-SWAP", f"{b}USDT", f"{b}-USDT")
    if exchange == "bybit":
        return (f"{b}USDT", f"{b}-USDT")
    if exchange == "toobit":
        return (f"{b}-SWAP-USDT", f"{b}USDT", f"{b}-USDT-SWAP", f"{b}-USDT")
    return (f"{b}USDT",)


def atomic_copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".tmp")
    shutil.copy2(src, tmp)
    os.replace(tmp, dst)


def stable_hash(value: Any, length: int = 16) -> str:
    payload = json_dumps(value).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:length]


def side_to_toobit_open(side: str) -> str:
    raw = str(side).upper()
    if raw in {"LONG", "BUY", "BUY_OPEN"}:
        return "BUY_OPEN"
    if raw in {"SHORT", "SELL", "SELL_OPEN"}:
        return "SELL_OPEN"
    raise ValueError(f"invalid side: {side}")


def side_to_toobit_position(side: str) -> str:
    raw = str(side).upper()
    if raw in {"LONG", "BUY", "BUY_OPEN"}:
        return "LONG"
    if raw in {"SHORT", "SELL", "SELL_OPEN"}:
        return "SHORT"
    raise ValueError(f"invalid side: {side}")


def toobit_symbol_candidates(symbol: str) -> tuple[str, ...]:
    base = canonical_base_from_symbol(symbol)
    return alias_candidates(base, "toobit")


def extract_filter(info: dict[str, Any], filter_type: str) -> dict[str, Any]:
    filters = info.get("filters") or []
    if isinstance(filters, list):
        for item in filters:
            if isinstance(item, dict) and str(item.get("filterType", "")).upper() == filter_type.upper():
                return item
    if filter_type.upper() == "LOT_SIZE":
        candidate = info.get("lotSizeFilter") or info.get("quantityFilter")
        return candidate if isinstance(candidate, dict) else {}
    if filter_type.upper() == "PRICE_FILTER":
        candidate = info.get("priceFilter")
        return candidate if isinstance(candidate, dict) else {}
    return {}
