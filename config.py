from __future__ import annotations

"""
01 - config.py

Production-ready configuration layer for the locked Movement Hunter crypto futures bot.

Responsibilities:
- Read environment variables and optional .env file.
- Provide typed immutable config objects.
- Centralize feature flags and runtime limits.
- Validate startup requirements before bot launch.

Strictly forbidden in this file:
- No market analysis.
- No AI decision logic.
- No Toobit HTTP/API calls.
- No Telegram handlers.
- No persistence logic.
- No Paper mode.
- No Setup flow.

Architecture lock:
- REAL / GHOST / REJECT only.
- Toobit v2 real trading only.
- No Paper mode anywhere.
- No Setup architecture anywhere.
- TP1 / TP2 / SL supported.
- AI exit and learning flags supported.
"""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_ENV_FILE = BASE_DIR / ".env"


class ConfigError(RuntimeError):
    """Raised when required runtime configuration is missing or invalid."""


def _load_dotenv(path: Path = DEFAULT_ENV_FILE) -> None:
    """Minimal dependency-free .env loader. Existing process env values always win."""
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


def _get_list(name: str, default: Optional[List[str]] = None) -> List[str]:
    raw = _get_str(name, "")
    if not raw:
        return list(default or [])
    return [item.strip().upper() for item in raw.split(",") if item.strip()]


@dataclass(frozen=True)
class BotConfig:
    token: str
    owner_id: int
    timezone: str
    command_language: str = "fa"
    admin_only: bool = True

    def validate(self) -> List[str]:
        errors: List[str] = []
        if not self.token:
            errors.append("BOT_TOKEN is required")
        if self.owner_id <= 0:
            errors.append("OWNER_ID must be a positive integer")
        return errors


@dataclass(frozen=True)
class ToobitConfig:
    api_key: str
    api_secret: str
    base_url: str
    recv_window: int
    category: str = "USDT"
    api_version: str = "v2"
    timeout_seconds: int = 12

    def validate(self) -> List[str]:
        errors: List[str] = []
        if not self.api_key:
            errors.append("TOOBIT_API_KEY is required")
        if not self.api_secret:
            errors.append("TOOBIT_API_SECRET is required")
        if self.api_version.lower() != "v2":
            errors.append("Toobit API version must remain v2")
        if not self.base_url.startswith("http"):
            errors.append("TOOBIT_BASE_URL must be a valid URL")
        if self.timeout_seconds <= 0:
            errors.append("TOOBIT_TIMEOUT_SECONDS must be positive")
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
            errors.append("Paper mode is forbidden by architecture")
        if self.setup_flow_enabled:
            errors.append("Setup flow is forbidden by architecture")
        if self.max_positions <= 0:
            errors.append("MAX_POSITIONS must be positive")
        if self.margin_usdt <= 0:
            errors.append("TRADE_MARGIN_USDT must be positive")
        if self.leverage <= 0:
            errors.append("TRADE_LEVERAGE must be positive")
        if not self.isolated_only:
            errors.append("ISOLATED_ONLY must remain enabled for real trading safety")
        if not self.require_tp_sl_on_entry:
            errors.append("REQUIRE_TP_SL_ON_ENTRY must remain enabled")
        if self.position_confirm_timeout_seconds < 20:
            errors.append("POSITION_CONFIRM_TIMEOUT_SECONDS should be at least 20")
        if self.result_sync_timeout_seconds < 60:
            errors.append("RESULT_SYNC_TIMEOUT_SECONDS should be at least 60")
        return errors


@dataclass(frozen=True)
class AIConfig:
    enabled: bool
    min_real_confidence: float
    min_ghost_confidence: float
    max_real_risk: float
    reject_risk_threshold: float
    movement_hunter_enabled: bool
    movement_memory_enabled: bool
    movement_predictor_enabled: bool
    trap_engine_enabled: bool
    liquidity_engine_enabled: bool
    state_engine_enabled: bool
    confidence_engine_enabled: bool
    correlation_engine_enabled: bool
    rsi_slope_enabled: bool
    macd_histogram_acceleration_enabled: bool
    atr_explosion_enabled: bool
    range_suppression_enabled: bool
    exhaustion_detection_enabled: bool

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
        if not (0 <= self.reject_risk_threshold <= 100):
            errors.append("AI_REJECT_RISK_THRESHOLD must be 0..100")
        return errors


