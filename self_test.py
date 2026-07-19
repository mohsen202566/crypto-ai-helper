"""تست آفلاین؛ هیچ درخواست شبکه و هیچ سفارش واقعی ارسال نمی‌کند.

اجرا:
    python -m unittest -v self_test.py
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

import config
from bot import BotEngine
from storage import Storage
from telegram_bot import CommandRouter, result_message, signal_message, stats_panel, trade_panel
from toobit_client import RateLimiter
from utils import now_ms


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

    def find_realized_result(self, **kwargs):
        return None


class OfflineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = config.RUNTIME_DB
        config.RUNTIME_DB = Path(self.tmp.name) / "runtime.db"
        self.storage = Storage(config.RUNTIME_DB)
        self.fake = FakeToobit()
        self.engine = BotEngine(self.storage, self.fake)  # type: ignore[arg-type]

    def tearDown(self):
        self.storage.close()
        config.RUNTIME_DB = self.old_db
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

    def test_rate_limiter_snapshot(self):
        limiter = RateLimiter()
        limiter.acquire(40, "market")
        limiter.acquire(1, "trade")
        snap = limiter.snapshot()
        self.assertEqual(snap["total_60s"], 41)
        self.assertEqual(snap["market_60s"], 40)


if __name__ == "__main__":
    unittest.main(verbosity=2)
