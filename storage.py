"""ذخیره‌سازی سریع SQLite برای وضعیت، سیگنال‌ها، آمار، اسلات و سلامت.
هیچ دستور تلگرامی نباید مسیر تحلیل را قفل کند؛ همه خواندن‌ها سبک و آماده هستند.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import config

class Storage:
    def __init__(self, path: str = config.DB_PATH):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._init_db()

    @contextmanager
    def connect(self):
        with self._lock:
            conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                yield conn
            finally:
                conn.close()

    def _init_db(self) -> None:
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS kv (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol_id TEXT NOT NULL,
                    okx_symbol TEXT NOT NULL,
                    toobit_symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    strength TEXT,
                    entry REAL NOT NULL,
                    tp REAL NOT NULL,
                    sl REAL NOT NULL,
                    rr REAL NOT NULL,
                    trade_mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    is_real INTEGER NOT NULL DEFAULT 0,
                    slot_id INTEGER,
                    message_id INTEGER,
                    created_at INTEGER NOT NULL,
                    opened_at INTEGER,
                    closed_at INTEGER,
                    entry_real REAL,
                    exit_price REAL,
                    gross_pnl REAL DEFAULT 0,
                    fee_usdt REAL DEFAULT 0,
                    net_pnl REAL DEFAULT 0,
                    close_reason TEXT,
                    mfe REAL DEFAULT 0,
                    mae REAL DEFAULT 0,
                    order_id TEXT,
                    raw_json TEXT DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS profiles (
                    symbol_id TEXT PRIMARY KEY,
                    noise_median REAL DEFAULT 0,
                    noise_p70 REAL DEFAULT 0,
                    min_sl_pct REAL DEFAULT 0,
                    tp_median REAL DEFAULT 0,
                    tp_p70 REAL DEFAULT 0,
                    signal_count INTEGER DEFAULT 0,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS health_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    component TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    message TEXT NOT NULL,
                    symbol_id TEXT,
                    created_at INTEGER NOT NULL,
                    cleared_at INTEGER
                );
                CREATE TABLE IF NOT EXISTS blacklist (
                    symbol_id TEXT PRIMARY KEY,
                    reason TEXT NOT NULL,
                    until_ts INTEGER NOT NULL,
                    count INTEGER NOT NULL DEFAULT 1
                );
                """
            )
            defaults = {
                "trading_enabled": config.TRADING_ENABLED_DEFAULT,
                "auto_signal_enabled": config.AUTO_SIGNAL_ENABLED_DEFAULT,
                "trade_usdt": config.TRADE_USDT_DEFAULT,
                "leverage": config.LEVERAGE_DEFAULT,
                "max_positions": config.MAX_POSITIONS_DEFAULT,
                "profit_today": 0.0,
                "profit_total": 0.0,
                "stats_reset_at": int(time.time()),
                "profit_reset_at": int(time.time()),
                "toobit_connected": False,
                "toobit_margin_usdt": 0.0,
                "toobit_available_usdt": 0.0,
                "toobit_total_usdt": 0.0,
                "toobit_last_error": "",
                "toobit_last_update": 0,
            }
            for key, value in defaults.items():
                if self.get(key, None) is None:
                    self.set(key, value)

    def get(self, key: str, default: Any = None) -> Any:
        with self.connect() as db:
            row = db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
            if not row:
                return default
            try:
                return json.loads(row["value"])
            except Exception:
                return default

    def set(self, key: str, value: Any) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO kv(key,value,updated_at) VALUES(?,?,?)",
                (key, json.dumps(value, ensure_ascii=False), int(time.time())),
            )

    def create_signal(self, data: dict[str, Any]) -> int:
        now = int(time.time())
        cols = [
            "symbol_id", "okx_symbol", "toobit_symbol", "side", "strength", "entry", "tp", "sl", "rr",
            "trade_mode", "status", "is_real", "slot_id", "message_id", "created_at", "order_id", "raw_json"
        ]
        vals = [
            data.get("symbol_id"), data.get("okx_symbol"), data.get("toobit_symbol"), data.get("side"),
            data.get("strength"), data.get("entry"), data.get("tp"), data.get("sl"), data.get("rr"),
            data.get("trade_mode", "virtual"), data.get("status", "open"), int(bool(data.get("is_real"))),
            data.get("slot_id"), data.get("message_id"), data.get("created_at", now), data.get("order_id"),
            json.dumps(data.get("raw", {}), ensure_ascii=False),
        ]
        with self.connect() as db:
            cur = db.execute(f"INSERT INTO signals({','.join(cols)}) VALUES({','.join(['?']*len(cols))})", vals)
            return int(cur.lastrowid)

    def update_signal(self, signal_id: int, **fields: Any) -> None:
        if not fields:
            return
        allowed = {
            "status", "message_id", "opened_at", "closed_at", "entry_real", "exit_price", "gross_pnl",
            "fee_usdt", "net_pnl", "close_reason", "mfe", "mae", "order_id", "slot_id", "raw_json"
        }
        parts, vals = [], []
        for k, v in fields.items():
            if k in allowed:
                parts.append(f"{k}=?")
                vals.append(v if k != "raw_json" or isinstance(v, str) else json.dumps(v, ensure_ascii=False))
        if not parts:
            return
        vals.append(signal_id)
        with self.connect() as db:
            db.execute(f"UPDATE signals SET {','.join(parts)} WHERE id=?", vals)

    def get_open_signals(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM signals WHERE status IN ('open','pending') ORDER BY id ASC").fetchall()
            return [dict(r) for r in rows]

    def get_signal(self, signal_id: int) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM signals WHERE id=?", (signal_id,)).fetchone()
            return dict(row) if row else None

    def count_real_open(self) -> int:
        with self.connect() as db:
            row = db.execute("SELECT COUNT(*) c FROM signals WHERE is_real=1 AND status IN ('open','pending')").fetchone()
            return int(row["c"])

    def stats(self) -> dict[str, Any]:
        reset_at = int(self.get("stats_reset_at", 0) or 0)
        with self.connect() as db:
            total = db.execute("SELECT COUNT(*) c FROM signals WHERE created_at>=?", (reset_at,)).fetchone()["c"]
            open_c = db.execute("SELECT COUNT(*) c FROM signals WHERE created_at>=? AND status IN ('open','pending')", (reset_at,)).fetchone()["c"]
            tp = db.execute("SELECT COUNT(*) c FROM signals WHERE created_at>=? AND close_reason='TP'", (reset_at,)).fetchone()["c"]
            sl = db.execute("SELECT COUNT(*) c FROM signals WHERE created_at>=? AND close_reason='SL'", (reset_at,)).fetchone()["c"]
            real = db.execute("SELECT COUNT(*) c FROM signals WHERE created_at>=? AND is_real=1", (reset_at,)).fetchone()["c"]
            virt = db.execute("SELECT COUNT(*) c FROM signals WHERE created_at>=? AND is_real=0", (reset_at,)).fetchone()["c"]
            pnl = db.execute("SELECT COALESCE(SUM(net_pnl),0) p FROM signals WHERE created_at>=? AND status='closed'", (reset_at,)).fetchone()["p"]
            return {"signals": total, "open": open_c, "tp": tp, "sl": sl, "real": real, "virtual": virt, "net_pnl": float(pnl)}

    def reset_stats(self) -> None:
        # حذف فیزیکی انجام نمی‌دهد تا مانیتورینگ پوزیشن‌های باز خراب نشود؛ فقط پنل آمار از این لحظه صفر می‌شود.
        self.set("stats_reset_at", int(time.time()) + 1)

    def reset_profit(self) -> None:
        # فقط پنل سود/ضرر را صفر می‌کند؛ جزئیات نتایج هر سیگنال دست‌نخورده می‌ماند.
        self.set("profit_today", 0.0)
        self.set("profit_total", 0.0)
        self.set("profit_reset_at", int(time.time()))

    def add_profit(self, amount: float) -> None:
        self.set("profit_today", float(self.get("profit_today", 0.0)) + float(amount))
        self.set("profit_total", float(self.get("profit_total", 0.0)) + float(amount))

    def upsert_profile(self, symbol_id: str, data: dict[str, Any]) -> None:
        with self.connect() as db:
            db.execute(
                """INSERT OR REPLACE INTO profiles(symbol_id,noise_median,noise_p70,min_sl_pct,tp_median,tp_p70,signal_count,updated_at)
                VALUES(?,?,?,?,?,?,?,?)""",
                (
                    symbol_id, float(data.get("noise_median", 0)), float(data.get("noise_p70", 0)),
                    float(data.get("min_sl_pct", 0)), float(data.get("tp_median", 0)), float(data.get("tp_p70", 0)),
                    int(data.get("signal_count", 0)), int(data.get("updated_at", time.time())),
                ),
            )

    def get_profile(self, symbol_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM profiles WHERE symbol_id=?", (symbol_id,)).fetchone()
            return dict(row) if row else None

    def add_health_event(self, component: str, severity: str, message: str, symbol_id: str | None = None) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO health_events(component,severity,message,symbol_id,created_at) VALUES(?,?,?,?,?)",
                (component, severity, message, symbol_id, int(time.time())),
            )

    def active_health_events(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM health_events WHERE cleared_at IS NULL ORDER BY id DESC LIMIT 20").fetchall()
            return [dict(r) for r in rows]

    def blacklist_symbol(self, symbol_id: str, reason: str, seconds: int) -> None:
        until_ts = int(time.time() + seconds)
        with self.connect() as db:
            row = db.execute("SELECT count FROM blacklist WHERE symbol_id=?", (symbol_id,)).fetchone()
            count = int(row["count"]) + 1 if row else 1
            db.execute("INSERT OR REPLACE INTO blacklist(symbol_id,reason,until_ts,count) VALUES(?,?,?,?)", (symbol_id, reason, until_ts, count))

    def is_blacklisted(self, symbol_id: str) -> bool:
        now = int(time.time())
        with self.connect() as db:
            row = db.execute("SELECT until_ts FROM blacklist WHERE symbol_id=?", (symbol_id,)).fetchone()
            if not row:
                return False
            if int(row["until_ts"]) <= now:
                db.execute("DELETE FROM blacklist WHERE symbol_id=?", (symbol_id,))
                return False
            return True

    def blacklist_rows(self) -> list[dict[str, Any]]:
        now = int(time.time())
        with self.connect() as db:
            rows = db.execute("SELECT * FROM blacklist WHERE until_ts>?", (now,)).fetchall()
            return [dict(r) for r in rows]
