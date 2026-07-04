from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from config import DEFAULT_LEVERAGE, DEFAULT_MARGIN_USDT, DEFAULT_MAX_POSITIONS, DEFAULT_TRADE_ENABLED, LEVERAGE_MAX, LEVERAGE_MIN, MARGIN_MAX_USDT, MARGIN_MIN_USDT, MAX_POSITIONS_MAX, MAX_POSITIONS_MIN, DB_PATH
from utils import direction_profit_pct, json_safe, now_utc, round_trip_fee_usdt
from guard_utils import half_hour_bucket, session_info, signal_time, weekday_key


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
    status: str
    signal_type: str
    real_status: str
    message_id: int | None
    result_message_id: int | None
    order_id: str | None
    margin_usdt: float
    leverage: int
    features_key: str
    approx_pnl: float | None
    real_pnl: float | None
    best_price: float
    worst_price: float
    mfe_pct: float
    mae_pct: float


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
            conn.execute("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS signals(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    okx_symbol TEXT NOT NULL,
                    toobit_symbol TEXT NOT NULL,
                    symbol_name TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    entry REAL NOT NULL,
                    tp REAL NOT NULL,
                    sl REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'OPEN',
                    signal_type TEXT NOT NULL,
                    real_status TEXT NOT NULL DEFAULT 'none',
                    real_allowed INTEGER DEFAULT 0,
                    message_id INTEGER,
                    result_message_id INTEGER,
                    order_id TEXT,
                    margin_usdt REAL NOT NULL,
                    leverage INTEGER NOT NULL,
                    features_key TEXT,
                    confidence INTEGER DEFAULT 0,
                    samples INTEGER DEFAULT 0,
                    win_rate REAL DEFAULT 0,
                    predicted_move_pct REAL DEFAULT 0,
                    tp_distance_pct REAL DEFAULT 0,
                    sl_distance_pct REAL DEFAULT 0,
                    risk_reward REAL DEFAULT 0,
                    estimated_net_profit_usdt REAL DEFAULT 0,
                    estimated_cost_pct REAL DEFAULT 0,
                    market_state TEXT,
                    alignment TEXT,
                    indicator_profile TEXT,
                    reason TEXT,
                    approx_pnl REAL,
                    real_pnl REAL,
                    result_source TEXT,
                    result_at TEXT,
                    exit_price REAL,
                    best_price REAL,
                    worst_price REAL,
                    mfe_pct REAL DEFAULT 0,
                    mae_pct REAL DEFAULT 0
                )
            """)
            conn.execute("CREATE TABLE IF NOT EXISTS signal_snapshots(id INTEGER PRIMARY KEY AUTOINCREMENT, signal_id INTEGER NOT NULL, created_at TEXT NOT NULL, price REAL NOT NULL, mfe_pct REAL DEFAULT 0, mae_pct REAL DEFAULT 0)")
            conn.execute("CREATE TABLE IF NOT EXISTS range_profiles(features_key TEXT PRIMARY KEY, symbol_name TEXT, direction TEXT, samples INTEGER DEFAULT 0, tp INTEGER DEFAULT 0, sl INTEGER DEFAULT 0, win_rate REAL DEFAULT 0, net_profit REAL DEFAULT 0, avg_mfe_pct REAL DEFAULT 0, avg_mae_pct REAL DEFAULT 0, best_tp_pct REAL DEFAULT 0, best_sl_pct REAL DEFAULT 0, confidence INTEGER DEFAULT 0, last_updated TEXT)")
            conn.execute("CREATE TABLE IF NOT EXISTS range_observations(id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, source TEXT, signal_id INTEGER, features_key TEXT, symbol_name TEXT, direction TEXT, result TEXT, net_profit REAL DEFAULT 0, mfe_pct REAL DEFAULT 0, mae_pct REAL DEFAULT 0, tp_distance_pct REAL DEFAULT 0, sl_distance_pct REAL DEFAULT 0, reason TEXT)")
            conn.execute("CREATE TABLE IF NOT EXISTS shadow_tests(id INTEGER PRIMARY KEY AUTOINCREMENT, signal_id INTEGER NOT NULL, name TEXT NOT NULL, tp REAL NOT NULL, sl REAL NOT NULL, result TEXT DEFAULT 'pending', created_at TEXT NOT NULL, updated_at TEXT)")
            conn.execute("CREATE TABLE IF NOT EXISTS historical_replay_runs(id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, symbol_name TEXT, days INTEGER, observations INTEGER DEFAULT 0, notes TEXT)")
            conn.execute("CREATE TABLE IF NOT EXISTS missed_opportunities(id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, symbol_name TEXT, direction TEXT, features_key TEXT, future_mfe_pct REAL, reason TEXT)")
            conn.execute("CREATE TABLE IF NOT EXISTS symbol_direction_profiles(symbol_name TEXT NOT NULL, direction TEXT NOT NULL, samples INTEGER DEFAULT 0, tp INTEGER DEFAULT 0, sl INTEGER DEFAULT 0, win_rate REAL DEFAULT 0, net_profit REAL DEFAULT 0, confidence INTEGER DEFAULT 0, last_updated TEXT, PRIMARY KEY(symbol_name, direction))")
            conn.execute("CREATE TABLE IF NOT EXISTS session_profiles(symbol_name TEXT NOT NULL, direction TEXT NOT NULL, session_bucket TEXT NOT NULL, samples INTEGER DEFAULT 0, tp INTEGER DEFAULT 0, sl INTEGER DEFAULT 0, net_profit REAL DEFAULT 0, PRIMARY KEY(symbol_name, direction, session_bucket))")
            conn.execute("CREATE TABLE IF NOT EXISTS capital_suggestions(id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, level TEXT, message TEXT, status TEXT DEFAULT 'open')")
            conn.execute("CREATE TABLE IF NOT EXISTS indicator_requests(id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, indicator TEXT, reason TEXT, status TEXT DEFAULT 'open')")
            conn.execute("CREATE TABLE IF NOT EXISTS toobit_orders(id INTEGER PRIMARY KEY AUTOINCREMENT, signal_id INTEGER, symbol TEXT, action TEXT, order_id TEXT, status TEXT, reason TEXT, created_at TEXT)")
            conn.execute("CREATE TABLE IF NOT EXISTS no_signal_log(id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, symbol_name TEXT, direction TEXT, reason TEXT, features_key TEXT)")
            conn.execute("CREATE TABLE IF NOT EXISTS guard_alerts(alert_key TEXT PRIMARY KEY, guard_type TEXT, title TEXT, created_at TEXT NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS guard_events(id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, guard_type TEXT, severity TEXT, reason TEXT, action TEXT, payload TEXT)")
            conn.execute("CREATE TABLE IF NOT EXISTS time_risk_profiles(profile_key TEXT PRIMARY KEY, scope TEXT, symbol_name TEXT, direction TEXT, session_name TEXT, hour_bucket TEXT, weekday TEXT, samples INTEGER DEFAULT 0, tp INTEGER DEFAULT 0, sl INTEGER DEFAULT 0, max_consecutive_sl INTEGER DEFAULT 0, risk_score INTEGER DEFAULT 0, main_cause TEXT, action TEXT DEFAULT 'ALLOW', last_updated TEXT)")
            conn.execute("CREATE TABLE IF NOT EXISTS adaptive_fix_profiles(profile_key TEXT PRIMARY KEY, scope TEXT, symbol_name TEXT, direction TEXT, market_state TEXT, alignment TEXT, session_name TEXT, hour_bucket TEXT, weekday TEXT, samples INTEGER DEFAULT 0, tp INTEGER DEFAULT 0, sl INTEGER DEFAULT 0, total_net_profit REAL DEFAULT 0, avg_mfe_pct REAL DEFAULT 0, avg_mae_pct REAL DEFAULT 0, avg_tp_distance_pct REAL DEFAULT 0, avg_sl_distance_pct REAL DEFAULT 0, risk_score INTEGER DEFAULT 0, last_cause TEXT, recommended_action TEXT DEFAULT 'ALLOW', recommended_tp_pct REAL DEFAULT 0, recommended_sl_pct REAL DEFAULT 0, last_updated TEXT)")
            # V3 forensic learning: complete stop cases and treatment testing. Existing DBs are migrated with ALTER below.
            conn.execute("CREATE TABLE IF NOT EXISTS signal_forensic_snapshots(id INTEGER PRIMARY KEY AUTOINCREMENT, signal_id INTEGER UNIQUE, created_at TEXT NOT NULL, symbol_name TEXT, direction TEXT, session_name TEXT, hour_bucket TEXT, weekday TEXT, is_session_open INTEGER DEFAULT 0, near_news INTEGER DEFAULT 0, entry REAL, tp REAL, sl REAL, risk_reward REAL, fee_usdt REAL DEFAULT 0, expected_net_profit REAL DEFAULT 0, market_state TEXT, alignment TEXT, indicator_profile TEXT, features_key TEXT, payload TEXT)")
            conn.execute("CREATE TABLE IF NOT EXISTS loss_cases(id INTEGER PRIMARY KEY AUTOINCREMENT, signal_id INTEGER UNIQUE, created_at TEXT NOT NULL, symbol_name TEXT, direction TEXT, primary_cause TEXT, secondary_causes TEXT, cause_scores TEXT, fix_policy TEXT, action TEXT, treatment_level INTEGER DEFAULT 0, message TEXT, indicator_suggestion TEXT)")
            conn.execute("CREATE TABLE IF NOT EXISTS treatment_tests(id INTEGER PRIMARY KEY AUTOINCREMENT, profile_key TEXT, signal_id INTEGER, created_at TEXT NOT NULL, result TEXT, net_profit REAL DEFAULT 0, treatment_level INTEGER DEFAULT 0, fix_policy TEXT, cause TEXT)")
            self._ensure_column(conn, 'adaptive_fix_profiles', 'treatment_level', 'INTEGER DEFAULT 0')
            self._ensure_column(conn, 'adaptive_fix_profiles', 'tests', 'INTEGER DEFAULT 0')
            self._ensure_column(conn, 'adaptive_fix_profiles', 'successes', 'INTEGER DEFAULT 0')
            self._ensure_column(conn, 'adaptive_fix_profiles', 'failures', 'INTEGER DEFAULT 0')
            self._ensure_column(conn, 'adaptive_fix_profiles', 'consecutive_failures', 'INTEGER DEFAULT 0')
            self._ensure_column(conn, 'adaptive_fix_profiles', 'fix_policy', 'TEXT')
            self._ensure_column(conn, 'adaptive_fix_profiles', 'last_message', 'TEXT')
            self._set_default(conn, "guard_cooldown_until", "")
            self._set_default(conn, "guard_cooldown_reason", "")
            self._set_default(conn, "trade_enabled", "1" if DEFAULT_TRADE_ENABLED else "0")
            self._set_default(conn, "margin_usdt", str(DEFAULT_MARGIN_USDT))
            self._set_default(conn, "leverage", str(DEFAULT_LEVERAGE))
            self._set_default(conn, "max_positions", str(DEFAULT_MAX_POSITIONS))
            self._set_default(conn, "auto_signals_enabled", "1")

    def _set_default(self, conn: sqlite3.Connection, key: str, value: str) -> None:
        conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)", (key, value))

    def _ensure_column(self, conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        cols = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


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

    def auto_signals_enabled(self) -> bool:
        return self._get_setting("auto_signals_enabled", "1") == "1"

    def set_auto_signals_enabled(self, enabled: bool) -> None:
        self._set_setting("auto_signals_enabled", "1" if enabled else "0")

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
            raise ValueError("حداکثر پوزیشن باید بین 1 تا 200 باشد.")
        self._set_setting("max_positions", str(int(value)))

    def add_signal(self, *, okx_symbol: str, toobit_symbol: str, symbol_name: str, decision, signal_type: str, real_status: str) -> int:
        now = now_utc().isoformat()
        with self._connect() as conn:
            cur = conn.execute("""
                INSERT INTO signals(created_at, okx_symbol, toobit_symbol, symbol_name, direction, entry, tp, sl, signal_type, real_status, real_allowed, margin_usdt, leverage, features_key, confidence, samples, win_rate, predicted_move_pct, tp_distance_pct, sl_distance_pct, risk_reward, estimated_net_profit_usdt, estimated_cost_pct, market_state, alignment, indicator_profile, reason, best_price, worst_price)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (now, okx_symbol, toobit_symbol, symbol_name, decision.direction, decision.entry, decision.tp, decision.sl, signal_type, real_status, 1 if decision.real_allowed else 0, self.margin_usdt(), self.leverage(), decision.features_key, decision.confidence, decision.samples, decision.win_rate, decision.predicted_move_pct, decision.tp_distance_pct, decision.sl_distance_pct, decision.risk_reward, decision.estimated_net_profit_usdt, decision.estimated_cost_pct, decision.market_state, decision.alignment, decision.indicator_profile, decision.reason, decision.entry, decision.entry))
            signal_id = int(cur.lastrowid)
            info = session_info()
            fee = round_trip_fee_usdt(self.margin_usdt(), self.leverage())
            payload = {
                "confidence": decision.confidence, "samples": decision.samples, "win_rate": decision.win_rate,
                "predicted_move_pct": decision.predicted_move_pct, "tp_distance_pct": decision.tp_distance_pct,
                "sl_distance_pct": decision.sl_distance_pct, "estimated_cost_pct": decision.estimated_cost_pct,
                "rsi": getattr(decision, "rsi", 0.0), "adx": getattr(decision, "adx", 0.0),
                "atr_pct": getattr(decision, "atr_pct", 0.0), "volume_ratio": getattr(decision, "volume_ratio", 0.0),
                "reason": decision.reason[:1000],
            }
            conn.execute("""
                INSERT OR REPLACE INTO signal_forensic_snapshots(signal_id, created_at, symbol_name, direction, session_name, hour_bucket, weekday, is_session_open, near_news, entry, tp, sl, risk_reward, fee_usdt, expected_net_profit, market_state, alignment, indicator_profile, features_key, payload)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (signal_id, now, symbol_name, decision.direction, info.name, info.hour_bucket, info.weekday, 1 if info.is_open_watch else 0, 1 if ("NEWS" in decision.reason.upper() or "خبر" in decision.reason) else 0, decision.entry, decision.tp, decision.sl, decision.risk_reward, fee, decision.estimated_net_profit_usdt, decision.market_state, decision.alignment, decision.indicator_profile, decision.features_key, json.dumps(json_safe(payload), ensure_ascii=False)))
            return signal_id

    def update_message_id(self, signal_id: int, message_id: int | None) -> None:
        if message_id is None:
            return
        with self._connect() as conn:
            conn.execute("UPDATE signals SET message_id=? WHERE id=?", (int(message_id), signal_id))

    def mark_real_opening(self, signal_id: int) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE signals SET real_status='opening' WHERE id=? AND status='OPEN'", (signal_id,))

    def mark_real_open_result(self, signal_id: int, *, opened: bool, order_id: str | None, reason: str) -> None:
        with self._connect() as conn:
            if opened:
                conn.execute("UPDATE signals SET real_status='opened', order_id=? WHERE id=? AND status='OPEN'", (order_id, signal_id))
                conn.execute("INSERT INTO toobit_orders(signal_id, symbol, action, order_id, status, reason, created_at) SELECT id, toobit_symbol, 'open', ?, 'opened', ?, ? FROM signals WHERE id=?", (order_id, reason, now_utc().isoformat(), signal_id))
            else:
                conn.execute("UPDATE signals SET status='FAILED', real_status='failed', result_at=?, reason=COALESCE(reason,'') || ? WHERE id=? AND status='OPEN'", (now_utc().isoformat(), f" | Real failed: {reason}", signal_id))

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
            return int(row["n"])

    def update_excursions(self, signal: StoredSignal, price: float) -> tuple[float, float]:
        best = signal.best_price
        worst = signal.worst_price
        if signal.direction == "LONG":
            best = max(best, price)
            worst = min(worst, price)
            mfe = max(0.0, (best - signal.entry) / signal.entry)
            mae = max(0.0, (signal.entry - worst) / signal.entry)
        else:
            best = min(best, price)
            worst = max(worst, price)
            mfe = max(0.0, (signal.entry - best) / signal.entry)
            mae = max(0.0, (worst - signal.entry) / signal.entry)
        with self._connect() as conn:
            conn.execute("UPDATE signals SET best_price=?, worst_price=?, mfe_pct=?, mae_pct=? WHERE id=?", (best, worst, mfe, mae, signal.id))
            conn.execute("INSERT INTO signal_snapshots(signal_id, created_at, price, mfe_pct, mae_pct) VALUES(?, ?, ?, ?, ?)", (signal.id, now_utc().isoformat(), price, mfe, mae))
        return mfe, mae

    def finish_signal(self, signal_id: int, *, status: str, exit_price: float, approx_pnl: float, real_pnl: float | None, result_message_id: int | None, result_source: str, mfe_pct: float, mae_pct: float) -> bool:
        with self._connect() as conn:
            cur = conn.execute("UPDATE signals SET status=?, exit_price=?, approx_pnl=?, real_pnl=?, result_message_id=?, result_source=?, result_at=?, mfe_pct=?, mae_pct=? WHERE id=? AND status='OPEN'", (status, exit_price, approx_pnl, real_pnl, result_message_id, result_source, now_utc().isoformat(), mfe_pct, mae_pct, signal_id))
            return cur.rowcount > 0

    def register_shadows(self, signal_id: int, shadows: tuple[tuple[str, float, float], ...]) -> None:
        with self._connect() as conn:
            for name, tp, sl in shadows:
                conn.execute("INSERT INTO shadow_tests(signal_id, name, tp, sl, created_at) VALUES(?, ?, ?, ?, ?)", (signal_id, name, tp, sl, now_utc().isoformat()))

    def update_shadow_results(self, signal_id: int, direction: str, best_price: float, worst_price: float) -> None:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM shadow_tests WHERE signal_id=? AND result='pending'", (signal_id,)).fetchall()
            for row in rows:
                tp = float(row["tp"])
                sl = float(row["sl"])
                result = "open"
                if direction == "LONG":
                    if best_price >= tp:
                        result = "TP"
                    elif worst_price <= sl:
                        result = "SL"
                else:
                    if best_price <= tp:
                        result = "TP"
                    elif worst_price >= sl:
                        result = "SL"
                conn.execute("UPDATE shadow_tests SET result=?, updated_at=? WHERE id=?", (result, now_utc().isoformat(), row["id"]))

    def record_no_signal(self, symbol_name: str, direction: str | None, reason: str, features_key: str = "") -> None:
        with self._connect() as conn:
            conn.execute("INSERT INTO no_signal_log(created_at, symbol_name, direction, reason, features_key) VALUES(?, ?, ?, ?, ?)", (now_utc().isoformat(), symbol_name, direction, reason[:1000], features_key))

    def latest_no_signals(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM no_signal_log ORDER BY id DESC LIMIT ?",
                (int(max(1, min(limit, 100))),),
            ).fetchall()
        return [dict(row) for row in rows]

    def scan_summary(self, minutes: int = 60) -> dict[str, Any]:
        since = (now_utc() - timedelta(minutes=minutes)).isoformat()
        with self._connect() as conn:
            rejects = conn.execute("SELECT COUNT(*) AS n FROM no_signal_log WHERE created_at>=?", (since,)).fetchone()
            signals = conn.execute("SELECT COUNT(*) AS n FROM signals WHERE created_at>=?", (since,)).fetchone()
            active = conn.execute("SELECT COUNT(*) AS n FROM signals WHERE status='OPEN'").fetchone()
            last_reject = conn.execute("SELECT created_at FROM no_signal_log ORDER BY id DESC LIMIT 1").fetchone()
            last_signal = conn.execute("SELECT created_at FROM signals ORDER BY id DESC LIMIT 1").fetchone()
            symbols = conn.execute("SELECT COUNT(DISTINCT symbol_name) AS n FROM no_signal_log WHERE created_at>=?", (since,)).fetchone()
        last_values = [row["created_at"] for row in (last_reject, last_signal) if row and row["created_at"]]
        last_activity = max(last_values) if last_values else None
        return {
            "auto_signals_enabled": self.auto_signals_enabled(),
            "trade_enabled": self.trade_enabled(),
            "minutes": minutes,
            "last_activity": last_activity,
            "rejected": int(rejects["n"] or 0),
            "signals": int(signals["n"] or 0),
            "open": int(active["n"] or 0),
            "symbols_with_rejects": int(symbols["n"] or 0),
        }


    def guard_alert_sent(self, alert_key: str) -> bool:
        with self._connect() as conn:
            row = conn.execute("SELECT 1 FROM guard_alerts WHERE alert_key=?", (alert_key,)).fetchone()
            return row is not None

    def mark_guard_alert_sent(self, alert_key: str, guard_type: str, title: str) -> None:
        with self._connect() as conn:
            conn.execute("INSERT OR IGNORE INTO guard_alerts(alert_key, guard_type, title, created_at) VALUES(?, ?, ?, ?)", (alert_key, guard_type, title[:300], now_utc().isoformat()))

    def record_guard_event(self, guard_type: str, severity: str, reason: str, action: str, payload: dict[str, Any] | None = None) -> None:
        with self._connect() as conn:
            conn.execute("INSERT INTO guard_events(created_at, guard_type, severity, reason, action, payload) VALUES(?, ?, ?, ?, ?, ?)", (now_utc().isoformat(), guard_type, severity, reason[:1000], action, json.dumps(json_safe(payload or {}), ensure_ascii=False)))

    def set_guard_cooldown(self, until_iso: str, reason: str) -> None:
        self._set_setting("guard_cooldown_until", until_iso or "")
        self._set_setting("guard_cooldown_reason", reason[:1000] if reason else "")

    def guard_cooldown(self) -> dict[str, str]:
        return {
            "until": self._get_setting("guard_cooldown_until", ""),
            "reason": self._get_setting("guard_cooldown_reason", ""),
        }

    def recent_closed_signals(self, limit: int = 20, minutes: int | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM signals WHERE status IN ('TP','SL')"
        params: list[Any] = []
        if minutes is not None:
            since = (now_utc() - timedelta(minutes=int(minutes))).isoformat()
            sql += " AND COALESCE(result_at, created_at) >= ?"
            params.append(since)
        sql += " ORDER BY COALESCE(result_at, created_at) DESC, id DESC LIMIT ?"
        params.append(int(max(1, min(limit, 200))))
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def update_time_risk_profile(self, *, signal: dict[str, Any], result: str, cause: str) -> None:
        dt = signal_time(signal)
        info = session_info(dt)
        symbol = str(signal.get("symbol_name") or "GLOBAL")
        direction = str(signal.get("direction") or "ANY")
        result = str(result).upper()
        scopes = [
            ("GLOBAL", "ALL", "ANY"),
            ("GLOBAL_WEEKDAY", "ALL", "ANY"),
            ("SYMBOL", symbol, "ANY"),
            ("SYMBOL_DIRECTION", symbol, direction),
        ]
        weekdays = {"GLOBAL": "ANYDAY", "SYMBOL": "ANYDAY", "GLOBAL_WEEKDAY": info.weekday, "SYMBOL_DIRECTION": info.weekday}
        with self._connect() as conn:
            for scope, prof_symbol, prof_direction in scopes:
                weekday = weekdays.get(scope, "ANYDAY")
                key = "|".join([scope, prof_symbol, prof_direction, info.name, info.hour_bucket, weekday])
                row = conn.execute("SELECT * FROM time_risk_profiles WHERE profile_key=?", (key,)).fetchone()
                if row:
                    samples = int(row["samples"] or 0) + 1
                    tp = int(row["tp"] or 0) + (1 if result == "TP" else 0)
                    sl = int(row["sl"] or 0) + (1 if result == "SL" else 0)
                    risk = int(row["risk_score"] or 0)
                    if result == "SL":
                        risk += 10 if scope.startswith("GLOBAL") else 12
                    else:
                        risk -= 5
                    risk = max(0, min(100, risk))
                    max_consecutive = int(row["max_consecutive_sl"] or 0)
                else:
                    samples = 1
                    tp = 1 if result == "TP" else 0
                    sl = 1 if result == "SL" else 0
                    risk = 10 if result == "SL" else 0
                    max_consecutive = 1 if result == "SL" else 0
                sl_rate = (sl / max(samples, 1)) * 100.0
                action = "ALLOW"
                if samples >= 4 and sl >= 3 and sl_rate >= 70:
                    action = "BLOCK"
                elif samples >= 3 and sl >= 2 and sl_rate >= 60:
                    action = "REAL_BLOCK"
                elif samples >= 2 and sl >= 2:
                    action = "CAUTION"
                if risk >= 70:
                    action = "BLOCK"
                elif risk >= 45 and action == "ALLOW":
                    action = "REAL_BLOCK"
                elif risk >= 25 and action == "ALLOW":
                    action = "CAUTION"
                conn.execute("""
                    INSERT INTO time_risk_profiles(profile_key, scope, symbol_name, direction, session_name, hour_bucket, weekday, samples, tp, sl, max_consecutive_sl, risk_score, main_cause, action, last_updated)
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(profile_key) DO UPDATE SET
                        samples=excluded.samples, tp=excluded.tp, sl=excluded.sl, max_consecutive_sl=MAX(time_risk_profiles.max_consecutive_sl, excluded.max_consecutive_sl),
                        risk_score=excluded.risk_score, main_cause=excluded.main_cause, action=excluded.action, last_updated=excluded.last_updated
                """, (key, scope, prof_symbol, prof_direction, info.name, info.hour_bucket, weekday, samples, tp, sl, max_consecutive, risk, cause, action, now_utc().isoformat()))

    def get_time_risk_profile(self, *, symbol_name: str, direction: str | None = None, at: datetime | None = None) -> dict[str, Any] | None:
        info = session_info(at)
        symbol = str(symbol_name or "ALL")
        direction_value = str(direction or "ANY")
        keys = [
            "|".join(["SYMBOL_DIRECTION", symbol, direction_value, info.name, info.hour_bucket, info.weekday]),
            "|".join(["SYMBOL", symbol, "ANY", info.name, info.hour_bucket, "ANYDAY"]),
            "|".join(["GLOBAL_WEEKDAY", "ALL", "ANY", info.name, info.hour_bucket, info.weekday]),
            "|".join(["GLOBAL", "ALL", "ANY", info.name, info.hour_bucket, "ANYDAY"]),
        ]
        with self._connect() as conn:
            rows = conn.execute(f"SELECT * FROM time_risk_profiles WHERE profile_key IN ({','.join('?' for _ in keys)})", keys).fetchall()
        if not rows:
            return None
        rows_dict = [dict(r) for r in rows]
        return max(rows_dict, key=lambda r: int(r.get("risk_score") or 0))

    def top_time_risk_profiles(self, limit: int = 5) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM time_risk_profiles WHERE action!='ALLOW' ORDER BY risk_score DESC, sl DESC, samples DESC LIMIT ?", (int(max(1, min(limit, 20))),)).fetchall()
        return [dict(row) for row in rows]


    # -----------------------------
    # Adaptive fix learning: every closed signal changes these profiles
    # -----------------------------
    def record_adaptive_fix(self, *, signal: dict[str, Any], result: str, cause: str, report: Any | None = None) -> None:
        result = str(result or "").upper()
        if result not in {"TP", "SL"}:
            return
        keys = self._adaptive_keys_from_signal(signal)
        symbol = str(signal.get("symbol_name") or "-")
        direction = str(signal.get("direction") or "-")
        market_state = str(signal.get("market_state") or "-")
        alignment = str(signal.get("alignment") or "-")
        dt = signal_time(signal)
        info = session_info(dt)
        weekday = info.weekday
        net_profit = float(signal.get("real_pnl") if signal.get("real_pnl") is not None else signal.get("approx_pnl") or 0.0)
        mfe = float(signal.get("mfe_pct") or 0.0)
        mae = float(signal.get("mae_pct") or 0.0)
        tp_distance = float(signal.get("tp_distance_pct") or 0.0)
        sl_distance = float(signal.get("sl_distance_pct") or 0.0)
        report_action = str(getattr(report, "action", "") or "")
        report_policy = str(getattr(report, "fix_policy", "") or "")
        report_level = int(getattr(report, "treatment_level", 0) or 0)
        report_message = str(getattr(report, "message", "") or "")
        with self._connect() as conn:
            for scope, key in keys:
                row = conn.execute("SELECT * FROM adaptive_fix_profiles WHERE profile_key=?", (key,)).fetchone()
                if row:
                    samples = int(row["samples"] or 0) + 1
                    tp = int(row["tp"] or 0) + (1 if result == "TP" else 0)
                    sl = int(row["sl"] or 0) + (1 if result == "SL" else 0)
                    total_net = float(row["total_net_profit"] or 0.0) + net_profit
                    avg_mfe = ((float(row["avg_mfe_pct"] or 0.0) * (samples - 1)) + mfe) / samples
                    avg_mae = ((float(row["avg_mae_pct"] or 0.0) * (samples - 1)) + mae) / samples
                    avg_tp = ((float(row["avg_tp_distance_pct"] or 0.0) * (samples - 1)) + tp_distance) / samples
                    avg_sl = ((float(row["avg_sl_distance_pct"] or 0.0) * (samples - 1)) + sl_distance) / samples
                    risk = int(row["risk_score"] or 0)
                    tests = int(row["tests"] or 0) + 1
                    successes = int(row["successes"] or 0) + (1 if result == "TP" else 0)
                    failures = int(row["failures"] or 0) + (1 if result == "SL" else 0)
                    consecutive = (int(row["consecutive_failures"] or 0) + 1) if result == "SL" else 0
                    level = int(row["treatment_level"] or 0)
                else:
                    samples = 1
                    tp = 1 if result == "TP" else 0
                    sl = 1 if result == "SL" else 0
                    total_net = net_profit
                    avg_mfe = mfe
                    avg_mae = mae
                    avg_tp = tp_distance
                    avg_sl = sl_distance
                    risk = 0
                    tests = 1
                    successes = 1 if result == "TP" else 0
                    failures = 1 if result == "SL" else 0
                    consecutive = 1 if result == "SL" else 0
                    level = 0

                # Every single result matters. First SL creates a real but light treatment.
                if result == "SL":
                    add = 18
                    if cause in {"WRONG_DIRECTION_OR_CONTEXT", "FAKE_BREAKOUT_OR_CLIMAX", "NEWS_RISK", "SESSION_OPEN_NOISE", "MARKET_NOISE_OR_RANGE", "HTF_ALIGNMENT_WEAKNESS", "BTC_ETH_CONFLICT"}:
                        add += 8
                    if cause in {"ECONOMIC_EDGE_TOO_SMALL", "FEE_TOO_HEAVY_FOR_TARGET"}:
                        add += 10
                    if scope == "FEATURE":
                        add += 4
                    risk += add
                    level = max(level, report_level or 1)
                    if consecutive >= 2:
                        level += 1
                    if consecutive >= 3 or (failures >= 4 and total_net < 0):
                        level = max(level, 4)
                else:
                    risk -= 12
                    if net_profit > 0:
                        risk -= 4
                    if level > 0:
                        level -= 1
                if total_net < 0:
                    risk += 3
                risk = max(0, min(100, risk))
                level = max(0, min(5, level))

                sl_rate = sl / max(samples, 1)
                action = "ALLOW"
                if result == "SL" and samples == 1 and scope in {"FEATURE", "SYMBOL_DIRECTION", "SYMBOL_STATE"}:
                    action = "CAUTION"
                if report_action in {"NEWS_PAUSE", "SESSION_PAUSE"}:
                    # The actual hard pause remains in news/session guard. For future similar cases,
                    # adaptive layer only downgrades to Watch/Normal.
                    action = "WATCH_ONLY" if level >= 4 else "REAL_BLOCK"
                elif report_action == "WATCH_ONLY":
                    action = "WATCH_ONLY"
                elif report_action == "REAL_BLOCK":
                    action = "REAL_BLOCK"
                elif report_action == "CAUTION":
                    action = "CAUTION"

                if level >= 5:
                    action = "WATCH_ONLY"
                elif level >= 4 and total_net < 0:
                    action = "WATCH_ONLY"
                elif level >= 3 and action == "ALLOW":
                    action = "REAL_BLOCK"
                elif level >= 1 and action == "ALLOW":
                    action = "CAUTION"
                if samples >= 3 and sl_rate >= 0.60 and total_net < 0:
                    action = "REAL_BLOCK" if action != "WATCH_ONLY" else action
                if risk >= 70 and total_net < 0:
                    action = "WATCH_ONLY"
                elif risk >= 45 and action == "ALLOW":
                    action = "REAL_BLOCK"
                elif risk >= 22 and action == "ALLOW":
                    action = "CAUTION"

                # Learned price cure candidates. Applied later only if RR/net-profit after fees stays valid.
                rec_tp = avg_tp
                if avg_mfe > 0 and (cause == "TP_TOO_FAR_OR_REVERSAL" or avg_mfe < avg_tp * 0.92):
                    rec_tp = max(avg_mfe * 0.72, avg_tp * 0.58)
                rec_sl = avg_sl
                if avg_mae > avg_sl * 0.85 or cause in {"STOP_TOO_TIGHT", "SL_HIT_AFTER_NOISE_OR_BAD_RANGE"}:
                    # First single SL can propose a SMALL test widening; repeated failures widen more.
                    factor = 1.10 if samples <= 1 else (1.18 if level <= 2 else 1.28)
                    rec_sl = max(avg_sl, avg_mae * factor)

                conn.execute("""
                    INSERT INTO adaptive_fix_profiles(profile_key, scope, symbol_name, direction, market_state, alignment, session_name, hour_bucket, weekday, samples, tp, sl, total_net_profit, avg_mfe_pct, avg_mae_pct, avg_tp_distance_pct, avg_sl_distance_pct, risk_score, last_cause, recommended_action, recommended_tp_pct, recommended_sl_pct, last_updated, treatment_level, tests, successes, failures, consecutive_failures, fix_policy, last_message)
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(profile_key) DO UPDATE SET
                        samples=excluded.samples, tp=excluded.tp, sl=excluded.sl, total_net_profit=excluded.total_net_profit,
                        avg_mfe_pct=excluded.avg_mfe_pct, avg_mae_pct=excluded.avg_mae_pct,
                        avg_tp_distance_pct=excluded.avg_tp_distance_pct, avg_sl_distance_pct=excluded.avg_sl_distance_pct,
                        risk_score=excluded.risk_score, last_cause=excluded.last_cause,
                        recommended_action=excluded.recommended_action, recommended_tp_pct=excluded.recommended_tp_pct,
                        recommended_sl_pct=excluded.recommended_sl_pct, last_updated=excluded.last_updated,
                        treatment_level=excluded.treatment_level, tests=excluded.tests, successes=excluded.successes,
                        failures=excluded.failures, consecutive_failures=excluded.consecutive_failures,
                        fix_policy=excluded.fix_policy, last_message=excluded.last_message
                """, (key, scope, symbol, direction, market_state, alignment, info.name, info.hour_bucket, weekday, samples, tp, sl, total_net, avg_mfe, avg_mae, avg_tp, avg_sl, risk, cause, action, rec_tp, rec_sl, now_utc().isoformat(), level, tests, successes, failures, consecutive, report_policy, report_message[:1000]))
                conn.execute("INSERT INTO treatment_tests(profile_key, signal_id, created_at, result, net_profit, treatment_level, fix_policy, cause) VALUES(?, ?, ?, ?, ?, ?, ?, ?)", (key, int(signal.get("id") or 0), now_utc().isoformat(), result, net_profit, level, report_policy, cause))

    def record_loss_case(self, *, signal: dict[str, Any], report: Any) -> None:
        if str(signal.get("status") or "").upper() != "SL":
            return
        with self._connect() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO loss_cases(signal_id, created_at, symbol_name, direction, primary_cause, secondary_causes, cause_scores, fix_policy, action, treatment_level, message, indicator_suggestion)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                int(signal.get("id") or 0), now_utc().isoformat(), str(signal.get("symbol_name") or "-"), str(signal.get("direction") or "-"),
                str(getattr(report, "primary_cause", "UNKNOWN_SL_REASON")), json.dumps(list(getattr(report, "secondary_causes", ())), ensure_ascii=False),
                json.dumps(getattr(report, "cause_scores", {}), ensure_ascii=False), str(getattr(report, "fix_policy", "")), str(getattr(report, "action", "")),
                int(getattr(report, "treatment_level", 0) or 0), str(getattr(report, "message", ""))[:1200], str(getattr(report, "indicator_suggestion", "") or "")[:600],
            ))
        suggestion = getattr(report, "indicator_suggestion", None)
        if suggestion:
            self.record_indicator_request_once("STOP_FORENSIC", str(suggestion))

    def record_indicator_request_once(self, indicator: str, reason: str) -> None:
        reason = (reason or "")[:1000]
        if not reason:
            return
        with self._connect() as conn:
            recent = conn.execute("SELECT reason FROM indicator_requests ORDER BY id DESC LIMIT 5").fetchall()
            if any(str(r["reason"]) == reason for r in recent):
                return
            conn.execute("INSERT INTO indicator_requests(created_at, indicator, reason) VALUES(?, ?, ?)", (now_utc().isoformat(), indicator[:80], reason))

    def latest_loss_cases(self, limit: int = 5) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM loss_cases ORDER BY id DESC LIMIT ?", (int(max(1, min(limit, 20))),)).fetchall()
        return [dict(row) for row in rows]

    def _adaptive_keys_from_signal(self, signal: dict[str, Any]) -> list[tuple[str, str]]:
        symbol = str(signal.get("symbol_name") or "-")
        direction = str(signal.get("direction") or "-")
        features_key = str(signal.get("features_key") or "")
        market_state = str(signal.get("market_state") or "UNKNOWN")
        alignment = str(signal.get("alignment") or "UNKNOWN")
        info = session_info(signal_time(signal))
        keys: list[tuple[str, str]] = []
        if features_key:
            keys.append(("FEATURE", "FEATURE|" + features_key))
            parts = features_key.split("|")
            if len(parts) >= 12:
                indicator_signature = "|".join([direction, parts[3], parts[4], *parts[5:]])
                keys.append(("INDICATOR", "INDICATOR|" + indicator_signature))
        keys.append(("SYMBOL_DIRECTION", "SYMBOL_DIRECTION|" + "|".join([symbol, direction])))
        keys.append(("SYMBOL_STATE", "SYMBOL_STATE|" + "|".join([symbol, direction, market_state, alignment])))
        keys.append(("TIME", "TIME|" + "|".join([info.name, info.hour_bucket, info.weekday])))
        keys.append(("TIME_GLOBAL", "TIME_GLOBAL|" + "|".join([info.name, info.hour_bucket, "ANYDAY"])))
        return list(dict.fromkeys(keys))

    def get_adaptive_fix_profiles(self, keys: list[str]) -> list[dict[str, Any]]:
        keys = [str(k) for k in keys if str(k)]
        if not keys:
            return []
        with self._connect() as conn:
            rows = conn.execute(f"SELECT * FROM adaptive_fix_profiles WHERE profile_key IN ({','.join('?' for _ in keys)})", keys).fetchall()
        return [dict(row) for row in rows]

    def top_adaptive_fix_profiles(self, limit: int = 5) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM adaptive_fix_profiles WHERE recommended_action!='ALLOW' ORDER BY risk_score DESC, sl DESC, samples DESC LIMIT ?", (int(max(1, min(limit, 20))),)).fetchall()
        return [dict(row) for row in rows]

    def rebuild_adaptive_fix_profiles_from_history(self, limit: int = 5000) -> int:
        # Backfill the new stronger learning from existing closed signals so old 2000+ samples matter immediately.
        with self._connect() as conn:
            count_row = conn.execute("SELECT COUNT(*) AS n FROM adaptive_fix_profiles").fetchone()
            if int(count_row["n"] or 0) > 0:
                return 0
            rows = conn.execute("SELECT * FROM signals WHERE status IN ('TP','SL') ORDER BY COALESCE(result_at, created_at) ASC, id ASC LIMIT ?", (int(limit),)).fetchall()
        from stop_forensic_engine import StopForensicEngine
        forensic = StopForensicEngine(self)
        done = 0
        for row in rows:
            signal = dict(row)
            status = str(signal.get("status") or "")
            report = forensic.analyze(signal)
            cause = report.primary_cause if status == "SL" else "TP_OK"
            if status == "SL":
                self.record_loss_case(signal=signal, report=report)
            self.record_adaptive_fix(signal=signal, result=status, cause=cause, report=report)
            done += 1
        if done:
            self._set_setting("adaptive_fix_rebuild_done", now_utc().isoformat())
        return done

    @staticmethod
    def _legacy_failure_reason(signal: dict[str, Any]) -> str:
        mae = float(signal.get("mae_pct") or 0)
        sl_dist = float(signal.get("sl_distance_pct") or 0)
        mfe = float(signal.get("mfe_pct") or 0)
        tp_dist = float(signal.get("tp_distance_pct") or 0)
        market_state = str(signal.get("market_state") or "")
        reason = str(signal.get("reason") or "")
        if "خبر" in reason or "NEWS" in reason.upper():
            return "NEWS_RISK"
        if "BTC" in reason or "ETH" in reason:
            return "BTC_ETH_CONFLICT"
        if sl_dist > 0 and mae >= sl_dist * 0.95 and mfe < tp_dist * 0.25:
            return "DIRECTION_OR_ENTRY_WRONG"
        if mfe >= tp_dist * 0.60:
            return "TP_TOO_FAR_OR_REVERSAL"
        if market_state in {"CLIMAX", "FAKE_BREAKOUT_RISK"}:
            return "CLIMAX_OR_FAKE_BREAKOUT"
        if sl_dist > 0 and mae <= sl_dist * 1.05:
            return "SL_HIT_AFTER_NOISE_OR_BAD_RANGE"
        return "UNKNOWN_SL_REASON"

    def get_range_profile(self, features_key: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM range_profiles WHERE features_key=?", (features_key,)).fetchone()
            return dict(row) if row else None

    def record_observation(self, *, source: str, signal_id: int | None, features_key: str, symbol_name: str, direction: str, result: str, net_profit: float, mfe_pct: float, mae_pct: float, tp_distance_pct: float, sl_distance_pct: float, reason: str = "") -> None:
        with self._connect() as conn:
            conn.execute("INSERT INTO range_observations(created_at, source, signal_id, features_key, symbol_name, direction, result, net_profit, mfe_pct, mae_pct, tp_distance_pct, sl_distance_pct, reason) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (now_utc().isoformat(), source, signal_id, features_key, symbol_name, direction, result, net_profit, mfe_pct, mae_pct, tp_distance_pct, sl_distance_pct, reason))
        self._refresh_range_profile(features_key, symbol_name, direction)

    def _refresh_range_profile(self, features_key: str, symbol_name: str, direction: str) -> None:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM range_observations WHERE features_key=?", (features_key,)).fetchall()
            samples = len(rows)
            if samples == 0:
                return
            tp = sum(1 for r in rows if str(r["result"]) == "TP")
            sl = sum(1 for r in rows if str(r["result"]) == "SL")
            net = sum(float(r["net_profit"] or 0) for r in rows)
            avg_mfe = sum(float(r["mfe_pct"] or 0) for r in rows) / samples
            avg_mae = sum(float(r["mae_pct"] or 0) for r in rows) / samples
            best_tp = sum(float(r["tp_distance_pct"] or 0) for r in rows if str(r["result"]) == "TP") / max(tp, 1)
            best_sl = avg_mae * 1.25 if avg_mae > 0 else 0.0
            win_rate = tp / samples * 100.0
            confidence = int(max(0, min(100, win_rate * 0.6 + min(samples, 150) * 0.25 + (10 if net > 0 else -10))))
            conn.execute("INSERT INTO range_profiles(features_key, symbol_name, direction, samples, tp, sl, win_rate, net_profit, avg_mfe_pct, avg_mae_pct, best_tp_pct, best_sl_pct, confidence, last_updated) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(features_key) DO UPDATE SET samples=excluded.samples, tp=excluded.tp, sl=excluded.sl, win_rate=excluded.win_rate, net_profit=excluded.net_profit, avg_mfe_pct=excluded.avg_mfe_pct, avg_mae_pct=excluded.avg_mae_pct, best_tp_pct=excluded.best_tp_pct, best_sl_pct=excluded.best_sl_pct, confidence=excluded.confidence, last_updated=excluded.last_updated", (features_key, symbol_name, direction, samples, tp, sl, win_rate, net, avg_mfe, avg_mae, best_tp, best_sl, confidence, now_utc().isoformat()))
            self._refresh_symbol_profile_conn(conn, symbol_name, direction)

    def _refresh_symbol_profile_conn(self, conn: sqlite3.Connection, symbol_name: str, direction: str) -> None:
        rows = conn.execute("SELECT * FROM range_observations WHERE symbol_name=? AND direction=?", (symbol_name, direction)).fetchall()
        samples = len(rows)
        if samples == 0:
            return
        tp = sum(1 for r in rows if str(r["result"]) == "TP")
        sl = sum(1 for r in rows if str(r["result"]) == "SL")
        net = sum(float(r["net_profit"] or 0) for r in rows)
        win_rate = tp / samples * 100.0
        confidence = int(max(0, min(100, win_rate * 0.55 + min(samples, 200) * 0.20 + (15 if net > 0 else -15))))
        conn.execute("INSERT INTO symbol_direction_profiles(symbol_name, direction, samples, tp, sl, win_rate, net_profit, confidence, last_updated) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(symbol_name, direction) DO UPDATE SET samples=excluded.samples, tp=excluded.tp, sl=excluded.sl, win_rate=excluded.win_rate, net_profit=excluded.net_profit, confidence=excluded.confidence, last_updated=excluded.last_updated", (symbol_name, direction, samples, tp, sl, win_rate, net, confidence, now_utc().isoformat()))

    def today_stats(self) -> dict[str, Any]:
        start = now_utc().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM signals WHERE created_at>=?", (start,)).fetchall()
        return self._stats_from_rows(rows)

    def all_stats(self) -> dict[str, Any]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM signals").fetchall()
        return self._stats_from_rows(rows)

    def ai_summary(self) -> dict[str, Any]:
        with self._connect() as conn:
            best = conn.execute("SELECT * FROM symbol_direction_profiles ORDER BY net_profit DESC, win_rate DESC LIMIT 1").fetchone()
            worst = conn.execute("SELECT * FROM symbol_direction_profiles ORDER BY net_profit ASC, win_rate ASC LIMIT 1").fetchone()
            profiles = conn.execute("SELECT * FROM symbol_direction_profiles").fetchall()
            suggestions = conn.execute("SELECT * FROM capital_suggestions WHERE status='open' ORDER BY id DESC LIMIT 3").fetchall()
            requests = conn.execute("SELECT * FROM indicator_requests WHERE status='open' ORDER BY id DESC LIMIT 3").fetchall()
        total_samples = sum(int(p["samples"] or 0) for p in profiles)
        avg_conf = sum(float(p["confidence"] or 0) for p in profiles) / len(profiles) if profiles else 0.0
        return {"best": dict(best) if best else None, "worst": dict(worst) if worst else None, "total_samples": total_samples, "confidence": avg_conf, "suggestions": [dict(x) for x in suggestions], "requests": [dict(x) for x in requests]}

    def reset_stats(self) -> None:
        with self._connect() as conn:
            for table in ("signals", "signal_snapshots", "range_profiles", "range_observations", "shadow_tests", "historical_replay_runs", "missed_opportunities", "symbol_direction_profiles", "session_profiles", "capital_suggestions", "indicator_requests", "toobit_orders", "no_signal_log", "guard_events", "guard_alerts", "time_risk_profiles", "adaptive_fix_profiles", "signal_forensic_snapshots", "loss_cases", "treatment_tests"):
                conn.execute(f"DELETE FROM {table}")
            conn.execute("INSERT INTO settings(key, value) VALUES('guard_cooldown_until', '') ON CONFLICT(key) DO UPDATE SET value='' ")
            conn.execute("INSERT INTO settings(key, value) VALUES('guard_cooldown_reason', '') ON CONFLICT(key) DO UPDATE SET value='' ")

    def _stats_from_rows(self, rows: list[sqlite3.Row]) -> dict[str, Any]:
        closed = [r for r in rows if str(r["status"]) in {"TP", "SL"}]
        tp = sum(1 for r in closed if str(r["status"]) == "TP")
        sl = sum(1 for r in closed if str(r["status"]) == "SL")
        real = sum(1 for r in rows if str(r["signal_type"]) == "real")
        normal = sum(1 for r in rows if str(r["signal_type"]) == "normal")
        watch = sum(1 for r in rows if str(r["signal_type"]) == "watch")

        def row_net(row: sqlite3.Row) -> float:
            return float(row["real_pnl"] if row["real_pnl"] is not None else row["approx_pnl"] or 0.0)

        pnl = sum(row_net(r) for r in closed)
        real_pnl = sum(row_net(r) for r in closed if str(r["signal_type"]) == "real")
        normal_pnl = sum(row_net(r) for r in closed if str(r["signal_type"]) == "normal")
        watch_pnl = sum(row_net(r) for r in closed if str(r["signal_type"]) == "watch")
        fees = sum(round_trip_fee_usdt(float(r["margin_usdt"] or 0.0), int(r["leverage"] or 1)) for r in closed)
        return {
            "total": len(rows), "open": sum(1 for r in rows if str(r["status"]) == "OPEN"), "closed": len(closed),
            "real": real, "normal": normal, "watch": watch, "tp": tp, "sl": sl,
            "win_rate": tp / len(closed) * 100.0 if closed else 0.0,
            "pnl": pnl, "real_pnl": real_pnl, "normal_pnl": normal_pnl, "watch_pnl": watch_pnl, "fees": fees,
        }

    def _row_to_signal(self, row: sqlite3.Row) -> StoredSignal:
        return StoredSignal(
            id=int(row["id"]), created_at=str(row["created_at"]), okx_symbol=str(row["okx_symbol"]), toobit_symbol=str(row["toobit_symbol"]), symbol_name=str(row["symbol_name"]), direction=str(row["direction"]), entry=float(row["entry"]), tp=float(row["tp"]), sl=float(row["sl"]), status=str(row["status"]), signal_type=str(row["signal_type"]), real_status=str(row["real_status"]), message_id=row["message_id"], result_message_id=row["result_message_id"], order_id=row["order_id"], margin_usdt=float(row["margin_usdt"]), leverage=int(row["leverage"]), features_key=str(row["features_key"] or ""), approx_pnl=row["approx_pnl"], real_pnl=row["real_pnl"], best_price=float(row["best_price"] or row["entry"]), worst_price=float(row["worst_price"] or row["entry"]), mfe_pct=float(row["mfe_pct"] or 0), mae_pct=float(row["mae_pct"] or 0)
        )
