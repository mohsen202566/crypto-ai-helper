from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from config import DEFAULT_LEVERAGE, DEFAULT_MARGIN_USDT, DEFAULT_MAX_POSITIONS, DEFAULT_TRADE_ENABLED, LEVERAGE_MAX, LEVERAGE_MIN, MARGIN_MAX_USDT, MARGIN_MIN_USDT, MAX_POSITIONS_MAX, MAX_POSITIONS_MIN, DB_PATH
from utils import direction_profit_pct, json_safe, now_utc


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
            self._set_default(conn, "trade_enabled", "1" if DEFAULT_TRADE_ENABLED else "0")
            self._set_default(conn, "margin_usdt", str(DEFAULT_MARGIN_USDT))
            self._set_default(conn, "leverage", str(DEFAULT_LEVERAGE))
            self._set_default(conn, "max_positions", str(DEFAULT_MAX_POSITIONS))
            self._set_default(conn, "auto_signals_enabled", "1")

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
            return int(cur.lastrowid)

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
            for table in ("signals", "signal_snapshots", "range_profiles", "range_observations", "shadow_tests", "historical_replay_runs", "missed_opportunities", "symbol_direction_profiles", "session_profiles", "capital_suggestions", "indicator_requests", "toobit_orders", "no_signal_log"):
                conn.execute(f"DELETE FROM {table}")

    def _stats_from_rows(self, rows: list[sqlite3.Row]) -> dict[str, Any]:
        closed = [r for r in rows if str(r["status"]) in {"TP", "SL"}]
        tp = sum(1 for r in closed if str(r["status"]) == "TP")
        sl = sum(1 for r in closed if str(r["status"]) == "SL")
        real = sum(1 for r in rows if str(r["signal_type"]) == "real")
        normal = sum(1 for r in rows if str(r["signal_type"]) == "normal")
        pnl = sum(float(r["real_pnl"] if r["real_pnl"] is not None else r["approx_pnl"] or 0.0) for r in rows)
        return {"total": len(rows), "open": sum(1 for r in rows if str(r["status"]) == "OPEN"), "closed": len(closed), "real": real, "normal": normal, "tp": tp, "sl": sl, "win_rate": tp / len(closed) * 100.0 if closed else 0.0, "pnl": pnl}

    def _row_to_signal(self, row: sqlite3.Row) -> StoredSignal:
        return StoredSignal(
            id=int(row["id"]), created_at=str(row["created_at"]), okx_symbol=str(row["okx_symbol"]), toobit_symbol=str(row["toobit_symbol"]), symbol_name=str(row["symbol_name"]), direction=str(row["direction"]), entry=float(row["entry"]), tp=float(row["tp"]), sl=float(row["sl"]), status=str(row["status"]), signal_type=str(row["signal_type"]), real_status=str(row["real_status"]), message_id=row["message_id"], result_message_id=row["result_message_id"], order_id=row["order_id"], margin_usdt=float(row["margin_usdt"]), leverage=int(row["leverage"]), features_key=str(row["features_key"] or ""), approx_pnl=row["approx_pnl"], real_pnl=row["real_pnl"], best_price=float(row["best_price"] or row["entry"]), worst_price=float(row["worst_price"] or row["entry"]), mfe_pct=float(row["mfe_pct"] or 0), mae_pct=float(row["mae_pct"] or 0)
        )
