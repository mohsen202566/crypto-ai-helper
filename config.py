from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent
STORAGE_DIR = BASE_DIR / "storage"
LOG_DIR = BASE_DIR / "logs"
STORAGE_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

SETTINGS_FILE = STORAGE_DIR / "settings.json"
SIGNALS_FILE = STORAGE_DIR / "signals.json"
SLOTS_FILE = STORAGE_DIR / "slots.json"


@dataclass
class BotSettings:
    trade_enabled: bool = False
    trade_amount_usdt: float = 10.0
    leverage: int = 10
    max_positions: int = 3
    fee_usdt: float = 0.05
    min_net_profit_usdt: float = 0.05
    min_rr: float = 1.5
    scan_interval_seconds: int = 60
    result_interval_seconds: int = 20
    candle_limit: int = 220
    telegram_chat_id: str = ""

    def clean(self) -> "BotSettings":
        self.trade_amount_usdt = min(max(float(self.trade_amount_usdt), 1.0), 10000.0)
        self.leverage = min(max(int(self.leverage), 1), 100)
        self.max_positions = min(max(int(self.max_positions), 1), 100)
        self.fee_usdt = max(float(self.fee_usdt), 0.0)
        self.min_net_profit_usdt = max(float(self.min_net_profit_usdt), 0.0)
        self.min_rr = max(float(self.min_rr), 0.1)
        self.scan_interval_seconds = max(int(self.scan_interval_seconds), 10)
        self.result_interval_seconds = max(int(self.result_interval_seconds), 5)
        self.candle_limit = max(int(self.candle_limit), 120)
        return self


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return float(default)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return int(default)


def default_settings() -> BotSettings:
    return BotSettings(
        trade_enabled=os.getenv("TRADE_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"},
        trade_amount_usdt=_env_float("TRADE_AMOUNT_USDT", 10.0),
        leverage=_env_int("LEVERAGE", 10),
        max_positions=_env_int("MAX_POSITIONS", 3),
        fee_usdt=_env_float("FIXED_POSITION_FEE_USDT", 0.05),
        min_net_profit_usdt=_env_float("MIN_NET_PROFIT_USDT", 0.05),
        min_rr=_env_float("MIN_RR", 1.5),
        scan_interval_seconds=_env_int("SCAN_INTERVAL_SECONDS", 60),
        result_interval_seconds=_env_int("RESULT_INTERVAL_SECONDS", 20),
        candle_limit=_env_int("CANDLE_LIMIT", 220),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
    ).clean()


def load_settings() -> BotSettings:
    base = default_settings()
    if not SETTINGS_FILE.exists():
        save_settings(base)
        return base
    try:
        data: dict[str, Any] = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception:
        save_settings(base)
        return base
    for key, value in data.items():
        if hasattr(base, key):
            setattr(base, key, value)
    return base.clean()


def save_settings(settings: BotSettings) -> None:
    settings.clean()
    SETTINGS_FILE.write_text(json.dumps(asdict(settings), ensure_ascii=False, indent=2), encoding="utf-8")


def set_trade_enabled(enabled: bool) -> BotSettings:
    settings = load_settings()
    settings.trade_enabled = bool(enabled)
    save_settings(settings)
    return settings


def set_trade_amount(value: float) -> BotSettings:
    settings = load_settings()
    settings.trade_amount_usdt = value
    save_settings(settings)
    return settings


def set_leverage(value: int) -> BotSettings:
    settings = load_settings()
    settings.leverage = value
    save_settings(settings)
    return settings


def set_max_positions(value: int) -> BotSettings:
    settings = load_settings()
    settings.max_positions = value
    save_settings(settings)
    return settings
