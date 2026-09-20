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
    best_price        REAL    NOT NULL DEFAULT 0,
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

-- نمادهایی که شرط کاندید (24h >= آستانه) را پاس کرده‌اند.
-- عضویت در این جدول به معنی معامله نیست؛ فقط زیر نظر گرفتن است.
CREATE TABLE IF NOT EXISTS watchlist (
    symbol            TEXT PRIMARY KEY,
    added_ts          INTEGER,
    last_seen_ts      INTEGER,
    entry_gain_pct    REAL,
    peak_gain_pct     REAL,
    peak_price        REAL,
    last_gain_pct     REAL,
    last_price        REAL,
    setup_count       INTEGER DEFAULT 0,
    trade_count       INTEGER DEFAULT 0,
    last_reject_code  TEXT,
    active            INTEGER DEFAULT 1
);

-- عکس لحظه‌ای کامل شرایط در زمان هر تریگر (ورود یا رد شدن).
-- این جدول ستون فقرات Audit است: بعداً باید بتوانیم دقیقاً بفهمیم چرا
-- یک معامله باز شد یا نشد، بدون حدس زدن.
CREATE TABLE IF NOT EXISTS signal_snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id      INTEGER,
    symbol        TEXT,
    trigger_ts    INTEGER,
    accepted      INTEGER,
    reject_code   TEXT,
    entry_type    TEXT,
    payload       TEXT,
    created_ts    INTEGER
);

-- دورهٔ استراحت هر نماد بعد از خروج.
CREATE TABLE IF NOT EXISTS cooldowns (
    symbol      TEXT PRIMARY KEY,
    until_ts    INTEGER,
    reason      TEXT,
    set_ts      INTEGER
);

