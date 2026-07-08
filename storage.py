from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

import config
from utils import normalize_symbol, safe_float, safe_int


@dataclass(frozen=True)
class StoredSignal:
    id: int
    symbol: str
    okx_symbol: str
    toobit_symbol: str
    direction: str
    signal_type: str
    status: str
    entry_price: float
    tp_price: float
    sl_price: float
    risk_reward: float
    score: float
    strength: str
    created_at: int
    opened_at: int
    message_id: int | None
    order_id: str | None
    client_order_id: str | None
    reasons_json: str
    result: str | None = None
    result_price: float | None = None
    result_pnl_usdt: float | None = None
    closed_at: int | None = None

    @property
    def reasons(self) -> list[str]:
        try:
            return list(json.loads(self.reasons_json or "[]"))
        except Exception:
            return []

    @property
    def risk_per_unit(self) -> float:
        if self.direction == "LONG":
            return max(0.0, self.entry_price - self.sl_price)
        return max(0.0, self.sl_price - self.entry_price)


class Storage:
    def __init__(self, path: str = config.BOT_DB_PATH) -> None:
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._init()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self._conn() as con:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runtime (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    okx_symbol TEXT NOT NULL,
                    toobit_symbol TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    signal_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    tp_price REAL NOT NULL,
                    sl_price REAL NOT NULL,
                    risk_reward REAL NOT NULL,
                    score REAL NOT NULL,
                    strength TEXT NOT NULL,
                    estimated_profit_usdt REAL DEFAULT 0,
                    estimated_loss_usdt REAL DEFAULT 0,
                    estimated_net_profit_usdt REAL DEFAULT 0,
                    round_trip_fee_usdt REAL DEFAULT 0,
                    reasons_json TEXT DEFAULT '[]',
                    created_at INTEGER NOT NULL,
                    opened_at INTEGER NOT NULL,
                    message_id INTEGER,
                    order_id TEXT,
                    client_order_id TEXT,
                    result TEXT,
                    result_price REAL,
                    result_pnl_usdt REAL,
                    closed_at INTEGER,
                    close_reason TEXT
                );
                CREATE TABLE IF NOT EXISTS scan_rejects (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS coin_errors (
                    symbol TEXT PRIMARY KEY,
                    error TEXT NOT NULL,
                    until_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                """
            )
        defaults = config.RuntimeDefaults()
        initial = {
            "real_trade_enabled": "1" if defaults.trade_enabled else "0",
            "auto_signal_enabled": "1" if defaults.auto_signal_enabled else "0",
            "trade_dollar_usdt": str(defaults.trade_dollar_usdt),
            "trade_capital_usdt": str(defaults.trade_capital_usdt),
            "leverage": str(defaults.leverage),
            "max_positions": str(defaults.max_positions),
            "min_net_profit_usdt": str(defaults.min_net_profit_usdt),
        }
        with self._conn() as con:
            for k, v in initial.items():
                con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v))

    def settings(self) -> dict[str, Any]:
        with self._conn() as con:
            rows = con.execute("SELECT key,value FROM settings").fetchall()
        data = {r["key"]: r["value"] for r in rows}
        return {
            "real_trade_enabled": str(data.get("real_trade_enabled", "0")) == "1",
            "auto_signal_enabled": str(data.get("auto_signal_enabled", "1")) == "1",
            "trade_dollar_usdt": safe_float(data.get("trade_dollar_usdt"), config.DEFAULT_TRADE_DOLLAR),
            "trade_capital_usdt": safe_float(data.get("trade_capital_usdt"), config.DEFAULT_TRADE_CAPITAL),
            "leverage": safe_int(data.get("leverage"), config.DEFAULT_LEVERAGE),
            "max_positions": safe_int(data.get("max_positions"), config.DEFAULT_MAX_POSITIONS),
            "min_net_profit_usdt": safe_float(data.get("min_net_profit_usdt"), config.DEFAULT_MIN_NET_PROFIT_USDT),
        }

    def set_setting(self, key: str, value: Any) -> None:
        with self._conn() as con:
            con.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, str(value)))

    def runtime_set(self, key: str, value: Any) -> None:
        with self._conn() as con:
            con.execute("INSERT OR REPLACE INTO runtime(key,value) VALUES(?,?)", (key, str(value)))

    def runtime_get(self, key: str, default: str = "") -> str:
        with self._conn() as con:
            row = con.execute("SELECT value FROM runtime WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row else default

    def add_signal(self, plan, signal_type: str, order_id: str | None = None, client_order_id: str | None = None) -> int:
        now = int(time.time())
        d = plan.to_legacy_dict() if hasattr(plan, "to_legacy_dict") else dict(plan)
        reasons = d.get("reasons") or []
        with self._conn() as con:
            cur = con.execute(
                """
                INSERT INTO signals(symbol,okx_symbol,toobit_symbol,direction,signal_type,status,entry_price,tp_price,sl_price,
                risk_reward,score,strength,estimated_profit_usdt,estimated_loss_usdt,estimated_net_profit_usdt,round_trip_fee_usdt,
                reasons_json,created_at,opened_at,order_id,client_order_id)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    normalize_symbol(str(d.get("symbol") or d.get("coin"))),
                    str(d.get("okx_symbol") or ""),
                    str(d.get("toobit_symbol") or d.get("symbol") or ""),
                    str(d.get("direction") or ""),
                    signal_type,
                    "OPEN",
                    safe_float(d.get("entry_price") or d.get("entry")),
                    safe_float(d.get("tp_price") or d.get("tp")),
                    safe_float(d.get("sl_price") or d.get("sl")),
                    safe_float(d.get("risk_reward")),
                    safe_float(d.get("score")),
                    str(d.get("strength") or ""),
                    safe_float(d.get("estimated_profit_usdt")),
                    safe_float(d.get("estimated_loss_usdt")),
                    safe_float(d.get("estimated_net_profit_usdt")),
                    safe_float(d.get("round_trip_fee_usdt")),
                    json.dumps(reasons, ensure_ascii=False),
                    now,
                    now,
                    order_id,
                    client_order_id,
                ),
            )
            return int(cur.lastrowid)

    def update_message_id(self, signal_id: int, msg_id: int | None) -> None:
        if msg_id is None:
            return
        with self._conn() as con:
            con.execute("UPDATE signals SET message_id=? WHERE id=?", (int(msg_id), int(signal_id)))

    def mark_real_failed(self, symbol: str, reason: str) -> None:
        self.runtime_set("last_real_failed", f"{normalize_symbol(symbol)} | {reason}")

    def has_open_symbol(self, symbol: str) -> bool:
        symbol = normalize_symbol(symbol)
        with self._conn() as con:
            row = con.execute("SELECT 1 FROM signals WHERE symbol=? AND status='OPEN' LIMIT 1", (symbol,)).fetchone()
        return bool(row)

    def active_signals(self) -> list[StoredSignal]:
        with self._conn() as con:
            rows = con.execute("SELECT * FROM signals WHERE status='OPEN' ORDER BY id ASC").fetchall()
        return [self._row_to_signal(r) for r in rows]

    def active_real_signals(self) -> list[StoredSignal]:
        with self._conn() as con:
            rows = con.execute("SELECT * FROM signals WHERE status='OPEN' AND signal_type='real' ORDER BY id ASC").fetchall()
        return [self._row_to_signal(r) for r in rows]

    def free_real_slots(self, max_positions: int) -> int:
        return max(0, int(max_positions) - len(self.active_real_signals()))

    def close_signal(self, signal_id: int, result: str, price: float, pnl_usdt: float, reason: str = "") -> None:
        with self._conn() as con:
            con.execute(
                "UPDATE signals SET status='CLOSED', result=?, result_price=?, result_pnl_usdt=?, closed_at=?, close_reason=? WHERE id=? AND status='OPEN'",
                (result, float(price), float(pnl_usdt), int(time.time()), reason, int(signal_id)),
            )

    def release_real_slot_external(self, signal_id: int, reason: str = "") -> None:
        self.close_signal(signal_id, "EXTERNAL_CLOSE", 0.0, 0.0, reason)

    def add_scan_reject(self, symbol: str, reason: str) -> None:
        with self._conn() as con:
            con.execute("INSERT INTO scan_rejects(symbol,reason,created_at) VALUES(?,?,?)", (normalize_symbol(symbol), reason[:500], int(time.time())))

    def record_coin_error(self, symbol: str, error: str, cooldown_seconds: int) -> None:
        now = int(time.time())
        with self._conn() as con:
            con.execute(
                "INSERT OR REPLACE INTO coin_errors(symbol,error,until_at,updated_at) VALUES(?,?,?,?)",
                (normalize_symbol(symbol), error[:500], now + int(cooldown_seconds), now),
            )

    def clear_coin_error(self, symbol: str) -> None:
        with self._conn() as con:
            con.execute("DELETE FROM coin_errors WHERE symbol=?", (normalize_symbol(symbol),))

    def coin_in_cooldown(self, symbol: str) -> bool:
        with self._conn() as con:
            row = con.execute("SELECT until_at FROM coin_errors WHERE symbol=?", (normalize_symbol(symbol),)).fetchone()
        return bool(row and int(row["until_at"]) > int(time.time()))

    def stats(self) -> dict[str, Any]:
        with self._conn() as con:
            rows = con.execute("SELECT * FROM signals WHERE status='CLOSED'").fetchall()
            open_rows = con.execute("SELECT * FROM signals WHERE status='OPEN'").fetchall()
            last_rejects = con.execute("SELECT symbol,reason,created_at FROM scan_rejects ORDER BY id DESC LIMIT 8").fetchall()
        closed = [dict(r) for r in rows]
        wins = [r for r in closed if r.get("result") == "TP"]
        losses = [r for r in closed if r.get("result") == "SL"]
        soft = [r for r in closed if r.get("result") == "SOFT_EXIT"]
        total = len(wins) + len(losses) + len(soft)
        pnl = sum(safe_float(r.get("result_pnl_usdt")) for r in closed)
        return {
            "closed": len(closed),
            "open": len(open_rows),
            "wins": len(wins),
            "losses": len(losses),
            "soft": len(soft),
            "winrate": (len(wins) / total * 100) if total else 0.0,
            "pnl": pnl,
            "real_open": len([r for r in open_rows if r["signal_type"] == "real"]),
            "normal_open": len([r for r in open_rows if r["signal_type"] == "normal"]),
            "last_rejects": [dict(r) for r in last_rejects],
        }

    def reset_stats(self) -> None:
        with self._conn() as con:
            con.execute("DELETE FROM signals")
            con.execute("DELETE FROM scan_rejects")
            con.execute("DELETE FROM runtime WHERE key LIKE 'last_%'")

    def _row_to_signal(self, r: sqlite3.Row) -> StoredSignal:
        return StoredSignal(
            id=int(r["id"]), symbol=r["symbol"], okx_symbol=r["okx_symbol"], toobit_symbol=r["toobit_symbol"],
            direction=r["direction"], signal_type=r["signal_type"], status=r["status"],
            entry_price=float(r["entry_price"]), tp_price=float(r["tp_price"]), sl_price=float(r["sl_price"]),
            risk_reward=float(r["risk_reward"]), score=float(r["score"]), strength=r["strength"],
            created_at=int(r["created_at"]), opened_at=int(r["opened_at"]),
            message_id=int(r["message_id"]) if r["message_id"] is not None else None,
            order_id=r["order_id"], client_order_id=r["client_order_id"], reasons_json=r["reasons_json"] or "[]",
            result=r["result"], result_price=float(r["result_price"]) if r["result_price"] is not None else None,
            result_pnl_usdt=float(r["result_pnl_usdt"]) if r["result_pnl_usdt"] is not None else None,
            closed_at=int(r["closed_at"]) if r["closed_at"] is not None else None,
        )
