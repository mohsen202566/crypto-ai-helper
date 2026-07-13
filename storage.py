"""ذخیره پایدار تنظیمات، پروفایل‌ها، سیگنال‌ها، نتایج و سلامت در SQLite."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
import json
import sqlite3
import threading
import time
from zoneinfo import ZoneInfo
from datetime import datetime

import config


class Storage:
    def __init__(self, path: str = config.DB_PATH) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_db()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            db = sqlite3.connect(self.path, timeout=20, isolation_level=None)
            db.row_factory = sqlite3.Row
            try:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("PRAGMA synchronous=NORMAL")
                db.execute("PRAGMA foreign_keys=ON")
                yield db
            finally:
                db.close()

    def _init_db(self) -> None:
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS kv(
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS profiles(
                    symbol_id TEXT PRIMARY KEY,
                    okx_symbol TEXT NOT NULL,
                    toobit_symbol TEXT NOT NULL,
                    updated_at INTEGER NOT NULL,
                    candle_count INTEGER NOT NULL,
                    profile_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS signals(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol_id TEXT NOT NULL,
                    okx_symbol TEXT NOT NULL,
                    toobit_symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    entry REAL NOT NULL,
                    tp REAL NOT NULL,
                    sl REAL NOT NULL,
                    tp_pct REAL NOT NULL,
                    sl_pct REAL NOT NULL,
                    rr REAL NOT NULL,
                    expected_minutes INTEGER NOT NULL,
                    trigger_window INTEGER NOT NULL,
                    trigger_reason TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    is_real INTEGER NOT NULL DEFAULT 0,
                    message_id INTEGER,
                    order_id TEXT,
                    quantity TEXT,
                    trade_usdt REAL NOT NULL,
                    leverage INTEGER NOT NULL,
                    notional REAL NOT NULL,
                    estimated_tp_net REAL NOT NULL DEFAULT 0,
                    estimated_sl_net_loss REAL NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL,
                    opened_at INTEGER,
                    closed_at INTEGER,
                    entry_real REAL,
                    close_price REAL,
                    gross_pnl REAL NOT NULL DEFAULT 0,
                    fees REAL NOT NULL DEFAULT 0,
                    net_pnl REAL NOT NULL DEFAULT 0,
                    result TEXT,
                    result_detail TEXT,
                    mfe_pct REAL NOT NULL DEFAULT 0,
                    mae_pct REAL NOT NULL DEFAULT 0,
                    raw_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_signals_status ON signals(status);
                CREATE INDEX IF NOT EXISTS idx_signals_symbol_status ON signals(symbol_id,status);
                CREATE TABLE IF NOT EXISTS learning_patterns(
                    pattern_key TEXT PRIMARY KEY,
                    symbol_id TEXT NOT NULL,
                    side TEXT NOT NULL,
                    trigger_window INTEGER NOT NULL,
                    support_tool TEXT NOT NULL,
                    horizon INTEGER NOT NULL,
                    factors_json TEXT NOT NULL,
                    best_factors_json TEXT NOT NULL,
                    previous_factors_json TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'BASE',
                    active_variable TEXT,
                    active_reason TEXT,
                    source_signal_id INTEGER,
                    trial_tp INTEGER NOT NULL DEFAULT 0,
                    trial_sl INTEGER NOT NULL DEFAULT 0,
                    total_tp INTEGER NOT NULL DEFAULT 0,
                    total_sl INTEGER NOT NULL DEFAULT 0,
                    consecutive_tp INTEGER NOT NULL DEFAULT 0,
                    consecutive_sl INTEGER NOT NULL DEFAULT 0,
                    failed_trials INTEGER NOT NULL DEFAULT 0,
                    stats_json TEXT NOT NULL DEFAULT '{}',
                    updated_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_learning_lookup
                    ON learning_patterns(symbol_id,side,trigger_window,support_tool,horizon);
                CREATE TABLE IF NOT EXISTS learning_changes(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pattern_key TEXT NOT NULL,
                    signal_id INTEGER,
                    version INTEGER NOT NULL,
                    variable TEXT NOT NULL,
                    old_value REAL NOT NULL,
                    new_value REAL NOT NULL,
                    reason TEXT NOT NULL,
                    status TEXT NOT NULL,
                    details_json TEXT NOT NULL DEFAULT '{}',
                    created_at INTEGER NOT NULL,
                    resolved_at INTEGER
                );
                CREATE INDEX IF NOT EXISTS idx_learning_changes_pattern
                    ON learning_changes(pattern_key,id);
                CREATE TABLE IF NOT EXISTS learning_outcomes(
                    signal_id INTEGER PRIMARY KEY,
                    pattern_key TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    result TEXT NOT NULL,
                    net_pnl REAL NOT NULL DEFAULT 0,
                    valid INTEGER NOT NULL DEFAULT 1,
                    created_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_learning_outcomes_pattern
                    ON learning_outcomes(pattern_key,version,created_at);
                CREATE TABLE IF NOT EXISTS learning_reviews(
                    signal_id INTEGER PRIMARY KEY,
                    pattern_key TEXT NOT NULL,
                    symbol_id TEXT NOT NULL,
                    side TEXT NOT NULL,
                    entry REAL NOT NULL,
                    tp REAL NOT NULL,
                    sl REAL NOT NULL,
                    tp_pct REAL NOT NULL,
                    sl_pct REAL NOT NULL,
                    started_at INTEGER NOT NULL,
                    sl_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    max_favorable_pct REAL NOT NULL DEFAULT 0,
                    max_adverse_pct REAL NOT NULL DEFAULT 0,
                    hit_original_tp INTEGER NOT NULL DEFAULT 0,
                    finalized INTEGER NOT NULL DEFAULT 0,
                    diagnosis TEXT,
                    updated_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_learning_reviews_open
                    ON learning_reviews(finalized,symbol_id,expires_at);
                CREATE TABLE IF NOT EXISTS telegram_updates(
                    update_id INTEGER PRIMARY KEY,
                    claimed_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_telegram_updates_claimed_at
                    ON telegram_updates(claimed_at);
                CREATE TABLE IF NOT EXISTS health_events(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    component TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    message TEXT NOT NULL,
                    symbol_id TEXT,
                    created_at INTEGER NOT NULL,
                    cleared_at INTEGER
                );
                """
            )
            signal_columns = {str(row[1]) for row in db.execute("PRAGMA table_info(signals)").fetchall()}
            if "result_detail" not in signal_columns:
                db.execute("ALTER TABLE signals ADD COLUMN result_detail TEXT")
            learning_columns = {str(row[1]) for row in db.execute("PRAGMA table_info(learning_patterns)").fetchall()}
            if "stats_json" not in learning_columns:
                db.execute("ALTER TABLE learning_patterns ADD COLUMN stats_json TEXT NOT NULL DEFAULT '{}'")
        defaults = {
            "trading_enabled": config.TRADING_ENABLED_DEFAULT,
            "auto_signal_enabled": config.AUTO_SIGNAL_ENABLED_DEFAULT,
            "trade_usdt": config.TRADE_USDT_DEFAULT,
            "leverage": config.LEVERAGE_DEFAULT,
            "max_positions": config.MAX_POSITIONS_DEFAULT,
            "telegram_offset": 0,
            "telegram_updates_initialized": False,
            "toobit_connected": False,
            "toobit_available_usdt": 0.0,
            "toobit_total_usdt": 0.0,
            "toobit_margin_usdt": 0.0,
            "toobit_last_error": "",
            "toobit_last_update": 0,
            "profiles_ready": 0,
            "profiles_updated_at": 0,
            "stats_reset_at": int(time.time()),
            "profit_reset_at": int(time.time()),
            "profit_today": 0.0,
            "profit_total": 0.0,
            "profit_day": self._today(),
        }
        for key, value in defaults.items():
            if self.get(key, None) is None:
                self.set(key, value)

    @staticmethod
    def _today() -> str:
        return datetime.now(ZoneInfo(config.TIMEZONE)).strftime("%Y-%m-%d")

    def get(self, key: str, default: Any = None) -> Any:
        with self.connect() as db:
            row = db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(str(row["value"]))
        except (TypeError, json.JSONDecodeError):
            return default

    def set(self, key: str, value: Any) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO kv(key,value,updated_at) VALUES(?,?,?)",
                (key, json.dumps(value, ensure_ascii=False), int(time.time())),
            )

    def advance_telegram_offset(self, update_id: int) -> int:
        """Persist a monotonically increasing Telegram offset.

        This is intentionally separate from duplicate claiming. Even an update
        already claimed by another process must advance the local/global offset,
        otherwise getUpdates can return the same item forever.
        """
        update_id = int(update_id or 0)
        if update_id <= 0:
            return int(self.get("telegram_offset", 0) or 0)
        now = int(time.time())
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute("SELECT value FROM kv WHERE key=?", ("telegram_offset",)).fetchone()
                current = 0
                if row is not None:
                    try:
                        current = int(json.loads(str(row["value"])))
                    except (TypeError, ValueError, json.JSONDecodeError):
                        current = 0
                final = max(current, update_id)
                db.execute(
                    "INSERT OR REPLACE INTO kv(key,value,updated_at) VALUES(?,?,?)",
                    ("telegram_offset", json.dumps(final), now),
                )
                db.execute("COMMIT")
                return final
            except Exception:
                db.execute("ROLLBACK")
                raise

    def claim_telegram_update(self, update_id: int) -> bool:
        """Atomically claim an update without coupling the claim to the offset."""
        update_id = int(update_id or 0)
        if update_id <= 0:
            return False
        now = int(time.time())
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                cursor = db.execute(
                    "INSERT OR IGNORE INTO telegram_updates(update_id,claimed_at) VALUES(?,?)",
                    (update_id, now),
                )
                claimed = int(cursor.rowcount or 0) == 1
                if claimed and update_id % 100 == 0:
                    db.execute(
                        "DELETE FROM telegram_updates WHERE claimed_at < ?",
                        (now - 30 * 24 * 3600,),
                    )
                db.execute("COMMIT")
                return claimed
            except Exception:
                db.execute("ROLLBACK")
                raise

    def save_profile(self, symbol_id: str, okx_symbol: str, toobit_symbol: str, profile: dict[str, Any]) -> None:
        now = int(time.time())
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO profiles(symbol_id,okx_symbol,toobit_symbol,updated_at,candle_count,profile_json)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(symbol_id) DO UPDATE SET
                    okx_symbol=excluded.okx_symbol,
                    toobit_symbol=excluded.toobit_symbol,
                    updated_at=excluded.updated_at,
                    candle_count=excluded.candle_count,
                    profile_json=excluded.profile_json
                """,
                (
                    symbol_id,
                    okx_symbol,
                    toobit_symbol,
                    now,
                    int(profile.get("candle_count") or 0),
                    json.dumps(profile, ensure_ascii=False, separators=(",", ":")),
                ),
            )

    def load_profile(self, symbol_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM profiles WHERE symbol_id=?", (symbol_id,)).fetchone()
        if row is None:
            return None
        try:
            profile = json.loads(str(row["profile_json"]))
            profile["_stored_updated_at"] = int(row["updated_at"])
            return profile
        except (TypeError, json.JSONDecodeError):
            return None

    def profile_rows(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT symbol_id,updated_at,candle_count FROM profiles ORDER BY symbol_id").fetchall()
        return [dict(row) for row in rows]

    def is_profile_fresh(self, symbol_id: str, max_age_hours: float = config.PROFILE_FRESH_HOURS) -> bool:
        with self.connect() as db:
            row = db.execute("SELECT updated_at,candle_count FROM profiles WHERE symbol_id=?", (symbol_id,)).fetchone()
        if row is None:
            return False
        return (
            int(row["candle_count"]) >= config.PROFILE_MIN_CANDLES
            and time.time() - int(row["updated_at"]) <= max_age_hours * 3600
        )

    def has_active_signal(self, symbol_id: str) -> bool:
        with self.connect() as db:
            row = db.execute(
                "SELECT 1 FROM signals WHERE symbol_id=? AND status IN ('pending','open') LIMIT 1",
                (symbol_id,),
            ).fetchone()
        return row is not None

    def create_signal(self, data: dict[str, Any]) -> int | None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            exists = db.execute(
                "SELECT 1 FROM signals WHERE symbol_id=? AND status IN ('pending','open') LIMIT 1",
                (data["symbol_id"],),
            ).fetchone()
            if exists:
                db.execute("ROLLBACK")
                return None
            columns = (
                "symbol_id,okx_symbol,toobit_symbol,side,entry,tp,sl,tp_pct,sl_pct,rr,"
                "expected_minutes,trigger_window,trigger_reason,mode,status,is_real,message_id,order_id,quantity,"
                "trade_usdt,leverage,notional,estimated_tp_net,estimated_sl_net_loss,created_at,opened_at,entry_real,raw_json"
            )
            values = (
                data["symbol_id"], data["okx_symbol"], data["toobit_symbol"], data["side"],
                data["entry"], data["tp"], data["sl"], data["tp_pct"], data["sl_pct"], data["rr"],
                data["expected_minutes"], data["trigger_window"], data["trigger_reason"], data["mode"],
                data["status"], int(bool(data.get("is_real"))), data.get("message_id"), data.get("order_id"),
                data.get("quantity"), data["trade_usdt"], data["leverage"], data["notional"],
                data.get("estimated_tp_net", 0.0), data.get("estimated_sl_net_loss", 0.0),
                int(data.get("created_at") or time.time()), data.get("opened_at"), data.get("entry_real"),
                json.dumps(data.get("raw") or {}, ensure_ascii=False),
            )
            placeholders = ",".join("?" for _ in values)
            cursor = db.execute(f"INSERT INTO signals({columns}) VALUES({placeholders})", values)
            signal_id = int(cursor.lastrowid)
            db.execute("COMMIT")
            return signal_id

    def update_signal(self, signal_id: int, **fields: Any) -> None:
        allowed = {
            "status", "message_id", "order_id", "quantity", "opened_at", "entry_real", "closed_at",
            "close_price", "gross_pnl", "fees", "net_pnl", "result", "result_detail", "mfe_pct", "mae_pct",
            "mode", "is_real", "raw_json",
        }
        items = [(key, value) for key, value in fields.items() if key in allowed]
        if not items:
            return
        sql = ",".join(f"{key}=?" for key, _ in items)
        values: list[Any] = []
        for key, value in items:
            if key == "raw_json" and not isinstance(value, str):
                value = json.dumps(value, ensure_ascii=False)
            values.append(value)
        values.append(signal_id)
        with self.connect() as db:
            db.execute(f"UPDATE signals SET {sql} WHERE id=?", values)

    def get_signal(self, signal_id: int) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM signals WHERE id=?", (signal_id,)).fetchone()
        return dict(row) if row else None

    def get_active_signals(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM signals WHERE status IN ('pending','open') ORDER BY id"
            ).fetchall()
        return [dict(row) for row in rows]

    def count_real_active(self) -> int:
        with self.connect() as db:
            row = db.execute(
                "SELECT COUNT(*) AS n FROM signals WHERE is_real=1 AND status IN ('pending','open')"
            ).fetchone()
        return int(row["n"])

    def close_signal(
        self,
        signal_id: int,
        *,
        close_price: float,
        gross_pnl: float,
        fees: float,
        net_pnl: float,
        result: str,
        mfe_pct: float,
        mae_pct: float,
        entry_real: float | None = None,
        closed_at: int | None = None,
        result_detail: str | None = None,
    ) -> bool:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status FROM signals WHERE id=?", (signal_id,)).fetchone()
            if row is None or str(row["status"]) == "closed":
                db.execute("ROLLBACK")
                return False
            db.execute(
                """
                UPDATE signals SET status='closed',closed_at=?,close_price=?,gross_pnl=?,fees=?,net_pnl=?,
                    result=?,result_detail=?,mfe_pct=?,mae_pct=?,entry_real=COALESCE(?,entry_real)
                WHERE id=?
                """,
                (
                    int(closed_at or time.time()), close_price, gross_pnl, fees, net_pnl,
                    result, result_detail, mfe_pct, mae_pct, entry_real, signal_id,
                ),
            )
            db.execute("COMMIT")
        self.roll_profit_day()
        self.set("profit_today", float(self.get("profit_today", 0.0)) + float(net_pnl))
        self.set("profit_total", float(self.get("profit_total", 0.0)) + float(net_pnl))
        return True

    def roll_profit_day(self) -> None:
        today = self._today()
        if self.get("profit_day", "") != today:
            self.set("profit_day", today)
            self.set("profit_today", 0.0)

    def reset_stats(self) -> None:
        self.set("stats_reset_at", int(time.time()))

    def reset_profit(self) -> None:
        self.set("profit_reset_at", int(time.time()))
        self.set("profit_today", 0.0)
        self.set("profit_total", 0.0)
        self.set("profit_day", self._today())

    def stats(self) -> dict[str, Any]:
        reset_at = int(self.get("stats_reset_at", 0) or 0)
        with self.connect() as db:
            rows = db.execute("SELECT * FROM signals WHERE created_at>=? ORDER BY id", (reset_at,)).fetchall()
        items = [dict(row) for row in rows]
        closed = [row for row in items if row["status"] == "closed"]
        tp = sum(1 for row in closed if row.get("result") == "TP")
        sl = sum(1 for row in closed if row.get("result") == "SL")
        ambiguous = sum(1 for row in closed if row.get("result") == "AMBIGUOUS")
        real = [row for row in items if int(row.get("is_real") or 0)]
        virtual = [row for row in items if not int(row.get("is_real") or 0)]
        return {
            "signals": len(items),
            "open": sum(1 for row in items if row["status"] in ("pending", "open")),
            "pending": sum(1 for row in items if row["status"] == "pending"),
            "tp": tp,
            "sl": sl,
            "ambiguous": ambiguous,
            "real": len(real),
            "virtual": len(virtual),
            "net_pnl": sum(float(row.get("net_pnl") or 0.0) for row in closed),
            "real_net": sum(float(row.get("net_pnl") or 0.0) for row in real if row["status"] == "closed"),
            "virtual_net": sum(float(row.get("net_pnl") or 0.0) for row in virtual if row["status"] == "closed"),
        }

    @staticmethod
    def _json_dict(value: Any, default: dict[str, Any] | None = None) -> dict[str, Any]:
        if isinstance(value, dict):
            return dict(value)
        try:
            parsed = json.loads(str(value or "{}"))
            return dict(parsed) if isinstance(parsed, dict) else dict(default or {})
        except (TypeError, json.JSONDecodeError):
            return dict(default or {})

    def get_learning_pattern(self, pattern_key: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM learning_patterns WHERE pattern_key=?", (pattern_key,)
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["factors"] = self._json_dict(item.pop("factors_json", "{}"))
        item["best_factors"] = self._json_dict(item.pop("best_factors_json", "{}"))
        item["previous_factors"] = self._json_dict(item.pop("previous_factors_json", "{}"))
        item["stats"] = self._json_dict(item.pop("stats_json", "{}"))
        return item

    def save_learning_pattern(self, pattern: dict[str, Any]) -> None:
        now = int(time.time())
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO learning_patterns(
                    pattern_key,symbol_id,side,trigger_window,support_tool,horizon,
                    factors_json,best_factors_json,previous_factors_json,version,status,
                    active_variable,active_reason,source_signal_id,trial_tp,trial_sl,
                    total_tp,total_sl,consecutive_tp,consecutive_sl,failed_trials,stats_json,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(pattern_key) DO UPDATE SET
                    symbol_id=excluded.symbol_id,side=excluded.side,
                    trigger_window=excluded.trigger_window,support_tool=excluded.support_tool,
                    horizon=excluded.horizon,factors_json=excluded.factors_json,
                    best_factors_json=excluded.best_factors_json,
                    previous_factors_json=excluded.previous_factors_json,
                    version=excluded.version,status=excluded.status,
                    active_variable=excluded.active_variable,active_reason=excluded.active_reason,
                    source_signal_id=excluded.source_signal_id,trial_tp=excluded.trial_tp,
                    trial_sl=excluded.trial_sl,total_tp=excluded.total_tp,total_sl=excluded.total_sl,
                    consecutive_tp=excluded.consecutive_tp,consecutive_sl=excluded.consecutive_sl,
                    failed_trials=excluded.failed_trials,stats_json=excluded.stats_json,
                    updated_at=excluded.updated_at
                """,
                (
                    pattern["pattern_key"], pattern["symbol_id"], pattern["side"],
                    int(pattern["trigger_window"]), pattern["support_tool"], int(pattern["horizon"]),
                    json.dumps(pattern.get("factors") or {}, ensure_ascii=False, separators=(",", ":")),
                    json.dumps(pattern.get("best_factors") or {}, ensure_ascii=False, separators=(",", ":")),
                    json.dumps(pattern.get("previous_factors") or {}, ensure_ascii=False, separators=(",", ":")),
                    int(pattern.get("version") or 0), str(pattern.get("status") or "BASE"),
                    pattern.get("active_variable"), pattern.get("active_reason"),
                    pattern.get("source_signal_id"), int(pattern.get("trial_tp") or 0),
                    int(pattern.get("trial_sl") or 0), int(pattern.get("total_tp") or 0),
                    int(pattern.get("total_sl") or 0), int(pattern.get("consecutive_tp") or 0),
                    int(pattern.get("consecutive_sl") or 0), int(pattern.get("failed_trials") or 0),
                    json.dumps(pattern.get("stats") or {}, ensure_ascii=False, separators=(",", ":")), now,
                ),
            )

    def get_learning_adjustments(
        self, symbol_id: str, side: str, trigger_window: int, support_tool: str
    ) -> dict[str, dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT pattern_key,horizon,version,status,factors_json
                FROM learning_patterns
                WHERE symbol_id=? AND side=? AND trigger_window=? AND support_tool=?
                """,
                (symbol_id, side, int(trigger_window), support_tool),
            ).fetchall()
        output: dict[str, dict[str, Any]] = {}
        for row in rows:
            output[str(int(row["horizon"]))] = {
                "pattern_key": str(row["pattern_key"]),
                "version": int(row["version"]),
                "status": str(row["status"]),
                "factors": self._json_dict(row["factors_json"]),
            }
        return output

    def get_symbol_learning_gates(self, symbol_id: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT side,trigger_window,support_tool,horizon,version,status,factors_json
                FROM learning_patterns WHERE symbol_id=?
                """,
                (symbol_id,),
            ).fetchall()
        output: list[dict[str, Any]] = []
        for row in rows:
            output.append({
                "side": str(row["side"]),
                "trigger_window": int(row["trigger_window"]),
                "support_tool": str(row["support_tool"]),
                "horizon": int(row["horizon"]),
                "version": int(row["version"]),
                "status": str(row["status"]),
                "factors": self._json_dict(row["factors_json"]),
            })
        return output

    def add_learning_change(
        self,
        pattern_key: str,
        signal_id: int | None,
        version: int,
        variable: str,
        old_value: float,
        new_value: float,
        reason: str,
        status: str,
        details: dict[str, Any] | None = None,
    ) -> int:
        with self.connect() as db:
            cursor = db.execute(
                """
                INSERT INTO learning_changes(
                    pattern_key,signal_id,version,variable,old_value,new_value,reason,status,details_json,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    pattern_key, signal_id, int(version), variable, float(old_value), float(new_value),
                    reason, status, json.dumps(details or {}, ensure_ascii=False, separators=(",", ":")),
                    int(time.time()),
                ),
            )
            return int(cursor.lastrowid)

    def resolve_learning_changes(self, pattern_key: str, version: int, status: str) -> None:
        with self.connect() as db:
            db.execute(
                """
                UPDATE learning_changes SET status=?,resolved_at=?
                WHERE pattern_key=? AND version=? AND resolved_at IS NULL
                """,
                (status, int(time.time()), pattern_key, int(version)),
            )

    def add_learning_outcome(
        self,
        signal_id: int,
        pattern_key: str,
        version: int,
        result: str,
        net_pnl: float,
        *,
        valid: bool = True,
    ) -> None:
        with self.connect() as db:
            db.execute(
                """
                INSERT OR REPLACE INTO learning_outcomes(
                    signal_id,pattern_key,version,result,net_pnl,valid,created_at
                ) VALUES(?,?,?,?,?,?,?)
                """,
                (
                    int(signal_id), pattern_key, int(version), str(result), float(net_pnl),
                    int(bool(valid)), int(time.time()),
                ),
            )

    def learning_outcome_stats(
        self,
        pattern_key: str,
        *,
        version: int | None = None,
        exclude_version: int | None = None,
        limit: int = 100,
    ) -> dict[str, float | int]:
        clauses = ["pattern_key=?", "valid=1", "result IN ('TP','SL')"]
        values: list[Any] = [pattern_key]
        if version is not None:
            clauses.append("version=?")
            values.append(int(version))
        if exclude_version is not None:
            clauses.append("version<>?")
            values.append(int(exclude_version))
        values.append(max(1, int(limit)))
        with self.connect() as db:
            rows = db.execute(
                f"""
                SELECT result,net_pnl FROM learning_outcomes
                WHERE {' AND '.join(clauses)}
                ORDER BY created_at DESC LIMIT ?
                """,
                values,
            ).fetchall()
        total = len(rows)
        tp = sum(1 for row in rows if str(row["result"]) == "TP")
        sl = sum(1 for row in rows if str(row["result"]) == "SL")
        net = sum(float(row["net_pnl"] or 0.0) for row in rows)
        return {
            "samples": total,
            "tp": tp,
            "sl": sl,
            "win_rate": tp / total if total else 0.0,
            "net_pnl": net,
            "expectancy": net / total if total else 0.0,
        }

    def start_learning_review(self, review: dict[str, Any]) -> None:
        now = int(time.time())
        with self.connect() as db:
            db.execute(
                """
                INSERT OR REPLACE INTO learning_reviews(
                    signal_id,pattern_key,symbol_id,side,entry,tp,sl,tp_pct,sl_pct,
                    started_at,sl_at,expires_at,max_favorable_pct,max_adverse_pct,
                    hit_original_tp,finalized,diagnosis,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    int(review["signal_id"]), review["pattern_key"], review["symbol_id"], review["side"],
                    float(review["entry"]), float(review["tp"]), float(review["sl"]),
                    float(review["tp_pct"]), float(review["sl_pct"]), int(review["started_at"]),
                    int(review["sl_at"]), int(review["expires_at"]),
                    float(review.get("max_favorable_pct") or 0.0),
                    float(review.get("max_adverse_pct") or 0.0),
                    int(bool(review.get("hit_original_tp"))), 0, review.get("diagnosis"), now,
                ),
            )

    def get_open_learning_reviews(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM learning_reviews WHERE finalized=0 ORDER BY signal_id"
            ).fetchall()
        return [dict(row) for row in rows]

    def update_learning_review(self, signal_id: int, **fields: Any) -> None:
        allowed = {
            "max_favorable_pct", "max_adverse_pct", "hit_original_tp",
            "finalized", "diagnosis", "expires_at",
        }
        items = [(key, value) for key, value in fields.items() if key in allowed]
        if not items:
            return
        items.append(("updated_at", int(time.time())))
        sql = ",".join(f"{key}=?" for key, _ in items)
        values = [value for _, value in items] + [int(signal_id)]
        with self.connect() as db:
            db.execute(f"UPDATE learning_reviews SET {sql} WHERE signal_id=?", values)

    def learning_summary(self) -> dict[str, int]:
        with self.connect() as db:
            patterns = db.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN status='TRIAL' THEN 1 ELSE 0 END) AS trial,
                       SUM(CASE WHEN status IN ('ACCEPTED','TRUSTED') THEN 1 ELSE 0 END) AS accepted,
                       SUM(CASE WHEN status='QUARANTINED' THEN 1 ELSE 0 END) AS quarantined,
                       SUM(total_tp) AS tp,SUM(total_sl) AS sl
                FROM learning_patterns
                """
            ).fetchone()
            reviews = db.execute(
                "SELECT COUNT(*) AS n FROM learning_reviews WHERE finalized=0"
            ).fetchone()
        return {
            "patterns": int(patterns["total"] or 0),
            "trial": int(patterns["trial"] or 0),
            "accepted": int(patterns["accepted"] or 0),
            "quarantined": int(patterns["quarantined"] or 0),
            "tp": int(patterns["tp"] or 0),
            "sl": int(patterns["sl"] or 0),
            "reviews": int(reviews["n"] or 0),
        }

    def recent_learning_patterns(self, limit: int = 8) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM learning_patterns ORDER BY updated_at DESC LIMIT ?", (int(limit),)
            ).fetchall()
        output: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["factors"] = self._json_dict(item.pop("factors_json", "{}"))
            item["stats"] = self._json_dict(item.pop("stats_json", "{}"))
            item.pop("best_factors_json", None)
            item.pop("previous_factors_json", None)
            output.append(item)
        return output

    def add_health_event(self, component: str, severity: str, message: str, symbol_id: str | None = None) -> None:
        now = int(time.time())
        with self.connect() as db:
            duplicate = db.execute(
                """
                SELECT 1 FROM health_events
                WHERE component=? AND severity=? AND message=? AND COALESCE(symbol_id,'')=COALESCE(?, '')
                  AND cleared_at IS NULL AND created_at>=? LIMIT 1
                """,
                (component, severity, message, symbol_id, now - 300),
            ).fetchone()
            if duplicate:
                return
            db.execute(
                "INSERT INTO health_events(component,severity,message,symbol_id,created_at) VALUES(?,?,?,?,?)",
                (component, severity, message, symbol_id, now),
            )

    def clear_health(self, component: str, symbol_id: str | None = None) -> None:
        with self.connect() as db:
            if symbol_id is None:
                db.execute(
                    "UPDATE health_events SET cleared_at=? WHERE component=? AND cleared_at IS NULL",
                    (int(time.time()), component),
                )
            else:
                db.execute(
                    "UPDATE health_events SET cleared_at=? WHERE component=? AND symbol_id=? AND cleared_at IS NULL",
                    (int(time.time()), component, symbol_id),
                )

    def active_health_events(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM health_events WHERE cleared_at IS NULL ORDER BY id DESC LIMIT 20"
            ).fetchall()
        return [dict(row) for row in rows]
