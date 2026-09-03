"""لایهٔ ذخیره‌سازی (SQLite) برای ربات اسکن چندارزی.

هر «چرخه» (cycle) یک پوزیشن مستقل روی یک ارز است. چند چرخه می‌توانند هم‌زمان
باز باشند (تا سقفی که کاربر تعیین می‌کند)، ولی روی هر ارز فقط یکی.
آمار واقعی و مجازی کاملاً از هم جدا نگهداری می‌شوند.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from typing import Any

import config
from utils import json_dumps, json_loads, logger, now_ms, safe_float, safe_int

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS cycles (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol            TEXT    NOT NULL,
    side              TEXT    NOT NULL,
    mode              TEXT    NOT NULL,           -- real | virtual
    status            TEXT    NOT NULL,           -- open | closed | failed
    leverage          INTEGER NOT NULL,
    planned_steps     INTEGER NOT NULL,
    filled_steps      INTEGER NOT NULL DEFAULT 0,
    capital_at_open   REAL    NOT NULL DEFAULT 0,
    plan_json         TEXT,
    avg_entry_price   REAL    NOT NULL DEFAULT 0,
    total_quantity    REAL    NOT NULL DEFAULT 0,
    total_margin      REAL    NOT NULL DEFAULT 0,
    total_notional    REAL    NOT NULL DEFAULT 0,
    liquidation_price REAL    NOT NULL DEFAULT 0,
    take_profit_price REAL    NOT NULL DEFAULT 0,
    hard_stop_price   REAL    NOT NULL DEFAULT 0,
    exit_price        REAL    NOT NULL DEFAULT 0,
    exit_reason       TEXT,                        -- tp | stop | manual | liquidation
    gross_pnl         REAL    NOT NULL DEFAULT 0,
    net_pnl           REAL    NOT NULL DEFAULT 0,
    fees              REAL    NOT NULL DEFAULT 0,
    opened_at         INTEGER NOT NULL,
    closed_at         INTEGER,
    tg_message_id     INTEGER,                     -- برای ریپلای نتیجه روی سیگنال
    final_step_warned INTEGER NOT NULL DEFAULT 0,
    entry_score       REAL    NOT NULL DEFAULT 0,   -- امتیاز لحظهٔ ورود
    entry_reason      TEXT                          -- تفکیک امتیاز بخش‌ها
);
CREATE INDEX IF NOT EXISTS idx_cycles_symbol ON cycles(symbol, status);
CREATE INDEX IF NOT EXISTS idx_cycles_status ON cycles(status, mode);
CREATE INDEX IF NOT EXISTS idx_cycles_closed ON cycles(closed_at);

CREATE TABLE IF NOT EXISTS cycle_steps (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id      INTEGER NOT NULL,
    step_index    INTEGER NOT NULL,
    trigger_price REAL    NOT NULL,
    fill_price    REAL    NOT NULL DEFAULT 0,
    quantity      REAL    NOT NULL DEFAULT 0,
    margin        REAL    NOT NULL DEFAULT 0,
    notional      REAL    NOT NULL DEFAULT 0,
    status        TEXT    NOT NULL,               -- planned | filled | skipped
    order_id      TEXT,
    filled_at     INTEGER,
    FOREIGN KEY (cycle_id) REFERENCES cycles(id)
);
CREATE INDEX IF NOT EXISTS idx_steps_cycle ON cycle_steps(cycle_id, step_index);

CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        INTEGER NOT NULL,
    kind      TEXT    NOT NULL,
    payload   TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);

CREATE TABLE IF NOT EXISTS outbox (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        INTEGER NOT NULL,
    text      TEXT    NOT NULL,
    reply_to  INTEGER,
    cycle_id  INTEGER,
    sent      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_outbox_sent ON outbox(sent, id);

CREATE TABLE IF NOT EXISTS health (
    component TEXT PRIMARY KEY,
    status    TEXT,
    detail    TEXT,
    ts        INTEGER
);
"""

