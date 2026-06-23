from __future__ import annotations

"""
01 - config.py

Locked config for the simplified Level 1 / 5M crypto futures bot.

Architecture lock:
- 10 selected coins only.
- BTC and ETH are excluded for now because of small-capital / min-order issues.
- Technical analysis is raw sensor data only.
- AI is the only final decision maker.
- Pattern Start Layer is a core component.
- Independent trap/confidence/correlation/meta/state engines are removed.
- Toobit real trading only; no paper mode and no setup flow.
- TP/SL must be attached at order entry.
"""

import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_ENV_FILE = BASE_DIR / ".env"


class ConfigError(RuntimeError):
    """Raised when required runtime configuration is missing or invalid."""


# ---------------------------------------------------------------------------
# Env helpers
# ---------------------------------------------------------------------------

def _load_dotenv(path: Path = DEFAULT_ENV_FILE) -> None:
    """Small dependency-free .env loader. Existing environment variables win."""
    if not path.exists() or not path.is_file():
        return
    try:
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if not key:
                continue
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            os.environ.setdefault(key, value)
    except Exception as exc:
        raise ConfigError(f"failed_to_load_env_file:{path}:{exc}") from exc


def _get_str(name: str, default: str = "") -> str:
    return str(os.getenv(name, default)).strip()


def _get_int(name: str, default: int, min_value: Optional[int] = None, max_value: Optional[int] = None) -> int:
    raw = _get_str(name, str(default))
    try:
        value = int(float(raw))
    except Exception:
        value = default
    if min_value is not None:
        value = max(min_value, value)
    if max_value is not None:
        value = min(max_value, value)
    return value


def _get_float(name: str, default: float, min_value: Optional[float] = None, max_value: Optional[float] = None) -> float:
    raw = _get_str(name, str(default))
    try:
        value = float(raw)
    except Exception:
        value = default
    if min_value is not None:
        value = max(min_value, value)
    if max_value is not None:
        value = min(max_value, value)
    return value


def _get_bool(name: str, default: bool = False) -> bool:
    raw = _get_str(name, "").lower()
    if raw == "":
        return default
    return raw in {"1", "true", "yes", "on", "enable", "enabled", "y", "بله", "روشن", "فعال"}


def _get_int_list(name: str, default: Optional[List[int]] = None) -> List[int]:
    raw = _get_str(name, "")
    if not raw:
        return list(default or [])
    values: List[int] = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            value = int(float(item))
            if value > 0:
                values.append(value)
        except Exception:
            continue
    return values


def _get_symbol_list(name: str, default: Optional[List[str]] = None) -> List[str]:
    raw = _get_str(name, "")
    source = raw.split(",") if raw else list(default or [])
    symbols: List[str] = []
    for item in source:
        symbol = normalize_symbol(str(item))
        if symbol and symbol not in symbols:
            symbols.append(symbol)
    return symbols


# ---------------------------------------------------------------------------
# Level 1 / 5M watchlist and symbol mapping
# ---------------------------------------------------------------------------

LEVEL1_WATCHLIST: List[str] = [
    "DOGEUSDT",
    "XRPUSDT",
    "SOLUSDT",
    "ADAUSDT",
    "AVAXUSDT",
    "LINKUSDT",
    "INJUSDT",
    "PEPEUSDT",
    "WIFUSDT",
    "BONKUSDT",
]

# English short names.
SYMBOL_SHORT_NAMES: Dict[str, str] = {
    "DOGEUSDT": "DOGE",
    "XRPUSDT": "XRP",
    "SOLUSDT": "SOL",
    "ADAUSDT": "ADA",
    "AVAXUSDT": "AVAX",
    "LINKUSDT": "LINK",
    "INJUSDT": "INJ",
    "PEPEUSDT": "PEPE",
    "WIFUSDT": "WIF",
    "BONKUSDT": "BONK",
}

# Persian names used in Telegram messages.
PERSIAN_SYMBOL_NAMES: Dict[str, str] = {
    "DOGEUSDT": "دوج",
    "XRPUSDT": "ریپل",
    "SOLUSDT": "سولانا",
    "ADAUSDT": "کاردانو",
    "AVAXUSDT": "آوالانچ",
    "LINKUSDT": "چین لینک",
    "INJUSDT": "اینجکتیو",
    "PEPEUSDT": "پپه",
    "WIFUSDT": "ویف",
    "BONKUSDT": "بونک",
}