@dataclass(frozen=True)
class LearningConfig:
    enabled: bool
    meta_learning_enabled: bool
    ghost_learning_enabled: bool
    real_learning_enabled: bool
    movement_memory_enabled: bool
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
        if self.real_weight <= 0:
            errors.append("REAL_LEARNING_WEIGHT must be positive")
        if self.ghost_weight <= 0:
            errors.append("GHOST_LEARNING_WEIGHT must be positive")
        if self.real_weight < self.ghost_weight:
            errors.append("REAL_LEARNING_WEIGHT should be >= GHOST_LEARNING_WEIGHT")
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
        if self.candle_limit < 50:
            errors.append("CANDLE_LIMIT should be at least 50")
        if not self.default_timeframes:
            errors.append("DEFAULT_TIMEFRAMES cannot be empty")
        if not self.scan_symbols:
            errors.append("SCAN_SYMBOLS cannot be empty")
        return errors


@dataclass(frozen=True)
class MarketContextConfig:
    fear_greed_enabled: bool
    altseason_enabled: bool
    btc_dominance_enabled: bool
    market_breadth_enabled: bool
    cache_ttl_seconds: int

    def validate(self) -> List[str]:
        if self.cache_ttl_seconds <= 0:
            return ["MARKET_CONTEXT_CACHE_TTL_SECONDS must be positive"]
        return []


@dataclass(frozen=True)
class TPConfig:
    tp2_enabled: bool
    tp2_requires_strong_signal: bool
    tp1_protection_enabled: bool
    move_sl_after_tp1: bool
    ai_exit_enabled: bool
    ai_exit_confirmation_required: bool
    breakout_survival_enabled: bool
    retest_tolerance_enabled: bool
    coin_noise_learning_enabled: bool
    min_rr: float
    max_sl_atr_multiplier: float
    min_sl_atr_multiplier: float

    def validate(self) -> List[str]:
        errors: List[str] = []
        if self.min_rr <= 0:
            errors.append("MIN_RR must be positive")
        if self.min_sl_atr_multiplier <= 0:
            errors.append("MIN_SL_ATR_MULTIPLIER must be positive")
        if self.max_sl_atr_multiplier < self.min_sl_atr_multiplier:
            errors.append("MAX_SL_ATR_MULTIPLIER must be >= MIN_SL_ATR_MULTIPLIER")
        if not self.ai_exit_confirmation_required:
            errors.append("AI exit confirmation should remain enabled")
        return errors


@dataclass(frozen=True)
class MonitorConfig:
    scan_interval_seconds: int
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
        if self.position_monitor_interval_seconds > 10:
            errors.append("POSITION_MONITOR_INTERVAL_SECONDS should be <= 10 for real-time monitoring")
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
    max_same_correlation_group: int
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
        if self.max_same_correlation_group <= 0:
            errors.append("MAX_SAME_CORRELATION_GROUP must be positive")
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
    stats_file: Path
    meta_learning_file: Path
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
    learning: LearningConfig
    market_data: MarketDataConfig
    market_context: MarketContextConfig
    tp: TPConfig
    monitor: MonitorConfig
    risk: RiskConfig
    storage: StorageConfig
    logging: LoggingConfig

    def validate(self) -> List[str]:
        errors: List[str] = []
        for section in (
            self.bot, self.toobit, self.trading, self.ai, self.learning,
            self.market_data, self.market_context, self.tp, self.monitor,
            self.risk, self.storage, self.logging,
        ):
            errors.extend(section.validate())
        return errors

    def require_valid(self) -> None:
        errors = self.validate()
        if errors:
            joined = "\n- " + "\n- ".join(errors)
            raise ConfigError(f"Invalid configuration:{joined}")

    def as_safe_dict(self) -> Dict[str, Any]:
        """Safe diagnostic view without secrets."""
        return {
            "bot": {"owner_id": self.bot.owner_id, "timezone": self.bot.timezone, "admin_only": self.bot.admin_only},
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
                "paper_mode_enabled": self.trading.paper_mode_enabled,
                "setup_flow_enabled": self.trading.setup_flow_enabled,
            },
            "ai": {
                "enabled": self.ai.enabled,
                "min_real_confidence": self.ai.min_real_confidence,
                "min_ghost_confidence": self.ai.min_ghost_confidence,
                "max_real_risk": self.ai.max_real_risk,
            },
            "learning": {
                "enabled": self.learning.enabled,
                "meta_learning_enabled": self.learning.meta_learning_enabled,
                "max_records": self.learning.max_records,
            },
            "monitor": {
                "scan_interval_seconds": self.monitor.scan_interval_seconds,
                "position_monitor_interval_seconds": self.monitor.position_monitor_interval_seconds,
                "ghost_monitor_interval_seconds": self.monitor.ghost_monitor_interval_seconds,
            },
            "risk": {"emergency_stop_enabled": self.risk.emergency_stop_enabled, "sl_streak_limit": self.risk.sl_streak_limit},
        }


