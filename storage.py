from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import threading
import time
from typing import Any

import config


class Storage:
    def __init__(self, path: str = config.DB_PATH):
        self.path = path
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self.lock = threading.RLock()
        self._init()

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        return c

    def _init(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS signals(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol_id TEXT,okx_symbol TEXT,toobit_symbol TEXT,side TEXT,setup_type TEXT,
            trade_mode TEXT,is_real INTEGER DEFAULT 0,status TEXT,
            entry REAL,tp REAL,sl REAL,gross_rr REAL,net_rr REAL,
            trade_usdt REAL,leverage INTEGER,notional_usdt REAL,
            estimated_net_profit REAL,estimated_net_loss REAL,
            estimated_cost REAL,estimated_cost_win REAL,estimated_cost_loss REAL,
            slot_id INTEGER,message_id INTEGER,order_id TEXT,
            created_at INTEGER,opened_at INTEGER,closed_at INTEGER,
            exit_price REAL,outcome TEXT,net_pnl REAL,fees REAL,slippage REAL,
            mfe_r REAL DEFAULT 0,mae_r REAL DEFAULT 0,
            direction_score REAL,strength_score REAL,freshness_score REAL,
            setup_score REAL,trigger_score REAL,final_score REAL,confidence REAL,
            model_version TEXT,raw_json TEXT,result_message_sent INTEGER DEFAULT 0,
            result_retry_count INTEGER DEFAULT 0,result_retry_at INTEGER DEFAULT 0,
            real_open_confirmed INTEGER DEFAULT 0,real_entry REAL,
            strategy_id TEXT,timeframe TEXT,result_claimed_at INTEGER DEFAULT 0,close_reason TEXT
        );
        CREATE TABLE IF NOT EXISTS experiences(
            id INTEGER PRIMARY KEY AUTOINCREMENT,signal_id INTEGER,symbol_id TEXT,
            outcome TEXT,primary_cause TEXT,direction_label TEXT,mfe_r REAL,mae_r REAL,
            net_pnl REAL,model_version TEXT,created_at INTEGER,raw_json TEXT
        );
        CREATE TABLE IF NOT EXISTS profiles(
            symbol_id TEXT PRIMARY KEY,version TEXT,parameters_json TEXT,
            confidence TEXT,samples INTEGER,updated_at INTEGER
        );
        CREATE TABLE IF NOT EXISTS model_changes(
            candidate_id TEXT PRIMARY KEY,symbol_id TEXT,parent_version TEXT,
            change_json TEXT,status TEXT,created_at INTEGER
        );
        CREATE TABLE IF NOT EXISTS health_events(
            id INTEGER PRIMARY KEY AUTOINCREMENT,component TEXT,severity TEXT,message TEXT,
            symbol_id TEXT,active INTEGER DEFAULT 1,created_at INTEGER
        );
        CREATE TABLE IF NOT EXISTS audit_log(
            id INTEGER PRIMARY KEY AUTOINCREMENT,action TEXT,detail TEXT,created_at INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_signals_status ON signals(status,is_real);
        CREATE INDEX IF NOT EXISTS idx_signals_symbol ON signals(symbol_id,created_at);
        CREATE INDEX IF NOT EXISTS idx_experience_symbol ON experiences(symbol_id,created_at);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_experience_signal_unique ON experiences(signal_id);
        """
        with self._conn() as c:
            c.executescript(schema)
            # Complete forward migration for databases created by older builds.
            # CREATE TABLE IF NOT EXISTS does not add new columns to an existing table,
            # therefore every column used by runtime code must be ensured explicitly.
            migrations = {
                "signals": {
                    "symbol_id": "TEXT", "okx_symbol": "TEXT", "toobit_symbol": "TEXT",
                    "side": "TEXT", "setup_type": "TEXT", "trade_mode": "TEXT",
                    "is_real": "INTEGER DEFAULT 0", "status": "TEXT",
                    "entry": "REAL", "tp": "REAL", "sl": "REAL",
                    "gross_rr": "REAL", "net_rr": "REAL", "trade_usdt": "REAL",
                    "leverage": "INTEGER", "notional_usdt": "REAL",
                    "estimated_net_profit": "REAL", "estimated_net_loss": "REAL",
                    "estimated_cost": "REAL", "estimated_cost_win": "REAL",
                    "estimated_cost_loss": "REAL", "slot_id": "INTEGER",
                    "message_id": "INTEGER", "order_id": "TEXT",
                    "created_at": "INTEGER", "opened_at": "INTEGER", "closed_at": "INTEGER",
                    "exit_price": "REAL", "outcome": "TEXT", "net_pnl": "REAL",
                    "fees": "REAL", "slippage": "REAL",
                    "mfe_r": "REAL DEFAULT 0", "mae_r": "REAL DEFAULT 0",
                    "direction_score": "REAL", "strength_score": "REAL",
                    "freshness_score": "REAL", "setup_score": "REAL",
                    "trigger_score": "REAL", "final_score": "REAL",
                    "confidence": "REAL", "model_version": "TEXT", "raw_json": "TEXT",
                    "result_message_sent": "INTEGER DEFAULT 0",
                    "result_retry_count": "INTEGER DEFAULT 0",
                    "result_retry_at": "INTEGER DEFAULT 0",
                    "real_open_confirmed": "INTEGER DEFAULT 0", "real_entry": "REAL",
                    "strategy_id": "TEXT", "timeframe": "TEXT",
                    "result_claimed_at": "INTEGER DEFAULT 0", "close_reason": "TEXT",
                },
                "experiences": {
                    "signal_id": "INTEGER", "symbol_id": "TEXT", "outcome": "TEXT",
                    "primary_cause": "TEXT", "direction_label": "TEXT",
                    "mfe_r": "REAL", "mae_r": "REAL", "net_pnl": "REAL",
                    "model_version": "TEXT", "created_at": "INTEGER", "raw_json": "TEXT",
                },
                "health_events": {
                    "component": "TEXT", "severity": "TEXT", "message": "TEXT",
                    "symbol_id": "TEXT", "active": "INTEGER DEFAULT 1",
                    "created_at": "INTEGER",
                },
            }
            for table, columns in migrations.items():
                existing = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
                for name, sql_type in columns.items():
                    if name not in existing:
                        c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")

            # Normalize legacy rows after adding columns.
            c.execute("UPDATE health_events SET active=1 WHERE active IS NULL")
            c.execute("UPDATE signals SET mfe_r=0 WHERE mfe_r IS NULL")
            c.execute("UPDATE signals SET mae_r=0 WHERE mae_r IS NULL")
            c.execute("UPDATE signals SET result_message_sent=0 WHERE result_message_sent IS NULL")
            c.execute("UPDATE signals SET result_retry_count=0 WHERE result_retry_count IS NULL")
            c.execute("UPDATE signals SET result_retry_at=0 WHERE result_retry_at IS NULL")
            c.execute("UPDATE signals SET real_open_confirmed=0 WHERE real_open_confirmed IS NULL")
            c.execute("UPDATE signals SET result_claimed_at=0 WHERE result_claimed_at IS NULL")
            c.execute("UPDATE signals SET strategy_id='legacy' WHERE strategy_id IS NULL OR TRIM(strategy_id)=''")
            c.execute("UPDATE signals SET timeframe='unknown' WHERE timeframe IS NULL OR TRIM(timeframe)=''")
        defaults = {
            "trading_enabled": config.TRADING_ENABLED_DEFAULT,
            "auto_signal_enabled": config.AUTO_SIGNAL_ENABLED_DEFAULT,
            "trade_usdt": config.TRADE_USDT_DEFAULT,
            "leverage": config.LEVERAGE_DEFAULT,
            "max_positions": config.MAX_POSITIONS_DEFAULT,
            "profit_today": 0.0,
            "profit_total": 0.0,
            "fees_total": 0.0,
            "stats_reset_ts": 0,
            "profit_day": self._today_key(),
        }
        for key, value in defaults.items():
            if self.get(key, None) is None:
                self.set(key, value)

    @staticmethod
    def _today_key() -> str:
        return dt.datetime.now().astimezone().date().isoformat()

    def ensure_daily_profit(self) -> None:
        today = self._today_key()
        if self.get("profit_day") != today:
            with self.lock, self._conn() as c:
                c.execute(
                    "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    ("profit_today", json.dumps(0.0)),
                )
                c.execute(
                    "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    ("profit_day", json.dumps(today)),
                )

    def set(self, key: str, value: Any) -> None:
        with self.lock, self._conn() as c:
            c.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value, ensure_ascii=False)),
            )

    def get(self, key: str, default: Any = None) -> Any:
        with self._conn() as c:
            row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row["value"])
        except Exception:
            return default

    def create_signal(self, d: dict[str, Any]) -> int:
        cols = [
            "symbol_id", "okx_symbol", "toobit_symbol", "side", "setup_type",
            "trade_mode", "is_real", "status", "entry", "tp", "sl", "gross_rr",
            "net_rr", "trade_usdt", "leverage", "notional_usdt",
            "estimated_net_profit", "estimated_net_loss", "estimated_cost",
            "estimated_cost_win", "estimated_cost_loss", "slot_id", "direction_score",
            "strength_score", "freshness_score", "setup_score", "trigger_score",
            "final_score", "confidence", "model_version", "strategy_id", "timeframe",
        ]
        d = dict(d)
        d.setdefault("strategy_id", config.STRATEGY_ID)
        d.setdefault("timeframe", config.STRATEGY_TIMEFRAME)
        vals = [d.get(x) for x in cols]
        now = int(time.time())
        with self.lock, self._conn() as c:
            q = (
                f"INSERT INTO signals({','.join(cols)},created_at,opened_at,raw_json) "
                f"VALUES({','.join('?' for _ in cols)},?,?,?)"
            )
            cur = c.execute(q, vals + [now, now, json.dumps(d.get("raw", {}), ensure_ascii=False)])
            return int(cur.lastrowid)

    def update_signal(self, signal_id: int, **fields: Any) -> None:
        if not fields:
            return
        with self._conn() as meta_conn:
            allowed = {r[1] for r in meta_conn.execute("PRAGMA table_info(signals)")}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"Unknown signal fields: {sorted(unknown)}")
        with self.lock, self._conn() as c:
            c.execute(
                "UPDATE signals SET " + ",".join(f"{k}=?" for k in fields) + " WHERE id=?",
                list(fields.values()) + [signal_id],
            )

    def get_signal(self, signal_id: int) -> dict[str, Any] | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM signals WHERE id=?", (signal_id,)).fetchone()
        return dict(row) if row else None


    def has_recent_signal(self, symbol_id: str, within_seconds: int) -> bool:
        cutoff = int(time.time()) - max(0, int(within_seconds))
        with self._conn() as c:
            row = c.execute(
                "SELECT 1 FROM signals WHERE symbol_id=? AND created_at>=? AND status NOT IN ('publish_failed','cancelled') LIMIT 1",
                (symbol_id, cutoff),
            ).fetchone()
        return bool(row)

    def has_recent_stop(self, symbol_id: str, side: str, within_seconds: int) -> bool:
        cutoff = int(time.time()) - max(0, int(within_seconds))
        with self._conn() as c:
            row = c.execute(
                "SELECT 1 FROM signals WHERE symbol_id=? AND side=? AND status='closed' AND outcome='SL' AND closed_at>=? LIMIT 1",
                (symbol_id, side.upper(), cutoff),
            ).fetchone()
        return bool(row)

    def quarantine_legacy_virtual_signals(self) -> int:
        """Archive old virtual rows without sending expiry/result messages.

        Real positions are deliberately preserved so the monitor never loses exchange risk.
        """
        now = int(time.time())
        with self.lock, self._conn() as c:
            cur = c.execute(
                """UPDATE signals
                   SET status='archived', outcome='MIGRATED', closed_at=COALESCE(closed_at,?),
                       result_message_sent=1, result_claimed_at=0, close_reason='legacy_strategy_migration'
                   WHERE is_real=0 AND status IN ('open','pending')
                     AND (COALESCE(strategy_id,'legacy')!=? OR COALESCE(timeframe,'unknown')!=?)""",
                (now, config.STRATEGY_ID, config.STRATEGY_TIMEFRAME),
            )
            return int(cur.rowcount)

    def get_open_signals(self) -> list[dict[str, Any]]:
        with self._conn() as c:
            return [dict(x) for x in c.execute(
                """SELECT * FROM signals
                   WHERE status IN ('open','pending')
                     AND (is_real=1 OR (strategy_id=? AND timeframe=?))
                   ORDER BY id""",
                (config.STRATEGY_ID, config.STRATEGY_TIMEFRAME),
            )]

    def claim_unsent_closed_signals(self, limit: int = 50) -> list[dict[str, Any]]:
        now = int(time.time())
        stale_before = now - int(config.RESULT_CLAIM_TIMEOUT_SECONDS)
        with self.lock, self._conn() as c:
            rows = [dict(x) for x in c.execute(
                """SELECT * FROM signals
                   WHERE status='closed' AND result_message_sent=0
                     AND COALESCE(result_retry_at,0)<=?
                     AND COALESCE(result_claimed_at,0)<=?
                     AND strategy_id=? AND timeframe=?
                     AND COALESCE(outcome,'') NOT IN ('MIGRATED','ARCHIVED')
                   ORDER BY id LIMIT ?""",
                (now, stale_before, config.STRATEGY_ID, config.STRATEGY_TIMEFRAME, max(1, int(limit))),
            )]
            if not rows:
                return []
            ids = [int(r['id']) for r in rows]
            q = ','.join('?' for _ in ids)
            c.execute(f"UPDATE signals SET result_claimed_at=? WHERE id IN ({q})", [now, *ids])
            for r in rows:
                r['result_claimed_at'] = now
            return rows

    def release_result_claim(self, signal_id: int) -> None:
        self.update_signal(signal_id, result_claimed_at=0)

    def get_unsent_closed_signals(self, limit: int = 50) -> list[dict[str, Any]]:
        # Backward-compatible read-only helper. Retry worker should use claim_unsent_closed_signals.
        with self._conn() as c:
            return [dict(x) for x in c.execute(
                """SELECT * FROM signals WHERE status='closed' AND result_message_sent=0
                   AND COALESCE(result_retry_at,0)<=? AND strategy_id=? AND timeframe=?
                   ORDER BY id LIMIT ?""",
                (int(time.time()), config.STRATEGY_ID, config.STRATEGY_TIMEFRAME, max(1, int(limit))),
            )]

    def schedule_result_retry(self, signal_id: int, retry_count: int) -> None:
        delay = min(config.RESULT_RETRY_MAX_SECONDS, config.RESULT_RETRY_BASE_SECONDS * (2 ** max(0, retry_count - 1)))
        self.update_signal(signal_id, result_retry_count=retry_count, result_retry_at=int(time.time()) + int(delay))

    def count_real_open(self) -> int:
        with self._conn() as c:
            return int(c.execute("SELECT COUNT(*) n FROM signals WHERE is_real=1 AND status IN ('open','pending')").fetchone()["n"])

    def close_signal(self, signal_id: int, **result: Any) -> bool:
        self.ensure_daily_profit()
        allowed = {"outcome", "exit_price", "net_pnl", "fees", "slippage", "mfe_r", "mae_r", "close_reason"}
        unknown = set(result) - allowed
        if unknown:
            raise ValueError(f"Unknown close fields: {sorted(unknown)}")
        with self.lock, self._conn() as c:
            row = c.execute("SELECT status FROM signals WHERE id=?", (signal_id,)).fetchone()
            if not row or row["status"] == "closed":
                return False
            result.update(status="closed", closed_at=int(time.time()), result_message_sent=0, result_claimed_at=0)
            cur = c.execute(
                "UPDATE signals SET " + ",".join(f"{k}=?" for k in result) + " WHERE id=? AND status!='closed'",
                list(result.values()) + [signal_id],
            )
            if cur.rowcount != 1:
                return False
            pnl = float(result.get("net_pnl", 0) or 0)
            fees = float(result.get("fees", 0) or 0)
            for key, delta in (("profit_today", pnl), ("profit_total", pnl), ("fees_total", fees)):
                old_row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
                old = float(json.loads(old_row["value"])) if old_row else 0.0
                c.execute(
                    "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, json.dumps(old + delta)),
                )
        return True

    def stats(self) -> dict[str, Any]:
        since = int(self.get("stats_reset_ts", 0) or 0)
        with self._conn() as c:
            rows = [dict(x) for x in c.execute("SELECT * FROM signals WHERE created_at>=? AND status NOT IN ('publish_failed','cancelled')", (since,))]
        closed = [x for x in rows if x["status"] == "closed"]
        tp = sum(x.get("outcome") == "TP" for x in closed)
        sl = sum(x.get("outcome") == "SL" for x in closed)
        expired = sum(x.get("outcome") == "EXPIRED" for x in closed)
        wins = [float(x.get("net_pnl") or 0) for x in closed if float(x.get("net_pnl") or 0) > 0]
        losses = [abs(float(x.get("net_pnl") or 0)) for x in closed if float(x.get("net_pnl") or 0) < 0]
        profit_factor = sum(wins) / sum(losses) if losses else (float("inf") if wins else 0.0)
        return {
            "signals": len(rows),
            "open": sum(x["status"] in ("open", "pending") for x in rows),
            "tp": tp,
            "sl": sl,
            "expired": expired,
            "real": sum(bool(x["is_real"]) for x in rows),
            "virtual": sum(not bool(x["is_real"]) for x in rows),
            "net_pnl": sum(float(x.get("net_pnl") or 0) for x in closed),
            "fees": sum(float(x.get("fees") or 0) for x in closed),
            "profit_factor": profit_factor,
        }

    def reset_stats(self) -> None:
        self.set("stats_reset_ts", int(time.time()))

    def reset_profit(self) -> None:
        self.set("profit_today", 0.0)
        self.set("profit_total", 0.0)
        self.set("profit_day", self._today_key())

    def add_experience(self, e: dict[str, Any]) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO experiences(signal_id,symbol_id,outcome,primary_cause,direction_label,mfe_r,mae_r,net_pnl,model_version,created_at,raw_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    e["signal_id"], e.get("symbol_id"), e.get("outcome"), e.get("primary_cause"),
                    e.get("direction_label"), e.get("mfe_r", 0), e.get("mae_r", 0),
                    e.get("net_pnl", 0), e.get("model_version"), int(time.time()),
                    json.dumps(e, ensure_ascii=False),
                ),
            )

    def get_experience_for_signal(self, signal_id: int) -> dict[str, Any] | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM experiences WHERE signal_id=? ORDER BY id DESC LIMIT 1", (signal_id,)).fetchone()
        return dict(row) if row else None

    def list_experiences(self, symbol_id: str, limit: int = 500) -> list[dict[str, Any]]:
        with self._conn() as c:
            return [dict(x) for x in c.execute("SELECT * FROM experiences WHERE symbol_id=? ORDER BY id DESC LIMIT ?", (symbol_id, limit))]

    def get_profile(self, symbol_id: str) -> dict[str, Any] | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM profiles WHERE symbol_id=?", (symbol_id,)).fetchone()
        if not row:
            return None
        return {
            "version": row["version"],
            "parameters": json.loads(row["parameters_json"] or "{}"),
            "confidence": row["confidence"],
            "samples": row["samples"],
        }

    def save_profile(self, symbol_id: str, version: str, parameters: dict[str, Any], confidence: str, samples: int) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO profiles VALUES(?,?,?,?,?,?) ON CONFLICT(symbol_id) DO UPDATE SET version=excluded.version,parameters_json=excluded.parameters_json,confidence=excluded.confidence,samples=excluded.samples,updated_at=excluded.updated_at",
                (symbol_id, version, json.dumps(parameters), confidence, samples, int(time.time())),
            )

    def save_model_change(self, cid: str, symbol_id: str, parent: str, change: dict[str, Any], status: str) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO model_changes VALUES(?,?,?,?,?,?)",
                (cid, symbol_id, parent, json.dumps(change), status, int(time.time())),
            )

    def add_health_event(self, component: str, severity: str, message: str, symbol_id: str | None = None) -> None:
        with self.lock, self._conn() as c:
            current = c.execute(
                "SELECT id,message,severity FROM health_events WHERE component=? AND COALESCE(symbol_id,'')=COALESCE(?, '') AND active=1 ORDER BY id DESC LIMIT 1",
                (component, symbol_id),
            ).fetchone()
            if current and current["message"] == message and current["severity"] == severity:
                return
            c.execute("UPDATE health_events SET active=0 WHERE component=? AND COALESCE(symbol_id,'')=COALESCE(?, '') AND active=1", (component, symbol_id))
            c.execute(
                "INSERT INTO health_events(component,severity,message,symbol_id,created_at) VALUES(?,?,?,?,?)",
                (component, severity, message, symbol_id, int(time.time())),
            )

    def resolve_health(self, component: str, symbol_id: str | None = None) -> None:
        with self._conn() as c:
            c.execute("UPDATE health_events SET active=0 WHERE component=? AND COALESCE(symbol_id,'')=COALESCE(?, '') AND active=1", (component, symbol_id))

    def active_health_events(self) -> list[dict[str, Any]]:
        with self._conn() as c:
            return [dict(x) for x in c.execute("SELECT * FROM health_events WHERE active=1 ORDER BY id DESC LIMIT 50")]

    def audit(self, action: str, detail: str = "") -> None:
        with self._conn() as c:
            c.execute("INSERT INTO audit_log(action,detail,created_at) VALUES(?,?,?)", (action, detail, int(time.time())))