CREATE TABLE IF NOT EXISTS health (
    component TEXT PRIMARY KEY,
    status    TEXT,
    detail    TEXT,
    ts        INTEGER
);
"""

DEFAULT_SETTINGS: dict[str, Any] = {
    "real_trading_enabled": False,
    "virtual_trading_enabled": config.DEFAULT_VIRTUAL_TRADING_ENABLED,
    "startup_ready": False,
    "startup_phase": "در حال راه‌اندازی",
    "virtual_balance": config.VIRTUAL_START_CAPITAL_USDT,
    "max_positions": config.MAX_CONCURRENT_POSITIONS,
    "position_size": config.POSITION_SIZE_USDT,
    "live_report_minutes": config.LIVE_REPORT_MINUTES,
    "cooldown_hours": config.COOLDOWN_HOURS,
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
        self._migrate()
        self._ensure_defaults()

    # ------------------------------------------------------------------
    def close(self) -> None:
        with self._lock:
            try:
                self._conn.commit()
            finally:
                self._conn.close()

    # ستون‌هایی که در نسخه‌های بعدی اضافه شده‌اند. ``CREATE TABLE IF NOT EXISTS``
    # به جدولی که از قبل وجود دارد ستون اضافه نمی‌کند، پس دیتابیس‌های قدیمی
    # بدون این مهاجرت با خطای «no such column» می‌خوابند.
    _MIGRATIONS: tuple[tuple[str, str, str], ...] = (
        ("cycles", "entry_score", "REAL NOT NULL DEFAULT 0"),
        ("cycles", "entry_reason", "TEXT"),
        # بهترین قیمت طی عمر پوزیشن — مبنای محاسبهٔ استاپ دنبال‌کننده.
        ("cycles", "best_price", "REAL NOT NULL DEFAULT 0"),
    )

    def _migrate(self) -> None:
        """ستون‌های جاافتاده را به جدول‌های موجود اضافه می‌کند."""
        with self._lock:
            for table, column, ddl in self._MIGRATIONS:
                try:
                    rows = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
                except sqlite3.Error:
                    continue
                if not rows:
                    continue
                existing = {str(r["name"]) for r in rows}
                if column in existing:
                    continue
                try:
                    self._conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"
                    )
                    self._conn.commit()
                    logger.info("DB_MIGRATE | %s.%s اضافه شد", table, column)
                except sqlite3.Error as exc:
                    logger.warning("DB_MIGRATE_FAIL | %s.%s | %s", table, column, exc)

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
                     entry_score: float = 0.0, entry_reason: str = "",
                     best_price: float = 0.0) -> int:
        """یک پوزیشن جدید ثبت می‌کند (تک‌ورودی، نه پله‌ای).

        ``best_price`` نقطهٔ شروع استاپ دنبال‌کننده است — معمولاً همان قیمت ورود.
        """
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO cycles(symbol,side,mode,status,leverage,planned_steps,"
                "capital_at_open,plan_json,take_profit_price,hard_stop_price,best_price,"
                "entry_score,entry_reason,opened_at) "
                "VALUES(?,?,?,'open',?,1,?,?,?,?,?,?,?,?)",
                (symbol, side, mode, int(leverage),
                 float(capital_at_open), json_dumps(plan),
                 float(take_profit_price), float(hard_stop_price), float(best_price),
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

    def update_trailing(self, cycle_id: int, *, best_price: float, active_stop: float) -> None:
        """بهترین قیمت و استاپ فعال (دنبال‌کننده یا اولیه، هرکدام تنگ‌تر) را ذخیره می‌کند."""
        with self._lock:
            self._conn.execute(
                "UPDATE cycles SET best_price=?, hard_stop_price=? WHERE id=?",
                (float(best_price), float(active_stop), cycle_id),
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
            # برد/باخت بر اساس سود/زیان خالص واقعی تعیین می‌شود، نه دلیل خروج —
            # چون دلیل خروج (tp/stop/reversal/manual/...) هیچ‌وقت نباید مجموعش
            # با «کل بسته‌شده» فرق کند؛ اینجا wins+losses همیشه دقیقاً برابر closed است.
            wins = self._conn.execute(
                "SELECT COUNT(*) c FROM cycles WHERE status='closed' AND mode=? "
                "AND net_pnl > 0", (mode,)
            ).fetchone()["c"]
            losses = self._conn.execute(
                "SELECT COUNT(*) c FROM cycles WHERE status='closed' AND mode=? "
                "AND net_pnl <= 0", (mode,)
            ).fetchone()["c"]
            # فقط برای اطلاع — کدام‌ها دقیقاً با استاپ دنبال‌کننده (سود قفل‌شده)
            # یا حد ضرر بسته شدند (ممکن است بعضی وین‌ها با «timeout» یا بستن
            # دستی هم بسته شده باشند).
            trail_count = self._conn.execute(
                "SELECT COUNT(*) c FROM cycles WHERE status='closed' AND mode=? "
                "AND exit_reason='trail'", (mode,)
            ).fetchone()["c"]
            stop_count = self._conn.execute(
                "SELECT COUNT(*) c FROM cycles WHERE status='closed' AND mode=? "
                "AND exit_reason IN ('stop','liquidation')", (mode,)
            ).fetchone()["c"]
        return {
            "open": int(open_count),
            "closed": int(closed["c"]),
            "wins": int(wins),
            "losses": int(losses),
            "trail": int(trail_count),
            "stop": int(stop_count),
            "pnl_total": safe_float(closed["pnl"]),
            "pnl_today": safe_float(today["pnl"]),
        }

    def closed_since(self, since_ms: int, mode: str | None = None) -> list[dict[str, Any]]:
        """پوزیشن‌های بسته‌شده بعد از یک زمان مشخص — برای خلاصهٔ دوره‌ای."""
        query = "SELECT * FROM cycles WHERE status='closed' AND closed_at>=?"
        params: list[Any] = [int(since_ms)]
        if mode:
            query += " AND mode=?"
            params.append(mode)
        query += " ORDER BY closed_at"
        with self._lock:
            rows = self._conn.execute(query, tuple(params)).fetchall()
        return [dict(r) for r in rows]

    def reset_statistics(self, mode: str | None = None) -> int:
        """پاک کردن تاریخچهٔ معاملات؛ برای شروع تمیز بعد از تغییر استراتژی."""
        query = "DELETE FROM cycles WHERE status='closed'"
        params: tuple[Any, ...] = ()
        if mode:
            query += " AND mode=?"
            params = (mode,)
        with self._lock:
            cur = self._conn.execute(query, params)
            removed = cur.rowcount or 0
            self._conn.execute(
                "DELETE FROM cycle_steps WHERE cycle_id NOT IN (SELECT id FROM cycles)"
            )
            self._conn.commit()
        return int(removed)

    def recent_cycles(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM cycles ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ==================================================================
    #  V3 — Watchlist
    # ==================================================================
    # عضویت در Watchlist به معنی معامله نیست. این جدول فقط نمادهایی را
    # نگه می‌دارد که شرط کاندید (24h >= آستانه) را پاس کرده‌اند تا با
    # فرکانس بالاتر مانیتور شوند.

    def watchlist_upsert(
        self,
        *,
        symbol: str,
        gain_pct: float,
        price: float,
        now_ts: int,
    ) -> dict[str, Any]:
        """افزودن نماد به Watchlist یا به‌روزرسانی آن.

        ``peak_gain_pct`` و ``peak_price`` بالاترین مقدار مشاهده‌شده از زمان
        ورود به Watchlist هستند و هرگز عقب نمی‌روند — برای محاسبهٔ «چقدر از
        سقف ریخته» در تحلیل بعدی لازم‌اند.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM watchlist WHERE symbol=?", (symbol,)
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO watchlist (symbol, added_ts, last_seen_ts, entry_gain_pct,"
                    " peak_gain_pct, peak_price, last_gain_pct, last_price, active)"
                    " VALUES (?,?,?,?,?,?,?,?,1)",
                    (symbol, now_ts, now_ts, gain_pct, gain_pct, price, gain_pct, price),
                )
            else:
                self._conn.execute(
                    "UPDATE watchlist SET last_seen_ts=?, last_gain_pct=?, last_price=?,"
                    " peak_gain_pct=MAX(peak_gain_pct, ?), peak_price=MAX(peak_price, ?),"
                    " active=1 WHERE symbol=?",
                    (now_ts, gain_pct, price, gain_pct, price, symbol),
                )
            self._conn.commit()
            out = self._conn.execute(
                "SELECT * FROM watchlist WHERE symbol=?", (symbol,)
            ).fetchone()
        return dict(out) if out else {}

    def watchlist_active(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM watchlist WHERE active=1 ORDER BY peak_gain_pct DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def watchlist_bump_peak(self, symbol: str, price: float) -> None:
        """آپدیت سریع سقف قیمت، بدون نیاز به gain% -- برای چرخهٔ سریع
        Peak-Pullback که هر چند ثانیه اجرا می‌شود و کندل/درصد نمی‌خواهد."""
        with self._lock:
            self._conn.execute(
                "UPDATE watchlist SET peak_price=MAX(peak_price, ?) WHERE symbol=?",
                (price, symbol),
            )
            self._conn.commit()

    def watchlist_get(self, symbol: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM watchlist WHERE symbol=?", (symbol,)
            ).fetchone()
        return dict(row) if row else None

    def watchlist_expire(self, older_than_ts: int) -> int:
        """خروج نمادهایی که مدت نگه‌داری‌شان تمام شده.

        حذف فوری وقتی نماد زیر آستانه می‌آید انجام *نمی‌شود* — چون ممکن است
        دقیقاً همان لحظه وارد فاز برگشت شده باشد. فقط بر اساس گذر زمان.
        """
        with self._lock:
            cur = self._conn.execute(
                "UPDATE watchlist SET active=0 WHERE active=1 AND last_seen_ts < ?",
                (older_than_ts,),
            )
            self._conn.commit()
        return int(cur.rowcount or 0)

    def watchlist_touch_counter(self, symbol: str, field: str) -> None:
        """افزایش شمارندهٔ Funnel: ``setup_count`` یا ``trade_count``."""
        if field not in {"setup_count", "trade_count"}:
            return
        with self._lock:
            self._conn.execute(
                f"UPDATE watchlist SET {field} = COALESCE({field},0) + 1 WHERE symbol=?",
                (symbol,),
            )
            self._conn.commit()

    def watchlist_set_reject(self, symbol: str, reject_code: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE watchlist SET last_reject_code=? WHERE symbol=?",
                (reject_code, symbol),
            )
            self._conn.commit()

    def watchlist_funnel(self) -> dict[str, Any]:
        """آمار قیف: از چند کاندید، چند Setup و چند معامله بیرون آمد."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS total,"
                " SUM(CASE WHEN setup_count>0 THEN 1 ELSE 0 END) AS with_setup,"
                " SUM(CASE WHEN trade_count>0 THEN 1 ELSE 0 END) AS with_trade,"
                " SUM(COALESCE(setup_count,0)) AS setups,"
                " SUM(COALESCE(trade_count,0)) AS trades"
                " FROM watchlist"
            ).fetchone()
        data = dict(row) if row else {}
        return {
            "total_candidates": int(data.get("total") or 0),
            "candidates_with_setup": int(data.get("with_setup") or 0),
            "candidates_with_trade": int(data.get("with_trade") or 0),
            "total_setups": int(data.get("setups") or 0),
            "total_trades": int(data.get("trades") or 0),
        }

    # ==================================================================
    #  V3 — Signal Snapshots (ستون فقرات Audit)
    # ==================================================================

    def save_snapshot(
        self,
        *,
        symbol: str,
        trigger_ts: int,
        accepted: bool,
        payload: Any,
        cycle_id: int | None = None,
        reject_code: str = "",
        entry_type: str = "",
    ) -> int:
        """ثبت عکس لحظه‌ای شرایط — هم برای ورودها و هم برای ردها.

        ردها هم ذخیره می‌شوند چون برای تحلیل قیف (چرا معامله نشد) لازم‌اند.
        """
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO signal_snapshots (cycle_id, symbol, trigger_ts, accepted,"
                " reject_code, entry_type, payload, created_ts) VALUES (?,?,?,?,?,?,?,?)",
                (
                    cycle_id,
                    symbol,
                    int(trigger_ts),
                    1 if accepted else 0,
                    reject_code,
                    entry_type,
                    json_dumps(payload),
                    now_ms(),
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid or 0)

    def snapshot_for_cycle(self, cycle_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM signal_snapshots WHERE cycle_id=? AND accepted=1"
                " ORDER BY id DESC LIMIT 1",
                (cycle_id,),
            ).fetchone()
        if not row:
            return None
        out = dict(row)
        out["payload"] = json_loads(out.get("payload"), {})
        return out

    def reject_breakdown(self, since_ms_ts: int = 0) -> list[dict[str, Any]]:
        """شمارش دلایل رد شدن — برای فهمیدن گلوگاه قیف."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT reject_code, COUNT(*) AS n FROM signal_snapshots"
                " WHERE accepted=0 AND trigger_ts >= ? AND reject_code <> ''"
                " GROUP BY reject_code ORDER BY n DESC",
                (int(since_ms_ts),),
            ).fetchall()
        return [dict(r) for r in rows]

    # ==================================================================
    #  V3 — Cooldown
    # ==================================================================

    def set_cooldown(self, symbol: str, *, hours: float, reason: str = "") -> int:
        """شروع دورهٔ استراحت برای یک نماد بعد از خروج.

        در این مدت هیچ معامله‌ای روی این نماد باز نمی‌شود — حتی اگر دوباره
        پامپ کند یا سیگنال جدید بدهد. هدف: جلوگیری از چند معامله روی یک
        Pump Episode واحد.
        """
        until = now_ms() + int(max(0.0, hours) * 3600 * 1000)
        with self._lock:
            self._conn.execute(
                "INSERT INTO cooldowns (symbol, until_ts, reason, set_ts) VALUES (?,?,?,?)"
                " ON CONFLICT(symbol) DO UPDATE SET until_ts=excluded.until_ts,"
                " reason=excluded.reason, set_ts=excluded.set_ts",
                (symbol, until, reason, now_ms()),
            )
            self._conn.commit()
        return until

    def in_cooldown(self, symbol: str) -> tuple[bool, int]:
        """آیا نماد در استراحت است؟ خروجی: (بله/نه، زمان پایان).

        نکته: رکورد منقضی‌شده پاک نمی‌شود تا تاریخچهٔ استراحت‌ها برای
        تحلیل بعدی باقی بماند.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT until_ts FROM cooldowns WHERE symbol=?", (symbol,)
            ).fetchone()
        if not row:
            return False, 0
        until = int(row["until_ts"] or 0)
        return (now_ms() < until), until

    def active_cooldowns(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM cooldowns WHERE until_ts > ? ORDER BY until_ts",
                (now_ms(),),
            ).fetchall()
        return [dict(r) for r in rows]

    # ==================================================================
    #  V3 — گزارش تحقیقاتی کامل
    # ==================================================================

    def research_report(self, mode: str = "virtual") -> dict[str, Any]:
        """همهٔ آماری که برای تحلیل بعد از Paper Test لازم است.

        این همان چیزی است که باید بعد از چند روز جمع‌آوری، بررسی شود:
        آیا پدیده در زمان واقعی قابل شکار بود یا نه.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM cycles WHERE status='closed' AND mode=? ORDER BY closed_at",
                (mode,),
            ).fetchall()
        trades = [dict(r) for r in rows]

        # تفکیک بر اساس نوع ورود — سؤال کلیدی: آیا سود فقط از یک مسیر می‌آید؟
        by_type: dict[str, dict[str, Any]] = {}
        by_exit: dict[str, int] = {}
        for t in trades:
            reason = str(t.get("entry_reason") or "")
            etype = "FAST_CASCADE" if "FAST_CASCADE" in reason else (
                "NORMAL_REVERSAL" if "NORMAL_REVERSAL" in reason else "UNKNOWN"
            )
            bucket = by_type.setdefault(etype, {"n": 0, "wins": 0, "net": 0.0,
                                                "gross": 0.0, "fees": 0.0})
            net = safe_float(t.get("net_pnl"))
            bucket["n"] += 1
            bucket["net"] += net
            bucket["gross"] += safe_float(t.get("gross_pnl"))
            bucket["fees"] += safe_float(t.get("fees"))
            if net > 0:
                bucket["wins"] += 1

            exit_reason = str(t.get("exit_reason") or "?")
            by_exit[exit_reason] = by_exit.get(exit_reason, 0) + 1

        # تمرکز: آیا چند نماد کل نتیجه را می‌سازند؟
        by_symbol: dict[str, dict[str, Any]] = {}
        for t in trades:
            sym = str(t.get("symbol"))
            b = by_symbol.setdefault(sym, {"n": 0, "net": 0.0})
            b["n"] += 1
            b["net"] += safe_float(t.get("net_pnl"))

        nets = [safe_float(t.get("net_pnl")) for t in trades]
        wins = [x for x in nets if x > 0]
        losses = [x for x in nets if x <= 0]
        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))

        # حداکثر افت سرمایه (روی توالی واقعی معاملات)
        equity = 0.0
        peak = 0.0
        max_dd = 0.0
        for x in nets:
            equity += x
            peak = max(peak, equity)
            max_dd = max(max_dd, peak - equity)

        durations = [
            (safe_int(t.get("closed_at")) - safe_int(t.get("opened_at"))) / 60000.0
            for t in trades
            if safe_int(t.get("closed_at")) and safe_int(t.get("opened_at"))
        ]

        return {
            "mode": mode,
            "funnel": self.watchlist_funnel(),
            "reject_breakdown": self.reject_breakdown(),
            "trades": {
                "total": len(trades),
                "wins": len(wins),
                "losses": len(losses),
                "win_rate": (len(wins) / len(trades) * 100.0) if trades else 0.0,
                "gross_profit": gross_profit,
                "gross_loss": gross_loss,
                "net_pnl": sum(nets),
                "total_fees": sum(safe_float(t.get("fees")) for t in trades),
                "profit_factor": (gross_profit / gross_loss) if gross_loss > 0 else None,
                "expectancy": (sum(nets) / len(nets)) if nets else 0.0,
                "avg_win": (sum(wins) / len(wins)) if wins else 0.0,
                "avg_loss": (sum(losses) / len(losses)) if losses else 0.0,
                "largest_win": max(nets) if nets else 0.0,
                "largest_loss": min(nets) if nets else 0.0,
                "max_drawdown": max_dd,
                "avg_duration_min": (sum(durations) / len(durations)) if durations else 0.0,
            },
            "by_entry_type": by_type,
            "by_exit_reason": by_exit,
            "by_symbol": by_symbol,
        }

    def export_trades_csv(self, path: str, mode: str = "virtual") -> int:
        """خروجی CSV کامل معاملات همراه با عکس لحظه‌ای سیگنال هر ورود.

        این فایل همان چیزی است که برای تحلیل بیرونی (مثل کاری که روی
        دادهٔ Binance کردیم) لازم می‌شود.
        """
        import csv as _csv

        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM cycles WHERE status='closed' AND mode=? ORDER BY opened_at",
                (mode,),
            ).fetchall()
        trades = [dict(r) for r in rows]
        if not trades:
            return 0

        out_rows = []
        for t in trades:
            snap = self.snapshot_for_cycle(safe_int(t.get("id"))) or {}
            payload = snap.get("payload") or {}
            entry = safe_float(t.get("avg_entry_price"))
            best = safe_float(t.get("best_price")) or entry
            out_rows.append({
                "cycle_id": t.get("id"),
                "symbol": t.get("symbol"),
                "entry_type": snap.get("entry_type") or "",
                "opened_at": t.get("opened_at"),
                "closed_at": t.get("closed_at"),
                "duration_min": round(
                    (safe_int(t.get("closed_at")) - safe_int(t.get("opened_at"))) / 60000.0, 2
                ),
                "entry_price": entry,
                "exit_price": t.get("exit_price"),
                "hard_stop": t.get("hard_stop_price"),
                "exit_reason": t.get("exit_reason"),
                "leverage": t.get("leverage"),
                "quantity": t.get("total_quantity"),
                "notional": t.get("total_notional"),
                "margin": t.get("total_margin"),
                "gross_pnl": t.get("gross_pnl"),
                "fees": t.get("fees"),
                "net_pnl": t.get("net_pnl"),
                "mfe_pct": round(((entry - best) / entry * 100.0) if entry > 0 else 0.0, 4),
                "change_24h_at_entry": payload.get("change_24h"),
                "upper_wick_ratio": payload.get("upper_wick_ratio"),
                "ret_recent": payload.get("ret_recent"),
                "ret_prior": payload.get("ret_prior"),
                "rate_current": payload.get("rate_current"),
                "rate_previous": payload.get("rate_previous"),
                "range_partial": payload.get("range_partial"),
                "range_baseline": payload.get("range_baseline"),
                "volume_partial": payload.get("volume_partial"),
                "volume_baseline": payload.get("volume_baseline"),
                "swing_low": payload.get("swing_low"),
                "swing_high": payload.get("swing_high"),
                "atr14": payload.get("atr14"),
                "stop_distance_pct": payload.get("stop_distance_pct"),
                "entry_reason": t.get("entry_reason"),
            })

        with open(path, "w", newline="", encoding="utf-8") as fh:
            writer = _csv.DictWriter(fh, fieldnames=list(out_rows[0].keys()))
            writer.writeheader()
            writer.writerows(out_rows)
        return len(out_rows)