# User-facing aliases. Every value must match Toobit futures symbol format.
SYMBOL_ALIASES: Dict[str, str] = {
    "doge": "DOGEUSDT",
    "dogeusdt": "DOGEUSDT",
    "دوج": "DOGEUSDT",
    "دوج کوین": "DOGEUSDT",
    "xrp": "XRPUSDT",
    "xrpusdt": "XRPUSDT",
    "ریپل": "XRPUSDT",
    "sol": "SOLUSDT",
    "solusdt": "SOLUSDT",
    "سول": "SOLUSDT",
    "سولانا": "SOLUSDT",
    "ada": "ADAUSDT",
    "adausdt": "ADAUSDT",
    "کاردانو": "ADAUSDT",
    "آدا": "ADAUSDT",
    "avax": "AVAXUSDT",
    "avaxusdt": "AVAXUSDT",
    "اواکس": "AVAXUSDT",
    "آوالانچ": "AVAXUSDT",
    "link": "LINKUSDT",
    "linkusdt": "LINKUSDT",
    "لینک": "LINKUSDT",
    "چین لینک": "LINKUSDT",
    "inj": "INJUSDT",
    "injusdt": "INJUSDT",
    "اینج": "INJUSDT",
    "اینجکتیو": "INJUSDT",
    "pepe": "PEPEUSDT",
    "pepeusdt": "PEPEUSDT",
    "پپه": "PEPEUSDT",
    "wif": "WIFUSDT",
    "wifusdt": "WIFUSDT",
    "ویف": "WIFUSDT",
    "bonk": "BONKUSDT",
    "bonkusdt": "BONKUSDT",
    "بونک": "BONKUSDT",
}


