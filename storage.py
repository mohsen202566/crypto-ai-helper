from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from config import (
    DB_PATH, DEFAULT_LEVERAGE, DEFAULT_MARGIN_USDT, DEFAULT_MAX_POSITIONS, DEFAULT_MIN_PROFIT_PCT,
    DEFAULT_MIN_PROFIT_USDT, DEFAULT_TRADE_ENABLED, LEARNING_DAYS, LEVERAGE_MAX, LEVERAGE_MIN,
    MARGIN_MAX_USDT, MARGIN_MIN_USDT, MAX_POSITIONS_MAX, MAX_POSITIONS_MIN,
    MIN_REAL_NET_PROFIT_USDT, AI_REENTRY_COOLDOWN_SECONDS, AI_REENTRY_MIN_RESET_ATR, AI_REENTRY_REQUIRE_NEW_SETUP,
)
from scorer import SignalDecision


@dataclass(frozen=True)
class StoredSignal:
    id: int
    created_at: str
    okx_symbol: str
    toobit_symbol: str
    symbol_name: str
    direction: str
    entry: float
    tp: float
    sl: float
    score: int
    ai_confidence: int
    ai_experience: int
    signal_type: str
    hunter_type: str
    status: str
    real_status: str
    message_id: int | None
    result_message_id: int | None
    order_id: str | None
    approx_pnl: float | None
    real_pnl: float | None
    margin_usdt: float
    leverage: int
    net_edge: float
    estimated_profit_usdt: float
    estimated_net_profit_usdt: float
    estimated_profit_pct: float
    risk_reward: float
    reason: str | None
    result_source: str | None = None
    exit_price: float | None = None
    ai_exit_reason: str | None = None
    ai_exit_status: str | None = None
    ai_exit_score: int = 0
    target_zone_reached: bool = False
    giveback_pct: float = 0.0
    entry_quality: str | None = None
    indicator_profile: str | None = None
    market_mode: str | None = None
    atr_pct_15m: float = 0.0
    best_price: float | None = None
    worst_price: float | None = None
    mfe_pct: float = 0.0
    mae_pct: float = 0.0


