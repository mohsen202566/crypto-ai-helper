from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import config
from utils import now_utc


@dataclass(frozen=True)
class StoredSignal:
    id: int
    created_at: str
    okx_symbol: str
    toobit_symbol: str
    symbol_name: str
    status: str
    signal_type: str
    message_id: int | None
    result_message_id: int | None
    buy_order_id: str | None
    sell_order_id: str | None
    trade_usdt: float
    entry_price: float
    target_price: float
    quantity: float
    buy_fee_usdt: float
    sell_fee_usdt: float
    estimated_net_profit_usdt: float
    confidence: int
    features_key: str
    market_state: str
    alignment: str
    reason: str
    best_price: float
    worst_price: float
    mfe_pct: float
    mae_pct: float
    last_warning_at: str | None


class Storage:
    def __init__(self, db_path: str = config.DB_PATH) -> None:
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
                    status TEXT NOT NULL DEFAULT 'OPEN',
                    signal_type TEXT NOT NULL,
                    message_id INTEGER,
                    result_message_id INTEGER,
                    buy_order_id TEXT,
                    sell_order_id TEXT,
                    trade_usdt REAL NOT NULL,
                    entry_price REAL NOT NULL,
                    target_price REAL NOT NULL,
                    quantity REAL DEFAULT 0,
                    buy_fee_usdt REAL DEFAULT 0,
                    sell_fee_usdt REAL DEFAULT 0,
                    estimated_net_profit_usdt REAL DEFAULT 0,
                    estimated_fee_usdt REAL DEFAULT 0,
                    predicted_move_pct REAL DEFAULT 0,
                    target_distance_pct REAL DEFAULT 0,
                    expected_hold_minutes INTEGER DEFAULT 0,
                    confidence INTEGER DEFAULT 0,
                    samples INTEGER DEFAULT 0,
                    win_rate REAL DEFAULT 0,
                    features_key TEXT,
                    market_state TEXT,
                    alignment TEXT,
                    indicator_profile TEXT,
                    reason TEXT,
                    best_price REAL DEFAULT 0,
                    worst_price REAL DEFAULT 0,
                    mfe_pct REAL DEFAULT 0,
                    mae_pct REAL DEFAULT 0,
                    result_at TEXT,
                    exit_price REAL,
                    approx_pnl REAL,
                    real_pnl REAL,
                    result_source TEXT,
                    last_warning_at TEXT,
                    warning_count INTEGER DEFAULT 0
                )
            """)
            conn.execute("CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY AUTOINCREMENT, signal_id INTEGER, symbol TEXT, side TEXT, order_id TEXT, status TEXT, reason TEXT, created_at TEXT)")
            conn.execute("CREATE TABLE IF NOT EXISTS signal_snapshots(id INTEGER PRIMARY KEY AUTOINCREMENT, signal_id INTEGER, created_at TEXT, price REAL, mfe_pct REAL, mae_pct REAL)")
            conn.execute("CREATE TABLE IF NOT EXISTS range_profiles(features_key TEXT PRIMARY KEY, symbol_name TEXT, samples INTEGER DEFAULT 0, target INTEGER DEFAULT 0, missed INTEGER DEFAULT 0, win_rate REAL DEFAULT 0, net_profit REAL DEFAULT 0, avg_mfe_pct REAL DEFAULT 0, avg_mae_pct REAL DEFAULT 0, best_target_pct REAL DEFAULT 0, confidence INTEGER DEFAULT 0, last_updated TEXT)")
            conn.execute("CREATE TABLE IF NOT EXISTS range_observations(id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, source TEXT, signal_id INTEGER, features_key TEXT, symbol_name TEXT, result TEXT, net_profit REAL, mfe_pct REAL, mae_pct REAL, target_distance_pct REAL, reason TEXT)")
            conn.execute("CREATE TABLE IF NOT EXISTS historical_replay_runs(id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, symbol_name TEXT, days INTEGER, observations INTEGER, notes TEXT)")
            conn.execute("CREATE TABLE IF NOT EXISTS missed_opportunities(id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, symbol_name TEXT, features_key TEXT, future_mfe_pct REAL, reason TEXT)")
            conn.execute("CREATE TABLE IF NOT EXISTS shadow_targets(id INTEGER PRIMARY KEY AUTOINCREMENT, signal_id INTEGER, name TEXT, target_price REAL, result TEXT DEFAULT 'pending', created_at TEXT, updated_at TEXT)")
            conn.execute("CREATE TABLE IF NOT EXISTS active_warnings(id INTEGER PRIMARY KEY AUTOINCREMENT, signal_id INTEGER, created_at TEXT, reason TEXT, current_price REAL, distance_to_target_pct REAL)")
            conn.execute("CREATE TABLE IF NOT EXISTS warning_results(id INTEGER PRIMARY KEY AUTOINCREMENT, warning_id INTEGER, result TEXT, notes TEXT, created_at TEXT)")
            conn.execute("CREATE TABLE IF NOT EXISTS symbol_profiles(symbol_name TEXT PRIMARY KEY, samples INTEGER DEFAULT 0, target INTEGER DEFAULT 0, missed INTEGER DEFAULT 0, win_rate REAL DEFAULT 0, net_profit REAL DEFAULT 0, confidence INTEGER DEFAULT 0, last_updated TEXT)")
            conn.execute("CREATE TABLE IF NOT EXISTS session_profiles(session_bucket TEXT PRIMARY KEY, samples INTEGER DEFAULT 0, target INTEGER DEFAULT 0, missed INTEGER DEFAULT 0, net_profit REAL DEFAULT 0)")
            conn.execute("CREATE TABLE IF NOT EXISTS capital_suggestions(id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, level TEXT, message TEXT, status TEXT DEFAULT 'open')")
            conn.execute("CREATE TABLE IF NOT EXISTS indicator_requests(id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, indicator TEXT, reason TEXT, status TEXT DEFAULT 'open')")
            conn.execute("CREATE TABLE IF NOT EXISTS no_signal_log(id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, symbol_name TEXT, reason TEXT, features_key TEXT)")
            conn.execute("CREATE TABLE IF NOT EXISTS symbol_health(symbol_name TEXT PRIMARY KEY, ok INTEGER DEFAULT 1, errors INTEGER DEFAULT 0, last_error TEXT, updated_at TEXT)")
            self._set_default(conn, "trade_enabled", "1" if config.DEFAULT_TRADE_ENABLED else "0")
            self._set_default(conn, "auto_signals_enabled", "1")
            self._set_default(conn, "trade_usdt", str(config.DEFAULT_TRADE_USDT))
            self._set_default(conn, "max_positions", str(config.DEFAULT_MAX_POSITIONS))
            self._set_default(conn, "last_scan_info", "{}")

    @staticmethod
    def _set_default(conn: sqlite3.Connection, key: str, value: str) -> None:
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

    def trade_usdt(self) -> float:
        return float(self._get_setting("trade_usdt", str(config.DEFAULT_TRADE_USDT)))

    def set_trade_usdt(self, value: float) -> None:
        if not config.TRADE_USDT_MIN <= value <= config.TRADE_USDT_MAX:
            raise ValueError("دلار ترید باید بین 1 تا 10000 باشد.")
        self._set_setting("trade_usdt", str(float(value)))

    def max_positions(self) -> int:
        return int(float(self._get_setting("max_positions", str(config.DEFAULT_MAX_POSITIONS))))

    def set_max_positions(self, value: int) -> None:
        if not config.MAX_POSITIONS_MIN <= value <= config.MAX_POSITIONS_MAX:
            raise ValueError("حداکثر پوزیشن باید بین 1 تا 200 باشد.")
        self._set_setting("max_positions", str(int(value)))

    def add_signal(self, decision, signal_type: str) -> int:
        with self._connect() as conn:
            cur = conn.execute("""
                INSERT INTO signals(created_at, okx_symbol, toobit_symbol, symbol_name, signal_type, trade_usdt, entry_price, target_price, estimated_net_profit_usdt, estimated_fee_usdt, predicted_move_pct, target_distance_pct, expected_hold_minutes, confidence, samples, win_rate, features_key, market_state, alignment, indicator_profile, reason, best_price, worst_price)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (now_utc().isoformat(), decision.okx_symbol, decision.toobit_symbol, decision.symbol_name, signal_type, self.trade_usdt(), decision.entry, decision.target, decision.estimated_net_profit_usdt, decision.estimated_fee_usdt, decision.predicted_move_pct, decision.target_distance_pct, decision.expected_hold_minutes, decision.confidence, decision.samples, decision.win_rate, decision.features_key, decision.market_state, decision.alignment, decision.indicator_profile, decision.reason, decision.entry, decision.entry))
            signal_id = int(cur.lastrowid)
            for name, target in decision.shadows:
                conn.execute("INSERT INTO shadow_targets(signal_id, name, target_price, created_at) VALUES(?, ?, ?, ?)", (signal_id, name, target, now_utc().isoformat()))
            return signal_id

    def update_message_id(self, signal_id: int, message_id: int | None) -> None:
        if message_id is None:
            return
        with self._connect() as conn:
            conn.execute("UPDATE signals SET message_id=? WHERE id=?", (message_id, signal_id))

    def mark_buy_order(self, signal_id: int, order_id: str | None, reason: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE signals SET buy_order_id=? WHERE id=?", (order_id, signal_id))
            conn.execute("INSERT INTO orders(signal_id, symbol, side, order_id, status, reason, created_at) SELECT id, toobit_symbol, 'BUY', ?, 'submitted', ?, ? FROM signals WHERE id=?", (order_id, reason, now_utc().isoformat(), signal_id))

    def mark_buy_filled(self, signal_id: int, *, quantity: float, entry_price: float, fee_usdt: float) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE signals SET quantity=?, entry_price=?, buy_fee_usdt=?, best_price=?, worst_price=? WHERE id=? AND status='OPEN'", (quantity, entry_price, fee_usdt, entry_price, entry_price, signal_id))

    def mark_sell_order(self, signal_id: int, order_id: str | None, target_price: float) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE signals SET sell_order_id=?, target_price=? WHERE id=? AND status='OPEN'", (order_id, target_price, signal_id))
            conn.execute("INSERT INTO orders(signal_id, symbol, side, order_id, status, reason, created_at) SELECT id, toobit_symbol, 'SELL', ?, 'submitted', 'target limit sell', ? FROM signals WHERE id=?", (order_id, now_utc().isoformat(), signal_id))

    def fail_signal(self, signal_id: int, reason: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE signals SET status='FAILED', result_at=?, reason=COALESCE(reason,'') || ? WHERE id=? AND status='OPEN'", (now_utc().isoformat(), f" | {reason}", signal_id))

    def open_signals(self) -> list[StoredSignal]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM signals WHERE status='OPEN' ORDER BY id ASC").fetchall()
            return [self._row_to_signal(row) for row in rows]

    def signal_by_id(self, signal_id: int) -> StoredSignal | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM signals WHERE id=?", (signal_id,)).fetchone()
            return self._row_to_signal(row) if row else None

    def active_symbol_exists(self, toobit_symbol: str) -> bool:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM signals WHERE status='OPEN' AND toobit_symbol=?", (toobit_symbol,)).fetchone()
            return int(row["n"]) > 0

    def active_real_count(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM signals WHERE status='OPEN' AND signal_type='real'").fetchone()
            return int(row["n"])

    def update_excursions(self, signal: StoredSignal, price: float) -> tuple[float, float]:
        best = max(signal.best_price, price)
        worst = min(signal.worst_price, price)
        mfe = max(0.0, (best - signal.entry_price) / signal.entry_price) if signal.entry_price > 0 else 0.0
        mae = max(0.0, (signal.entry_price - worst) / signal.entry_price) if signal.entry_price > 0 else 0.0
        with self._connect() as conn:
            conn.execute("UPDATE signals SET best_price=?, worst_price=?, mfe_pct=?, mae_pct=? WHERE id=?", (best, worst, mfe, mae, signal.id))
            conn.execute("INSERT INTO signal_snapshots(signal_id, created_at, price, mfe_pct, mae_pct) VALUES(?, ?, ?, ?, ?)", (signal.id, now_utc().isoformat(), price, mfe, mae))
        return mfe, mae

    def finish_signal(self, signal_id: int, *, status: str, exit_price: float, approx_pnl: float, real_pnl: float | None, sell_fee_usdt: float, result_message_id: int | None, result_source: str, mfe_pct: float, mae_pct: float) -> bool:
        with self._connect() as conn:
            cur = conn.execute("UPDATE signals SET status=?, exit_price=?, approx_pnl=?, real_pnl=?, sell_fee_usdt=?, result_message_id=?, result_source=?, result_at=?, mfe_pct=?, mae_pct=? WHERE id=? AND status='OPEN'", (status, exit_price, approx_pnl, real_pnl, sell_fee_usdt, result_message_id, result_source, now_utc().isoformat(), mfe_pct, mae_pct, signal_id))
            return cur.rowcount > 0

    def record_warning(self, signal_id: int, reason: str, current_price: float, distance_to_target_pct: float) -> None:
        with self._connect() as conn:
            conn.execute("INSERT INTO active_warnings(signal_id, created_at, reason, current_price, distance_to_target_pct) VALUES(?, ?, ?, ?, ?)", (signal_id, now_utc().isoformat(), reason, current_price, distance_to_target_pct))
            conn.execute("UPDATE signals SET last_warning_at=?, warning_count=warning_count+1 WHERE id=?", (now_utc().isoformat(), signal_id))

    def record_no_signal(self, symbol_name: str, reason: str, features_key: str = "") -> None:
        with self._connect() as conn:
            conn.execute("INSERT INTO no_signal_log(created_at, symbol_name, reason, features_key) VALUES(?, ?, ?, ?)", (now_utc().isoformat(), symbol_name, reason, features_key))

    def recent_rejections(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM no_signal_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return [dict(row) for row in rows]

    def set_scan_info(self, info: dict[str, Any]) -> None:
        self._set_setting("last_scan_info", json.dumps(info, ensure_ascii=False))

    def scan_info(self) -> dict[str, Any]:
        try:
            return json.loads(self._get_setting("last_scan_info", "{}"))
        except Exception:
            return {}

    def get_range_profile(self, features_key: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM range_profiles WHERE features_key=?", (features_key,)).fetchone()
            return dict(row) if row else None

    def record_observation(self, *, source: str, signal_id: int | None, features_key: str, symbol_name: str, result: str, net_profit: float, mfe_pct: float, mae_pct: float, target_distance_pct: float, reason: str) -> None:
        with self._connect() as conn:
            conn.execute("INSERT INTO range_observations(created_at, source, signal_id, features_key, symbol_name, result, net_profit, mfe_pct, mae_pct, target_distance_pct, reason) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (now_utc().isoformat(), source, signal_id, features_key, symbol_name, result, net_profit, mfe_pct, mae_pct, target_distance_pct, reason))
        self._refresh_profiles(features_key, symbol_name)

    def _refresh_profiles(self, features_key: str, symbol_name: str) -> None:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM range_observations WHERE features_key=?", (features_key,)).fetchall()
            self._write_profile(conn, "range_profiles", features_key, symbol_name, rows)
            srows = conn.execute("SELECT * FROM range_observations WHERE symbol_name=?", (symbol_name,)).fetchall()
            samples = len(srows)
            if samples:
                target = sum(1 for r in srows if str(r["result"]) == "TARGET")
                missed = sum(1 for r in srows if str(r["result"]) != "TARGET")
                net = sum(float(r["net_profit"] or 0) for r in srows)
                wr = target / samples * 100
                conf = int(max(0, min(100, wr * 0.55 + min(samples, 200) * 0.20 + (15 if net > 0 else -15))))
                conn.execute("INSERT INTO symbol_profiles(symbol_name, samples, target, missed, win_rate, net_profit, confidence, last_updated) VALUES(?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(symbol_name) DO UPDATE SET samples=excluded.samples,target=excluded.target,missed=excluded.missed,win_rate=excluded.win_rate,net_profit=excluded.net_profit,confidence=excluded.confidence,last_updated=excluded.last_updated", (symbol_name, samples, target, missed, wr, net, conf, now_utc().isoformat()))

    @staticmethod
    def _write_profile(conn: sqlite3.Connection, table: str, features_key: str, symbol_name: str, rows: list[sqlite3.Row]) -> None:
        samples = len(rows)
        if not samples:
            return
        target = sum(1 for r in rows if str(r["result"]) == "TARGET")
        missed = samples - target
        net = sum(float(r["net_profit"] or 0) for r in rows)
        avg_mfe = sum(float(r["mfe_pct"] or 0) for r in rows) / samples
        avg_mae = sum(float(r["mae_pct"] or 0) for r in rows) / samples
        best_target = sum(float(r["target_distance_pct"] or 0) for r in rows if str(r["result"]) == "TARGET") / max(target, 1)
        wr = target / samples * 100
        conf = int(max(0, min(100, wr * 0.6 + min(samples, 150) * 0.25 + (10 if net > 0 else -10))))
        if table == "range_profiles":
            conn.execute("INSERT INTO range_profiles(features_key, symbol_name, samples, target, missed, win_rate, net_profit, avg_mfe_pct, avg_mae_pct, best_target_pct, confidence, last_updated) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(features_key) DO UPDATE SET samples=excluded.samples,target=excluded.target,missed=excluded.missed,win_rate=excluded.win_rate,net_profit=excluded.net_profit,avg_mfe_pct=excluded.avg_mfe_pct,avg_mae_pct=excluded.avg_mae_pct,best_target_pct=excluded.best_target_pct,confidence=excluded.confidence,last_updated=excluded.last_updated", (features_key, symbol_name, samples, target, missed, wr, net, avg_mfe, avg_mae, best_target, conf, now_utc().isoformat()))

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
            best = conn.execute("SELECT * FROM symbol_profiles ORDER BY net_profit DESC, win_rate DESC LIMIT 1").fetchone()
            worst = conn.execute("SELECT * FROM symbol_profiles ORDER BY net_profit ASC, win_rate ASC LIMIT 1").fetchone()
            profiles = conn.execute("SELECT * FROM symbol_profiles").fetchall()
            suggestions = conn.execute("SELECT * FROM capital_suggestions WHERE status='open' ORDER BY id DESC LIMIT 3").fetchall()
            requests = conn.execute("SELECT * FROM indicator_requests WHERE status='open' ORDER BY id DESC LIMIT 3").fetchall()
            warnings = conn.execute("SELECT * FROM active_warnings ORDER BY id DESC LIMIT 3").fetchall()
        total = sum(int(p["samples"] or 0) for p in profiles)
        conf = sum(float(p["confidence"] or 0) for p in profiles) / len(profiles) if profiles else 0.0
        return {"best": dict(best) if best else None, "worst": dict(worst) if worst else None, "total_samples": total, "confidence": conf, "suggestions": [dict(x) for x in suggestions], "requests": [dict(x) for x in requests], "warnings": [dict(x) for x in warnings]}

    def add_capital_suggestion(self, level: str, message: str) -> None:
        with self._connect() as conn:
            conn.execute("INSERT INTO capital_suggestions(created_at, level, message) VALUES(?, ?, ?)", (now_utc().isoformat(), level, message))

    def reset_stats(self) -> None:
        with self._connect() as conn:
            for table in ("signals", "orders", "signal_snapshots", "range_profiles", "range_observations", "historical_replay_runs", "missed_opportunities", "shadow_targets", "active_warnings", "warning_results", "symbol_profiles", "session_profiles", "capital_suggestions", "indicator_requests", "no_signal_log", "symbol_health"):
                conn.execute(f"DELETE FROM {table}")

    @staticmethod
    def _stats_from_rows(rows: list[sqlite3.Row]) -> dict[str, Any]:
        closed = [r for r in rows if str(r["status"]) == "TARGET"]
        failed = [r for r in rows if str(r["status"]) == "FAILED"]
        real = sum(1 for r in rows if str(r["signal_type"]) == "real")
        normal = sum(1 for r in rows if str(r["signal_type"]) == "normal")
        pnl = sum(float(r["real_pnl"] if r["real_pnl"] is not None else r["approx_pnl"] or 0.0) for r in rows)
        return {"total": len(rows), "open": sum(1 for r in rows if str(r["status"]) == "OPEN"), "closed": len(closed), "failed": len(failed), "real": real, "normal": normal, "target": len(closed), "win_rate": len(closed) / max(len(closed) + len(failed), 1) * 100.0, "pnl": pnl}

    def _row_to_signal(self, row: sqlite3.Row) -> StoredSignal:
        return StoredSignal(id=int(row["id"]), created_at=str(row["created_at"]), okx_symbol=str(row["okx_symbol"]), toobit_symbol=str(row["toobit_symbol"]), symbol_name=str(row["symbol_name"]), status=str(row["status"]), signal_type=str(row["signal_type"]), message_id=row["message_id"], result_message_id=row["result_message_id"], buy_order_id=row["buy_order_id"], sell_order_id=row["sell_order_id"], trade_usdt=float(row["trade_usdt"]), entry_price=float(row["entry_price"]), target_price=float(row["target_price"]), quantity=float(row["quantity"] or 0), buy_fee_usdt=float(row["buy_fee_usdt"] or 0), sell_fee_usdt=float(row["sell_fee_usdt"] or 0), estimated_net_profit_usdt=float(row["estimated_net_profit_usdt"] or 0), confidence=int(row["confidence"] or 0), features_key=str(row["features_key"] or ""), market_state=str(row["market_state"] or ""), alignment=str(row["alignment"] or ""), reason=str(row["reason"] or ""), best_price=float(row["best_price"] or row["entry_price"]), worst_price=float(row["worst_price"] or row["entry_price"]), mfe_pct=float(row["mfe_pct"] or 0), mae_pct=float(row["mae_pct"] or 0), last_warning_at=row["last_warning_at"])
