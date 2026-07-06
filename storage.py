from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Iterable

import config
from utils import safe_float, safe_int


@dataclass(frozen=True)
class StoredSignal:
    id: int
    symbol: str
    okx_symbol: str
    toobit_symbol: str
    direction: str
    signal_type: str
    status: str
    real_status: str
    score: float
    risk_reward: float
    entry_price: float
    tp_price: float
    sl_price: float
    trade_margin_usdt: float
    leverage: int
    round_trip_fee_usdt: float
    opened_at: int
    closed_at: int | None
    message_id: int | None
    order_id: str | None
    approx_pnl: float | None
    real_pnl: float | None
    net_pnl: float | None
    mfe_pct: float
    mae_pct: float
    close_reason: str | None
    reasons: str | None


class Storage:
    def __init__(self, path: str = config.BOT_DB_PATH) -> None:
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init()

    def _init(self) -> None:
        cur = self.conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS runtime (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                okx_symbol TEXT NOT NULL,
                toobit_symbol TEXT NOT NULL,
                direction TEXT NOT NULL,
                signal_type TEXT NOT NULL,
                status TEXT NOT NULL,
                real_status TEXT NOT NULL DEFAULT '',
                score REAL NOT NULL,
                risk_reward REAL NOT NULL,
                entry_price REAL NOT NULL,
                tp_price REAL NOT NULL,
                sl_price REAL NOT NULL,
                trade_margin_usdt REAL NOT NULL,
                leverage INTEGER NOT NULL,
                round_trip_fee_usdt REAL NOT NULL,
                opened_at INTEGER NOT NULL,
                closed_at INTEGER,
                message_id INTEGER,
                result_message_id INTEGER,
                order_id TEXT,
                approx_pnl REAL,
                real_pnl REAL,
                net_pnl REAL,
                mfe_pct REAL NOT NULL DEFAULT 0,
                mae_pct REAL NOT NULL DEFAULT 0,
                close_reason TEXT,
                reasons TEXT
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_signals_open ON signals(status, symbol)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_signals_type ON signals(signal_type, real_status)")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS coin_errors (
                symbol TEXT PRIMARY KEY,
                error_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                last_error_at INTEGER NOT NULL DEFAULT 0,
                cooldown_until INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS monitor_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                price REAL,
                pnl REAL,
                net_pnl REAL,
                detail TEXT,
                created_at INTEGER NOT NULL
            )
            """
        )
        self.conn.commit()
        self._ensure_default_settings()

    def _ensure_default_settings(self) -> None:
        defaults = {
            "real_trade_enabled": "1" if config.DEFAULT_TRADE_ENABLED else "0",
            "trade_dollar_usdt": str(config.DEFAULT_TRADE_DOLLAR),
            "trade_capital_usdt": str(config.DEFAULT_TRADE_CAPITAL),
            "leverage": str(config.DEFAULT_LEVERAGE),
            "max_positions": str(config.DEFAULT_MAX_POSITIONS),
            "min_net_profit_usdt": str(config.DEFAULT_MIN_NET_PROFIT_USDT),
        }
        for k, v in defaults.items():
            self.conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v))
        self.conn.commit()

    def get_setting(self, key: str, default: Any = None) -> str | Any:
        row = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: Any) -> None:
        self.conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, str(value)))
        self.conn.commit()

    def settings(self) -> dict[str, Any]:
        return {
            "real_trade_enabled": self.get_setting("real_trade_enabled", "0") == "1",
            "trade_dollar_usdt": safe_float(self.get_setting("trade_dollar_usdt", config.DEFAULT_TRADE_DOLLAR), config.DEFAULT_TRADE_DOLLAR),
            "trade_capital_usdt": safe_float(self.get_setting("trade_capital_usdt", config.DEFAULT_TRADE_CAPITAL), config.DEFAULT_TRADE_CAPITAL),
            "leverage": safe_int(self.get_setting("leverage", config.DEFAULT_LEVERAGE), config.DEFAULT_LEVERAGE),
            "max_positions": safe_int(self.get_setting("max_positions", config.DEFAULT_MAX_POSITIONS), config.DEFAULT_MAX_POSITIONS),
            "min_net_profit_usdt": safe_float(self.get_setting("min_net_profit_usdt", config.DEFAULT_MIN_NET_PROFIT_USDT), config.DEFAULT_MIN_NET_PROFIT_USDT),
        }

    def runtime_get(self, key: str, default: Any = None) -> str | Any:
        row = self.conn.execute("SELECT value FROM runtime WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def runtime_set(self, key: str, value: Any) -> None:
        self.conn.execute("INSERT OR REPLACE INTO runtime(key,value) VALUES(?,?)", (key, str(value)))
        self.conn.commit()

    def add_signal(self, plan: Any, *, signal_type: str, real_status: str = "", order_id: str | None = None, message_id: int | None = None) -> int:
        now = int(time.time())
        data = plan.to_legacy_dict() if hasattr(plan, "to_legacy_dict") else dict(plan)
        cur = self.conn.execute(
            """
            INSERT INTO signals(
                symbol, okx_symbol, toobit_symbol, direction, signal_type, status, real_status,
                score, risk_reward, entry_price, tp_price, sl_price, trade_margin_usdt, leverage,
                round_trip_fee_usdt, opened_at, message_id, order_id, reasons
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                str(data["symbol"]).upper(),
                str(data.get("okx_symbol") or data["symbol"]).upper(),
                str(data.get("toobit_symbol") or data["symbol"]).upper(),
                str(data["direction"]).upper(),
                signal_type,
                "OPEN",
                real_status,
                safe_float(data.get("score")),
                safe_float(data.get("risk_reward")),
                safe_float(data.get("entry_price") or data.get("entry")),
                safe_float(data.get("tp_price") or data.get("tp")),
                safe_float(data.get("sl_price") or data.get("sl")),
                safe_float(data.get("trade_margin_usdt") or self.settings()["trade_dollar_usdt"]),
                safe_int(data.get("leverage") or self.settings()["leverage"]),
                safe_float(data.get("round_trip_fee_usdt"), config.ROUND_TRIP_FEE_USDT),
                now,
                message_id,
                order_id,
                json.dumps(data.get("reasons") or [], ensure_ascii=False),
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def update_message_id(self, signal_id: int, message_id: int | None) -> None:
        if message_id is None:
            return
        self.conn.execute("UPDATE signals SET message_id=? WHERE id=?", (int(message_id), int(signal_id)))
        self.conn.commit()

    def mark_real_opened(self, signal_id: int, order_id: str | None = None) -> None:
        self.conn.execute("UPDATE signals SET signal_type='real', real_status='opened', order_id=COALESCE(?, order_id) WHERE id=?", (order_id, int(signal_id)))
        self.conn.commit()

    def mark_real_failed(self, symbol: str, reason: str) -> None:
        self.add_monitor_event(0, "REAL_FAILED", None, None, None, f"{symbol}: {reason}")

    def open_signals(self) -> list[StoredSignal]:
        rows = self.conn.execute("SELECT * FROM signals WHERE status='OPEN' ORDER BY opened_at ASC").fetchall()
        return [self._row(row) for row in rows]

    def active_real_signals(self) -> list[StoredSignal]:
        rows = self.conn.execute("SELECT * FROM signals WHERE status='OPEN' AND signal_type='real' AND real_status IN ('opened','reserved','opening')").fetchall()
        return [self._row(row) for row in rows]

    def active_real_count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS c FROM signals WHERE status='OPEN' AND signal_type='real' AND real_status IN ('opened','reserved','opening')").fetchone()
        return int(row["c"] or 0)

    def has_open_symbol(self, symbol: str) -> bool:
        row = self.conn.execute("SELECT 1 FROM signals WHERE status='OPEN' AND symbol=? LIMIT 1", (symbol.upper(),)).fetchone()
        return row is not None

    def free_real_slots(self, max_positions: int) -> int:
        return max(0, int(max_positions) - self.active_real_count())

    def update_excursions(self, signal_id: int, price: float) -> tuple[float, float]:
        sig = self.get_signal(signal_id)
        if sig is None or sig.entry_price <= 0 or price <= 0:
            return (0.0, 0.0)
        if sig.direction == "LONG":
            move = (price - sig.entry_price) / sig.entry_price
        else:
            move = (sig.entry_price - price) / sig.entry_price
        mfe = max(sig.mfe_pct, move)
        mae = min(sig.mae_pct, move)
        self.conn.execute("UPDATE signals SET mfe_pct=?, mae_pct=? WHERE id=?", (mfe, mae, signal_id))
        self.conn.commit()
        return mfe, mae

    def finish_signal(
        self,
        signal_id: int,
        *,
        status: str,
        exit_price: float,
        approx_pnl: float,
        net_pnl: float,
        real_pnl: float | None = None,
        result_message_id: int | None = None,
        close_reason: str = "",
    ) -> None:
        self.conn.execute(
            """
            UPDATE signals
            SET status=?, real_status=CASE WHEN signal_type='real' THEN 'closed' ELSE real_status END,
                closed_at=?, approx_pnl=?, real_pnl=?, net_pnl=?, result_message_id=?, close_reason=?
            WHERE id=?
            """,
            (status, int(time.time()), approx_pnl, real_pnl, net_pnl, result_message_id, close_reason, int(signal_id)),
        )
        self.conn.commit()
        self.add_monitor_event(signal_id, status, exit_price, approx_pnl, net_pnl, close_reason)

    def release_real_slot_external(self, signal_id: int, detail: str = "Toobit position not found after 70s recheck") -> None:
        self.conn.execute("UPDATE signals SET real_status='external_closed' WHERE id=?", (int(signal_id),))
        self.conn.commit()
        self.add_monitor_event(signal_id, "SLOT_RELEASED", None, None, None, detail)

    def get_signal(self, signal_id: int) -> StoredSignal | None:
        row = self.conn.execute("SELECT * FROM signals WHERE id=?", (int(signal_id),)).fetchone()
        return self._row(row) if row else None

    def add_monitor_event(self, signal_id: int, event_type: str, price: float | None, pnl: float | None, net_pnl: float | None, detail: str = "") -> None:
        self.conn.execute(
            "INSERT INTO monitor_events(signal_id,event_type,price,pnl,net_pnl,detail,created_at) VALUES(?,?,?,?,?,?,?)",
            (int(signal_id), event_type, price, pnl, net_pnl, detail, int(time.time())),
        )
        self.conn.commit()

    def record_coin_error(self, symbol: str, error: str, cooldown_seconds: int = config.COIN_ERROR_COOLDOWN_SECONDS) -> None:
        now = int(time.time())
        row = self.conn.execute("SELECT error_count FROM coin_errors WHERE symbol=?", (symbol.upper(),)).fetchone()
        count = int(row["error_count"] or 0) + 1 if row else 1
        self.conn.execute(
            "INSERT OR REPLACE INTO coin_errors(symbol,error_count,last_error,last_error_at,cooldown_until) VALUES(?,?,?,?,?)",
            (symbol.upper(), count, str(error)[:500], now, now + int(cooldown_seconds)),
        )
        self.conn.commit()

    def coin_in_cooldown(self, symbol: str) -> bool:
        row = self.conn.execute("SELECT cooldown_until FROM coin_errors WHERE symbol=?", (symbol.upper(),)).fetchone()
        return bool(row and int(row["cooldown_until"] or 0) > int(time.time()))

    def clear_coin_error(self, symbol: str) -> None:
        self.conn.execute("DELETE FROM coin_errors WHERE symbol=?", (symbol.upper(),))
        self.conn.commit()

    def recent_open_positions_text(self) -> str:
        rows = self.open_signals()
        if not rows:
            return "پوزیشن/سیگنال باز نداریم."
        lines = ["📌 پوزیشن‌ها / سیگنال‌های باز:"]
        for s in rows:
            lines.append(f"#{s.id} {s.symbol} {s.direction} | {s.signal_type} | Entry {s.entry_price:g} | TP {s.tp_price:g} | SL {s.sl_price:g}")
        return "\n".join(lines)

    def stats(self, days: int = 30) -> dict[str, Any]:
        since = int(time.time()) - int(days) * 86400
        rows = self.conn.execute("SELECT * FROM signals WHERE opened_at>=?", (since,)).fetchall()
        items = [self._row(r) for r in rows]

        def bucket(filter_fn):
            selected = [x for x in items if filter_fn(x)]
            closed = [x for x in selected if x.status in {"TP", "SL", "EXIT"}]
            tp = len([x for x in selected if x.status == "TP"])
            sl = len([x for x in selected if x.status == "SL"])
            pnl = sum(safe_float(x.real_pnl if x.real_pnl is not None else x.net_pnl if x.net_pnl is not None else x.approx_pnl) for x in closed)
            done = tp + sl
            return {
                "total": len(selected),
                "open": len([x for x in selected if x.status == "OPEN"]),
                "tp": tp,
                "sl": sl,
                "exit": len([x for x in selected if x.status == "EXIT"]),
                "win_rate": (tp / done * 100.0) if done else 0.0,
                "pnl": pnl,
            }

        real_failed = self.conn.execute("SELECT COUNT(*) AS c FROM monitor_events WHERE event_type='REAL_FAILED' AND created_at>=?", (since,)).fetchone()
        return {
            "normal": bucket(lambda x: x.signal_type == "normal"),
            "real": bucket(lambda x: x.signal_type == "real"),
            "long": bucket(lambda x: x.direction == "LONG"),
            "short": bucket(lambda x: x.direction == "SHORT"),
            "real_failed": {"total": int(real_failed["c"] or 0)},
        }

    def _row(self, row: sqlite3.Row) -> StoredSignal:
        return StoredSignal(
            id=int(row["id"]), symbol=row["symbol"], okx_symbol=row["okx_symbol"], toobit_symbol=row["toobit_symbol"],
            direction=row["direction"], signal_type=row["signal_type"], status=row["status"], real_status=row["real_status"],
            score=safe_float(row["score"]), risk_reward=safe_float(row["risk_reward"]), entry_price=safe_float(row["entry_price"]),
            tp_price=safe_float(row["tp_price"]), sl_price=safe_float(row["sl_price"]), trade_margin_usdt=safe_float(row["trade_margin_usdt"]),
            leverage=safe_int(row["leverage"]), round_trip_fee_usdt=safe_float(row["round_trip_fee_usdt"]), opened_at=safe_int(row["opened_at"]),
            closed_at=safe_int(row["closed_at"], 0) or None, message_id=safe_int(row["message_id"], 0) or None,
            order_id=row["order_id"], approx_pnl=row["approx_pnl"], real_pnl=row["real_pnl"], net_pnl=row["net_pnl"],
            mfe_pct=safe_float(row["mfe_pct"]), mae_pct=safe_float(row["mae_pct"]), close_reason=row["close_reason"], reasons=row["reasons"],
        )