class Storage:
    def __init__(self, db_path: str = DB_PATH) -> None:
        self.db_path = db_path
        directory = os.path.dirname(db_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    okx_symbol TEXT NOT NULL,
                    toobit_symbol TEXT NOT NULL,
                    symbol_name TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    entry REAL NOT NULL,
                    tp REAL NOT NULL,
                    sl REAL NOT NULL,
                    score INTEGER NOT NULL,
                    threshold INTEGER NOT NULL,
                    real_threshold INTEGER DEFAULT 76,
                    threshold_source TEXT DEFAULT 'BOOT',
                    ai_confidence INTEGER DEFAULT 0,
                    ai_experience INTEGER DEFAULT 0,
                    ai_adjustment INTEGER DEFAULT 0,
                    ai_effect TEXT DEFAULT 'NEUTRAL',
                    signal_type TEXT NOT NULL,
                    hunter_type TEXT NOT NULL DEFAULT 'ordinary',
                    signal_label TEXT NOT NULL DEFAULT 'عادی',
                    status TEXT NOT NULL DEFAULT 'OPEN',
                    real_status TEXT NOT NULL DEFAULT 'none',
                    real_allowed INTEGER DEFAULT 0,
                    real_block_reason TEXT,
                    message_id INTEGER,
                    result_message_id INTEGER,
                    real_opened INTEGER NOT NULL DEFAULT 0,
                    order_id TEXT,
                    approx_pnl REAL,
                    real_pnl REAL,
                    result_source TEXT,
                    exit_price REAL,
                    ai_exit_reason TEXT,
                    ai_exit_status TEXT,
                    ai_exit_score INTEGER DEFAULT 0,
                    target_zone_reached INTEGER DEFAULT 0,
                    giveback_pct REAL DEFAULT 0,
                    margin_usdt REAL,
                    leverage INTEGER,
                    result_at TEXT,
                    score_direction INTEGER DEFAULT 0,
                    score_pre_ignition INTEGER DEFAULT 0,
                    score_candle_entry INTEGER DEFAULT 0,
                    score_entry_precision INTEGER DEFAULT 0,
                    score_ai_memory INTEGER DEFAULT 0,
                    score_tp_sl INTEGER DEFAULT 0,
                    score_market_mode INTEGER DEFAULT 0,
                    score_session INTEGER DEFAULT 0,
                    score_net_sync INTEGER DEFAULT 0,
                    direction_state_1h TEXT,
                    direction_confidence_1h INTEGER DEFAULT 0,
                    bias_4h TEXT,
                    setup_15m TEXT,
                    entry_5m TEXT,
                    entry_quality TEXT,
                    entry_precision_pct REAL DEFAULT 0,
                    technical_zone TEXT,
                    indicator_profile TEXT,
                    pattern_id TEXT,
                    candle_pattern TEXT,
                    net_edge REAL DEFAULT 0,
                    estimated_profit_usdt REAL DEFAULT 0,
                    estimated_net_profit_usdt REAL DEFAULT 0,
                    estimated_profit_pct REAL DEFAULT 0,
                    risk_reward REAL DEFAULT 0,
                    estimated_cost_pct REAL DEFAULT 0,
                    market_bias TEXT,
                    market_mode TEXT,
                    session_state TEXT,
                    order_block_state TEXT,
                    session_bucket TEXT,
                    reason TEXT,
                    notes TEXT,
                    real_open_reason TEXT,
                    actual_margin_usdt REAL,
                    quantity REAL,
                    mfe_pct REAL DEFAULT 0,
                    mae_pct REAL DEFAULT 0,
                    best_price REAL,
                    worst_price REAL,
                    rsi_5m REAL DEFAULT 0,
                    rsi_15m REAL DEFAULT 0,
                    macd_hist_5m REAL DEFAULT 0,
                    macd_hist_15m REAL DEFAULT 0,
                    adx_15m REAL DEFAULT 0,
                    atr_pct_15m REAL DEFAULT 0,
                    volume_ratio_5m REAL DEFAULT 0,
                    volume_ratio_15m REAL DEFAULT 0,
                    result_5m TEXT,
                    result_10m TEXT,
                    result_15m TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS watchlist (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol_name TEXT NOT NULL,
                    okx_symbol TEXT NOT NULL,
                    toobit_symbol TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    score INTEGER NOT NULL,
                    ai_confidence INTEGER DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    last_ready_alert_at TEXT,
                    UNIQUE(symbol_name, direction)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS symbol_health (
                    symbol_name TEXT PRIMARY KEY,
                    okx_error_count INTEGER DEFAULT 0,
                    toobit_error_count INTEGER DEFAULT 0,
                    okx_disabled_until TEXT,
                    toobit_real_disabled_until TEXT,
                    last_okx_error TEXT,
                    last_toobit_error TEXT,
                    updated_at TEXT
                )
                """
            )
            conn.execute("CREATE TABLE IF NOT EXISTS rejection_log (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, symbol_name TEXT NOT NULL, direction TEXT, code TEXT, reason TEXT, score INTEGER DEFAULT 0)")
            conn.execute("CREATE TABLE IF NOT EXISTS ai_symbol_direction_profiles (symbol_name TEXT NOT NULL, direction TEXT NOT NULL, total_signals INTEGER DEFAULT 0, tp INTEGER DEFAULT 0, sl INTEGER DEFAULT 0, exit_count INTEGER DEFAULT 0, win_rate REAL DEFAULT 0, net_profit REAL DEFAULT 0, avg_mfe REAL DEFAULT 0, avg_mae REAL DEFAULT 0, best_tp_range REAL DEFAULT 0, best_sl_range REAL DEFAULT 0, noise_level REAL DEFAULT 0, early_entry_success INTEGER DEFAULT 0, reversal_after_entry INTEGER DEFAULT 0, consecutive_sl INTEGER DEFAULT 0, sensor_weights TEXT, risk_level TEXT DEFAULT 'normal', learning_confidence INTEGER DEFAULT 0, signal_threshold INTEGER DEFAULT 68, real_threshold INTEGER DEFAULT 76, threshold_source TEXT DEFAULT 'BOOT', last_updated TEXT, PRIMARY KEY(symbol_name, direction))")
            conn.execute("CREATE TABLE IF NOT EXISTS ai_patterns (symbol_name TEXT NOT NULL, direction TEXT NOT NULL, pattern_id TEXT NOT NULL, total INTEGER DEFAULT 0, tp INTEGER DEFAULT 0, sl INTEGER DEFAULT 0, win_rate REAL DEFAULT 0, avg_mfe REAL DEFAULT 0, avg_mae REAL DEFAULT 0, weight REAL DEFAULT 1, verdict TEXT DEFAULT 'NEUTRAL', last_updated TEXT, PRIMARY KEY(symbol_name, direction, pattern_id))")
            conn.execute("CREATE TABLE IF NOT EXISTS ai_pattern_results (id INTEGER PRIMARY KEY AUTOINCREMENT, signal_id INTEGER, symbol_name TEXT, direction TEXT, pattern_id TEXT, status TEXT, mfe_pct REAL, mae_pct REAL, created_at TEXT)")
            conn.execute("CREATE TABLE IF NOT EXISTS ai_sensor_weights (symbol_name TEXT NOT NULL, direction TEXT NOT NULL, sensor TEXT NOT NULL, weight REAL DEFAULT 1, updated_at TEXT, PRIMARY KEY(symbol_name, direction, sensor))")
            conn.execute("CREATE TABLE IF NOT EXISTS ai_shadow_tests (id INTEGER PRIMARY KEY AUTOINCREMENT, signal_id INTEGER NOT NULL, plan_name TEXT NOT NULL, tp REAL NOT NULL, sl REAL NOT NULL, result TEXT DEFAULT 'pending', created_at TEXT NOT NULL, updated_at TEXT)")
            conn.execute("CREATE TABLE IF NOT EXISTS ai_judgements (id INTEGER PRIMARY KEY AUTOINCREMENT, signal_id INTEGER NOT NULL, entry_quality TEXT, tp_quality TEXT, sl_quality TEXT, failure_reason TEXT, score_delta INTEGER, reasons TEXT, created_at TEXT NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS ai_second_snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT, signal_id INTEGER NOT NULL, created_at TEXT NOT NULL, price REAL NOT NULL, mfe_pct REAL DEFAULT 0, mae_pct REAL DEFAULT 0)")
            conn.execute("CREATE TABLE IF NOT EXISTS ai_exit_events (id INTEGER PRIMARY KEY AUTOINCREMENT, signal_id INTEGER NOT NULL, created_at TEXT NOT NULL, status TEXT, price REAL, profit_pct REAL DEFAULT 0, mfe_pct REAL DEFAULT 0, mae_pct REAL DEFAULT 0, giveback_pct REAL DEFAULT 0, exit_score INTEGER DEFAULT 0, reason TEXT)")
            conn.execute("CREATE TABLE IF NOT EXISTS ai_suggestions (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, level TEXT, message TEXT, status TEXT DEFAULT 'open', suggestion_key TEXT, updated_at TEXT)")
            conn.execute("CREATE TABLE IF NOT EXISTS toobit_orders (id INTEGER PRIMARY KEY AUTOINCREMENT, signal_id INTEGER, symbol TEXT, action TEXT, order_id TEXT, status TEXT, reason TEXT, created_at TEXT)")
            self._migrate_columns(conn)
            self._set_default(conn, "trade_enabled", "1" if DEFAULT_TRADE_ENABLED else "0")
            self._set_default(conn, "margin_usdt", str(DEFAULT_MARGIN_USDT))
            self._set_default(conn, "leverage", str(DEFAULT_LEVERAGE))
            self._set_default(conn, "max_positions", str(DEFAULT_MAX_POSITIONS))
            self._set_default(conn, "min_profit_usdt", str(DEFAULT_MIN_PROFIT_USDT))
            self._set_default(conn, "min_profit_pct", str(DEFAULT_MIN_PROFIT_PCT))

    def _migrate_columns(self, conn: sqlite3.Connection) -> None:
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(signals)").fetchall()}
        columns = {
            "real_allowed": "INTEGER DEFAULT 0", "real_block_reason": "TEXT", "real_threshold": "INTEGER DEFAULT 76", "threshold_source": "TEXT DEFAULT 'BOOT'", "score_entry_precision": "INTEGER DEFAULT 0", "score_tp_sl": "INTEGER DEFAULT 0",
            "score_market_mode": "INTEGER DEFAULT 0", "score_net_sync": "INTEGER DEFAULT 0", "entry_precision_pct": "REAL DEFAULT 0", "pattern_id": "TEXT",
            "estimated_net_profit_usdt": "REAL DEFAULT 0", "market_mode": "TEXT", "best_price": "REAL", "worst_price": "REAL",
            "exit_price": "REAL", "ai_exit_reason": "TEXT", "ai_exit_status": "TEXT", "ai_exit_score": "INTEGER DEFAULT 0",
            "target_zone_reached": "INTEGER DEFAULT 0", "giveback_pct": "REAL DEFAULT 0",
        }
        for name, spec in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE signals ADD COLUMN {name} {spec}")
        profile_existing = {row["name"] for row in conn.execute("PRAGMA table_info(ai_symbol_direction_profiles)").fetchall()}
        profile_columns = {
            "signal_threshold": "INTEGER DEFAULT 68",
            "real_threshold": "INTEGER DEFAULT 76",
            "threshold_source": "TEXT DEFAULT 'BOOT'",
        }
        for name, spec in profile_columns.items():
            if name not in profile_existing:
                conn.execute(f"ALTER TABLE ai_symbol_direction_profiles ADD COLUMN {name} {spec}")
        suggestion_existing = {row["name"] for row in conn.execute("PRAGMA table_info(ai_suggestions)").fetchall()}
        suggestion_columns = {
            "suggestion_key": "TEXT",
            "updated_at": "TEXT",
        }
        for name, spec in suggestion_columns.items():
            if name not in suggestion_existing:
                conn.execute(f"ALTER TABLE ai_suggestions ADD COLUMN {name} {spec}")

    def _set_default(self, conn: sqlite3.Connection, key: str, value: str) -> None:
        conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)", (key, value))

    def _get_setting(self, key: str, default: str) -> str:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return str(row["value"]) if row else default

    def _set_setting(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute("INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def trade_enabled(self) -> bool:
        return self._get_setting("trade_enabled", "0") == "1"

    def set_trade_enabled(self, enabled: bool) -> None:
        self._set_setting("trade_enabled", "1" if enabled else "0")

    def margin_usdt(self) -> float:
        return float(self._get_setting("margin_usdt", str(DEFAULT_MARGIN_USDT)))

    def set_margin_usdt(self, value: float) -> None:
        if not MARGIN_MIN_USDT <= value <= MARGIN_MAX_USDT:
            raise ValueError("دلار ترید باید بین 1 تا 10000 باشد.")
        self._set_setting("margin_usdt", str(float(value)))

    def leverage(self) -> int:
        return int(float(self._get_setting("leverage", str(DEFAULT_LEVERAGE))))

    def set_leverage(self, value: int) -> None:
        if not LEVERAGE_MIN <= value <= LEVERAGE_MAX:
            raise ValueError("لوریج باید بین 1 تا 100 باشد.")
        self._set_setting("leverage", str(int(value)))

    def max_positions(self) -> int:
        return int(float(self._get_setting("max_positions", str(DEFAULT_MAX_POSITIONS))))

    def set_max_positions(self, value: int) -> None:
        if not MAX_POSITIONS_MIN <= value <= MAX_POSITIONS_MAX:
            raise ValueError("حداکثر پوزیشن باید بین 1 تا 100 باشد.")
        self._set_setting("max_positions", str(int(value)))

    def add_signal(self, *, okx_symbol: str, toobit_symbol: str, symbol_name: str, decision: SignalDecision, signal_type: str, real_status: str = "none", signal_label: str | None = None) -> int:
        if decision.direction is None:
            raise ValueError("جهت سیگنال مشخص نیست.")
        now = datetime.now(timezone.utc)
        bucket = f"{now.hour:02d}:{0 if now.minute < 30 else 30:02d}"
        notes = " | ".join(decision.notes[:32])
        label = signal_label or decision.signal_label
        hunter_type = "hunter" if decision.hunter else "ordinary"
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO signals(
                    created_at, okx_symbol, toobit_symbol, symbol_name, direction, entry, tp, sl, score, threshold, real_threshold, threshold_source,
                    ai_confidence, ai_experience, ai_adjustment, ai_effect, signal_type, hunter_type, signal_label,
                    status, real_status, real_allowed, real_block_reason, margin_usdt, leverage,
                    score_direction, score_pre_ignition, score_candle_entry, score_entry_precision, score_ai_memory,
                    score_tp_sl, score_market_mode, score_session, score_net_sync, direction_state_1h, direction_confidence_1h,
                    bias_4h, setup_15m, entry_5m, entry_quality, entry_precision_pct, technical_zone, indicator_profile,
                    pattern_id, candle_pattern, net_edge, estimated_profit_usdt, estimated_net_profit_usdt, estimated_profit_pct,
                    risk_reward, estimated_cost_pct, market_bias, market_mode, session_state, order_block_state, session_bucket,
                    reason, notes, best_price, worst_price, rsi_5m, rsi_15m, macd_hist_5m, macd_hist_15m, adx_15m,
                    atr_pct_15m, volume_ratio_5m, volume_ratio_15m
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now.isoformat(), okx_symbol, toobit_symbol, symbol_name, decision.direction, decision.entry, decision.tp, decision.sl,
                    decision.score, decision.threshold, decision.real_threshold, decision.threshold_source, decision.ai_confidence, decision.ai_experience, decision.ai_adjustment, decision.ai_effect,
                    signal_type, hunter_type, label, real_status, 1 if decision.real_allowed else 0, decision.real_block_reason,
                    self.margin_usdt(), self.leverage(), decision.breakdown.score_direction, decision.breakdown.score_pre_ignition,
                    decision.breakdown.score_candle_entry, decision.breakdown.score_entry_precision, decision.breakdown.score_ai_memory,
                    decision.breakdown.score_tp_sl, decision.breakdown.score_market_mode, decision.breakdown.score_session, decision.breakdown.score_net_sync,
                    decision.direction_state_1h, decision.direction_confidence_1h, decision.bias_4h, decision.setup_15m, decision.entry_5m,
                    decision.entry_quality, decision.entry_precision_pct, decision.technical_zone, decision.indicator_profile, decision.pattern_id,
                    decision.candle_pattern, decision.net_edge, decision.estimated_profit_usdt, decision.estimated_net_profit_usdt,
                    decision.estimated_profit_pct, decision.risk_reward, decision.estimated_cost_pct, decision.market_bias, decision.market_mode,
                    decision.session_state, decision.order_block_state, bucket, decision.reason, notes, decision.entry, decision.entry,
                    decision.rsi_5m, decision.rsi_15m, decision.macd_hist_5m, decision.macd_hist_15m, decision.adx_15m, decision.atr_pct_15m,
                    decision.volume_ratio_5m, decision.volume_ratio_15m,
                ),
            )
            return int(cur.lastrowid)

    def update_message_id(self, signal_id: int, message_id: int | None) -> None:
        if message_id is None:
            return
        with self._connect() as conn:
            conn.execute("UPDATE signals SET message_id=? WHERE id=?", (int(message_id), signal_id))

    def mark_real_opening(self, signal_id: int) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE signals SET real_status='opening' WHERE id=? AND status='OPEN'", (signal_id,))

    def mark_real_open_result(self, signal_id: int, *, opened: bool, order_id: str | None, reason: str, actual_margin_usdt: float | None = None, quantity: float | None = None) -> None:
        with self._connect() as conn:
            if opened:
                conn.execute("UPDATE signals SET real_status='opened', real_opened=1, order_id=?, real_open_reason=?, actual_margin_usdt=?, quantity=? WHERE id=? AND status='OPEN'", (order_id, reason, actual_margin_usdt, quantity, signal_id))
                conn.execute("INSERT INTO toobit_orders(signal_id, symbol, action, order_id, status, reason, created_at) SELECT id, toobit_symbol, 'open', ?, 'opened', ?, ? FROM signals WHERE id=?", (order_id, reason, datetime.now(timezone.utc).isoformat(), signal_id))
            else:
                conn.execute("UPDATE signals SET signal_type='real_failed', status='FAILED', real_status='failed', real_opened=0, order_id=NULL, real_open_reason=?, result_at=? WHERE id=? AND status='OPEN'", (reason, datetime.now(timezone.utc).isoformat(), signal_id))

    def mark_real_close(self, signal_id: int, *, order_id: str | None, reason: str) -> None:
        with self._connect() as conn:
            conn.execute("INSERT INTO toobit_orders(signal_id, symbol, action, order_id, status, reason, created_at) SELECT id, toobit_symbol, 'close', ?, 'sent', ?, ? FROM signals WHERE id=?", (order_id, reason, datetime.now(timezone.utc).isoformat(), signal_id))

    def update_signal_excursions(self, signal_id: int, price: float) -> tuple[float, float]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM signals WHERE id=?", (signal_id,)).fetchone()
            if not row:
                return 0.0, 0.0
            entry = float(row["entry"])
            best = float(row["best_price"] or entry)
            worst = float(row["worst_price"] or entry)
            direction = str(row["direction"])
            if direction == "LONG":
                best = max(best, price)
                worst = min(worst, price)
                mfe = max(0.0, (best - entry) / entry)
                mae = max(0.0, (entry - worst) / entry)
            else:
                best = min(best, price)
                worst = max(worst, price)
                mfe = max(0.0, (entry - best) / entry)
                mae = max(0.0, (worst - entry) / entry)
            conn.execute("UPDATE signals SET best_price=?, worst_price=?, mfe_pct=?, mae_pct=? WHERE id=?", (best, worst, mfe, mae, signal_id))
            conn.execute("INSERT INTO ai_second_snapshots(signal_id, created_at, price, mfe_pct, mae_pct) VALUES(?, ?, ?, ?, ?)", (signal_id, datetime.now(timezone.utc).isoformat(), price, mfe, mae))
            return mfe, mae

    def recent_second_snapshots(self, signal_id: int, limit: int = 48) -> list[dict[str, Any]]:
        limit = max(2, min(int(limit), 240))
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT created_at, price, mfe_pct, mae_pct FROM ai_second_snapshots WHERE signal_id=? ORDER BY id DESC LIMIT ?",
                (signal_id, limit),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def record_ai_exit_event(self, signal_id: int, *, status: str, price: float, reason: str | None, profit_pct: float, mfe_pct: float, mae_pct: float, giveback_pct: float, exit_score: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO ai_exit_events(signal_id, created_at, status, price, profit_pct, mfe_pct, mae_pct, giveback_pct, exit_score, reason) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (signal_id, datetime.now(timezone.utc).isoformat(), status, price, profit_pct, mfe_pct, mae_pct, giveback_pct, int(exit_score or 0), reason),
            )

    def finish_signal(self, signal_id: int, *, status: str, approx_pnl: float, real_pnl: float | None, result_message_id: int | None, mfe_pct: float, mae_pct: float, result_source: str | None = None, exit_price: float | None = None, ai_exit_reason: str | None = None, ai_exit_status: str | None = None, ai_exit_score: int = 0, target_zone_reached: bool = False, giveback_pct: float = 0.0) -> bool:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE signals
                SET status=?, approx_pnl=?, real_pnl=?, result_message_id=?, result_at=?, mfe_pct=?, mae_pct=?,
                    result_source=?, exit_price=?, ai_exit_reason=?, ai_exit_status=?, ai_exit_score=?,
                    target_zone_reached=?, giveback_pct=?,
                    real_status=CASE WHEN signal_type='real' THEN 'closed' ELSE real_status END,
                    result_5m=?, result_10m=?, result_15m=?
                WHERE id=? AND status='OPEN'
                """,
                (
                    status, approx_pnl, real_pnl, result_message_id, now, mfe_pct, mae_pct,
                    result_source, exit_price, ai_exit_reason, ai_exit_status, int(ai_exit_score or 0),
                    1 if target_zone_reached else 0, float(giveback_pct or 0.0),
                    status, status, status, signal_id,
                ),
            )
            return cur.rowcount > 0

    def open_signals(self) -> list[StoredSignal]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM signals WHERE status='OPEN' ORDER BY id ASC").fetchall()
            return [self._row_to_signal(row) for row in rows]

    def signal_dict(self, signal_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM signals WHERE id=?", (signal_id,)).fetchone()
            return dict(row) if row else None

    def active_symbol_exists(self, toobit_symbol: str) -> bool:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM signals WHERE status='OPEN' AND toobit_symbol=?", (toobit_symbol,)).fetchone()
            return int(row["n"]) > 0

    def same_wave_reentry_block(self, *, symbol_name: str, direction: str, entry: float, score: int = 0) -> str | None:
        """Block only immediate re-entry into the same unfinished wave.

        This is not a fixed 2-hour ban. It prevents spam after AI_EXIT while
        allowing a fresh breakout or a real pullback/reset to create a new setup.
        """
        if not AI_REENTRY_REQUIRE_NEW_SETUP:
            return None
        if entry <= 0 or not symbol_name or direction not in {"LONG", "SHORT"}:
            return None

        lookback = max(int(AI_REENTRY_COOLDOWN_SECONDS * 4), int(AI_REENTRY_COOLDOWN_SECONDS), 1)
        start = datetime.now(timezone.utc) - timedelta(seconds=lookback)
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, created_at, result_at, symbol_name, direction, entry, exit_price, best_price, worst_price,
                       tp, sl, score, ai_exit_status, ai_exit_reason, atr_pct_15m
                FROM signals
                WHERE symbol_name=?
                  AND direction=?
                  AND status='EXIT'
                  AND result_at>=?
                ORDER BY result_at DESC, id DESC
                LIMIT 1
                """,
                (symbol_name, direction, start.isoformat()),
            ).fetchone()
        if not row:
            return None

        result_at = str(row["result_at"] or row["created_at"])
        try:
            closed_at = datetime.fromisoformat(result_at)
        except ValueError:
            return None
        if closed_at.tzinfo is None:
            closed_at = closed_at.replace(tzinfo=timezone.utc)
        age = max(0.0, (datetime.now(timezone.utc) - closed_at).total_seconds())

        last_exit = float(row["exit_price"] or row["entry"] or entry)
        last_score = int(row["score"] or 0)
        atr_pct = max(float(row["atr_pct_15m"] or 0.0), abs(float(row["tp"] or entry) - float(row["entry"] or entry)) / max(entry, 1e-9) * 0.35, 0.001)
        reset_distance = max(entry * atr_pct * AI_REENTRY_MIN_RESET_ATR, entry * 0.001)
        strong_score_reset = int(score or 0) >= last_score + 6

        if direction == "LONG":
            last_best = float(row["best_price"] or row["entry"] or entry)
            breakout_reset = entry >= last_best + reset_distance
            pullback_reset = entry <= last_exit - reset_distance
        else:
            last_best = float(row["best_price"] or row["entry"] or entry)
            breakout_reset = entry <= last_best - reset_distance
            pullback_reset = entry >= last_exit + reset_distance

        wave_reset = breakout_reset or pullback_reset or strong_score_reset
        if wave_reset:
            return None

        if age <= AI_REENTRY_COOLDOWN_SECONDS:
            return f"SAME_WAVE_COOLDOWN: آخرین AI_EXIT همین ارز/جهت تازه بسته شده و هنوز موج جدید تأیید نشده است. age={age:.0f}s reset_distance={reset_distance:.6f}"
        return f"SAME_WAVE_NOT_RESET: بعد از AI_EXIT هنوز قیمت از موج قبلی جدا نشده؛ ورود دوباره همان موج بلاک شد. reset_distance={reset_distance:.6f}"

    def active_real_count(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM signals WHERE status='OPEN' AND signal_type='real' AND real_status IN ('reserved','opening','opened')").fetchone()
            return int(row["n"])

    def pending_real_count(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM signals WHERE status='OPEN' AND signal_type='real' AND real_status IN ('reserved','opening')").fetchone()
            return int(row["n"])

    def active_real_symbol_exists(self, toobit_symbol: str) -> bool:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM signals WHERE status='OPEN' AND signal_type='real' AND real_status IN ('reserved','opening','opened') AND toobit_symbol=?", (toobit_symbol,)).fetchone()
            return int(row["n"]) > 0

    def upsert_watch(self, *, symbol_name: str, okx_symbol: str, toobit_symbol: str, direction: str, score: int, ai_confidence: int, expire_seconds: int) -> None:
        now = datetime.now(timezone.utc)
        expires = now + timedelta(seconds=expire_seconds)
        with self._connect() as conn:
            conn.execute("INSERT INTO watchlist(symbol_name, okx_symbol, toobit_symbol, direction, score, ai_confidence, created_at, updated_at, expires_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(symbol_name, direction) DO UPDATE SET score=excluded.score, ai_confidence=excluded.ai_confidence, updated_at=excluded.updated_at, expires_at=excluded.expires_at", (symbol_name, okx_symbol, toobit_symbol, direction, score, ai_confidence, now.isoformat(), now.isoformat(), expires.isoformat()))

    def active_watches(self) -> list[dict[str, Any]]:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM watchlist WHERE expires_at>=? ORDER BY score DESC, updated_at DESC", (now,)).fetchall()
            return [dict(row) for row in rows]

    def can_send_ready_alert(self, symbol_name: str, direction: str, cooldown_seconds: int) -> bool:
        with self._connect() as conn:
            row = conn.execute("SELECT last_ready_alert_at FROM watchlist WHERE symbol_name=? AND direction=?", (symbol_name, direction)).fetchone()
        if not row or not row["last_ready_alert_at"]:
            return True
        try:
            last = datetime.fromisoformat(str(row["last_ready_alert_at"]))
        except ValueError:
            return True
        return datetime.now(timezone.utc) - last >= timedelta(seconds=cooldown_seconds)

    def mark_ready_alert_sent(self, symbol_name: str, direction: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE watchlist SET last_ready_alert_at=? WHERE symbol_name=? AND direction=?", (datetime.now(timezone.utc).isoformat(), symbol_name, direction))

    def remove_watch(self, symbol_name: str, direction: str | None = None) -> None:
        with self._connect() as conn:
            if direction:
                conn.execute("DELETE FROM watchlist WHERE symbol_name=? AND direction=?", (symbol_name, direction))
            else:
                conn.execute("DELETE FROM watchlist WHERE symbol_name=?", (symbol_name,))

    def trim_watchlist(self, max_count: int) -> None:
        with self._connect() as conn:
            rows = conn.execute("SELECT id FROM watchlist ORDER BY score DESC, updated_at DESC").fetchall()
            for row in rows[max_count:]:
                conn.execute("DELETE FROM watchlist WHERE id=?", (row["id"],))

    def record_rejection(self, symbol_name: str, direction: str | None, code: str | None, reason: str, score: int = 0) -> None:
        with self._connect() as conn:
            conn.execute("INSERT INTO rejection_log(created_at, symbol_name, direction, code, reason, score) VALUES(?, ?, ?, ?, ?, ?)", (datetime.now(timezone.utc).isoformat(), symbol_name, direction, code, reason, score))

    def recent_rejections(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM rejection_log ORDER BY id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
            return [dict(row) for row in rows]

    def recent_signals(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, created_at, symbol_name, direction, score, threshold, real_threshold, signal_type, real_status, status, entry, tp, sl, entry_quality, reason, message_id FROM signals ORDER BY id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
            return [dict(row) for row in rows]

    def active_hunt_status(self, limit: int = 20) -> list[dict[str, Any]]:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT symbol_name, direction, score, ai_confidence, updated_at, expires_at FROM watchlist WHERE expires_at>=? ORDER BY score DESC, updated_at DESC LIMIT ?",
                (now, int(limit)),
            ).fetchall()
            return [dict(row) for row in rows]

    def ai_pattern_stats(self, symbol_name: str, direction: str, pattern_id: str) -> dict[str, Any]:
        start = datetime.now(timezone.utc) - timedelta(days=LEARNING_DAYS)
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM signals WHERE created_at>=? AND symbol_name=? AND direction=? AND pattern_id=? AND status IN ('TP','SL')", (start.isoformat(), symbol_name, direction, pattern_id)).fetchall()
        return self._learning_summary(rows)

    def indicator_range_stats(self, *, symbol_name: str, direction: str, entry_quality: str, rsi_5m: float, rsi_15m: float, adx_15m: float, volume_ratio_5m: float, volume_ratio_15m: float) -> dict[str, Any]:
        start = datetime.now(timezone.utc) - timedelta(days=LEARNING_DAYS)
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM signals WHERE created_at>=? AND symbol_name=? AND direction=? AND status IN ('TP','SL') AND ABS(rsi_5m - ?) <= 5 AND ABS(rsi_15m - ?) <= 6 AND ABS(adx_15m - ?) <= 5 AND volume_ratio_5m BETWEEN ? AND ? AND volume_ratio_15m BETWEEN ? AND ?", (start.isoformat(), symbol_name, direction, rsi_5m, rsi_15m, adx_15m, max(0.0, volume_ratio_5m - 0.55), volume_ratio_5m + 0.55, max(0.0, volume_ratio_15m - 0.55), volume_ratio_15m + 0.55)).fetchall()
        if len(rows) < 5 and entry_quality:
            with self._connect() as conn:
                rows = conn.execute("SELECT * FROM signals WHERE created_at>=? AND symbol_name=? AND direction=? AND entry_quality=? AND status IN ('TP','SL')", (start.isoformat(), symbol_name, direction, entry_quality)).fetchall()
        return self._learning_summary(rows)

    def session_stats(self, symbol_name: str, direction: str, bucket: str) -> dict[str, Any]:
        start = datetime.now(timezone.utc) - timedelta(days=LEARNING_DAYS)
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM signals WHERE created_at>=? AND symbol_name=? AND direction=? AND session_bucket=? AND status IN ('TP','SL')", (start.isoformat(), symbol_name, direction, bucket)).fetchall()
        return self._learning_summary(rows)

    def symbol_direction_profile(self, symbol_name: str, direction: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM ai_symbol_direction_profiles WHERE symbol_name=? AND direction=?", (symbol_name, direction)).fetchone()
            return dict(row) if row else {"total_signals": 0, "win_rate": 0.0, "consecutive_sl": 0, "signal_threshold": 68, "real_threshold": 76, "threshold_source": "BOOT"}

    def score_bucket_stats(self, symbol_name: str, direction: str) -> list[dict[str, Any]]:
        start = datetime.now(timezone.utc) - timedelta(days=LEARNING_DAYS)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT score, status, mfe_pct, mae_pct FROM signals WHERE created_at>=? AND symbol_name=? AND direction=? AND status IN ('TP','SL')",
                (start.isoformat(), symbol_name, direction),
            ).fetchall()
        buckets: dict[int, list[sqlite3.Row]] = {}
        for row in rows:
            bucket_low = int(float(row["score"] or 0) // 5) * 5
            buckets.setdefault(bucket_low, []).append(row)
        result: list[dict[str, Any]] = []
        for bucket_low, items in sorted(buckets.items()):
            tp = sum(1 for r in items if r["status"] == "TP")
            sl = sum(1 for r in items if r["status"] == "SL")
            samples = len(items)
            result.append({
                "bucket_low": bucket_low,
                "bucket_high": bucket_low + 4,
                "samples": samples,
                "tp": tp,
                "sl": sl,
                "win_rate": (tp / max(1, tp + sl) * 100.0) if tp + sl else 0.0,
                "avg_mfe": sum(float(r["mfe_pct"] or 0.0) for r in items) / max(1, samples),
                "avg_mae": sum(float(r["mae_pct"] or 0.0) for r in items) / max(1, samples),
            })
        return result

    def adaptive_context_stats(self, *, symbol_name: str, direction: str, entry_quality: str, candle_pattern: str, pattern_id: str, market_mode: str, session_state: str, score: int) -> tuple[dict[str, Any], ...]:
        """Return learned outcome stats from broad-to-specific soft AI contexts.

        These stats let the meta-brain learn whether a state such as PRECISION_WAIT,
        weak movement, a pattern, a score bucket, or a market mode should become
        softer, stricter, Normal-only, or Real-eligible for this exact symbol+direction.
        """
        start = datetime.now(timezone.utc) - timedelta(days=LEARNING_DAYS)
        score_bucket_low = int(float(score or 0) // 5) * 5
        contexts: list[tuple[str, str, tuple[Any, ...]]] = [
            (
                "entry_quality",
                "entry_quality=?",
                (entry_quality,),
            ),
            (
                "quality+market",
                "entry_quality=? AND market_mode=?",
                (entry_quality, market_mode),
            ),
            (
                "quality+candle",
                "entry_quality=? AND candle_pattern=?",
                (entry_quality, candle_pattern),
            ),
            (
                "score_bucket",
                "score BETWEEN ? AND ?",
                (score_bucket_low, score_bucket_low + 4),
            ),
            (
                "quality+score_bucket",
                "entry_quality=? AND score BETWEEN ? AND ?",
                (entry_quality, score_bucket_low, score_bucket_low + 4),
            ),
            (
                "session+quality",
                "session_state=? AND entry_quality=?",
                (session_state, entry_quality),
            ),
        ]
        if pattern_id:
            contexts.append(("pattern_id", "pattern_id=?", (pattern_id,)))
            contexts.append(("pattern+quality", "pattern_id=? AND entry_quality=?", (pattern_id, entry_quality)))
        result: list[dict[str, Any]] = []
        with self._connect() as conn:
            for label, extra_where, params in contexts:
                rows = conn.execute(
                    f"""
                    SELECT status, mfe_pct, mae_pct, signal_type, real_status
                    FROM signals
                    WHERE created_at>=?
                      AND symbol_name=?
                      AND direction=?
                      AND status IN ('TP','SL')
                      AND {extra_where}
                    """,
                    (start.isoformat(), symbol_name, direction, *params),
                ).fetchall()
                samples = len(rows)
                if samples <= 0:
                    continue
                tp = sum(1 for r in rows if r["status"] == "TP")
                sl = sum(1 for r in rows if r["status"] == "SL")
                real_rows = [r for r in rows if str(r["signal_type"] or "") == "real" or str(r["real_status"] or "") in {"opened", "closed"}]
                real_tp = sum(1 for r in real_rows if r["status"] == "TP")
                real_sl = sum(1 for r in real_rows if r["status"] == "SL")
                result.append({
                    "label": label,
                    "samples": samples,
                    "tp": tp,
                    "sl": sl,
                    "win_rate": (tp / max(1, tp + sl) * 100.0) if tp + sl else 0.0,
                    "avg_mfe": sum(float(r["mfe_pct"] or 0.0) for r in rows) / max(1, samples),
                    "avg_mae": sum(float(r["mae_pct"] or 0.0) for r in rows) / max(1, samples),
                    "real_samples": len(real_rows),
                    "real_win_rate": (real_tp / max(1, real_tp + real_sl) * 100.0) if real_tp + real_sl else 0.0,
                })
        # Prefer specific contexts first when meta-brain applies effects.
        priority = {
            "pattern+quality": 0,
            "quality+score_bucket": 1,
            "quality+market": 2,
            "quality+candle": 3,
            "session+quality": 4,
            "pattern_id": 5,
            "score_bucket": 6,
            "entry_quality": 7,
        }
        result.sort(key=lambda x: (priority.get(str(x.get("label")), 99), -int(x.get("samples", 0))))
        return tuple(result)

    def store_profile_thresholds(self, *, symbol_name: str, direction: str, signal_threshold: int, real_threshold: int, source: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO ai_symbol_direction_profiles(symbol_name, direction, signal_threshold, real_threshold, threshold_source, last_updated)
                VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol_name, direction) DO UPDATE SET
                    signal_threshold=excluded.signal_threshold,
                    real_threshold=excluded.real_threshold,
                    threshold_source=excluded.threshold_source,
                    last_updated=excluded.last_updated
                """,
                (symbol_name, direction, int(signal_threshold), int(real_threshold), source, now),
            )

    def add_shadow_test(self, signal_id: int, plan_name: str, tp: float, sl: float) -> None:
        with self._connect() as conn:
            conn.execute("INSERT INTO ai_shadow_tests(signal_id, plan_name, tp, sl, created_at) VALUES(?, ?, ?, ?, ?)", (signal_id, plan_name, tp, sl, datetime.now(timezone.utc).isoformat()))

    def update_shadow_tests(self, signal_id: int) -> None:
        signal = self.signal_dict(signal_id)
        if not signal:
            return
        direction = str(signal["direction"])
        best = float(signal.get("best_price") or signal["entry"])
        worst = float(signal.get("worst_price") or signal["entry"])
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM ai_shadow_tests WHERE signal_id=?", (signal_id,)).fetchall()
            for row in rows:
                tp = float(row["tp"])
                sl = float(row["sl"])
                if direction == "LONG":
                    result = "TP" if best >= tp else "SL" if worst <= sl else "OPEN"
                else:
                    result = "TP" if best <= tp else "SL" if worst >= sl else "OPEN"
                conn.execute("UPDATE ai_shadow_tests SET result=?, updated_at=? WHERE id=?", (result, datetime.now(timezone.utc).isoformat(), row["id"]))

    def record_ai_judgement(self, signal_id: int, judgement) -> None:
        with self._connect() as conn:
            conn.execute("INSERT INTO ai_judgements(signal_id, entry_quality, tp_quality, sl_quality, failure_reason, score_delta, reasons, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?)", (signal_id, judgement.entry_quality, judgement.tp_quality, judgement.sl_quality, judgement.failure_reason, judgement.score_delta, " | ".join(judgement.reasons), datetime.now(timezone.utc).isoformat()))

    def update_ai_profiles_from_signal(self, signal_id: int, judgement) -> None:
        signal = self.signal_dict(signal_id)
        if not signal:
            return
        symbol = str(signal["symbol_name"])
        direction = str(signal["direction"])
        status = str(signal["status"])
        pattern_id = str(signal.get("pattern_id") or "")
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM signals WHERE symbol_name=? AND direction=? AND status IN ('TP','SL','EXIT')", (symbol, direction)).fetchall()
            total = len(rows)
            tp = sum(1 for r in rows if r["status"] == "TP")
            sl = sum(1 for r in rows if r["status"] == "SL")
            exit_count = sum(1 for r in rows if r["status"] == "EXIT")
            wr = tp / max(1, tp + sl) * 100.0 if tp + sl else 0.0
            avg_mfe = sum(float(r["mfe_pct"] or 0.0) for r in rows) / max(1, total)
            avg_mae = sum(float(r["mae_pct"] or 0.0) for r in rows) / max(1, total)
            consecutive_sl = 0
            for r in reversed(rows):
                if r["status"] == "SL":
                    consecutive_sl += 1
                else:
                    break
            conn.execute("INSERT INTO ai_symbol_direction_profiles(symbol_name, direction, total_signals, tp, sl, exit_count, win_rate, net_profit, avg_mfe, avg_mae, consecutive_sl, learning_confidence, last_updated) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(symbol_name, direction) DO UPDATE SET total_signals=excluded.total_signals, tp=excluded.tp, sl=excluded.sl, exit_count=excluded.exit_count, win_rate=excluded.win_rate, net_profit=excluded.net_profit, avg_mfe=excluded.avg_mfe, avg_mae=excluded.avg_mae, consecutive_sl=excluded.consecutive_sl, learning_confidence=excluded.learning_confidence, last_updated=excluded.last_updated", (symbol, direction, total, tp, sl, exit_count, wr, sum(float(r["approx_pnl"] or 0.0) for r in rows), avg_mfe, avg_mae, consecutive_sl, min(99, total * 2), datetime.now(timezone.utc).isoformat()))
            if pattern_id:
                pattern_rows = [r for r in rows if str(r["pattern_id"] or "") == pattern_id]
                p_total = len(pattern_rows)
                p_tp = sum(1 for r in pattern_rows if r["status"] == "TP")
                p_sl = sum(1 for r in pattern_rows if r["status"] == "SL")
                p_wr = p_tp / max(1, p_tp + p_sl) * 100.0 if p_tp + p_sl else 0.0
                verdict = "POSITIVE" if p_total >= 10 and p_wr >= 60 else "NEGATIVE" if p_total >= 10 and p_wr <= 40 else "NEUTRAL"
                conn.execute("INSERT INTO ai_patterns(symbol_name, direction, pattern_id, total, tp, sl, win_rate, avg_mfe, avg_mae, verdict, last_updated) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(symbol_name, direction, pattern_id) DO UPDATE SET total=excluded.total, tp=excluded.tp, sl=excluded.sl, win_rate=excluded.win_rate, avg_mfe=excluded.avg_mfe, avg_mae=excluded.avg_mae, verdict=excluded.verdict, last_updated=excluded.last_updated", (symbol, direction, pattern_id, p_total, p_tp, p_sl, p_wr, sum(float(r["mfe_pct"] or 0.0) for r in pattern_rows) / max(1, p_total), sum(float(r["mae_pct"] or 0.0) for r in pattern_rows) / max(1, p_total), verdict, datetime.now(timezone.utc).isoformat()))
                conn.execute("INSERT INTO ai_pattern_results(signal_id, symbol_name, direction, pattern_id, status, mfe_pct, mae_pct, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?)", (signal_id, symbol, direction, pattern_id, status, signal.get("mfe_pct") or 0.0, signal.get("mae_pct") or 0.0, datetime.now(timezone.utc).isoformat()))

    def refresh_ai_suggestions(self) -> None:
        """Generate AI suggestions for margin/leverage without changing user settings.

        The bot may learn that the current dollar-per-trade or leverage is too low
        for Toobit real execution, or that leverage should stay conservative after
        weak results. These are suggestions only; the user still controls margin,
        leverage, max slots and trading on/off from the panel.
        """
        start = datetime.now(timezone.utc) - timedelta(days=LEARNING_DAYS)
        current_margin = max(float(self.margin_usdt()), 0.0001)
        current_leverage = max(int(self.leverage()), 1)
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM signals
                WHERE created_at>=?
                ORDER BY id DESC
                LIMIT 300
                """,
                (start.isoformat(),),
            ).fetchall()
        closed = [r for r in rows if r["status"] in ("TP", "SL", "EXIT")]
        normal_closed = [r for r in closed if str(r["signal_type"] or "") != "real"]
        real_closed = [r for r in closed if str(r["signal_type"] or "") == "real" or str(r["real_status"] or "") in {"opened", "closed"}]
        profitable_but_real_blocked = [
            r for r in rows
            if str(r["signal_type"] or "") == "normal"
            and str(r["real_block_reason"] or "").find("سود خالص") >= 0
            and float(r["estimated_net_profit_usdt"] or 0.0) > 0
        ]
        low_net_candidates = [
            r for r in rows
            if str(r["signal_type"] or "") == "normal"
            and float(r["estimated_net_profit_usdt"] or 0.0) > 0
            and float(r["estimated_net_profit_usdt"] or 0.0) < MIN_REAL_NET_PROFIT_USDT
        ]
        blocked = profitable_but_real_blocked or low_net_candidates
        if len(blocked) >= 3:
            nets = [max(float(r["estimated_net_profit_usdt"] or 0.0), 0.000001) for r in blocked[:20]]
            avg_net = sum(nets) / len(nets)
            scale = max(1.0, (MIN_REAL_NET_PROFIT_USDT / avg_net) * 1.15)
            suggested_margin = min(MARGIN_MAX_USDT, max(MARGIN_MIN_USDT, current_margin * scale))
            suggested_leverage = min(LEVERAGE_MAX, max(LEVERAGE_MIN, int(round(current_leverage * scale))))
            if suggested_margin > current_margin * 1.12:
                self._upsert_ai_suggestion(
                    "margin_low_net",
                    "medium",
                    f"چند کاندید ۱ ساعته به خاطر سود خالص پایین Real نشدند. AI فقط پیشنهاد می‌دهد دلار هر پوزیشن حدود {suggested_margin:.2f} USDT تست شود؛ تغییر خودکار انجام نشد.",
                )
            if suggested_leverage > current_leverage and suggested_leverage <= current_leverage + 5:
                self._upsert_ai_suggestion(
                    "leverage_low_net",
                    "low",
                    f"برای بعضی کاندیدهای خوب، لوریج فعلی سود خالص Real را کم کرده. AI پیشنهاد می‌دهد فقط بعد از تایید کاربر لوریج {suggested_leverage}x به‌صورت آزمایشی بررسی شود.",
                )

        if len(closed) >= 10:
            tp = sum(1 for r in closed if r["status"] == "TP")
            sl = sum(1 for r in closed if r["status"] == "SL")
            wr = tp / max(1, tp + sl) * 100.0 if tp + sl else 0.0
            avg_mfe = sum(float(r["mfe_pct"] or 0.0) for r in closed) / len(closed)
            avg_mae = sum(float(r["mae_pct"] or 0.0) for r in closed) / len(closed)
            if wr >= 62.0 and avg_mfe >= max(avg_mae * 1.15, 0.00001):
                suggested_margin = min(MARGIN_MAX_USDT, current_margin * 1.25)
                self._upsert_ai_suggestion(
                    "margin_scale_good_learning",
                    "low",
                    f"نتایج اخیر ۱ ساعته مثبت‌تر شده‌اند: WR {wr:.1f}% و MFE بهتر از MAE. AI پیشنهاد می‌دهد در صورت تمایل، دلار هر پوزیشن آرام تا حدود {suggested_margin:.2f} USDT تست شود؛ اجرا فقط با دستور شماست.",
                )
            if wr <= 42.0 or sl >= tp + 2:
                safer_leverage = max(LEVERAGE_MIN, min(current_leverage, max(1, current_leverage - 1)))
                self._upsert_ai_suggestion(
                    "leverage_caution_weak_learning",
                    "medium",
                    f"نتایج اخیر هنوز ضعیف/پرریسک است: WR {wr:.1f}%. AI پیشنهاد می‌دهد لوریج افزایش داده نشود و در صورت نیاز حدود {safer_leverage}x محافظه‌کارانه بماند.",
                )

        if len(real_closed) >= 5:
            real_tp = sum(1 for r in real_closed if r["status"] == "TP")
            real_sl = sum(1 for r in real_closed if r["status"] == "SL")
            real_wr = real_tp / max(1, real_tp + real_sl) * 100.0 if real_tp + real_sl else 0.0
            real_pnl = sum(float(r["real_pnl"] or 0.0) for r in real_closed)
            if real_wr <= 45.0 or real_pnl < 0:
                self._upsert_ai_suggestion(
                    "real_leverage_caution",
                    "high",
                    f"Real Toobit در نمونه‌های اخیر مناسب نیست: WR {real_wr:.1f}% / PnL {real_pnl:.4f}. AI پیشنهاد می‌دهد لوریج/دلار افزایش پیدا نکند تا نمونه‌های بهتر جمع شود.",
                )

    def _upsert_ai_suggestion(self, suggestion_key: str, level: str, message: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id FROM ai_suggestions WHERE suggestion_key=? AND status='open' ORDER BY id DESC LIMIT 1",
                (suggestion_key,),
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE ai_suggestions SET level=?, message=?, updated_at=? WHERE id=?",
                    (level, message, now, row["id"]),
                )
            else:
                conn.execute(
                    "INSERT INTO ai_suggestions(created_at, level, message, status, suggestion_key, updated_at) VALUES(?, ?, ?, 'open', ?, ?)",
                    (now, level, message, suggestion_key, now),
                )

    def _learning_summary(self, rows: list[sqlite3.Row]) -> dict[str, Any]:
        samples = len(rows)
        tp = sum(1 for row in rows if row["status"] == "TP")
        sl = sum(1 for row in rows if row["status"] == "SL")
        avg_mfe = sum(float(row["mfe_pct"] or 0.0) for row in rows) / samples if samples else 0.0
        avg_mae = sum(float(row["mae_pct"] or 0.0) for row in rows) / samples if samples else 0.0
        return {"samples": samples, "tp": tp, "sl": sl, "win_rate": (tp / max(1, tp + sl) * 100.0) if tp + sl else 0.0, "avg_mfe": avg_mfe, "avg_mae": avg_mae}

    def stats(self, days: int) -> dict[str, Any]:
        days = max(1, min(days, 30))
        start = datetime.now(timezone.utc) - timedelta(days=days)
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM signals WHERE created_at >= ?", (start.isoformat(),)).fetchall()
        return self._build_stats(rows)

    def today_stats(self) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM signals WHERE created_at >= ?", (start.isoformat(),)).fetchall()
        stats = self._build_stats(rows)
        stats["approx_pnl"] = sum(float(row["approx_pnl"] or 0.0) for row in rows if row["signal_type"] != "real")
        stats["real_pnl"] = sum(float(row["real_pnl"] or 0.0) for row in rows if row["signal_type"] == "real")
        return stats

    def ai_panel_stats(self) -> dict[str, Any]:
        self.refresh_ai_suggestions()
        start = datetime.now(timezone.utc) - timedelta(days=LEARNING_DAYS)
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM signals WHERE created_at >= ?", (start.isoformat(),)).fetchall()
            patterns = conn.execute("SELECT * FROM ai_patterns ORDER BY total DESC, win_rate DESC LIMIT 5").fetchall()
            suggestions = conn.execute("SELECT * FROM ai_suggestions WHERE status='open' ORDER BY id DESC LIMIT 5").fetchall()
        closed = [r for r in rows if r["status"] in ("TP", "SL", "EXIT")]
        return {
            "learning_days": LEARNING_DAYS,
            "stored_patterns": len(patterns),
            "active_patterns": len(rows),
            "analysis_right": sum(1 for r in closed if r["status"] == "TP"),
            "analysis_wrong": sum(1 for r in closed if r["status"] == "SL"),
            "avg_ai_confidence": sum(float(r["ai_confidence"] or 0) for r in rows) / len(rows) if rows else 0.0,
            "best_symbol_side": self._best_worst_symbol_side(closed, True),
            "worst_symbol_side": self._best_worst_symbol_side(closed, False),
            "best_indicator_ranges": self._best_indicator_range_text(closed),
            "patterns": "\n".join(f"• {p['symbol_name']} {p['direction']} {p['pattern_id']} / WR {float(p['win_rate']):.1f}% / {p['total']}" for p in patterns) or "نمونه کافی نیست",
            "suggestions": "\n".join(f"• {s['message']}" for s in suggestions) or "پیشنهاد باز ندارد",
        }

    def _best_worst_symbol_side(self, rows: list[sqlite3.Row], reverse: bool) -> str:
        groups: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            groups.setdefault(f"{row['symbol_name']} {row['direction']}", []).append(row)
        scored = []
        for key, items in groups.items():
            tp = sum(1 for x in items if x["status"] == "TP")
            sl = sum(1 for x in items if x["status"] == "SL")
            if tp + sl >= 3:
                scored.append((tp / (tp + sl) * 100.0, key, tp + sl))
        if not scored:
            return "نمونه کافی نیست"
        scored.sort(reverse=reverse)
        wr, key, n = scored[0]
        return f"{key} / WR {wr:.1f}% / {n} نمونه"

    def _best_indicator_range_text(self, rows: list[sqlite3.Row]) -> str:
        groups: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            key = str(row["indicator_profile"] or "UNKNOWN")
            groups.setdefault(key, []).append(row)
        best = []
        for key, subset in groups.items():
            tp = sum(1 for r in subset if r["status"] == "TP")
            sl = sum(1 for r in subset if r["status"] == "SL")
            if tp + sl >= 3:
                best.append((tp / (tp + sl) * 100.0, key, tp + sl))
        if not best:
            return "نمونه کافی نیست"
        best.sort(reverse=True)
        return "\n".join(f"• {key} / WR {wr:.1f}% / {n} نمونه" for wr, key, n in best[:4])

    def reset_stats(self) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM signals")
            conn.execute("DELETE FROM rejection_log")
            conn.execute("DELETE FROM watchlist")
            conn.execute("DELETE FROM ai_second_snapshots")
            conn.execute("DELETE FROM ai_shadow_tests")
            conn.execute("DELETE FROM ai_judgements")

    def reset_learning(self) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM ai_symbol_direction_profiles")
            conn.execute("DELETE FROM ai_patterns")
            conn.execute("DELETE FROM ai_pattern_results")
            conn.execute("DELETE FROM ai_sensor_weights")
            conn.execute("DELETE FROM ai_shadow_tests")
            conn.execute("DELETE FROM ai_judgements")
            conn.execute("UPDATE signals SET ai_confidence=0, ai_experience=0, ai_adjustment=0, ai_effect='NEUTRAL', mfe_pct=0, mae_pct=0")

    def _build_stats(self, rows: list[sqlite3.Row]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        result["all"] = self._summarize(rows, pnl_key="approx_pnl")
        result["normal"] = self._summarize([r for r in rows if r["signal_type"] == "normal"], pnl_key="approx_pnl")
        result["real"] = self._summarize([r for r in rows if r["signal_type"] == "real"], pnl_key="real_pnl")
        result["hunter"] = self._summarize([r for r in rows if r["hunter_type"] == "hunter"], pnl_key="approx_pnl")
        result["real_failed"] = self._summarize([r for r in rows if r["signal_type"] == "real_failed"], pnl_key="real_pnl")
        result["toobit_real_tp_sl"] = self._summarize([r for r in rows if r["result_source"] == "toobit_real"], pnl_key="real_pnl")
        result["normal_tp_sl"] = self._summarize([r for r in rows if r["result_source"] in ("normal", "normal_on_real")], pnl_key="approx_pnl")
        for side in ("LONG", "SHORT"):
            key = side.lower()
            result[key] = self._summarize([r for r in rows if r["direction"] == side], pnl_key="approx_pnl")
        return result

    def _summarize(self, subset: list[sqlite3.Row], *, pnl_key: str) -> dict[str, Any]:
        closed = [row for row in subset if row["status"] in ("TP", "SL")]
        tp_count = sum(1 for row in subset if row["status"] == "TP")
        sl_count = sum(1 for row in subset if row["status"] == "SL")
        return {"total": len(subset), "tp": tp_count, "sl": sl_count, "open": sum(1 for row in subset if row["status"] == "OPEN"), "exit": sum(1 for row in subset if row["status"] == "EXIT"), "failed": sum(1 for row in subset if row["status"] == "FAILED"), "win_rate": (tp_count / len(closed) * 100.0) if closed else 0.0, "pnl": sum(float(row[pnl_key] or 0.0) for row in subset), "avg_score": sum(float(row["score"] or 0) for row in subset) / len(subset) if subset else 0.0}

    def _row_to_signal(self, row: sqlite3.Row) -> StoredSignal:
        return StoredSignal(
            id=int(row["id"]), created_at=str(row["created_at"]), okx_symbol=str(row["okx_symbol"]),
            toobit_symbol=str(row["toobit_symbol"]), symbol_name=str(row["symbol_name"] or ""),
            direction=str(row["direction"]), entry=float(row["entry"]), tp=float(row["tp"]), sl=float(row["sl"]),
            score=int(row["score"]), ai_confidence=int(row["ai_confidence"] or 0), ai_experience=int(row["ai_experience"] or 0),
            signal_type=str(row["signal_type"]), hunter_type=str(row["hunter_type"] or "ordinary"), status=str(row["status"]),
            real_status=str(row["real_status"]), message_id=int(row["message_id"]) if row["message_id"] is not None else None,
            result_message_id=int(row["result_message_id"]) if row["result_message_id"] is not None else None,
            order_id=str(row["order_id"]) if row["order_id"] is not None else None,
            approx_pnl=float(row["approx_pnl"]) if row["approx_pnl"] is not None else None,
            real_pnl=float(row["real_pnl"]) if row["real_pnl"] is not None else None,
            margin_usdt=float(row["margin_usdt"] or 0.0), leverage=int(row["leverage"] or 1),
            net_edge=float(row["net_edge"] or 0.0), estimated_profit_usdt=float(row["estimated_profit_usdt"] or 0.0),
            estimated_net_profit_usdt=float(row["estimated_net_profit_usdt"] or 0.0), estimated_profit_pct=float(row["estimated_profit_pct"] or 0.0),
            risk_reward=float(row["risk_reward"] or 0.0), reason=str(row["reason"]) if row["reason"] is not None else None,
            result_source=str(row["result_source"]) if row["result_source"] is not None else None,
            exit_price=float(row["exit_price"]) if row["exit_price"] is not None else None,
            ai_exit_reason=str(row["ai_exit_reason"]) if row["ai_exit_reason"] is not None else None,
            ai_exit_status=str(row["ai_exit_status"]) if row["ai_exit_status"] is not None else None,
            ai_exit_score=int(row["ai_exit_score"] or 0), target_zone_reached=bool(row["target_zone_reached"] or 0),
            giveback_pct=float(row["giveback_pct"] or 0.0),
            entry_quality=str(row["entry_quality"]) if row["entry_quality"] is not None else None,
            indicator_profile=str(row["indicator_profile"]) if row["indicator_profile"] is not None else None,
            market_mode=str(row["market_mode"]) if row["market_mode"] is not None else None,
            atr_pct_15m=float(row["atr_pct_15m"] or 0.0),
            best_price=float(row["best_price"]) if row["best_price"] is not None else None,
            worst_price=float(row["worst_price"]) if row["worst_price"] is not None else None,
            mfe_pct=float(row["mfe_pct"] or 0.0), mae_pct=float(row["mae_pct"] or 0.0),
        )