DEFAULT_SCAN_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
    "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "MATICUSDT",
    "TRXUSDT", "DOTUSDT", "LTCUSDT", "ATOMUSDT", "NEARUSDT",
    "ARBUSDT", "OPUSDT", "FILUSDT", "APTUSDT", "SUIUSDT",
    "1000SHIBUSDT", "1000PEPEUSDT", "1000FLOKIUSDT",
]

DEFAULT_TIMEFRAMES = ["5m", "15m", "30m", "1h", "4h"]


def load_config(validate: bool = False, env_file: Path = DEFAULT_ENV_FILE) -> AppConfig:
    """
    Build AppConfig from environment variables.

    Importing config.py should not fail because secrets are missing.
    The application entrypoint should call:
        SETTINGS.require_valid()
    before starting live services.
    """
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
            category=_get_str("TOOBIT_CATEGORY", "USDT"),
            api_version="v2",
            timeout_seconds=_get_int("TOOBIT_TIMEOUT_SECONDS", 12, min_value=1),
        ),
        trading=TradingConfig(
            enabled=_get_bool("REAL_TRADING_ENABLED", _get_bool("TRADE_ENABLED", False)),
            max_positions=_get_int("MAX_POSITIONS", 5, min_value=1, max_value=100),
            margin_usdt=_get_float("TRADE_MARGIN_USDT", _get_float("TRADE_DOLLAR", 5.0), min_value=0.01),
            leverage=_get_int("TRADE_LEVERAGE", _get_int("LEVERAGE", 10), min_value=1, max_value=125),
            isolated_only=_get_bool("ISOLATED_ONLY", True),
            allow_reduce_only_close=_get_bool("ALLOW_REDUCE_ONLY_CLOSE", True),
            require_tp_sl_on_entry=_get_bool("REQUIRE_TP_SL_ON_ENTRY", True),
            require_order_confirmation=_get_bool("REQUIRE_ORDER_CONFIRMATION", True),
            position_confirm_timeout_seconds=_get_int("POSITION_CONFIRM_TIMEOUT_SECONDS", 70, min_value=20),
            position_confirm_interval_seconds=_get_float("POSITION_CONFIRM_INTERVAL_SECONDS", 2.0, min_value=0.5),
            result_sync_timeout_seconds=_get_int("RESULT_SYNC_TIMEOUT_SECONDS", 70, min_value=60),
            result_sync_interval_seconds=_get_float("RESULT_SYNC_INTERVAL_SECONDS", 5.0, min_value=1.0),
            paper_mode_enabled=False,
            setup_flow_enabled=False,
        ),
        ai=AIConfig(
            enabled=_get_bool("AI_ENABLED", True),
            min_real_confidence=_get_float("AI_MIN_REAL_CONFIDENCE", 78.0, min_value=0, max_value=100),
            min_ghost_confidence=_get_float("AI_MIN_GHOST_CONFIDENCE", 55.0, min_value=0, max_value=100),
            max_real_risk=_get_float("AI_MAX_REAL_RISK", 35.0, min_value=0, max_value=100),
            reject_risk_threshold=_get_float("AI_REJECT_RISK_THRESHOLD", 75.0, min_value=0, max_value=100),
            movement_hunter_enabled=_get_bool("MOVEMENT_HUNTER_ENABLED", True),
            movement_memory_enabled=_get_bool("MOVEMENT_MEMORY_ENABLED", True),
            movement_predictor_enabled=_get_bool("MOVEMENT_PREDICTOR_ENABLED", True),
            trap_engine_enabled=_get_bool("TRAP_ENGINE_ENABLED", True),
            liquidity_engine_enabled=_get_bool("LIQUIDITY_ENGINE_ENABLED", True),
            state_engine_enabled=_get_bool("STATE_ENGINE_ENABLED", True),
            confidence_engine_enabled=_get_bool("CONFIDENCE_ENGINE_ENABLED", True),
            correlation_engine_enabled=_get_bool("CORRELATION_ENGINE_ENABLED", True),
            rsi_slope_enabled=_get_bool("RSI_SLOPE_ENABLED", True),
            macd_histogram_acceleration_enabled=_get_bool("MACD_HISTOGRAM_ACCELERATION_ENABLED", True),
            atr_explosion_enabled=_get_bool("ATR_EXPLOSION_ENABLED", True),
            range_suppression_enabled=_get_bool("RANGE_SUPPRESSION_ENABLED", True),
            exhaustion_detection_enabled=_get_bool("EXHAUSTION_DETECTION_ENABLED", True),
        ),
        learning=LearningConfig(
            enabled=_get_bool("LEARNING_ENABLED", True),
            meta_learning_enabled=_get_bool("META_LEARNING_ENABLED", True),
            ghost_learning_enabled=_get_bool("GHOST_LEARNING_ENABLED", True),
            real_learning_enabled=_get_bool("REAL_LEARNING_ENABLED", True),
            movement_memory_enabled=_get_bool("MOVEMENT_MEMORY_ENABLED", True),
            max_records=_get_int("LEARNING_MAX_RECORDS", 20000, min_value=100),
            real_weight=_get_float("REAL_LEARNING_WEIGHT", 1.0, min_value=0.01),
            ghost_weight=_get_float("GHOST_LEARNING_WEIGHT", 0.7, min_value=0.01),
            min_samples_for_confidence=_get_int("MIN_SAMPLES_FOR_CONFIDENCE", 10, min_value=1),
            backup_enabled=_get_bool("LEARNING_BACKUP_ENABLED", True),
            backup_interval_seconds=_get_int("LEARNING_BACKUP_INTERVAL_SECONDS", 3600, min_value=60),
        ),
        market_data=MarketDataConfig(
            okx_base_url=_get_str("OKX_BASE_URL", "https://www.okx.com").rstrip("/"),
            candle_limit=_get_int("CANDLE_LIMIT", 200, min_value=50, max_value=500),
            request_timeout_seconds=_get_int("MARKET_DATA_TIMEOUT_SECONDS", 10, min_value=1),
            default_timeframes=_get_list("DEFAULT_TIMEFRAMES", DEFAULT_TIMEFRAMES),
            scan_symbols=_get_list("SCAN_SYMBOLS", DEFAULT_SCAN_SYMBOLS),
        ),
        market_context=MarketContextConfig(
            fear_greed_enabled=_get_bool("FEAR_GREED_ENABLED", True),
            altseason_enabled=_get_bool("ALTSEASON_ENABLED", True),
            btc_dominance_enabled=_get_bool("BTC_DOMINANCE_ENABLED", True),
            market_breadth_enabled=_get_bool("MARKET_BREADTH_ENABLED", True),
            cache_ttl_seconds=_get_int("MARKET_CONTEXT_CACHE_TTL_SECONDS", 300, min_value=30),
        ),
        tp=TPConfig(
            tp2_enabled=_get_bool("TP2_ENABLED", True),
            tp2_requires_strong_signal=_get_bool("TP2_REQUIRES_STRONG_SIGNAL", True),
            tp1_protection_enabled=_get_bool("TP1_PROTECTION_ENABLED", True),
            move_sl_after_tp1=_get_bool("MOVE_SL_AFTER_TP1", True),
            ai_exit_enabled=_get_bool("AI_EXIT_ENABLED", True),
            ai_exit_confirmation_required=_get_bool("AI_EXIT_CONFIRMATION_REQUIRED", True),
            breakout_survival_enabled=_get_bool("BREAKOUT_SURVIVAL_ENABLED", True),
            retest_tolerance_enabled=_get_bool("RETEST_TOLERANCE_ENABLED", True),
            coin_noise_learning_enabled=_get_bool("COIN_NOISE_LEARNING_ENABLED", True),
            min_rr=_get_float("MIN_RR", 1.1, min_value=0.1),
            max_sl_atr_multiplier=_get_float("MAX_SL_ATR_MULTIPLIER", 2.6, min_value=0.1),
            min_sl_atr_multiplier=_get_float("MIN_SL_ATR_MULTIPLIER", 1.0, min_value=0.1),
        ),
        monitor=MonitorConfig(
            scan_interval_seconds=_get_int("SCAN_INTERVAL_SECONDS", 180, min_value=30),
            position_monitor_interval_seconds=_get_float("POSITION_MONITOR_INTERVAL_SECONDS", 2.0, min_value=0.5),
            ghost_monitor_interval_seconds=_get_float("GHOST_MONITOR_INTERVAL_SECONDS", 10.0, min_value=1.0),
            command_timeout_seconds=_get_int("COMMAND_TIMEOUT_SECONDS", 20, min_value=5),
            startup_grace_seconds=_get_int("STARTUP_GRACE_SECONDS", 5, min_value=0),
        ),
        risk=RiskConfig(
            emergency_stop_enabled=_get_bool("EMERGENCY_STOP_ENABLED", True),
            max_daily_loss_usdt=_get_float("MAX_DAILY_LOSS_USDT", 5.0, min_value=0.0),
            max_daily_loss_percent=_get_float("MAX_DAILY_LOSS_PERCENT", 20.0, min_value=0.0),
            sl_streak_limit=_get_int("SL_STREAK_LIMIT", 3, min_value=1),
            cooldown_after_sl_minutes=_get_int("COOLDOWN_AFTER_SL_MINUTES", 30, min_value=0),
            max_same_correlation_group=_get_int("MAX_SAME_CORRELATION_GROUP", 2, min_value=1),
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
            stats_file=Path(_get_str("STATS_FILE", str(data_dir / "stats.json"))),
            meta_learning_file=Path(_get_str("META_LEARNING_FILE", str(data_dir / "meta_learning.json"))),
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

BOT_TOKEN = SETTINGS.bot.token
OWNER_ID = SETTINGS.bot.owner_id

TOOBIT_API_KEY = SETTINGS.toobit.api_key
TOOBIT_API_SECRET = SETTINGS.toobit.api_secret
TOOBIT_BASE_URL = SETTINGS.toobit.base_url

REAL_TRADING_ENABLED = SETTINGS.trading.enabled
TRADE_ENABLED = SETTINGS.trading.enabled
MAX_POSITIONS = SETTINGS.trading.max_positions
TRADE_MARGIN_USDT = SETTINGS.trading.margin_usdt
TRADE_LEVERAGE = SETTINGS.trading.leverage
LEVERAGE = SETTINGS.trading.leverage
ISOLATED_ONLY = SETTINGS.trading.isolated_only

AI_ENABLED = SETTINGS.ai.enabled
LEARNING_ENABLED = SETTINGS.learning.enabled
META_LEARNING_ENABLED = SETTINGS.learning.meta_learning_enabled

GHOST_ENABLED = SETTINGS.learning.ghost_learning_enabled
TP2_ENABLED = SETTINGS.tp.tp2_enabled
AI_EXIT_ENABLED = SETTINGS.tp.ai_exit_enabled

SCAN_INTERVAL_SECONDS = SETTINGS.monitor.scan_interval_seconds
POSITION_MONITOR_INTERVAL_SECONDS = SETTINGS.monitor.position_monitor_interval_seconds

OKX_BASE_URL = SETTINGS.market_data.okx_base_url
SCAN_SYMBOLS = SETTINGS.market_data.scan_symbols
DEFAULT_TIMEFRAMES = SETTINGS.market_data.default_timeframes

PAPER_MODE_ENABLED = False
SETUP_FLOW_ENABLED = False
