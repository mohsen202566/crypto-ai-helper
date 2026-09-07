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


def timeframe_seconds(timeframe: str) -> int:
    """تبدیل رشتهٔ تایم‌فریم (مثل ``1h``، ``15m``، ``1d``) به ثانیه.

    برای محاسبهٔ مهلت نگه‌داشتن پوزیشن (MAX_HOLD_BARS × طول هر کندل) لازم است.
    """
    text = str(timeframe).strip().lower()
    if not text:
        return 3600
    unit = text[-1]
    try:
        amount = int(text[:-1])
    except ValueError:
        return 3600
    scale = {"m": 60, "h": 3600, "d": 86400, "w": 604800}.get(unit)
    if scale is None:
        return 3600
    return max(1, amount * scale)


def median(values: Iterable[float]) -> float:
    rows = sorted(float(x) for x in values)
    if not rows:
        return 0.0
    mid = len(rows) // 2
    return rows[mid] if len(rows) % 2 else (rows[mid - 1] + rows[mid]) / 2.0


_FA_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def normalize_command(text: str) -> str:
    value = str(text).translate(_FA_DIGITS).lower()
    value = value.replace("ي", "ی").replace("ك", "ک")
    value = value.replace("ۀ", "ه").replace("ة", "ه")
    for invisible in ("\u200c", "\u200d", "\u200e", "\u200f", "\ufeff"):
        value = value.replace(invisible, " ")
    value = " ".join(value.strip().split())
    # Telegram may deliver /start@BotUsername in groups.
    if value.startswith("/") and "@" in value.split()[0]:
        first, *rest = value.split()
        value = " ".join([first.split("@", 1)[0], *rest])
    return value


def parse_number(text: str) -> float:
    normalized = (
        str(text)
        .translate(_FA_DIGITS)
        .replace("٬", "")
        .replace(",", "")
        .replace("٫", ".")
        .strip()
    )
    return float(normalized)
