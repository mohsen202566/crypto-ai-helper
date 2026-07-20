"""تست آفلاین؛ هیچ درخواست شبکه و هیچ سفارش واقعی ارسال نمی‌کند.

اجرا:
    python -m unittest -v self_test.py
"""
from __future__ import annotations

import importlib
import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any

import config
from bot import BotEngine
from storage import Storage
from telegram_bot import CommandRouter, TelegramBot, result_message, signal_message, stats_panel, trade_panel
from toobit_client import RateLimiter
from utils import json_dumps, now_ms


class FakeToobit:
    has_credentials = True

    def __init__(self):
        self.rate = RateLimiter()
        self.orders: list[dict[str, Any]] = []
        self.stops: list[dict[str, Any]] = []
        self.closed: list[tuple[str, str]] = []
        self.positions: list[dict[str, Any]] = []

    def get_contracts(self):
        return {
            "DOGE-SWAP-USDT": {
                "canonical": "DOGEUSDT", "exchange_symbol": "DOGE-SWAP-USDT",
                "status": "TRADING", "marginToken": "USDT", "tickSize": "0.00001",
                "stepSize": "1", "minQty": "1", "minNotional": "5",
            }
        }

    def get_positions(self, symbol=None):
        return list(self.positions)

    def get_usdt_balance_summary(self):
        return {"balance": 100, "available": 90, "position_margin": 10, "order_margin": 0, "unrealized_pnl": 0}

    @staticmethod
    def position_qty(item):
        return abs(float(item.get("size", 0)))

    @staticmethod
    def position_side(item):
        return item.get("side", "SHORT")

    @staticmethod
    def item_symbol(item):
        return item.get("symbol", "")

    def place_market_order(self, **kwargs):
        self.orders.append(kwargs)
        return {"order_id": "123", "submitted": True}

    def set_trading_stop(self, symbol, side, tp_price, sl_price, size=None):
        self.stops.append({"symbol": symbol, "side": side, "tp": tp_price, "sl": sl_price})
        return {"ok": True}

    def flash_close(self, symbol, side):
        self.closed.append((symbol, side))
        return {"ok": True}

    def get_all_prices(self):
        return {"DOGEUSDT": 0.077}

    def get_24h_tickers(self):
        return [{
            "symbol": "DOGE-SWAP-USDT", "lastPrice": "0.10", "openPrice": "0.075",
            "priceChangePercent": "33.3", "highPrice": "0.11", "lowPrice": "0.07",
            "volume": "10000000", "quoteVolume": "1000000",
        }]

    def get_all_book_tickers(self):
        return {"DOGEUSDT": {"bid": 0.0999, "ask": 0.1001}}

    def find_realized_result(self, **kwargs):
        return None


class FakeResponse:
    def __init__(self, data: dict[str, Any], status_code: int = 200):
        self._data = data
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self.text = str(data)

    def json(self):
        return self._data


class FakePollSession:
    def __init__(self, updates: list[dict[str, Any]]):
        self.updates = updates
        self.get_updates_calls = 0
        self.closed = False

    def post(self, url, json=None, timeout=None):
        return FakeResponse({"ok": True, "result": True})

    def get(self, url, params=None, timeout=None):
        if url.endswith("/getMe"):
            return FakeResponse({"ok": True, "result": {"username": "OfflineTestBot"}})
        if url.endswith("/getUpdates"):
            self.get_updates_calls += 1
            if self.get_updates_calls == 1:
                return FakeResponse({"ok": True, "result": list(self.updates)})
            return FakeResponse({"ok": True, "result": []})
        raise AssertionError(url)

    def close(self):
        self.closed = True


class FakeSendSession:
    def __init__(self, on_send, *, success: bool = True):
        self.on_send = on_send
        self.success = success
        self.payloads: list[dict[str, Any]] = []
        self.closed = False

    def post(self, url, json=None, timeout=None):
        self.payloads.append(dict(json or {}))
        self.on_send()
        if self.success:
            return FakeResponse({"ok": True, "result": {"message_id": 777}})
        return FakeResponse({"ok": False, "description": "temporary send failure"}, 500)

    def close(self):
        self.closed = True


class OfflineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = config.RUNTIME_DB
        self.old_token = config.TELEGRAM_BOT_TOKEN
        self.old_chat_id = config.TELEGRAM_CHAT_ID
        config.RUNTIME_DB = Path(self.tmp.name) / "runtime.db"
        self.storage = Storage(config.RUNTIME_DB)
        self.fake = FakeToobit()
        self.engine = BotEngine(self.storage, self.fake)  # type: ignore[arg-type]

    def tearDown(self):
        self.storage.close()
        config.RUNTIME_DB = self.old_db
        config.TELEGRAM_BOT_TOKEN = self.old_token
        config.TELEGRAM_CHAT_ID = self.old_chat_id
        self.tmp.cleanup()

    @staticmethod
    def signal() -> dict[str, Any]:
        return {
            "canonical": "DOGEUSDT", "exchange_symbol": "DOGE-SWAP-USDT", "side": "SHORT",
            "status": "ACTIVE", "created_at": now_ms(), "entry": 0.10, "sl": 0.106, "tp": 0.078,
            "initial_sl": 0.106, "trailing_stop": None, "best_price": 0.10, "atr": 0.002,
            "margin_usdt": 5.0, "leverage": 10, "notional_usdt": 50.0,
            "expected_net_profit": 2.0, "signal_score": 82.0, "confirmations": 6,
            "reasons": ["ساختار نزولی اولیه", "فروش تهاجمی غالب"], "metrics": {},
            "contract_info": {"stepSize": "1", "tickSize": "0.00001", "minQty": "1", "minNotional": "5"},
        }

    def test_defaults_force_real_off(self):
        self.assertFalse(self.storage.get_setting("real_trade_enabled"))
        self.assertEqual(self.storage.get_setting("trade_margin_usdt"), config.DEFAULT_TRADE_MARGIN_USDT)
        self.assertTrue(self.storage.integrity_check())

    def test_command_ranges_and_panel(self):
        router = CommandRouter(self.storage, self.fake)  # type: ignore[arg-type]
        self.storage.save_account_snapshot(True, {
            "wallet_balance": 100.0,
            "equity": 101.0,
            "available": 90.0,
            "position_margin": 7.0,
            "order_margin": 3.0,
            "used_margin": 10.0,
            "unrealized_pnl": 1.0,
            "open_positions": 0,
            "open_position_keys": [],
        })
        self.assertIn("7 USDT", router.handle("ترید دلار ۷"))
        self.assertEqual(self.storage.get_setting("trade_margin_usdt"), 7.0)
        self.assertIn("9 x", router.handle("ترید لوریج ۹"))
        self.assertEqual(self.storage.get_setting("leverage"), 9)
        self.assertIn("حداکثر پوزیشن واقعی", router.handle("تعداد اسلات ۴"))
        self.assertEqual(self.storage.get_setting("max_open_positions"), 4)
        self.assertIn("خارج از بازه", router.handle("حداکثر پوزیشن ۲۰۱"))
        panel = router.handle("ترید")
        for label in (
            "موجودی کیف پول Toobit", "اکویتی حساب", "مارجین آزاد", "مارجین پوزیشن‌ها",
            "مارجین سفارش‌ها", "مارجین استفاده‌شده", "سود/ضرر شناور", "دلار هر پوزیشن",
            "لوریج", "Isolated اجباری", "اسلات پُر", "اسلات خالی", "پوزیشن باز Toobit",
            "پوزیشن تأییدشده ربات", "Pending Open ربات", "پوزیشن دستی/خارج از ربات",
        ):
            self.assertIn(label, panel)

    def test_whole_symbol_lock(self):
        first = self.storage.create_virtual_signal(self.signal())
        self.assertIsNotNone(first)
        second = self.storage.create_virtual_signal(self.signal())
        self.assertIsNone(second)
        self.storage.finalize_signal(int(first), "STOP", 0.106, -3)
        self.assertIsNotNone(self.storage.create_virtual_signal(self.signal()))

    def test_real_slot_atomic(self):
        self.storage.set_setting("max_open_positions", 1)
        first = self.storage.create_real_signal_and_reserve(self.signal())
        self.assertIsNotNone(first)
        other = dict(self.signal(), canonical="PEPEUSDT", exchange_symbol="PEPE-SWAP-USDT")
        self.assertIsNone(self.storage.create_real_signal_and_reserve(other))
        self.assertEqual(self.storage.slot_counts()["used"], 1)

    def test_trade_off_routes_virtual(self):
        sid = self.engine.route_signal(self.signal())
        saved = self.storage.get_signal(int(sid))
        self.assertEqual(saved["mode"], "VIRTUAL")
        self.assertEqual(saved["virtual_reason"], "TRADING_OFF")

    def test_real_route_and_submit(self):
        self.storage.save_account_snapshot(True, {"open_positions": 0, "open_position_keys": []})
        self.storage.set_setting("real_trade_enabled", True)
        sid = self.engine.route_signal(self.signal())
        saved = self.storage.get_signal(int(sid))
        self.assertEqual(saved["mode"], "REAL")
        self.engine.process_trade_one(timeout=0.1)
        self.assertEqual(len(self.fake.orders), 1)
        self.assertGreater(int(self.storage.get_signal(int(sid))["confirm_after"]), now_ms())

    def test_late_trade_off_converts_to_virtual(self):
        self.storage.save_account_snapshot(True, {"open_positions": 0, "open_position_keys": []})
        self.storage.set_setting("real_trade_enabled", True)
        sid = self.engine.route_signal(self.signal())
        self.storage.set_setting("real_trade_enabled", False)
        self.engine.process_trade_one(timeout=0.1)
        saved = self.storage.get_signal(int(sid))
        self.assertEqual(saved["mode"], "VIRTUAL")
        self.assertEqual(saved["status"], "ACTIVE")
        self.assertEqual(self.storage.slot_counts()["used"], 0)

    def test_virtual_tp_and_stats(self):
        sid = self.storage.create_virtual_signal(self.signal())
        self.engine.monitor_prices()
        saved = self.storage.get_signal(int(sid))
        self.assertEqual(saved["result"], "TP")
        self.assertGreater(saved["net_pnl"], 0)
        self.assertEqual(self.storage.stats()["VIRTUAL"]["wins"], 1)

    def test_full_signal_result_and_stats_messages(self):
        sid = self.storage.create_virtual_signal(self.signal())
        saved = self.storage.get_signal(int(sid))
        self.assertIn("سیگنال VIRTUAL", signal_message(saved))
        final = self.storage.finalize_signal(int(sid), "TP", 0.078, 10.0)
        self.assertIn("سود/ضرر خالص", result_message(final))
        panel = stats_panel(self.storage)
        self.assertIn("TP: 1", panel)
        self.assertIn("امروز:", panel)

    def test_scanner_cycle_updates_heartbeat_and_counts(self):
        old_warmup = config.NEW_CONTRACT_WARMUP_MINUTES
        old_deep = config.DEEP_CANDIDATE_SIZE
        try:
            config.NEW_CONTRACT_WARMUP_MINUTES = 0
            config.DEEP_CANDIDATE_SIZE = 0
            self.engine.startup()
            with self.assertLogs("toobit_pump_bot", level="INFO") as logs:
                emitted = self.engine.scan_once()
            self.assertEqual(emitted, 0)
            self.assertGreater(int(self.storage.get_setting("last_scan_finished_ms", 0)), 0)
            self.assertEqual(self.storage.get_setting("last_scan_ticker_count"), 1)
            self.assertEqual(self.storage.get_setting("last_scan_ranked_count"), 1)
            joined = "\n".join(logs.output)
            self.assertIn("SCAN_START", joined)
            self.assertIn("SCAN_DONE", joined)
        finally:
            config.NEW_CONTRACT_WARMUP_MINUTES = old_warmup
            config.DEEP_CANDIDATE_SIZE = old_deep


    def test_negative_24h_never_enters_pump_watchlist_even_with_bad_env_threshold(self):
        old_warmup = config.NEW_CONTRACT_WARMUP_MINUTES
        old_threshold = config.MIN_PUMP_24H_PERCENT
        try:
            config.NEW_CONTRACT_WARMUP_MINUTES = 0
            # شبیه‌سازی تنظیم قدیمی/اشتباه منفی روی VPS.
            config.MIN_PUMP_24H_PERCENT = -18.0
            self.fake.get_24h_tickers = lambda: [{
                "symbol": "DOGE-SWAP-USDT", "lastPrice": "0.05", "openPrice": "0.10",
                "priceChangePercent": "-50", "highPrice": "0.11", "lowPrice": "0.04",
                "volume": "10000000", "quoteVolume": "1000000",
            }]
            self.fake.get_all_book_tickers = lambda: {"DOGEUSDT": {"bid": 0.0499, "ask": 0.0501}}
            self.engine.startup()
            emitted = self.engine.scan_once()
            self.assertEqual(emitted, 0)
            self.assertEqual(self.storage.get_setting("last_scan_ranked_count"), 0)
            self.assertEqual(self.storage.get_setting("watchlist"), [])
            self.assertEqual(self.storage.get_setting("deep_candidates"), [])
            self.assertGreaterEqual(int(self.storage.get_setting("last_scan_rejected_non_positive", 0)), 1)
        finally:
            config.NEW_CONTRACT_WARMUP_MINUTES = old_warmup
            config.MIN_PUMP_24H_PERCENT = old_threshold

    def test_rate_limiter_snapshot(self):
        limiter = RateLimiter()
        limiter.acquire(40, "market")
        limiter.acquire(1, "trade")
        snap = limiter.snapshot()
        self.assertEqual(snap["total_60s"], 41)
        self.assertEqual(snap["market_60s"], 40)


    def test_command_normalization_and_all_trade_mutations(self):
        router = CommandRouter(self.storage, self.fake)  # type: ignore[arg-type]
        self.assertIn("7.5 USDT", router.handle("تريد دلار۷٫۵"))
        self.assertEqual(self.storage.get_setting("trade_margin_usdt"), 7.5)
        self.assertIn("12 x", router.handle("لوريج‌تريد:۱۲"))
        self.assertEqual(self.storage.get_setting("leverage"), 12)
        self.assertIn("حداکثر پوزیشن واقعی", router.handle("حداکثر‌پوزیشن=۵"))
        self.assertEqual(self.storage.get_setting("max_open_positions"), 5)
        self.assertIn("پنل ترید", router.handle("/trade@OfflineTestBot"))
        self.assertIn("آمار سیگنال‌ها", router.handle("/stats"))

    def test_env_file_parser_supports_export_and_systemd_lines(self):
        key1, key2 = "TOOBIT_V3_TEST_ONE", "TOOBIT_V3_TEST_TWO"
        os.environ.pop(key1, None)
        os.environ.pop(key2, None)
        path = Path(self.tmp.name) / "test.env"
        path.write_text(
            f'export {key1}="alpha value" # comment\nEnvironment={key2}=beta\n',
            encoding="utf-8",
        )
        config._load_env_file(path)  # type: ignore[attr-defined]
        self.assertEqual(os.environ.get(key1), "alpha value")
        self.assertEqual(os.environ.get(key2), "beta")
        os.environ.pop(key1, None)
        os.environ.pop(key2, None)

    def test_telegram_poll_autobinds_private_owner_and_replies(self):
        config.TELEGRAM_BOT_TOKEN = "123:test-token"
        config.TELEGRAM_CHAT_ID = ""
        update = {
            "update_id": 100,
            "message": {
                "message_id": 9,
                "text": "ترید",
                "chat": {"id": 555, "type": "private"},
                "from": {"id": 555, "username": "mohsen", "is_bot": False},
            },
        }
        bot = TelegramBot(self.storage, self.engine, self.fake)  # type: ignore[arg-type]
        bot.poll_session = FakePollSession([update])  # type: ignore[assignment]
        bot.send_session = FakeSendSession(bot.stop_event.set, success=True)  # type: ignore[assignment]
        thread = threading.Thread(target=bot.poll_loop)
        thread.start()
        thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.storage.telegram_offset(), 100)
        self.assertEqual(self.storage.get_setting("telegram_chat_id"), "555")
        self.assertEqual(bot.send_session.payloads[0]["chat_id"], "555")  # type: ignore[attr-defined]
        self.assertIn("پنل ترید", bot.send_session.payloads[0]["text"])  # type: ignore[attr-defined]
        bot.stop()

    def test_telegram_does_not_consume_command_when_reply_fails(self):
        config.TELEGRAM_BOT_TOKEN = "123:test-token"
        config.TELEGRAM_CHAT_ID = "555"
        update = {
            "update_id": 101,
            "message": {
                "message_id": 10,
                "text": "آمار",
                "chat": {"id": 555, "type": "private"},
                "from": {"id": 555, "username": "mohsen", "is_bot": False},
            },
        }
        bot = TelegramBot(self.storage, self.engine, self.fake)  # type: ignore[arg-type]
        bot.poll_session = FakePollSession([update])  # type: ignore[assignment]
        bot.send_session = FakeSendSession(bot.stop_event.set, success=False)  # type: ignore[assignment]
        thread = threading.Thread(target=bot.poll_loop)
        thread.start()
        thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.storage.telegram_offset(), 0)
        bot.stop()

    def test_telegram_username_owner_is_converted_to_numeric_chat(self):
        config.TELEGRAM_BOT_TOKEN = "123:test-token"
        config.TELEGRAM_CHAT_ID = "@mohsen"
        update = {
            "update_id": 102,
            "message": {
                "message_id": 11,
                "text": "سلامت",
                "chat": {"id": 777, "type": "private"},
                "from": {"id": 777, "username": "Mohsen", "is_bot": False},
            },
        }
        bot = TelegramBot(self.storage, self.engine, self.fake)  # type: ignore[arg-type]
        bot.poll_session = FakePollSession([update])  # type: ignore[assignment]
        bot.send_session = FakeSendSession(bot.stop_event.set, success=True)  # type: ignore[assignment]
        thread = threading.Thread(target=bot.poll_loop)
        thread.start()
        thread.join(timeout=3)
        self.assertEqual(self.storage.get_setting("telegram_chat_id"), "777")
        self.assertEqual(self.storage.telegram_offset(), 102)
        bot.stop()

    def test_all_root_modules_import_cleanly(self):
        for module_name in (
            "config", "utils", "storage", "toobit_client", "strategy",
            "bot", "telegram_bot", "main",
        ):
            module = importlib.import_module(module_name)
            self.assertIsNotNone(module)

    def test_legacy_database_migrates_and_accepts_new_signals(self):
        legacy_path = Path(self.tmp.name) / "legacy_runtime.db"
        conn = sqlite3.connect(legacy_path)
        conn.executescript(
            """
            CREATE TABLE settings(key TEXT PRIMARY KEY,value_json TEXT NOT NULL,updated_at INTEGER NOT NULL);
            CREATE TABLE signals(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                canonical TEXT NOT NULL,
                exchange_symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                tier TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                telegram_message_id INTEGER,
                order_id TEXT,
                payload_json TEXT NOT NULL
            );
            CREATE INDEX idx_signals_active ON signals(status,tier,canonical);
            CREATE TABLE symbol_locks(
                canonical TEXT PRIMARY KEY,
                signal_id INTEGER NOT NULL,
                tier TEXT NOT NULL,
                side TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE positions(
                signal_id INTEGER PRIMARY KEY,
                canonical TEXT NOT NULL,
                toobit_symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                status TEXT NOT NULL,
                reserved_at INTEGER NOT NULL,
                confirm_after INTEGER NOT NULL,
                opened_at INTEGER,
                last_seen_at INTEGER,
                order_id TEXT,
                payload_json TEXT NOT NULL
            );
            CREATE TABLE account_snapshot(
                singleton INTEGER PRIMARY KEY,
                updated_at INTEGER NOT NULL,
                connected INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                error TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE telegram_state(key TEXT PRIMARY KEY,value TEXT NOT NULL,updated_at INTEGER NOT NULL);
            CREATE TABLE health_state(component TEXT PRIMARY KEY,level TEXT NOT NULL,message TEXT NOT NULL,updated_at INTEGER NOT NULL);
            CREATE TABLE runtime_events(
                id INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT NOT NULL,canonical TEXT,
                message TEXT NOT NULL,created_at INTEGER NOT NULL,payload_json TEXT NOT NULL DEFAULT '{}'
            );
            """
        )
        timestamp = now_ms()
        legacy_payload = {
            "id": 1, "canonical": "OLDUSDT", "exchange_symbol": "OLD-SWAP-USDT",
            "side": "SHORT", "tier": "REAL", "status": "TP", "result": "TP",
            "created_at": timestamp - 60000, "closed_at": timestamp, "net_pnl": 1.25,
        }
        conn.execute(
            "INSERT INTO signals(canonical,exchange_symbol,side,tier,status,created_at,updated_at,payload_json) VALUES(?,?,?,?,?,?,?,?)",
            ("OLDUSDT", "OLD-SWAP-USDT", "SHORT", "REAL", "TP", timestamp - 60000, timestamp, json_dumps(legacy_payload)),
        )
        conn.commit()
        conn.close()

        migrated = Storage(legacy_path)
        try:
            signal_columns = {row[1] for row in migrated.conn.execute("PRAGMA table_info(signals)")}
            position_columns = {row[1] for row in migrated.conn.execute("PRAGMA table_info(positions)")}
            self.assertIn("mode", signal_columns)
            self.assertIn("exchange_symbol", position_columns)
            old = migrated.get_signal(1)
            self.assertEqual(old["mode"], "REAL")
            self.assertAlmostEqual(migrated.displayed_real_pnl()["total"], 1.25)
            self.assertIn("پنل ترید", CommandRouter(migrated, self.fake).handle("ترید"))  # type: ignore[arg-type]

            virtual = self.signal()
            virtual["canonical"] = "NEWVUSDT"
            virtual["exchange_symbol"] = "NEWV-SWAP-USDT"
            virtual_id = migrated.create_virtual_signal(virtual)
            self.assertIsNotNone(virtual_id)
            self.assertEqual(migrated.get_signal(int(virtual_id))["mode"], "VIRTUAL")

            real = self.signal()
            real["canonical"] = "NEWRUSDT"
            real["exchange_symbol"] = "NEWR-SWAP-USDT"
            real_id = migrated.create_real_signal_and_reserve(real)
            self.assertIsNotNone(real_id)
            row = migrated.conn.execute(
                "SELECT exchange_symbol,toobit_symbol FROM positions WHERE signal_id=?", (real_id,)
            ).fetchone()
            self.assertEqual(row[0], "NEWR-SWAP-USDT")
            self.assertEqual(row[1], "NEWR-SWAP-USDT")
        finally:
            migrated.close()



if __name__ == "__main__":
    unittest.main(verbosity=2)
