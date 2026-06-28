from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from config import (
    DB_PATH,
    DEFAULT_LEVERAGE,
    DEFAULT_MARGIN_USDT,
    DEFAULT_MAX_POSITIONS,
    DEFAULT_MIN_PROFIT_PCT,
    DEFAULT_MIN_PROFIT_USDT,
    DEFAULT_TRADE_ENABLED,
    LEARNING_DAYS,
    LEVERAGE_MAX,
    LEVERAGE_MIN,
    MARGIN_MAX_USDT,
    MARGIN_MIN_USDT,
    MAX_POSITIONS_MAX,
    MAX_POSITIONS_MIN,
    MIN_PROFIT_PCT_MAX,
    MIN_PROFIT_PCT_MIN,
    MIN_PROFIT_USDT_MAX,
    MIN_PROFIT_USDT_MIN,
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
    estimated_profit_pct: float
    risk_reward: float
    reason: str | None


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
                    symbol_name TEXT NOT NULL DEFAULT '',
                    direction TEXT NOT NULL,
                    entry REAL NOT NULL,
                    tp REAL NOT NULL,
                    sl REAL NOT NULL,
                    score INTEGER NOT NULL,
                    threshold INTEGER DEFAULT 75,
                    ai_confidence INTEGER DEFAULT 0,
                    ai_experience INTEGER DEFAULT 0,
                    ai_adjustment INTEGER DEFAULT 0,
                    signal_type TEXT NOT NULL,
                    hunter_type TEXT NOT NULL DEFAULT 'ordinary',
                    signal_label TEXT NOT NULL DEFAULT 'عادی',
                    status TEXT NOT NULL DEFAULT 'OPEN',
                    real_status TEXT NOT NULL DEFAULT 'none',
                    message_id INTEGER,
                    result_message_id INTEGER,
                    real_opened INTEGER NOT NULL DEFAULT 0,
                    order_id TEXT,
                    approx_pnl REAL,
                    real_pnl REAL,
                    margin_usdt REAL,
                    leverage INTEGER,
                    result_at TEXT,
                    score_direction INTEGER DEFAULT 0,
                    score_pre_ignition INTEGER DEFAULT 0,
                    score_candle_entry INTEGER DEFAULT 0,
                    score_ai_memory INTEGER DEFAULT 0,
                    score_risk_net INTEGER DEFAULT 0,
                    score_session INTEGER DEFAULT 0,
                    score_order_block INTEGER DEFAULT 0,
                    direction_state_1h TEXT,
                    direction_confidence_1h INTEGER DEFAULT 0,
                    bias_4h TEXT,
                    setup_15m TEXT,
                    entry_5m TEXT,
                    candle_pattern TEXT,
                    entry_stage_pct REAL DEFAULT 100,
                    net_edge REAL DEFAULT 0,
                    estimated_profit_usdt REAL DEFAULT 0,
                    estimated_profit_pct REAL DEFAULT 0,
                    risk_reward REAL DEFAULT 0,
                    estimated_cost_pct REAL DEFAULT 0,
                    market_bias TEXT,
                    session_state TEXT,
                    order_block_state TEXT,
                    session_bucket TEXT,
                    reason TEXT,
                    notes TEXT,
                    real_open_reason TEXT,
                    actual_margin_usdt REAL,
                    quantity REAL,
                    mfe_pct REAL DEFAULT 0,
                    mae_pct REAL DEFAULT 0
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
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS rejection_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    symbol_name TEXT NOT NULL,
                    direction TEXT,
                    code TEXT,
                    reason TEXT,
                    score INTEGER DEFAULT 0
                )
                """
            )
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
            "threshold": "INTEGER DEFAULT 75",
            "ai_confidence": "INTEGER DEFAULT 0",
            "ai_experience": "INTEGER DEFAULT 0",
            "ai_adjustment": "INTEGER DEFAULT 0",
            "hunter_type": "TEXT NOT NULL DEFAULT 'ordinary'",
            "signal_label": "TEXT NOT NULL DEFAULT 'عادی'",
            "score_direction": "INTEGER DEFAULT 0",
            "score_pre_ignition": "INTEGER DEFAULT 0",
            "score_candle_entry": "INTEGER DEFAULT 0",
            "score_ai_memory": "INTEGER DEFAULT 0",
            "score_risk_net": "INTEGER DEFAULT 0",
            "score_session": "INTEGER DEFAULT 0",
            "score_order_block": "INTEGER DEFAULT 0",
            "candle_pattern": "TEXT",
            "entry_stage_pct": "REAL DEFAULT 100",
            "estimated_profit_usdt": "REAL DEFAULT 0",
            "estimated_profit_pct": "REAL DEFAULT 0",
            "session_state": "TEXT",
            "order_block_state": "TEXT",
            "session_bucket": "TEXT",
            "mfe_pct": "REAL DEFAULT 0",
            "mae_pct": "REAL DEFAULT 0",
        }
        for name, spec in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE signals ADD COLUMN {name} {spec}")

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

    def min_profit_usdt(self) -> float:
        return float(self._get_setting("min_profit_usdt", str(DEFAULT_MIN_PROFIT_USDT)))

    def set_min_profit_usdt(self, value: float) -> None:
        if not MIN_PROFIT_USDT_MIN <= value <= MIN_PROFIT_USDT_MAX:
            raise ValueError("حداقل سود باید بین 0.10 تا 1000 دلار باشد.")
        self._set_setting("min_profit_usdt", str(float(value)))

    def min_profit_pct(self) -> float:
        return float(self._get_setting("min_profit_pct", str(DEFAULT_MIN_PROFIT_PCT)))

    def set_min_profit_pct(self, value: float) -> None:
        if not MIN_PROFIT_PCT_MIN <= value <= MIN_PROFIT_PCT_MAX:
            raise ValueError("درصد سود باید بین 0.10 تا 100 باشد.")
        self._set_setting("min_profit_pct", str(float(value)))

    def add_signal(self, *, okx_symbol: str, toobit_symbol: str, symbol_name: str, decision: SignalDecision, signal_type: str, real_status: str = "none", signal_label: str | None = None) -> int:
        if decision.direction is None:
            raise ValueError("جهت سیگنال مشخص نیست.")
        now = datetime.now(timezone.utc)
        bucket = f"{now.hour:02d}:{0 if now.minute < 30 else 30:02d}"
        notes = " | ".join(decision.notes[:20])
        label = signal_label or decision.signal_label
        hunter_type = "hunter" if decision.hunter else "ordinary"
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO signals(
                    created_at, okx_symbol, toobit_symbol, symbol_name, direction, entry, tp, sl,
                    score, threshold, ai_confidence, ai_experience, ai_adjustment, signal_type, hunter_type, signal_label,
                    status, real_status, margin_usdt, leverage, score_direction, score_pre_ignition, score_candle_entry,
                    score_ai_memory, score_risk_net, score_session, score_order_block, direction_state_1h,
                    direction_confidence_1h, bias_4h, setup_15m, entry_5m, candle_pattern, entry_stage_pct,
                    net_edge, estimated_profit_usdt, estimated_profit_pct, risk_reward, estimated_cost_pct, market_bias,
                    session_state, order_block_state, session_bucket, reason, notes
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now.isoformat(), okx_symbol, toobit_symbol, symbol_name, decision.direction, decision.entry, decision.tp, decision.sl,
                    decision.score, decision.threshold, decision.ai_confidence, decision.ai_experience, decision.ai_adjustment,
                    signal_type, hunter_type, label, real_status, self.margin_usdt(), self.leverage(),
                    decision.breakdown.score_direction, decision.breakdown.score_pre_ignition, decision.breakdown.score_candle_entry,
                    decision.breakdown.score_ai_memory, decision.breakdown.score_risk_net, decision.breakdown.score_session,
                    decision.breakdown.score_order_block, decision.direction_state_1h, decision.direction_confidence_1h,
                    decision.bias_4h, decision.setup_15m, decision.entry_5m, decision.candle_pattern, decision.entry_stage_pct,
                    decision.net_edge, decision.estimated_profit_usdt, decision.estimated_profit_pct, decision.risk_reward,
                    decision.estimated_cost_pct, decision.market_bias, decision.session_state, decision.order_block_state,
                    bucket, decision.reason, notes,
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
            else:
                conn.execute("UPDATE signals SET signal_type='real_failed', status='FAILED', real_status='failed', real_opened=0, order_id=NULL, real_open_reason=?, result_at=? WHERE id=? AND status='OPEN'", (reason, datetime.now(timezone.utc).isoformat(), signal_id))

    def finish_signal(self, signal_id: int, *, status: str, approx_pnl: float, real_pnl: float | None, result_message_id: int | None, mfe_pct: float = 0.0, mae_pct: float = 0.0) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute("UPDATE signals SET status=?, approx_pnl=?, real_pnl=?, result_message_id=?, result_at=?, mfe_pct=?, mae_pct=? WHERE id=? AND status='OPEN'", (status, approx_pnl, real_pnl, result_message_id, now, mfe_pct, mae_pct, signal_id))

    def open_signals(self) -> list[StoredSignal]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM signals WHERE status='OPEN' ORDER BY id ASC").fetchall()
            return [self._row_to_signal(row) for row in rows]

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
            return int(row["n"]) > 0

    def upsert_watch(self, *, symbol_name: str, okx_symbol: str, toobit_symbol: str, direction: str, score: int, ai_confidence: int, expire_seconds: int) -> None:
        now = datetime.now(timezone.utc)
        expires = now + timedelta(seconds=expire_seconds)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO watchlist(symbol_name, okx_symbol, toobit_symbol, direction, score, ai_confidence, created_at, updated_at, expires_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol_name, direction) DO UPDATE SET score=excluded.score, ai_confidence=excluded.ai_confidence, updated_at=excluded.updated_at, expires_at=excluded.expires_at
                """,
                (symbol_name, okx_symbol, toobit_symbol, direction, score, ai_confidence, now.isoformat(), now.isoformat(), expires.isoformat()),
            )

    def active_watches(self) -> list[dict[str, Any]]:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute("DELETE FROM watchlist WHERE expires_at < ?", (now,))
            rows = conn.execute("SELECT * FROM watchlist ORDER BY score DESC, ai_confidence DESC").fetchall()
            return [dict(row) for row in rows]

    def can_send_ready_alert(self, symbol_name: str, direction: str, cooldown_seconds: int) -> bool:
        with self._connect() as conn:
            row = conn.execute("SELECT last_ready_alert_at FROM watchlist WHERE symbol_name=? AND direction=?", (symbol_name, direction)).fetchone()
            if not row or not row["last_ready_alert_at"]:
                return True
            last = datetime.fromisoformat(str(row["last_ready_alert_at"]))
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
        watches = self.active_watches()
        if len(watches) <= max_count:
            return
        keep_ids = [int(item["id"]) for item in watches[:max_count]]
        marks = ",".join("?" for _ in keep_ids)
        with self._connect() as conn:
            conn.execute(f"DELETE FROM watchlist WHERE id NOT IN ({marks})", keep_ids)

    def record_rejection(self, symbol_name: str, direction: str | None, code: str | None, reason: str, score: int = 0) -> None:
        with self._connect() as conn:
            conn.execute("INSERT INTO rejection_log(created_at, symbol_name, direction, code, reason, score) VALUES(?, ?, ?, ?, ?, ?)", (datetime.now(timezone.utc).isoformat(), symbol_name, direction, code, reason, score))

    def pattern_stats(self, symbol_name: str, direction: str, pattern: str, rsi: float, adx: float, volume_ratio: float) -> dict[str, Any]:
        start = datetime.now(timezone.utc) - timedelta(days=LEARNING_DAYS)
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM signals WHERE created_at>=? AND symbol_name=? AND direction=? AND candle_pattern=? AND status IN ('TP','SL')", (start.isoformat(), symbol_name, direction, pattern)).fetchall()
        return self._learning_summary(rows)

    def session_stats(self, symbol_name: str, direction: str, bucket: str) -> dict[str, Any]:
        start = datetime.now(timezone.utc) - timedelta(days=LEARNING_DAYS)
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM signals WHERE created_at>=? AND symbol_name=? AND direction=? AND session_bucket=? AND status IN ('TP','SL')", (start.isoformat(), symbol_name, direction, bucket)).fetchall()
        return self._learning_summary(rows)

    def _learning_summary(self, rows: list[sqlite3.Row]) -> dict[str, Any]:
        samples = len(rows)
        tp = sum(1 for row in rows if row["status"] == "TP")
        sl = sum(1 for row in rows if row["status"] == "SL")
        avg_mfe = sum(float(row["mfe_pct"] or 0.0) for row in rows) / samples if samples else 0.0
        return {"samples": samples, "tp": tp, "sl": sl, "win_rate": (tp / samples * 100.0) if samples else 0.0, "avg_mfe": avg_mfe}

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
        start = datetime.now(timezone.utc) - timedelta(days=LEARNING_DAYS)
        with self._connect() as conn:
            active_rows = conn.execute("SELECT * FROM signals WHERE created_at >= ?", (start.isoformat(),)).fetchall()
            all_patterns = conn.execute("SELECT COUNT(*) AS n FROM signals WHERE candle_pattern IS NOT NULL").fetchone()
        closed = [r for r in active_rows if r["status"] in ("TP", "SL")]
        hunter = [r for r in closed if r["hunter_type"] == "hunter"]
        avg_ai = sum(float(r["ai_confidence"] or 0) for r in active_rows) / len(active_rows) if active_rows else 0.0
        def best_worst(reverse: bool) -> str:
            groups: dict[str, list[sqlite3.Row]] = {}
            for row in closed:
                key = f"{row['symbol_name']} {row['direction']}"
                groups.setdefault(key, []).append(row)
            scored = []
            for key, items in groups.items():
                if len(items) >= 3:
                    tp = sum(1 for x in items if x["status"] == "TP")
                    scored.append((tp / len(items) * 100.0, key, len(items)))
            if not scored:
                return "نمونه کافی نیست"
            scored.sort(reverse=reverse)
            wr, key, n = scored[0]
            return f"{key} / WR {wr:.1f}% / {n} نمونه"
        session_good = self._session_list(closed, good=True)
        session_bad = self._session_list(closed, good=False)
        return {
            "learning_days": LEARNING_DAYS,
            "stored_patterns": int(all_patterns["n"] or 0),
            "active_patterns": len(active_rows),
            "hunter_tp": sum(1 for r in hunter if r["status"] == "TP"),
            "hunter_sl": sum(1 for r in hunter if r["status"] == "SL"),
            "analysis_right": sum(1 for r in closed if r["status"] == "TP"),
            "analysis_wrong": sum(1 for r in closed if r["status"] == "SL"),
            "avg_ai_confidence": avg_ai,
            "best_symbol_side": best_worst(True),
            "worst_symbol_side": best_worst(False),
            "good_sessions": session_good,
            "bad_sessions": session_bad,
            "best_indicator_patterns": self._best_pattern_text(closed),
        }

    def _session_list(self, rows: list[sqlite3.Row], *, good: bool) -> str:
        groups: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            bucket = str(row["session_bucket"] or "--")
            groups.setdefault(bucket, []).append(row)
        items = []
        for bucket, subset in groups.items():
            if len(subset) < 5:
                continue
            tp = sum(1 for r in subset if r["status"] == "TP")
            wr = tp / len(subset) * 100.0
            if (good and wr >= 62) or ((not good) and wr <= 42):
                items.append((wr, bucket))
        items.sort(reverse=good)
        return "، ".join(bucket for _, bucket in items[:4]) or "نمونه کافی نیست"

    def _best_pattern_text(self, rows: list[sqlite3.Row]) -> str:
        groups: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            key = str(row["candle_pattern"] or "UNKNOWN")
            groups.setdefault(key, []).append(row)
        best = []
        for key, subset in groups.items():
            if len(subset) < 3:
                continue
            tp = sum(1 for r in subset if r["status"] == "TP")
            best.append((tp / len(subset) * 100.0, key, len(subset)))
        if not best:
            return "نمونه کافی نیست"
        best.sort(reverse=True)
        return " / ".join(f"{key} WR {wr:.1f}%" for wr, key, _ in best[:3])

    def reset_stats(self) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM signals")
            conn.execute("DELETE FROM rejection_log")
            conn.execute("DELETE FROM watchlist")

    def reset_learning(self) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE signals SET ai_confidence=0, ai_experience=0, ai_adjustment=0, mfe_pct=0, mae_pct=0")

    def _build_stats(self, rows: list[sqlite3.Row]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        result["all"] = self._summarize(rows, pnl_key="approx_pnl")
        result["normal"] = self._summarize([r for r in rows if r["signal_type"] == "normal"], pnl_key="approx_pnl")
        result["real"] = self._summarize([r for r in rows if r["signal_type"] == "real"], pnl_key="real_pnl")
        result["hunter"] = self._summarize([r for r in rows if r["hunter_type"] == "hunter"], pnl_key="approx_pnl")
        result["ordinary"] = self._summarize([r for r in rows if r["hunter_type"] != "hunter"], pnl_key="approx_pnl")
        result["real_failed"] = self._summarize([r for r in rows if r["signal_type"] == "real_failed"], pnl_key="real_pnl")
        for side in ("LONG", "SHORT"):
            key = side.lower()
            result[key] = self._summarize([r for r in rows if r["direction"] == side], pnl_key="approx_pnl")
            result[f"normal_{key}"] = self._summarize([r for r in rows if r["signal_type"] == "normal" and r["direction"] == side], pnl_key="approx_pnl")
            result[f"real_{key}"] = self._summarize([r for r in rows if r["signal_type"] == "real" and r["direction"] == side], pnl_key="real_pnl")
        return result

    def _summarize(self, subset: list[sqlite3.Row], *, pnl_key: str) -> dict[str, Any]:
        closed = [row for row in subset if row["status"] in ("TP", "SL")]
        tp_count = sum(1 for row in subset if row["status"] == "TP")
        sl_count = sum(1 for row in subset if row["status"] == "SL")
        return {
            "total": len(subset),
            "tp": tp_count,
            "sl": sl_count,
            "open": sum(1 for row in subset if row["status"] == "OPEN"),
            "failed": sum(1 for row in subset if row["status"] == "FAILED"),
            "win_rate": (tp_count / len(closed) * 100.0) if closed else 0.0,
            "pnl": sum(float(row[pnl_key] or 0.0) for row in subset),
            "avg_score": sum(float(row["score"] or 0) for row in subset) / len(subset) if subset else 0.0,
        }

    def _row_to_signal(self, row: sqlite3.Row) -> StoredSignal:
        return StoredSignal(
            id=int(row["id"]), created_at=str(row["created_at"]), okx_symbol=str(row["okx_symbol"]), toobit_symbol=str(row["toobit_symbol"]),
            symbol_name=str(row["symbol_name"] or ""), direction=str(row["direction"]), entry=float(row["entry"]), tp=float(row["tp"]), sl=float(row["sl"]),
            score=int(row["score"]), ai_confidence=int(row["ai_confidence"] or 0), ai_experience=int(row["ai_experience"] or 0),
            signal_type=str(row["signal_type"]), hunter_type=str(row["hunter_type"] or "ordinary"), status=str(row["status"]), real_status=str(row["real_status"]),
            message_id=int(row["message_id"]) if row["message_id"] is not None else None,
            result_message_id=int(row["result_message_id"]) if row["result_message_id"] is not None else None,
            order_id=str(row["order_id"]) if row["order_id"] is not None else None,
            approx_pnl=float(row["approx_pnl"]) if row["approx_pnl"] is not None else None,
            real_pnl=float(row["real_pnl"]) if row["real_pnl"] is not None else None,
            margin_usdt=float(row["margin_usdt"] or 0.0), leverage=int(row["leverage"] or 1), net_edge=float(row["net_edge"] or 0.0),
            estimated_profit_usdt=float(row["estimated_profit_usdt"] or 0.0), estimated_profit_pct=float(row["estimated_profit_pct"] or 0.0),
            risk_reward=float(row["risk_reward"] or 0.0), reason=str(row["reason"]) if row["reason"] is not None else None,
        )