DEFAULT_SETTINGS: dict[str, Any] = {
    "real_trading_enabled": False,
    "startup_ready": False,
    "startup_phase": "در حال راه‌اندازی",
    "virtual_balance": config.VIRTUAL_START_CAPITAL_USDT,
    "max_positions": config.MAX_CONCURRENT_POSITIONS,
    "score_threshold": config.SCORE_THRESHOLD,
    "leverage": config.DEFAULT_LEVERAGE,
    "margin_mode": config.MARGIN_MODE,
    "capital_cap": config.CAPITAL_CAP_USDT,
    "last_balance": 0.0,
    "last_balance_ts": 0,
}


class Storage:
    def __init__(self, path: str | None = None):
        self.path = str(path or config.RUNTIME_DB)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(f"PRAGMA busy_timeout={config.SQLITE_BUSY_TIMEOUT_MS}")
        self._conn.execute("PRAGMA journal_mode=WAL")
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()
        self._ensure_defaults()

    # ------------------------------------------------------------------
    def close(self) -> None:
        with self._lock:
            try:
                self._conn.commit()
            finally:
                self._conn.close()

    def _ensure_defaults(self) -> None:
        for key, value in DEFAULT_SETTINGS.items():
            if self.get_setting(key, None) is None:
                self.set_setting(key, value)

    # --- settings -----------------------------------------------------
    def get_setting(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM settings WHERE key=?", (key,)
            ).fetchone()
        if row is None:
            return default
        return json_loads(row["value"], default)

    def set_setting(self, key: str, value: Any) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json_dumps(value)),
            )
            self._conn.commit()

    # --- health / events ---------------------------------------------
    def set_health(self, component: str, status: str, detail: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO health(component,status,detail,ts) VALUES(?,?,?,?) "
                "ON CONFLICT(component) DO UPDATE SET status=excluded.status,"
                "detail=excluded.detail, ts=excluded.ts",
                (component, status, str(detail)[:400], now_ms()),
            )
            self._conn.commit()

    def health_rows(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM health ORDER BY component").fetchall()
        return [dict(r) for r in rows]

    def log_event(self, kind: str, payload: Any = None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO events(ts,kind,payload) VALUES(?,?,?)",
                (now_ms(), kind, json_dumps(payload)),
            )
            self._conn.commit()

    # --- outbox (پیام‌های تلگرام) --------------------------------------
    def queue_message(self, text: str, reply_to: int | None = None, cycle_id: int | None = None) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO outbox(ts,text,reply_to,cycle_id,sent) VALUES(?,?,?,?,0)",
                (now_ms(), text, reply_to, cycle_id),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def pending_messages(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM outbox WHERE sent=0 ORDER BY id LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_message_sent(self, outbox_id: int) -> None:
        with self._lock:
            self._conn.execute("UPDATE outbox SET sent=1 WHERE id=?", (outbox_id,))
            self._conn.commit()

    # --- cycles -------------------------------------------------------
    def create_cycle(self, *, symbol: str, side: str, mode: str, leverage: int,
                     capital_at_open: float, plan: dict[str, Any],
                     take_profit_price: float, hard_stop_price: float,
                     entry_score: float = 0.0, entry_reason: str = "") -> int:
        """یک پوزیشن جدید ثبت می‌کند (تک‌ورودی، نه پله‌ای)."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO cycles(symbol,side,mode,status,leverage,planned_steps,"
                "capital_at_open,plan_json,take_profit_price,hard_stop_price,"
                "entry_score,entry_reason,opened_at) "
                "VALUES(?,?,?,'open',?,1,?,?,?,?,?,?,?)",
                (symbol, side, mode, int(leverage),
                 float(capital_at_open), json_dumps(plan),
                 float(take_profit_price), float(hard_stop_price),
                 float(entry_score), str(entry_reason)[:400], now_ms()),
            )
            cycle_id = int(cur.lastrowid)
            self._conn.execute(
                "INSERT INTO cycle_steps(cycle_id,step_index,trigger_price,"
                "margin,notional,quantity,status) VALUES(?,1,?,?,?,?,'planned')",
                (cycle_id, safe_float(plan.get("entry_price")),
                 safe_float(plan.get("margin_usdt")), safe_float(plan.get("notional_usdt")),
                 safe_float(plan.get("quantity"))),
            )
            self._conn.commit()
        return cycle_id

    def get_cycle(self, cycle_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM cycles WHERE id=?", (cycle_id,)).fetchone()
        return dict(row) if row else None

    def open_cycle(self, mode: str | None = None) -> dict[str, Any] | None:
        """آخرین چرخهٔ باز (برای سازگاری؛ معمولاً open_cycles استفاده می‌شود)."""
        query = "SELECT * FROM cycles WHERE status='open'"
        params: tuple[Any, ...] = ()
        if mode:
            query += " AND mode=?"
            params = (mode,)
        query += " ORDER BY id DESC LIMIT 1"
        with self._lock:
            row = self._conn.execute(query, params).fetchone()
        return dict(row) if row else None

    def open_cycles(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM cycles WHERE status='open' ORDER BY id DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def open_cycles_for_mode(self, mode: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM cycles WHERE status='open' AND mode=? ORDER BY id DESC",
                (mode,),
            ).fetchall()
        return [dict(r) for r in rows]

    def open_symbols(self, mode: str | None = None) -> set[str]:
        """ارزهایی که همین حالا پوزیشن باز دارند — برای جلوگیری از ورود تکراری."""
        query = "SELECT DISTINCT symbol FROM cycles WHERE status='open'"
        params: tuple[Any, ...] = ()
        if mode:
            query += " AND mode=?"
            params = (mode,)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return {str(r["symbol"]) for r in rows}

    def open_position_count(self, mode: str | None = None) -> int:
        query = "SELECT COUNT(*) c FROM cycles WHERE status='open'"
        params: tuple[Any, ...] = ()
        if mode:
            query += " AND mode=?"
            params = (mode,)
        with self._lock:
            return int(self._conn.execute(query, params).fetchone()["c"])

    def open_margin_total(self, mode: str | None = None) -> float:
        query = "SELECT COALESCE(SUM(total_margin),0) m FROM cycles WHERE status='open'"
        params: tuple[Any, ...] = ()
        if mode:
            query += " AND mode=?"
            params = (mode,)
        with self._lock:
            return safe_float(self._conn.execute(query, params).fetchone()["m"])

    def cycle_steps(self, cycle_id: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM cycle_steps WHERE cycle_id=? ORDER BY step_index", (cycle_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    def filled_steps(self, cycle_id: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM cycle_steps WHERE cycle_id=? AND status='filled' "
                "ORDER BY step_index", (cycle_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_step_filled(self, *, cycle_id: int, step_index: int, fill_price: float,
                         quantity: float, margin: float, order_id: str | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE cycle_steps SET status='filled', fill_price=?, quantity=?, "
                "margin=?, notional=?, order_id=?, filled_at=? "
                "WHERE cycle_id=? AND step_index=?",
                (float(fill_price), float(quantity), float(margin),
                 float(quantity) * float(fill_price), order_id, now_ms(),
                 cycle_id, int(step_index)),
            )
            self._conn.execute(
                "UPDATE cycles SET filled_steps=(SELECT COUNT(*) FROM cycle_steps "
                "WHERE cycle_id=? AND status='filled') WHERE id=?",
                (cycle_id, cycle_id),
            )
            self._conn.commit()

    def update_cycle_position(self, cycle_id: int, snapshot: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE cycles SET avg_entry_price=?, total_quantity=?, total_margin=?, "
                "total_notional=?, liquidation_price=? WHERE id=?",
                (safe_float(snapshot.get("avg_entry")), safe_float(snapshot.get("quantity")),
                 safe_float(snapshot.get("margin")), safe_float(snapshot.get("notional")),
                 safe_float(snapshot.get("liquidation_price")), cycle_id),
            )
            self._conn.commit()

    def set_cycle_message_id(self, cycle_id: int, message_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE cycles SET tg_message_id=? WHERE id=?", (int(message_id), cycle_id)
            )
            self._conn.commit()

    def mark_final_step_warned(self, cycle_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE cycles SET final_step_warned=1 WHERE id=?", (cycle_id,)
            )
            self._conn.commit()

    def update_stops(self, cycle_id: int, *, take_profit: float, hard_stop: float) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE cycles SET take_profit_price=?, hard_stop_price=? WHERE id=?",
                (float(take_profit), float(hard_stop), cycle_id),
            )
            self._conn.commit()

    def close_cycle(self, cycle_id: int, *, exit_price: float, exit_reason: str,
                    gross_pnl: float, net_pnl: float, fees: float) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE cycles SET status='closed', exit_price=?, exit_reason=?, "
                "gross_pnl=?, net_pnl=?, fees=?, closed_at=? WHERE id=?",
                (float(exit_price), exit_reason, float(gross_pnl), float(net_pnl),
                 float(fees), now_ms(), cycle_id),
            )
            self._conn.commit()

    # --- موجودی --------------------------------------------------------
    def cache_balance(self, balance: float) -> None:
        self.set_setting("last_balance", float(balance))
        self.set_setting("last_balance_ts", now_ms())

    def cached_balance(self) -> tuple[float, int]:
        return (
            safe_float(self.get_setting("last_balance", 0.0)),
            safe_int(self.get_setting("last_balance_ts", 0)),
        )

    def balance_is_fresh(self) -> bool:
        _, ts = self.cached_balance()
        return (now_ms() - ts) < config.BALANCE_REFRESH_SECONDS * 1000

    def adjust_virtual_balance(self, delta: float) -> float:
        current = safe_float(self.get_setting("virtual_balance", config.VIRTUAL_START_CAPITAL_USDT))
        updated = max(0.0, current + float(delta))
        self.set_setting("virtual_balance", updated)
        return updated

    # --- آمار ----------------------------------------------------------
    def _day_start_ms(self) -> int:
        return int((time.time() - (time.time() % 86400)) * 1000)

    def stats(self, mode: str) -> dict[str, Any]:
        day_start = self._day_start_ms()
        with self._lock:
            open_count = self._conn.execute(
                "SELECT COUNT(*) c FROM cycles WHERE status='open' AND mode=?", (mode,)
            ).fetchone()["c"]
            closed = self._conn.execute(
                "SELECT COUNT(*) c, COALESCE(SUM(net_pnl),0) pnl FROM cycles "
                "WHERE status='closed' AND mode=?", (mode,)
            ).fetchone()
            today = self._conn.execute(
                "SELECT COALESCE(SUM(net_pnl),0) pnl FROM cycles "
                "WHERE status='closed' AND mode=? AND closed_at>=?", (mode, day_start)
            ).fetchone()
            tp_count = self._conn.execute(
                "SELECT COUNT(*) c FROM cycles WHERE status='closed' AND mode=? "
                "AND exit_reason='tp'", (mode,)
            ).fetchone()["c"]
            stop_count = self._conn.execute(
                "SELECT COUNT(*) c FROM cycles WHERE status='closed' AND mode=? "
                "AND exit_reason IN ('stop','liquidation')", (mode,)
            ).fetchone()["c"]
        return {
            "open": int(open_count),
            "closed": int(closed["c"]),
            "tp": int(tp_count),
            "stop": int(stop_count),
            "pnl_total": safe_float(closed["pnl"]),
            "pnl_today": safe_float(today["pnl"]),
        }

    def recent_cycles(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM cycles ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]