def normalize_symbol(value: str) -> str:
    """Normalize Persian/English user input to the exact Toobit USDT symbol."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    key = raw.lower().replace("/", "").replace("-", "").replace("_", "").replace(" ", "")
    direct_key = raw.lower().strip()
    if raw.upper() in LEVEL1_WATCHLIST:
        return raw.upper()
    if key in SYMBOL_ALIASES:
        return SYMBOL_ALIASES[key]
    if direct_key in SYMBOL_ALIASES:
        return SYMBOL_ALIASES[direct_key]
    candidate = key.upper()
    if candidate and not candidate.endswith("USDT"):
        candidate = f"{candidate}USDT"
    return candidate if candidate in LEVEL1_WATCHLIST else ""


def get_persian_symbol_name(symbol: str) -> str:
    symbol = normalize_symbol(symbol) or str(symbol or "").upper()
    return PERSIAN_SYMBOL_NAMES.get(symbol, symbol)


def get_symbol_short_name(symbol: str) -> str:
    symbol = normalize_symbol(symbol) or str(symbol or "").upper()
    return SYMBOL_SHORT_NAMES.get(symbol, symbol.replace("USDT", ""))


DEFAULT_TIMEFRAMES: List[str] = ["5m"]
MARKET_MODE_SYMBOLS: List[str] = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]


# ---------------------------------------------------------------------------
# Config sections
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TelegramConfig:
    bot_token: str
    owner_id: int
    allowed_user_ids: List[int]

    def validate(self) -> List[str]:
        errors: List[str] = []
        if not self.bot_token:
            errors.append("BOT_TOKEN is required")
        if self.owner_id <= 0:
            errors.append("OWNER_ID must be a positive integer")
        return errors


@dataclass(frozen=True)
class BotConfig:
    token: str
    owner_id: int
    timezone: str
    command_language: str = "fa"
    admin_only: bool = True

    def validate(self) -> List[str]:
        return TelegramConfig(self.token, self.owner_id, []).validate()


@dataclass(frozen=True)
class ToobitConfig:
    api_key: str
    api_secret: str
    base_url: str
    recv_window: int
    api_version: str = "v1"
    category: str = "USDT"
    timeout_seconds: int = 12

    def validate(self) -> List[str]:
        errors: List[str] = []
        if not self.api_key:
            errors.append("TOOBIT_API_KEY is required")
        if not self.api_secret:
            errors.append("TOOBIT_API_SECRET is required")
        if not self.base_url.startswith("http"):
            errors.append("TOOBIT_BASE_URL must be a valid URL")
        if self.timeout_seconds <= 0:
            errors.append("TOOBIT_TIMEOUT_SECONDS must be positive")
        if self.recv_window < 1000:
            errors.append("TOOBIT_RECV_WINDOW must be at least 1000")
        return errors


@dataclass(frozen=True)
class TradingConfig:
    enabled: bool
    max_positions: int
    margin_usdt: float
    leverage: int
    isolated_only: bool
    allow_reduce_only_close: bool
    require_tp_sl_on_entry: bool
    require_order_confirmation: bool
    position_confirm_timeout_seconds: int
    position_confirm_interval_seconds: float
    result_sync_timeout_seconds: int
    result_sync_interval_seconds: float
    paper_mode_enabled: bool = False
    setup_flow_enabled: bool = False

    def validate(self) -> List[str]:
        errors: List[str] = []
        if self.paper_mode_enabled:
            errors.append("Paper mode is forbidden")
        if self.setup_flow_enabled:
            errors.append("Setup flow is forbidden")
        if self.max_positions <= 0:
            errors.append("MAX_POSITIONS must be positive")
        if self.margin_usdt <= 0:
            errors.append("TRADE_MARGIN_USDT must be positive")
        if self.leverage <= 0:
            errors.append("TRADE_LEVERAGE must be positive")
        if not self.isolated_only:
            errors.append("ISOLATED_ONLY must remain enabled")
        if not self.require_tp_sl_on_entry:
            errors.append("TP/SL must be attached at entry")
        if self.position_confirm_timeout_seconds < 60:
            errors.append("POSITION_CONFIRM_TIMEOUT_SECONDS should be at least 60")
        if self.result_sync_timeout_seconds < 60:
            errors.append("RESULT_SYNC_TIMEOUT_SECONDS should be at least 60")
        return errors


@dataclass(frozen=True)
class AIConfig:
    enabled: bool
    min_real_confidence: float
    min_ghost_confidence: float
    max_real_risk: float
    technical_sensors_enabled: bool
    pattern_start_enabled: bool
    movement_predictor_enabled: bool
    learning_enabled: bool

    def validate(self) -> List[str]:
        errors: List[str] = []
        if not (0 <= self.min_ghost_confidence <= 100):
            errors.append("AI_MIN_GHOST_CONFIDENCE must be 0..100")
        if not (0 <= self.min_real_confidence <= 100):
            errors.append("AI_MIN_REAL_CONFIDENCE must be 0..100")
        if self.min_real_confidence < self.min_ghost_confidence:
            errors.append("AI_MIN_REAL_CONFIDENCE should be >= AI_MIN_GHOST_CONFIDENCE")
        if not (0 <= self.max_real_risk <= 100):
            errors.append("AI_MAX_REAL_RISK must be 0..100")
        return errors


@dataclass(frozen=True)
class PatternConfig:
    enabled: bool
    history_days: int
    min_patterns_per_symbol_direction: int
    min_repeats_for_importance: int
    strong_match_score: float
    live_match_score: float
    pre_start_score: float
    max_patterns_per_symbol_direction: int

    def validate(self) -> List[str]:
        errors: List[str] = []
        if self.history_days <= 0:
            errors.append("PATTERN_HISTORY_DAYS must be positive")
        if self.min_patterns_per_symbol_direction <= 0:
            errors.append("PATTERN_MIN_PER_DIRECTION must be positive")
        if self.min_repeats_for_importance <= 0:
            errors.append("PATTERN_MIN_REPEATS must be positive")
        if self.max_patterns_per_symbol_direction < self.min_patterns_per_symbol_direction:
            errors.append("PATTERN_MAX_PER_DIRECTION must be >= PATTERN_MIN_PER_DIRECTION")
        return errors


@dataclass(frozen=True)
class LearningConfig:
    enabled: bool
    ghost_learning_enabled: bool
    real_learning_enabled: bool
    movement_memory_enabled: bool
    pattern_learning_enabled: bool
    max_records: int
    real_weight: float
    ghost_weight: float
    min_samples_for_confidence: int
    backup_enabled: bool
    backup_interval_seconds: int

    def validate(self) -> List[str]:
        errors: List[str] = []
        if self.max_records <= 0:
            errors.append("LEARNING_MAX_RECORDS must be positive")
        if self.real_weight <= 0 or self.ghost_weight <= 0:
            errors.append("Learning weights must be positive")
        if self.min_samples_for_confidence <= 0:
            errors.append("MIN_SAMPLES_FOR_CONFIDENCE must be positive")
        return errors


@dataclass(frozen=True)
class MarketDataConfig:
    okx_base_url: str
    candle_limit: int
    request_timeout_seconds: int
    default_timeframes: List[str]
    scan_symbols: List[str]

    def validate(self) -> List[str]:
        errors: List[str] = []
        if not self.okx_base_url.startswith("http"):
            errors.append("OKX_BASE_URL must be a valid URL")
        if self.candle_limit < 100:
            errors.append("CANDLE_LIMIT should be at least 100 for pattern scanning")
        if not self.default_timeframes:
            errors.append("DEFAULT_TIMEFRAMES cannot be empty")
        if not self.scan_symbols:
            errors.append("SCAN_SYMBOLS cannot be empty")
        invalid = [s for s in self.scan_symbols if s not in LEVEL1_WATCHLIST]
        if invalid:
            errors.append(f"SCAN_SYMBOLS contains symbols outside Level 1 watchlist: {invalid}")
        return errors


@dataclass(frozen=True)
class MarketContextConfig:
    enabled: bool
    okx_market_mode_enabled: bool
    cache_ttl_seconds: int
    leader_symbols: List[str]

    def validate(self) -> List[str]:
        if self.cache_ttl_seconds <= 0:
            return ["MARKET_CONTEXT_CACHE_TTL_SECONDS must be positive"]
        return []


@dataclass(frozen=True)
class TPConfig:
    tp2_enabled: bool
    tp1_protection_enabled: bool
    move_sl_after_tp1: bool
    ai_exit_enabled: bool
    min_tp1_atr_multiplier: float
    min_sl_atr_multiplier: float
    tp2_atr_multiplier: float
    min_rr: float
    ai_exit_min_tp1_progress: float
    min_net_profit_usdt: float
    fee_rate_per_side: float
    support_resistance_adjust_enabled: bool

    def validate(self) -> List[str]:
        errors: List[str] = []
        if self.min_tp1_atr_multiplier < 0.95:
            errors.append("TP1 minimum must not be below 0.95 ATR")
        if self.min_sl_atr_multiplier < 1.25:
            errors.append("SL minimum must not be below 1.25 ATR")
        if self.tp2_atr_multiplier <= self.min_tp1_atr_multiplier:
            errors.append("TP2 ATR multiplier must be greater than TP1")
        if not (0.0 <= self.ai_exit_min_tp1_progress <= 1.0):
            errors.append("AI_EXIT_MIN_TP1_PROGRESS must be 0..1")
        if self.ai_exit_min_tp1_progress < 0.60:
            errors.append("AI exit before TP1 should not happen before 60% of TP1 path")
        if self.min_rr <= 0:
            errors.append("MIN_RR must be positive")
        return errors


@dataclass(frozen=True)
class MonitorConfig:
    scan_interval_seconds: float
    position_monitor_interval_seconds: float
    ghost_monitor_interval_seconds: float
    command_timeout_seconds: int
    startup_grace_seconds: int

    def validate(self) -> List[str]:
        errors: List[str] = []
        if self.scan_interval_seconds <= 0:
            errors.append("SCAN_INTERVAL_SECONDS must be positive")
        if self.position_monitor_interval_seconds <= 0:
            errors.append("POSITION_MONITOR_INTERVAL_SECONDS must be positive")
        if self.ghost_monitor_interval_seconds <= 0:
            errors.append("GHOST_MONITOR_INTERVAL_SECONDS must be positive")
        return errors


@dataclass(frozen=True)
class RiskConfig:
    emergency_stop_enabled: bool
    max_daily_loss_usdt: float
    max_daily_loss_percent: float
    sl_streak_limit: int
    cooldown_after_sl_minutes: int
    max_same_direction_positions: int
    save_raw_exchange_errors: bool

    def validate(self) -> List[str]:
        errors: List[str] = []
        if self.max_daily_loss_usdt < 0:
            errors.append("MAX_DAILY_LOSS_USDT cannot be negative")
        if self.max_daily_loss_percent < 0:
            errors.append("MAX_DAILY_LOSS_PERCENT cannot be negative")
        if self.sl_streak_limit <= 0:
            errors.append("SL_STREAK_LIMIT must be positive")
        if self.max_same_direction_positions <= 0:
            errors.append("MAX_SAME_DIRECTION_POSITIONS must be positive")
        return errors


@dataclass(frozen=True)
class StorageConfig:
    data_dir: Path
    signals_file: Path
    positions_file: Path
    ghosts_file: Path
    learning_file: Path
    movement_memory_file: Path
    pattern_file: Path
    stats_file: Path
    backups_dir: Path
    atomic_writes: bool = True

    def validate(self) -> List[str]:
        if not self.data_dir:
            return ["DATA_DIR is required"]
        return []


@dataclass(frozen=True)
class LoggingConfig:
    level: str
    debug_mode: bool
    log_raw_decisions: bool
    log_raw_toobit_errors: bool

    def validate(self) -> List[str]:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if self.level.upper() not in allowed:
            return [f"LOG_LEVEL must be one of {sorted(allowed)}"]
        return []


@dataclass(frozen=True)
class AppConfig:
    bot: BotConfig
    toobit: ToobitConfig
    trading: TradingConfig
    ai: AIConfig
    pattern: PatternConfig
    learning: LearningConfig
    market_data: MarketDataConfig
    market_context: MarketContextConfig
    tp: TPConfig
    monitor: MonitorConfig
    risk: RiskConfig
    storage: StorageConfig
    logging: LoggingConfig

    @property
    def telegram(self) -> TelegramConfig:
        allowed = _get_int_list("ALLOWED_USER_IDS", [])
        if self.bot.owner_id > 0 and self.bot.owner_id not in allowed:
            allowed.insert(0, self.bot.owner_id)
        return TelegramConfig(
            bot_token=self.bot.token,
            owner_id=self.bot.owner_id,
            allowed_user_ids=allowed,
        )

    def validate(self) -> List[str]:
        errors: List[str] = []
        for section in (
            self.bot,
            self.toobit,
            self.trading,
            self.ai,
            self.pattern,
            self.learning,
            self.market_data,
            self.market_context,
            self.tp,
            self.monitor,
            self.risk,
            self.storage,
            self.logging,
        ):
            errors.extend(section.validate())
        return errors

    def require_valid(self) -> None:
        errors = self.validate()
        if errors:
            joined = "\n- " + "\n- ".join(errors)
            raise ConfigError(f"Invalid configuration:{joined}")

    def as_safe_dict(self) -> Dict[str, Any]:
        return {
            "bot": {"owner_id": self.bot.owner_id, "timezone": self.bot.timezone, "admin_only": self.bot.admin_only},
            "telegram": {"owner_id": self.telegram.owner_id, "allowed_user_count": len(self.telegram.allowed_user_ids)},
            "toobit": {
                "base_url": self.toobit.base_url,
                "api_version": self.toobit.api_version,
                "category": self.toobit.category,
                "has_api_key": bool(self.toobit.api_key),
                "has_api_secret": bool(self.toobit.api_secret),
            },
            "trading": {
                "enabled": self.trading.enabled,
                "max_positions": self.trading.max_positions,
                "margin_usdt": self.trading.margin_usdt,
                "leverage": self.trading.leverage,
                "isolated_only": self.trading.isolated_only,
            },
            "ai": asdict(self.ai),
            "pattern": asdict(self.pattern),
            "learning": {
                "enabled": self.learning.enabled,
                "pattern_learning_enabled": self.learning.pattern_learning_enabled,
                "max_records": self.learning.max_records,
            },
            "market_data": {"scan_symbols": self.market_data.scan_symbols, "default_timeframes": self.market_data.default_timeframes},
            "tp": {
                "min_tp1_atr_multiplier": self.tp.min_tp1_atr_multiplier,
                "min_sl_atr_multiplier": self.tp.min_sl_atr_multiplier,
                "ai_exit_min_tp1_progress": self.tp.ai_exit_min_tp1_progress,
            },
            "monitor": asdict(self.monitor),
        }


# ---------------------------------------------------------------------------
# Build config
# ---------------------------------------------------------------------------

def load_config(validate: bool = False, env_file: Path = DEFAULT_ENV_FILE) -> AppConfig:
    _load_dotenv(env_file)
    data_dir = Path(_get_str("DATA_DIR", str(BASE_DIR / "data"))).expanduser()
    backups_dir = data_dir / "backups"

    cfg = AppConfig(
        bot=BotConfig(
            token=_get_str("BOT_TOKEN"),
            owner_id=_get_int("OWNER_ID", _get_int("TELEGRAM_OWNER_ID", 0), min_value=0),
            timezone=_get_str("TIMEZONE", "Asia/Tehran"),
            command_language=_get_str("COMMAND_LANGUAGE", "fa"),
            admin_only=_get_bool("ADMIN_ONLY", True),
        ),
        toobit=ToobitConfig(
            api_key=_get_str("TOOBIT_API_KEY"),
            api_secret=_get_str("TOOBIT_API_SECRET", _get_str("TOOBIT_SECRET")),
            base_url=_get_str("TOOBIT_BASE_URL", "https://api.toobit.com").rstrip("/"),
            recv_window=_get_int("TOOBIT_RECV_WINDOW", 5000, min_value=1000),
            api_version=_get_str("TOOBIT_API_VERSION", "v1"),
            category=_get_str("TOOBIT_CATEGORY", "USDT"),
            timeout_seconds=_get_int("TOOBIT_TIMEOUT_SECONDS", 12, min_value=1),
        ),
        trading=TradingConfig(
            enabled=_get_bool("REAL_TRADING_ENABLED", _get_bool("TRADE_ENABLED", False)),
            max_positions=_get_int("MAX_POSITIONS", 3, min_value=1, max_value=100),
            margin_usdt=_get_float("TRADE_MARGIN_USDT", _get_float("TRADE_DOLLAR", 5.0), min_value=0.01),
            leverage=_get_int("TRADE_LEVERAGE", _get_int("LEVERAGE", 10), min_value=1, max_value=125),
            isolated_only=_get_bool("ISOLATED_ONLY", True),
            allow_reduce_only_close=_get_bool("ALLOW_REDUCE_ONLY_CLOSE", True),
            require_tp_sl_on_entry=_get_bool("REQUIRE_TP_SL_ON_ENTRY", True),
            require_order_confirmation=_get_bool("REQUIRE_ORDER_CONFIRMATION", True),
            position_confirm_timeout_seconds=_get_int("POSITION_CONFIRM_TIMEOUT_SECONDS", 70, min_value=60),
            position_confirm_interval_seconds=_get_float("POSITION_CONFIRM_INTERVAL_SECONDS", 2.0, min_value=0.5),
            result_sync_timeout_seconds=_get_int("RESULT_SYNC_TIMEOUT_SECONDS", 70, min_value=60),
            result_sync_interval_seconds=_get_float("RESULT_SYNC_INTERVAL_SECONDS", 5.0, min_value=1.0),
            paper_mode_enabled=False,
            setup_flow_enabled=False,
        ),
        ai=AIConfig(
            enabled=_get_bool("AI_ENABLED", True),
            min_real_confidence=_get_float("AI_MIN_REAL_CONFIDENCE", 70.0, min_value=0, max_value=100),
            min_ghost_confidence=_get_float("AI_MIN_GHOST_CONFIDENCE", 40.0, min_value=0, max_value=100),
            max_real_risk=_get_float("AI_MAX_REAL_RISK", 42.0, min_value=0, max_value=100),
            technical_sensors_enabled=_get_bool("TECHNICAL_SENSORS_ENABLED", True),
            pattern_start_enabled=_get_bool("PATTERN_START_ENABLED", True),
            movement_predictor_enabled=_get_bool("MOVEMENT_PREDICTOR_ENABLED", True),
            learning_enabled=_get_bool("LEARNING_ENABLED", True),
        ),
        pattern=PatternConfig(
            enabled=_get_bool("PATTERN_START_ENABLED", True),
            history_days=_get_int("PATTERN_HISTORY_DAYS", 5, min_value=1, max_value=30),
            min_patterns_per_symbol_direction=_get_int("PATTERN_MIN_PER_DIRECTION", 10, min_value=1, max_value=100),
            min_repeats_for_importance=_get_int("PATTERN_MIN_REPEATS", 3, min_value=1, max_value=20),
            strong_match_score=_get_float("PATTERN_STRONG_MATCH_SCORE", 75.0, min_value=0, max_value=100),
            live_match_score=_get_float("PATTERN_LIVE_MATCH_SCORE", 62.0, min_value=0, max_value=100),
            pre_start_score=_get_float("PATTERN_PRE_START_SCORE", 55.0, min_value=0, max_value=100),
            max_patterns_per_symbol_direction=_get_int("PATTERN_MAX_PER_DIRECTION", 80, min_value=10, max_value=500),
        ),
        learning=LearningConfig(
            enabled=_get_bool("LEARNING_ENABLED", True),
            ghost_learning_enabled=_get_bool("GHOST_LEARNING_ENABLED", True),
            real_learning_enabled=_get_bool("REAL_LEARNING_ENABLED", True),
            movement_memory_enabled=_get_bool("MOVEMENT_MEMORY_ENABLED", True),
            pattern_learning_enabled=_get_bool("PATTERN_LEARNING_ENABLED", True),
            max_records=_get_int("LEARNING_MAX_RECORDS", 20000, min_value=100),
            real_weight=_get_float("REAL_LEARNING_WEIGHT", 1.0, min_value=0.01),
            ghost_weight=_get_float("GHOST_LEARNING_WEIGHT", 0.7, min_value=0.01),
            min_samples_for_confidence=_get_int("MIN_SAMPLES_FOR_CONFIDENCE", 10, min_value=1),
            backup_enabled=_get_bool("LEARNING_BACKUP_ENABLED", True),
            backup_interval_seconds=_get_int("LEARNING_BACKUP_INTERVAL_SECONDS", 3600, min_value=60),
        ),
        market_data=MarketDataConfig(
            okx_base_url=_get_str("OKX_BASE_URL", "https://www.okx.com").rstrip("/"),
            candle_limit=_get_int("CANDLE_LIMIT", 300, min_value=100, max_value=1000),
            request_timeout_seconds=_get_int("MARKET_DATA_TIMEOUT_SECONDS", 8, min_value=1),
            default_timeframes=_get_symbol_list("DEFAULT_TIMEFRAMES", DEFAULT_TIMEFRAMES) if False else DEFAULT_TIMEFRAMES,
            scan_symbols=_get_symbol_list("SCAN_SYMBOLS", LEVEL1_WATCHLIST),
        ),
        market_context=MarketContextConfig(
            enabled=_get_bool("MARKET_CONTEXT_ENABLED", True),
            okx_market_mode_enabled=_get_bool("OKX_MARKET_MODE_ENABLED", True),
            cache_ttl_seconds=_get_int("MARKET_CONTEXT_CACHE_TTL_SECONDS", 60, min_value=10),
            leader_symbols=MARKET_MODE_SYMBOLS,
        ),
        tp=TPConfig(
            tp2_enabled=_get_bool("TP2_ENABLED", True),
            tp1_protection_enabled=_get_bool("TP1_PROTECTION_ENABLED", True),
            move_sl_after_tp1=_get_bool("MOVE_SL_AFTER_TP1", True),
            ai_exit_enabled=_get_bool("AI_EXIT_ENABLED", True),
            min_tp1_atr_multiplier=_get_float("MIN_TP1_ATR_MULTIPLIER", 0.95, min_value=0.95),
            min_sl_atr_multiplier=_get_float("MIN_SL_ATR_MULTIPLIER", 1.25, min_value=1.25),
            tp2_atr_multiplier=_get_float("TP2_ATR_MULTIPLIER", 1.65, min_value=1.0),
            min_rr=_get_float("MIN_RR", 1.0, min_value=0.1),
            ai_exit_min_tp1_progress=_get_float("AI_EXIT_MIN_TP1_PROGRESS", 0.65, min_value=0.60, max_value=0.80),
            min_net_profit_usdt=_get_float("MIN_NET_PROFIT_USDT", 0.10, min_value=0.0),
            fee_rate_per_side=_get_float("FEE_RATE_PER_SIDE", 0.0006, min_value=0.0),
            support_resistance_adjust_enabled=_get_bool("SR_TP_SL_ADJUST_ENABLED", True),
        ),
        monitor=MonitorConfig(
            scan_interval_seconds=_get_float("SCAN_INTERVAL_SECONDS", 5.0, min_value=1.0),
            position_monitor_interval_seconds=_get_float("POSITION_MONITOR_INTERVAL_SECONDS", 2.0, min_value=0.5),
            ghost_monitor_interval_seconds=_get_float("GHOST_MONITOR_INTERVAL_SECONDS", 5.0, min_value=1.0),
            command_timeout_seconds=_get_int("COMMAND_TIMEOUT_SECONDS", 20, min_value=5),
            startup_grace_seconds=_get_int("STARTUP_GRACE_SECONDS", 5, min_value=0),
        ),
        risk=RiskConfig(
            emergency_stop_enabled=_get_bool("EMERGENCY_STOP_ENABLED", True),
            max_daily_loss_usdt=_get_float("MAX_DAILY_LOSS_USDT", 5.0, min_value=0.0),
            max_daily_loss_percent=_get_float("MAX_DAILY_LOSS_PERCENT", 20.0, min_value=0.0),
            sl_streak_limit=_get_int("SL_STREAK_LIMIT", 3, min_value=1),
            cooldown_after_sl_minutes=_get_int("COOLDOWN_AFTER_SL_MINUTES", 30, min_value=0),
            max_same_direction_positions=_get_int("MAX_SAME_DIRECTION_POSITIONS", 3, min_value=1),
            save_raw_exchange_errors=_get_bool("SAVE_RAW_EXCHANGE_ERRORS", True),
        ),
        storage=StorageConfig(
            data_dir=data_dir,
            signals_file=Path(_get_str("SIGNALS_FILE", str(data_dir / "signals.json"))),
            positions_file=Path(_get_str("POSITIONS_FILE", str(data_dir / "positions.json"))),
            ghosts_file=Path(_get_str("GHOSTS_FILE", str(data_dir / "ghosts.json"))),
            learning_file=Path(_get_str("LEARNING_FILE", str(data_dir / "learning.json"))),
            movement_memory_file=Path(_get_str("MOVEMENT_MEMORY_FILE", str(data_dir / "movement_memory.json"))),
            pattern_file=Path(_get_str("PATTERN_FILE", str(data_dir / "patterns.json"))),
            stats_file=Path(_get_str("STATS_FILE", str(data_dir / "stats.json"))),
            backups_dir=Path(_get_str("BACKUPS_DIR", str(backups_dir))),
            atomic_writes=True,
        ),
        logging=LoggingConfig(
            level=_get_str("LOG_LEVEL", "INFO").upper(),
            debug_mode=_get_bool("DEBUG_MODE", False),
            log_raw_decisions=_get_bool("LOG_RAW_DECISIONS", False),
            log_raw_toobit_errors=_get_bool("LOG_RAW_TOOBIT_ERRORS", True),
        ),
    )

    if validate:
        cfg.require_valid()
    return cfg


SETTINGS = load_config(validate=False)

# Backward-compatible top-level constants used by older files while rewrites are in progress.
BOT_TOKEN = SETTINGS.bot.token
OWNER_ID = SETTINGS.bot.owner_id

TOOBIT_API_KEY = SETTINGS.toobit.api_key
TOOBIT_API_SECRET = SETTINGS.toobit.api_secret
TOOBIT_BASE_URL = SETTINGS.toobit.base_url
TOOBIT_API_VERSION = SETTINGS.toobit.api_version

REAL_TRADING_ENABLED = SETTINGS.trading.enabled
TRADE_ENABLED = SETTINGS.trading.enabled
MAX_POSITIONS = SETTINGS.trading.max_positions
TRADE_MARGIN_USDT = SETTINGS.trading.margin_usdt
TRADE_LEVERAGE = SETTINGS.trading.leverage
LEVERAGE = SETTINGS.trading.leverage
ISOLATED_ONLY = SETTINGS.trading.isolated_only

AI_ENABLED = SETTINGS.ai.enabled
LEARNING_ENABLED = SETTINGS.learning.enabled
GHOST_ENABLED = SETTINGS.learning.ghost_learning_enabled
TP2_ENABLED = SETTINGS.tp.tp2_enabled
AI_EXIT_ENABLED = SETTINGS.tp.ai_exit_enabled

SCAN_INTERVAL_SECONDS = SETTINGS.monitor.scan_interval_seconds
POSITION_MONITOR_INTERVAL_SECONDS = SETTINGS.monitor.position_monitor_interval_seconds

OKX_BASE_URL = SETTINGS.market_data.okx_base_url
SCAN_SYMBOLS = SETTINGS.market_data.scan_symbols
WATCHLIST = LEVEL1_WATCHLIST
DEFAULT_SCAN_SYMBOLS = LEVEL1_WATCHLIST
DEFAULT_TIMEFRAMES = SETTINGS.market_data.default_timeframes

MIN_TP1_ATR_MULTIPLIER = SETTINGS.tp.min_tp1_atr_multiplier
MIN_SL_ATR_MULTIPLIER = SETTINGS.tp.min_sl_atr_multiplier
AI_EXIT_MIN_TP1_PROGRESS = SETTINGS.tp.ai_exit_min_tp1_progress

PAPER_MODE_ENABLED = False
SETUP_FLOW_ENABLED = False
