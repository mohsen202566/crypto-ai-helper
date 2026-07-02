"""ابزارهای عمومی ربات Spot Hunter."""
from __future__ import annotations

import fcntl
import json
import logging
import math
import os
import time
import uuid
from contextlib import contextmanager
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP, InvalidOperation
from pathlib import Path
from typing import Any, Iterator

import config


def setup_logger() -> logging.Logger:
    logger = logging.getLogger("spot_hunter")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    try:
        fh = logging.FileHandler(config.BOT_LOG_FILE, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except Exception:
        pass
    return logger


logger = setup_logger()


def now_ms() -> int:
    return int(time.time() * 1000)


def now_s() -> int:
    return int(time.time())


def make_id(prefix: str) -> str:
    return f"{prefix}_{int(time.time())}_{uuid.uuid4().hex[:10]}"


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        out = float(value)
        if math.isnan(out) or math.isinf(out):
            return default
        return out
    except Exception:
        return default


def safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except Exception:
        return default


def to_decimal(value: Any, default: str = "0") -> Decimal:
    try:
        if value is None or value == "":
            return Decimal(default)
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal(default)


def decimal_to_api(value: Any) -> str:
    dec = to_decimal(value)
    return format(dec.normalize(), "f")


def decimal_round_down(value: Any, step: str | float | Decimal = "0.00000001", digits: int = 8) -> str:
    dec = to_decimal(value)
    step_dec = to_decimal(step, "0")
    if step_dec > 0:
        units = (dec / step_dec).to_integral_value(rounding=ROUND_DOWN)
        dec = units * step_dec
    quant = Decimal("1").scaleb(-int(digits))
    dec = dec.quantize(quant, rounding=ROUND_DOWN)
    return format(dec.normalize(), "f")


def round_price_to_tick(price: float, tick: str | float | Decimal, *, direction: str = "up") -> float:
    dec = to_decimal(price)
    tick_dec = to_decimal(tick, "0")
    if tick_dec <= 0:
        return float(dec)
    units = dec / tick_dec
    rounding = ROUND_HALF_UP if direction == "nearest" else ROUND_DOWN
    if direction == "up":
        units_int = units.to_integral_value(rounding=ROUND_DOWN)
        if units_int * tick_dec < dec:
            units_int += 1
    else:
        units_int = units.to_integral_value(rounding=rounding)
    return float(units_int * tick_dec)


def target_price_from_entry(entry_price: float, target_percent: float) -> float:
    return float(entry_price) * (1.0 + float(target_percent) / 100.0)


def pct_change(entry_price: float, current_price: float) -> float:
    if entry_price <= 0:
        return 0.0
    return (current_price - entry_price) / entry_price * 100.0


def gross_profit_usdt(amount_usdt: float, percent: float) -> float:
    return float(amount_usdt) * float(percent) / 100.0


def estimate_round_trip_fee(amount_usdt: float, target_percent: float, buy_fee_pct: float, sell_fee_pct: float) -> float:
    buy_value = float(amount_usdt)
    sell_value = buy_value * (1.0 + float(target_percent) / 100.0)
    return (buy_value * float(buy_fee_pct) / 100.0) + (sell_value * float(sell_fee_pct) / 100.0)


def net_profit_estimate(amount_usdt: float, target_percent: float, buy_fee_pct: float, sell_fee_pct: float) -> dict[str, float]:
    gross = gross_profit_usdt(amount_usdt, target_percent)
    fee = estimate_round_trip_fee(amount_usdt, target_percent, buy_fee_pct, sell_fee_pct)
    return {"gross_profit_usdt": gross, "fee_usdt": fee, "net_profit_usdt": gross - fee}


def format_float(value: Any, digits: int = 6) -> str:
    val = safe_float(value)
    if abs(val) >= 100:
        digits = min(digits, 4)
    if abs(val) >= 1000:
        digits = min(digits, 2)
    text = f"{val:.{digits}f}".rstrip("0").rstrip(".")
    return text if text else "0"


def okx_inst_id(base_symbol: str) -> str:
    base = base_symbol.upper().replace("USDT", "").replace("-", "")
    return f"{base}-{config.QUOTE_ASSET}"


def toobit_symbol(base_symbol: str) -> str:
    base = base_symbol.upper().replace("USDT", "").replace("-", "")
    return f"{base}{config.QUOTE_ASSET}"


def base_from_symbol(symbol: str) -> str:
    s = symbol.upper().replace("-", "")
    if s.endswith(config.QUOTE_ASSET):
        return s[: -len(config.QUOTE_ASSET)]
    return s


def extract_filter(info: dict[str, Any], filter_type: str) -> dict[str, Any]:
    filters = info.get("filters") or info.get("filter") or []
    if isinstance(filters, list):
        for item in filters:
            if not isinstance(item, dict):
                continue
            if str(item.get("filterType") or item.get("type") or "").upper() == filter_type.upper():
                return item
    return {}


def json_load(path: Path, default: Any) -> Any:
    try:
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("خواندن فایل JSON ناموفق بود %s: %s", path, exc)
        return default


def json_save_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


@contextmanager
def single_instance_lock(lock_file: str) -> Iterator[None]:
    path = Path(lock_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = path.open("w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fh.write(str(os.getpid()))
        fh.flush()
        yield
    except BlockingIOError as exc:
        raise RuntimeError("یک نسخه دیگر از ربات در حال اجراست") from exc
    finally:
        try:
            fcntl.flock(fh, fcntl.LOCK_UN)
            fh.close()
        except Exception:
            pass
