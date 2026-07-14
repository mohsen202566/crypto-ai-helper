"""Offline integration tests for the adaptive bot.

No live exchange order or network request is made by this suite.
Run: python3 -m unittest -v self_test.py
"""
from __future__ import annotations

import queue
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
import config
from behavior_engine import BehaviorEngine
from command_router import CommandRouter
from feature_engine import FeatureEngine
from learning_engine import LearningEngine
from logger_setup import RejectLogger
from market_data import MarketDataClient, MarketDataError
from news_filter import NewsFilter
from models import Candle, Signal, SymbolMapping
from real_monitor import RealMonitor
from scenario_lab import ScenarioLab
from signal_engine import SignalEngine
from storage import LearningStore, Storage
from toobit_client import ToobitClient, ToobitError
from trade_engine import TradeEngine
from tp_sl_engine import TPSLEngine
from utils import now_ms
from validator import Validator
from virtual_monitor import VirtualMonitor


def make_signal(
    canonical: str = "DOGEUSDT",
    side: str = "LONG",
    tier: str = "INITIAL",
    status: str = "ACTIVE",
    entry: float = 0.1,
    tp: float = 0.103,
    sl: float = 0.098,
    margin: float = 5.0,
    leverage: int = 10,
    decision: dict | None = None,
    features: dict | None = None,
    trade_timeframe: str = "30m",
    entry_timeframe: str = "15m",
) -> Signal:
    return Signal(
        id=None,
        canonical=canonical,
        exchange_symbol=f"{canonical[:-4]}-USDT-SWAP" if tier != "REAL" else f"{canonical[:-4]}-SWAP-USDT",
        side=side,
        tier=tier,
        status=status,
        created_at=now_ms(),
        entry=entry,
        tp=tp,
        sl=sl,
        rr=abs(tp - entry) / max(abs(entry - sl), 1e-12),
        margin_usdt=margin,
        leverage=leverage,
        notional_usdt=margin * leverage,
        expected_net_profit=0.08,
        expected_hold_minutes=20,
        data_source="OKX",
        profile_version=1,
        model_version="test",
        feature_version="test",
        behavior_version="test",
        tp_sl_version="test",
        decision=decision or {
            "direction_score": 65.0,
            "strength_score": 62.0,
            "entry_quality": 60.0,
            "regime_confidence": 61.0,
            "behavior": "TREND_START",
            "entry_type": "EARLY_MOVEMENT",
        },
        features=features or {
            "raw": {"selected": {"atr_natr": {"natr": 0.01, "atr": 0.001}}}
        },
        trade_timeframe=trade_timeframe,
        entry_timeframe=entry_timeframe,
    )


def synthetic_candles(count: int, interval_ms: int, start: float = 100.0) -> list[Candle]:
    rows: list[Candle] = []
    ts0 = 1_700_000_000_000
    price = start
    for i in range(count):
        drift = 0.00035 + (0.00012 if (i // 25) % 2 == 0 else -0.00004)
        wiggle = ((i % 7) - 3) * 0.00003
        open_ = price
        close = max(0.01, open_ * (1.0 + drift + wiggle))
        high = max(open_, close) * 1.0008
        low = min(open_, close) * 0.9992
        volume = 1000.0 + (i % 20) * 35.0
        rows.append(Candle(ts0 + i * interval_ms, open_, high, low, close, volume, close * volume, True))
        price = close
    return rows


class BotOfflineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_backup = config.BACKUP_DIR
        config.BACKUP_DIR = self.root / "backups"
        self.storage = Storage(self.root / "runtime.db", self.root / "learning.db")

    def tearDown(self) -> None:
        try:
            self.storage.close()
        except Exception:
            pass
        config.BACKUP_DIR = self.old_backup
        self.tmp.cleanup()

    def _ready_profile(self, canonical: str, side: str, stage: str = "INITIAL") -> None:
        engine = FeatureEngine()
        bootstrap = {
            "built_at": now_ms(), "candles": 1300, "natr_p50": 0.008, "natr_p90": 0.02,
        }
        self.storage.learning.save_bootstrap_profile(canonical, side, bootstrap, engine.default_profile_config())
        self.storage.learning.update_profile(canonical, side, stage=stage)

    def _final_result(self, signal: Signal, result: str, pnl: float) -> dict:
        sid = self.storage.runtime.create_official_signal(signal)
        self.assertIsNotNone(sid)
        final = self.storage.runtime.finalize_signal(int(sid), result, signal.tp if result == "TP" else signal.sl, pnl)
        self.assertIsNotNone(final)
        return final or {}

    def test_storage_defaults_integrity_and_restart_safety(self) -> None:
        self.assertTrue(self.storage.integrity_check())
        self.assertFalse(self.storage.runtime.get_setting("real_trade_enabled"))
        self.storage.runtime.set_setting("real_trade_enabled", True)
        self._ready_profile("BTCUSDT", "LONG")
        self.storage.close()
        self.storage = Storage(self.root / "runtime.db", self.root / "learning.db")
        self.assertFalse(self.storage.runtime.get_setting("real_trade_enabled"))
        self.assertTrue(self.storage.learning.get_profile("BTCUSDT", "LONG")["ready"])

    def test_learning_db_v1_migrates_without_losing_real_state(self) -> None:
        legacy = Path(self.tmp.name) / "legacy_learning.db"
        conn = sqlite3.connect(legacy)
        conn.execute(
            "CREATE TABLE symbol_real_state(canonical TEXT PRIMARY KEY, stop_streak INTEGER NOT NULL DEFAULT 0, "
            "last_result TEXT, demotion_reason TEXT, demoted_at INTEGER, updated_at INTEGER NOT NULL)"
        )
        conn.execute(
            "INSERT INTO symbol_real_state(canonical,stop_streak,last_result,demotion_reason,demoted_at,updated_at) "
            "VALUES('DOGEUSDT',2,'STOP','legacy',123,456)"
        )
        conn.execute("PRAGMA user_version=1")
        conn.commit()
        conn.close()

        store = LearningStore(legacy)
        try:
            self.assertEqual(store.user_version(), config.LEARNING_SCHEMA_VERSION)
            state = store.real_state("DOGEUSDT")
            self.assertEqual(state["stop_streak"], 2)
            self.assertEqual(state["demotion_reason"], "legacy")
            self.assertFalse(state["real_blocked"])
            columns = {row[1] for row in store.conn.execute("PRAGMA table_info(symbol_real_state)")}
            self.assertTrue({"stop_sides_json", "real_blocked", "relearn_sides_json"}.issubset(columns))
        finally:
            store.close()

    def test_command_ranges_and_pnl_resets(self) -> None:
        router = CommandRouter(self.storage)
        self.assertIn("10000", router.handle("ترید دلار ۱۰۰۰۰"))
        self.assertEqual(self.storage.runtime.get_setting("trade_margin_usdt"), 10000.0)
        self.assertIn("100x", router.handle("ترید لوریج ۱۰۰"))
        self.assertEqual(self.storage.runtime.get_setting("leverage"), 100)
        self.assertIn("7 USDT", router.handle("دلار ترید ۷"))
        self.assertEqual(self.storage.runtime.get_setting("trade_margin_usdt"), 7.0)
        self.assertIn("9x", router.handle("لوریج ترید ۹"))
        self.assertEqual(self.storage.runtime.get_setting("leverage"), 9)
        self.assertIn("200", router.handle("حداکثر پوزیشن ۲۰۰"))
        self.assertEqual(self.storage.runtime.get_setting("max_open_positions"), 200)
        self.assertIn("خارج از بازه", router.handle("ترید دلار 10001"))
        self.assertIn("عدد صحیح", router.handle("ترید لوریج 1.5"))

        final = self._final_result(make_signal(tier="REAL"), "MANUAL_CLOSE", 1.25)
        self.assertEqual(final["result"], "MANUAL_CLOSE")
        self.assertAlmostEqual(self.storage.runtime.displayed_real_pnl()["total"], 1.25, places=6)
        router.handle("ریست سود کل")
        self.assertAlmostEqual(self.storage.runtime.displayed_real_pnl()["total"], 0.0, places=6)
        # Raw history remains intact.
        self.assertAlmostEqual(self.storage.runtime.raw_real_pnl()["total"], 1.25, places=6)

    def test_same_timeframe_lock_and_real_slot_cap(self) -> None:
        first = make_signal("DOGEUSDT", "LONG", "INITIAL")
        sid1 = self.storage.runtime.create_official_signal(first)
        self.assertIsNotNone(sid1)
        second = make_signal("DOGEUSDT", "SHORT", "MEDIUM")
        self.assertIsNone(self.storage.runtime.create_official_signal(second))
        self.storage.runtime.finalize_signal(int(sid1), "TP", first.tp, 0.1)
        self.assertIsNotNone(self.storage.runtime.create_official_signal(second))

        # Separate storage state for atomic slot test.
        self.storage.runtime.set_setting("max_open_positions", 1)
        a = make_signal("BTCUSDT", "LONG", "REAL", "PENDING_OPEN", entry=100, tp=102, sl=99)
        b = make_signal("ETHUSDT", "LONG", "REAL", "PENDING_OPEN", entry=100, tp=102, sl=99)
        aid = self.storage.runtime.create_real_signal_and_reserve(a)
        self.assertIsNotNone(aid)
        self.assertIsNone(self.storage.runtime.create_real_signal_and_reserve(b))
        slots = self.storage.runtime.slot_counts()
        self.assertEqual(slots["used"], 1)
        self.assertEqual(slots["free"], 0)
        self.storage.runtime.finalize_signal(int(aid), "FAILED_OPEN", None, None)
        self.assertIsNotNone(self.storage.runtime.create_real_signal_and_reserve(b))

    def test_feature_behavior_tp_sl_pipeline(self) -> None:
        engine = FeatureEngine()
        five = synthetic_candles(1300, 300_000)
        one = synthetic_candles(600, 60_000)
        bootstrap = engine.bootstrap_profile(five)
        profile = {"bootstrap": bootstrap, "config": engine.default_profile_config()}
        bundle = {
            "1m": one,
            "5m": five[-600:],
            "15m": MarketDataClient.resample(five[-900:], 15),
            # 50 hourly bars must be ignored safely rather than crashing analysis.
            "1H": MarketDataClient.resample(five[-600:], 60),
        }
        snap = engine.analyze("TESTUSDT", "OKX", bundle, profile, data_quality=99.0)
        self.assertIn(snap.entry_timeframe, config.ENTRY_TIMEFRAMES)
        self.assertNotIn("1H", snap.raw["per_tf"])
        self.assertEqual(set(config.BASE_TOOL_WEIGHTS).issubset(snap.long_scores), True)
        decision = BehaviorEngine().decide(snap, profile)
        self.assertGreaterEqual(decision.final_score, 0)
        plan = TPSLEngine().build_plan(snap, decision, profile, 100.0, 10, tick_size=0.001, tier="INITIAL")
        self.assertTrue(plan.valid, plan.reject_reason)
        self.assertGreaterEqual(plan.rr, 1.49)
        self.assertGreater(plan.expected_cost, 0)

    def test_scenario_budget_rotates_parameter_families_across_signals(self) -> None:
        profile = {"config": FeatureEngine.default_profile_config()}
        first = make_signal("DOGEUSDT", "LONG", "INITIAL").to_dict()
        second = make_signal("DOGEUSDT", "LONG", "INITIAL").to_dict()
        first["id"] = 101
        second["id"] = 102
        keys_a = [x["change_key"] for x in ScenarioLab._candidate_changes(first, profile)[:6]]
        keys_b = [x["change_key"] for x in ScenarioLab._candidate_changes(second, profile)[:6]]
        self.assertNotEqual(keys_a, keys_b)

    def test_scenario_creation_and_finalization(self) -> None:
        self._ready_profile("DOGEUSDT", "LONG")
        sig = make_signal("DOGEUSDT", "LONG", "INITIAL")
        sid = self.storage.runtime.create_official_signal(sig)
        q: queue.Queue[int] = queue.Queue()
        lab = ScenarioLab(self.storage, q)
        created = lab.create_for_signal(int(sid))
        self.assertGreaterEqual(created, config.SCENARIOS_MIN_PER_SIGNAL)
        scenarios = self.storage.learning.active_scenarios()
        self.assertTrue(all(len(s.get("patch") or {}) == 1 for s in scenarios))
        done = self.storage.learning.finalize_scenario(scenarios[0]["id"], "TP", 0.2, scenarios[0]["tp"])
        self.assertEqual(done["status"], "DONE")

    def test_duplicate_scenario_rows_count_as_one_independent_opportunity(self) -> None:
        canonical, side = "LINKUSDT", "LONG"
        self._ready_profile(canonical, side, "INITIAL")
        sig = make_signal(canonical, side, "INITIAL", entry=10.0, tp=10.3, sl=9.8)
        sid = int(self.storage.runtime.create_official_signal(sig) or 0)
        self.assertGreater(sid, 0)
        self.storage.runtime.finalize_signal(sid, "STOP", sig.sl, -0.2)
        parent = self.storage.runtime.get_signal(sid)
        self.storage.learning.insert_result(parent)
        patch = {"rr": 1.65}
        base = {
            "parent_signal_id": sid, "canonical": canonical, "side": side,
            "status": "ACTIVE", "change_key": "rr", "patch": patch,
            "entry": 10.0, "tp": 10.33, "sl": 9.8, "margin_usdt": 5.0,
            "leverage": 10, "old_value": 1.5, "new_value": 1.65, "no_entry": False,
        }
        first = dict(base, created_at=now_ms())
        first_id = self.storage.learning.create_scenario(first)
        self.storage.learning.finalize_scenario(first_id, "STOP", -0.2, 9.8)
        time.sleep(0.002)
        second = dict(base, created_at=now_ms())
        second_id = self.storage.learning.create_scenario(second)
        self.storage.learning.finalize_scenario(second_id, "TP", 0.3, 10.33)

        metrics = self.storage.learning.scenario_metrics(canonical, side, "rr", patch)
        self.assertEqual(metrics["count"], 1)
        self.assertEqual(metrics["wins"], 1)
        self.assertEqual(metrics["losses"], 0)
        self.assertAlmostEqual(metrics["net_pnl"], 0.3)

        groups = self.storage.learning.scenario_candidate_groups(min_count=1)
        group = next(x for x in groups if x["canonical"] == canonical and x["patch"] == patch)
        self.assertEqual(group["count"], 1)
        self.assertEqual(group["wins"], 1)
        self.assertEqual(group["losses"], 0)

        since = self.storage.learning.scenario_patch_metrics_since(canonical, side, patch, 0)
        self.assertEqual(since["count"], 1)
        self.assertEqual(since["wins"], 1)
        self.assertEqual(since["losses"], 0)

    def test_two_consecutive_real_stops_demote_same_trade_timeframe(self) -> None:
        canonical = "XRPUSDT"
        self._ready_profile(canonical, "LONG", "REAL_READY")
        self._ready_profile(canonical, "SHORT", "REAL_READY")
        results: queue.Queue[int] = queue.Queue()
        notifications: queue.Queue[dict] = queue.Queue()
        learner = LearningEngine(self.storage, results, notifications)

        s1 = make_signal(canonical, "LONG", "REAL")
        id1 = self.storage.runtime.create_official_signal(s1)
        self.storage.runtime.finalize_signal(int(id1), "STOP", s1.sl, -0.3)
        learner.learn_from_result(int(id1))
        self.assertEqual(self.storage.learning.real_state(canonical)["stop_streak"], 1)
        self.assertEqual(self.storage.learning.get_profile(canonical, "LONG")["stage"], "REAL_WATCH")

        s2 = make_signal(canonical, "SHORT", "REAL")
        id2 = self.storage.runtime.create_official_signal(s2)
        self.storage.runtime.finalize_signal(int(id2), "STOP", s2.sl, -0.25)
        learner.learn_from_result(int(id2))
        self.assertEqual(self.storage.learning.real_state(canonical)["stop_streak"], 2)
        self.assertEqual(self.storage.learning.get_profile(canonical, "LONG")["stage"], "MEDIUM_RELEARN")
        self.assertEqual(self.storage.learning.get_profile(canonical, "SHORT")["stage"], "MEDIUM_RELEARN")
        blocked = self.storage.learning.real_state(canonical)
        self.assertTrue(blocked["real_blocked"])
        self.assertEqual(set(blocked["relearn_sides"]), {"LONG", "SHORT"})

        # Even an externally recorded TP may reset the streak, but it must never bypass relearning.
        self.storage.learning.record_real_result(canonical, "TP", "LONG")
        state = self.storage.learning.real_state(canonical)
        self.assertEqual(state["stop_streak"], 0)
        self.assertTrue(state["real_blocked"])

    def test_challenger_requires_future_evidence_before_promotion(self) -> None:
        canonical, side = "ADAUSDT", "LONG"
        self._ready_profile(canonical, side, "INITIAL")
        patch_value = {"rr": 1.65}

        def add_pair(index: int) -> None:
            sig = make_signal(canonical, side, "INITIAL", entry=1.0, tp=1.015, sl=0.99)
            sid = self.storage.runtime.create_official_signal(sig)
            scenario = {
                "parent_signal_id": int(sid), "canonical": canonical, "side": side,
                "status": "ACTIVE", "created_at": now_ms(), "change_key": "rr",
                "patch": patch_value, "entry": 1.0, "tp": 1.02, "sl": 0.99,
                "margin_usdt": 5.0, "leverage": 10, "old_value": 1.5,
                "new_value": 1.65, "no_entry": False,
            }
            scenario_id = self.storage.learning.create_scenario(scenario)
            self.storage.runtime.finalize_signal(int(sid), "STOP", 0.99, -0.20)
            final = self.storage.runtime.get_signal(int(sid))
            self.storage.learning.insert_result(final)
            self.storage.learning.finalize_scenario(scenario_id, "TP", 0.25, 1.02)

        for i in range(8):
            add_pair(i)
        validator = Validator(self.storage)
        first = validator.run_once()
        self.assertEqual(first["challengers_created"], 1)
        self.assertEqual(first["config_promotions"], 0)
        profile = self.storage.learning.get_profile(canonical, side)
        self.assertAlmostEqual(profile["config"]["rr"], config.DEFAULT_RR)
        challenger = self.storage.learning.profile_versions(status="CHALLENGER", canonical=canonical, side=side)
        self.assertEqual(len(challenger), 1)

        time.sleep(0.003)  # Ensure these are future opportunities, not historical reuse.
        for i in range(config.CHALLENGER_CONFIRM_MIN_RESULTS):
            add_pair(100 + i)
        second = validator.run_once()
        self.assertEqual(second["config_promotions"], 1)
        promoted = self.storage.learning.get_profile(canonical, side)
        self.assertAlmostEqual(promoted["config"]["rr"], 1.65)
        self.assertEqual(len(self.storage.learning.profile_versions(status="CHAMPION", canonical=canonical, side=side)), 1)

    def test_toobit_exchange_contract_parsing_and_order_payload(self) -> None:
        class FakeToobit(ToobitClient):
            def __init__(self):
                super().__init__(base_url="https://example.invalid", timeout=1)
                self.api_key = "key"
                self.api_secret = "secret"
                self.calls: list[tuple[str, str, dict, bool]] = []

            def get_exchange_info(self):
                return {
                    "symbols": [{"symbol": "SPOTUSDT", "status": "TRADING"}],
                    "contracts": [
                        {"symbol": "BTC-SWAP-USDT", "status": "TRADING", "marginToken": "USDT", "inverse": False},
                        {"symbol": "BAD-SWAP-USDT", "status": "TRADING", "marginToken": "USDT", "inverse": True},
                        {"symbol": "OFF-SWAP-USDT", "status": "SUSPENDED", "marginToken": "USDT"},
                    ],
                }

            def prepare_symbol_for_trade(self, symbol: str, leverage: int, margin_type: str = "ISOLATED") -> None:
                self.prepared = (symbol, leverage, margin_type)

            def _request(self, method, path, params=None, signed=False):
                self.calls.append((method, path, dict(params or {}), signed))
                return {"code": 0, "data": {"orderId": "123"}}

        client = FakeToobit()
        symbols = client.get_exchange_symbols()
        self.assertIn("BTC-SWAP-USDT", symbols)
        self.assertNotIn("SPOTUSDT", symbols)
        self.assertNotIn("BAD-SWAP-USDT", symbols)

        result = client.place_market_order(
            symbol="BTC-SWAP-USDT", side="LONG", entry_price=100.0,
            trade_amount_usdt=5.0, leverage=10, tp_price=101.21, sl_price=99.19,
            client_order_id="abc", symbol_info={"stepSize": "0.01", "tickSize": "0.1", "minQty": "0.01", "minNotional": "5"},
        )
        self.assertEqual(client.prepared, ("BTC-SWAP-USDT", 10, "ISOLATED"))
        params = client.calls[-1][2]
        self.assertNotIn("valueQuantity", params)
        self.assertEqual(params["quantity"], "0.5")
        self.assertEqual(params["takeProfit"], "101.3")
        self.assertEqual(params["stopLoss"], "99.1")
        self.assertEqual(result["order_id"], "123")

        with self.assertRaises(ToobitError):
            client.place_market_order(
                symbol="BTC-SWAP-USDT", side="LONG", entry_price=100.0,
                trade_amount_usdt=1.0, leverage=1, tp_price=101, sl_price=99,
                client_order_id="too-small", symbol_info={"stepSize": "0.01", "tickSize": "0.1", "minNotional": "5"},
            )
        # Rounding down must not silently turn a valid requested notional into an
        # exchange-invalid actual notional or increase the user's margin.
        with self.assertRaises(ToobitError):
            client.place_market_order(
                symbol="BTC-SWAP-USDT", side="LONG", entry_price=3.0,
                trade_amount_usdt=10.0, leverage=1, tp_price=3.2, sl_price=2.8,
                client_order_id="rounded-below-min",
                symbol_info={"stepSize": "1", "tickSize": "0.1", "minNotional": "10"},
            )


    def test_toobit_balance_prefers_explicit_usdt_row(self) -> None:
        class BalanceToobit(ToobitClient):
            def __init__(self):
                super().__init__(base_url="https://example.invalid", timeout=1)

            def get_balance(self):
                return [
                    {"asset": "BTC", "balance": "99", "availableBalance": "88"},
                    {
                        "asset": "USDT", "balance": "123.45",
                        "availableBalance": "100.25", "positionMargin": "20",
                        "orderMargin": "1.5", "unrealizedPnL": "2.25",
                    },
                    {"balance": "9999", "availableBalance": "9999"},
                ]

        summary = BalanceToobit().get_usdt_balance_summary()
        self.assertAlmostEqual(summary["balance"], 123.45)
        self.assertAlmostEqual(summary["available"], 100.25)
        self.assertAlmostEqual(summary["position_margin"], 20.0)
        self.assertAlmostEqual(summary["order_margin"], 1.5)
        self.assertAlmostEqual(summary["unrealized_pnl"], 2.25)

    def test_pending_position_closed_before_70s_is_recovered_from_history(self) -> None:
        class ClosedFastToobit:
            has_credentials = True

            @staticmethod
            def _symbol_from_item(item):
                return str(item.get("symbol") or "")

            @staticmethod
            def _position_side(item):
                return str(item.get("side") or "LONG").upper()

            @staticmethod
            def _position_qty(item):
                return abs(float(item.get("position", 0)))

            def get_positions(self):
                return []

            def find_realized_result(self, **kwargs):
                return {
                    "pnl": 0.31,
                    "close_price": 0.103,
                    "close_time_ms": now_ms(),
                    "raw": {"closeType": "TAKE_PROFIT"},
                }

        result_q: queue.Queue[int] = queue.Queue()
        note_q: queue.Queue[dict] = queue.Queue()
        monitor = RealMonitor(self.storage, ClosedFastToobit(), result_q, note_q)
        sid = self.storage.runtime.create_real_signal_and_reserve(
            make_signal("DOGEUSDT", "LONG", "REAL", "PENDING_OPEN")
        )
        self.assertIsNotNone(sid)
        self.storage.runtime.update_signal(
            int(sid), order_id="o-1", client_order_id="c-1", order_submitted_at=now_ms() - 80_000
        )
        self.storage.runtime.update_position(
            int(sid), order_id="o-1", client_order_id="c-1",
            submitted_at=now_ms() - 80_000, confirm_after=now_ms() - 1,
        )
        counts = monitor.confirm_due()
        self.assertEqual(counts["closed"], 1)
        final = self.storage.runtime.get_signal(int(sid))
        self.assertEqual(final["result"], "TP")
        self.assertAlmostEqual(final["net_pnl"], 0.31)
        self.assertEqual(self.storage.runtime.slot_counts()["used"], 0)
        self.assertEqual(result_q.get_nowait(), sid)

    def test_find_realized_result_rejects_older_same_side_trade(self) -> None:
        class HistoryToobit(ToobitClient):
            def __init__(self):
                super().__init__(base_url="https://example.invalid", timeout=1)

            def get_history_positions(self, **kwargs):
                start = kwargs["start_ms"]
                return [
                    {
                        "symbol": "DOGE-SWAP-USDT", "side": "LONG",
                        "realizedPnl": "9.9", "closeTime": start - 60_000,
                        "closePrice": "0.5",
                    },
                    {
                        "symbol": "DOGE-SWAP-USDT", "side": "LONG",
                        "realizedPnl": "0.3", "closeTime": start + 10_000,
                        "closePrice": "0.103", "clientOrderId": "current-client",
                    },
                ]

            def get_order_history(self, **kwargs):
                return []

        started = now_ms() - 30_000
        result = HistoryToobit().find_realized_result(
            "DOGE-SWAP-USDT", "LONG", started, now_ms(),
            client_order_id="current-client",
        )
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result["pnl"], 0.3)
        self.assertTrue(result["identifier_match"])


    def test_analysis_bundle_caches_higher_timeframes_and_refreshes_only_stale_tail(self) -> None:
        class CachedMarket(MarketDataClient):
            def __init__(self):
                super().__init__()
                self.calls: list[tuple[str, str, int]] = []

            def _okx_candles(self, symbol, interval, limit):
                self.calls.append(("OKX", interval, limit))
                step = {"5m":300_000,"15m":900_000,"30m":1_800_000,"1H":3_600_000,"4H":14_400_000,"1D":86_400_000}[interval]
                return synthetic_candles(limit, step, 100.0)

            def _bybit_candles(self, symbol, interval, limit):
                raise AssertionError("fallback should not be used")

        mapping = SymbolMapping("TESTUSDT", "TEST", "TEST-USDT-SWAP", "TESTUSDT", "TEST-SWAP-USDT")
        market = CachedMarket()
        source1, bundle1 = market.analysis_bundle(mapping)
        source2, bundle2 = market.analysis_bundle(mapping)
        market._candle_cache_updated[("OKX", mapping.okx.upper(), "5m")] = 0
        source3, bundle3 = market.analysis_bundle(mapping)
        self.assertEqual((source1, source2, source3), ("OKX", "OKX", "OKX"))
        self.assertEqual(set(bundle1), set(config.ANALYSIS_CANDLE_LIMITS))
        self.assertEqual(len(bundle1["5m"]), config.ANALYSIS_CANDLE_LIMITS["5m"])
        self.assertEqual(len(bundle2["1D"]), config.ANALYSIS_CANDLE_LIMITS["1D"])
        self.assertEqual(len(bundle3["5m"]), config.ANALYSIS_CANDLE_LIMITS["5m"])
        expected = [("OKX", tf, limit) for tf, limit in config.ANALYSIS_CANDLE_LIMITS.items()]
        expected.append(("OKX", "5m", 12))
        self.assertEqual(market.calls, expected)

    def test_market_bundle_fallback_never_mixes_sources(self) -> None:
        class FakeMarket(MarketDataClient):
            def __init__(self):
                super().__init__()
                self.calls = []

            def _okx_candles(self, symbol, interval, limit):
                self.calls.append(("OKX", interval))
                if interval == "30m":
                    raise MarketDataError("forced OKX failure")
                step = {"5m":300_000,"15m":900_000,"30m":1_800_000,"1H":3_600_000,"4H":14_400_000,"1D":86_400_000}[interval]
                return synthetic_candles(limit, step, 100.0)

            def _bybit_candles(self, symbol, interval, limit):
                self.calls.append(("BYBIT", interval))
                step = {"5m":300_000,"15m":900_000,"30m":1_800_000,"1H":3_600_000,"4H":14_400_000,"1D":86_400_000}[interval]
                return synthetic_candles(limit, step, 200.0)

        mapping = SymbolMapping("TESTUSDT", "TEST", "TEST-USDT-SWAP", "TESTUSDT", "TEST-SWAP-USDT")
        market = FakeMarket()
        source, bundle = market.analysis_bundle(mapping)
        self.assertEqual(source, "BYBIT_FALLBACK")
        self.assertEqual(set(bundle), set(config.ANALYSIS_CANDLE_LIMITS))
        self.assertTrue(all(rows and rows[0].open > 190 for rows in bundle.values()))
        for tf in config.ANALYSIS_CANDLE_LIMITS:
            self.assertIn(("BYBIT", tf), market.calls)

    def test_post_stop_path_diagnosis_distinguishes_direction_error_and_whipsaw(self) -> None:
        adverse_probs, adverse_evidence = VirtualMonitor._post_stop_diagnosis({
            "side": "LONG", "entry": 100.0, "tp": 103.0, "sl": 98.0,
            "max_after": 98.2, "min_after": 95.5,
        })
        self.assertEqual(adverse_evidence["post_path"], "CONTINUED_ADVERSE_AFTER_STOP")
        self.assertGreater(adverse_probs["WRONG_DIRECTION"], adverse_probs["STOP_INSIDE_NOISE"])

        rebound_probs, rebound_evidence = VirtualMonitor._post_stop_diagnosis({
            "side": "LONG", "entry": 100.0, "tp": 103.0, "sl": 98.0,
            "max_after": 100.0, "min_after": 97.8,
        })
        self.assertEqual(rebound_evidence["post_path"], "REBOUNDED_AFTER_STOP_WITHOUT_TP")
        self.assertGreater(rebound_probs["STOP_INSIDE_NOISE"], rebound_probs["WRONG_DIRECTION"])

    def test_virtual_monitor_catches_missed_wick_conservatively_from_1m_path(self) -> None:
        created = ((now_ms() - 180_000) // 60_000) * 60_000
        sig = make_signal("DOGEUSDT", "LONG", "MEDIUM", entry=100.0, tp=102.0, sl=99.0)
        sig.created_at = created
        sid = self.storage.runtime.create_official_signal(sig)
        self.assertIsNotNone(sid)

        class Registry:
            def get(self, canonical):
                return object()

        class Market:
            def candles(self, mapping, interval, limit, allow_fallback=True):
                return "OKX", [
                    Candle(created + 60_000, 100.0, 102.5, 98.5, 100.5, 10.0, 1000.0, True),
                    Candle(created + 120_000, 100.5, 101.0, 100.0, 100.8, 10.0, 1000.0, True),
                ]

        q: queue.Queue[int] = queue.Queue()
        monitor = VirtualMonitor(self.storage, Registry(), Market(), TPSLEngine(), q)
        counts = monitor._reconcile_minute_paths()
        self.assertEqual(counts["signals"], 1)
        final = self.storage.runtime.get_signal(int(sid))
        self.assertEqual(final["result"], "STOP", "both TP and SL in one candle must be conservative")
        self.assertEqual(q.get_nowait(), sid)

    def test_real_monitor_alias_confirmation_close_and_api_error_safety(self) -> None:
        class FakeToobit:
            has_credentials = True

            def __init__(self):
                self.mode = "open"

            @staticmethod
            def _symbol_from_item(item):
                return item.get("symbol", "")

            @staticmethod
            def _position_side(item):
                return item.get("side", "LONG")

            @staticmethod
            def _position_qty(item):
                return abs(float(item.get("position", 0)))

            def get_positions(self):
                if self.mode == "error":
                    raise RuntimeError("temporary API error")
                if self.mode == "open":
                    return [{"symbol": "DOGEUSDT", "side": "LONG", "position": "10", "entryPrice": "0.101"}]
                return []

            def get_usdt_balance_summary(self):
                return {"balance": 100.0, "available": 90.0, "position_margin": 5.0, "order_margin": 0.0}

            def find_realized_result(self, **kwargs):
                return {"pnl": 0.25, "close_price": 0.103, "close_time_ms": now_ms(), "raw": {"closeType": "TAKE_PROFIT"}}

        fake = FakeToobit()
        result_q: queue.Queue[int] = queue.Queue()
        note_q: queue.Queue[dict] = queue.Queue()
        monitor = RealMonitor(self.storage, fake, result_q, note_q)
        sig = make_signal("DOGEUSDT", "LONG", "REAL", "PENDING_OPEN")
        sid = self.storage.runtime.create_real_signal_and_reserve(sig)
        self.storage.runtime.update_position(int(sid), confirm_after=now_ms() - 1, submitted_at=now_ms() - 70_001)
        first = monitor.tick()
        self.assertEqual(first["confirmed"], 1)
        self.assertEqual(self.storage.runtime.get_signal(int(sid))["status"], "OPEN")
        self.assertEqual(self.storage.runtime.slot_counts()["open"], 1)

        fake.mode = "error"
        monitor.tick()
        self.assertEqual(self.storage.runtime.slot_counts()["used"], 1, "API error must not free slot")

        fake.mode = "closed"
        second = monitor.tick()
        self.assertEqual(second["closed"], 1)
        self.assertEqual(self.storage.runtime.get_signal(int(sid))["result"], "TP")
        self.assertEqual(self.storage.runtime.slot_counts()["used"], 0)
        self.assertEqual(result_q.get_nowait(), sid)

    def test_pending_confirmation_makes_no_api_call_before_70s_deadline(self) -> None:
        class FakeToobit:
            has_credentials = True

            def __init__(self):
                self.calls = 0

            @staticmethod
            def _symbol_from_item(item):
                return str(item.get("symbol") or "")

            @staticmethod
            def _position_side(item):
                return str(item.get("side") or "").upper()

            @staticmethod
            def _position_qty(item):
                return abs(float(item.get("position", 0)))

            def get_positions(self):
                self.calls += 1
                return []

        fake = FakeToobit()
        monitor = RealMonitor(self.storage, fake, queue.Queue(), queue.Queue())
        sid = self.storage.runtime.create_real_signal_and_reserve(make_signal(tier="REAL", status="PENDING_OPEN"))
        self.assertIsNotNone(sid)
        counts = monitor.confirm_due()
        self.assertEqual(fake.calls, 0)
        self.assertEqual(counts["pending"], 0)
        self.storage.runtime.update_position(int(sid), confirm_after=now_ms() - 1, submitted_at=now_ms() - 70_001)
        monitor.confirm_due()
        self.assertEqual(fake.calls, 1)
        self.assertEqual(self.storage.runtime.get_signal(int(sid))["status"], "FAILED_OPEN")
        self.assertEqual(self.storage.runtime.slot_counts()["used"], 0)

    def test_missing_credentials_fail_before_submit_without_hanging_slot(self) -> None:
        class MissingCredentials:
            has_credentials = False

        self.storage.runtime.set_setting("real_trade_enabled", True)
        sig = make_signal("SOLUSDT", "LONG", "REAL", "PENDING_OPEN", entry=100, tp=102, sl=99)
        sid = self.storage.runtime.create_real_signal_and_reserve(sig)
        engine = TradeEngine(self.storage, MissingCredentials(), queue.Queue())
        engine.execute(int(sid))
        self.assertEqual(self.storage.runtime.get_signal(int(sid))["status"], "FAILED_OPEN")
        self.assertEqual(self.storage.runtime.slot_counts()["used"], 0)

    def test_submit_attempt_starts_full_70_second_confirmation_window(self) -> None:
        class SuccessfulToobit:
            has_credentials = True

            def place_market_order(self, **kwargs):
                return {"order_id": "order-1", "submitted": True}

        self.storage.runtime.set_setting("real_trade_enabled", True)
        sid = self.storage.runtime.create_real_signal_and_reserve(
            make_signal("SOLUSDT", "LONG", "REAL", "PENDING_OPEN", entry=100, tp=102, sl=99)
        )
        self.assertIsNotNone(sid)
        before = now_ms()
        TradeEngine(self.storage, SuccessfulToobit(), queue.Queue()).execute(int(sid))
        pos = self.storage.runtime.positions(statuses=("PENDING_OPEN",))[0]
        submitted_at = int(pos.get("submitted_at") or 0)
        confirm_after = int(pos.get("confirm_after") or 0)
        self.assertGreaterEqual(submitted_at, before)
        self.assertEqual(confirm_after - submitted_at, config.PENDING_CONFIRM_AFTER_SECONDS * 1000)

    def test_toobit_external_positions_consume_real_slots_without_double_count(self) -> None:
        self.storage.runtime.set_setting("max_open_positions", 2)
        self.storage.runtime.save_account_snapshot(
            True,
            {"open_positions": 1, "open_position_keys": ["BTCUSDT:LONG"]},
        )
        counts = self.storage.runtime.slot_counts()
        self.assertEqual(counts["used"], 1)
        self.assertEqual(counts["free"], 1)
        sid = self.storage.runtime.create_real_signal_and_reserve(
            make_signal("DOGEUSDT", "LONG", "REAL", "PENDING_OPEN")
        )
        self.assertIsNotNone(sid)
        self.assertEqual(self.storage.runtime.slot_counts()["used"], 2)
        blocked = self.storage.runtime.create_real_signal_and_reserve(
            make_signal("SOLUSDT", "LONG", "REAL", "PENDING_OPEN")
        )
        self.assertIsNone(blocked)
        # When Toobit reports the same pending position, it is not counted twice.
        self.storage.runtime.save_account_snapshot(
            True,
            {"open_positions": 2, "open_position_keys": ["BTCUSDT:LONG", "DOGEUSDT:LONG"]},
        )
        self.assertEqual(self.storage.runtime.slot_counts()["used"], 2)

    def test_incremental_profile_refresh_preserves_stage_and_champion_config(self) -> None:
        mapping = SymbolMapping(
            canonical="DOGEUSDT", base="DOGE", okx="DOGE-USDT-SWAP",
            bybit="DOGEUSDT", toobit="DOGE-SWAP-USDT", active=True, valid=True,
        )
        feature = FeatureEngine()
        old_bootstrap = {"built_at": 1, "candles": 1300, "natr_p50": 0.01, "natr_p90": 0.02}
        for side in ("LONG", "SHORT"):
            cfg = feature.default_profile_config()
            cfg["rr"] = 2.1
            self.storage.learning.save_bootstrap_profile(mapping.canonical, side, old_bootstrap, cfg)
            self.storage.learning.update_profile(mapping.canonical, side, stage="MEDIUM", champion_version=3, profile_version=3)

        class Registry:
            @staticmethod
            def active():
                return [mapping]

        class Market:
            @staticmethod
            def candles(_mapping, _interval, _limit):
                return "OKX", synthetic_candles(config.PROFILE_5M_CANDLES, 300_000, 0.1)

        engine = SignalEngine(
            self.storage, Registry(), Market(), feature, BehaviorEngine(), TPSLEngine(),
            queue.Queue(), queue.Queue(), queue.Queue(), RejectLogger(lambda: False),
        )
        refreshed = engine.refresh_one_stale_active_profile()
        self.assertEqual(refreshed, "DOGEUSDT")
        for side in ("LONG", "SHORT"):
            profile = self.storage.learning.get_profile("DOGEUSDT", side)
            self.assertEqual(profile["stage"], "MEDIUM")
            self.assertEqual(profile["champion_version"], 3)
            self.assertAlmostEqual(profile["config"]["rr"], 2.1)
            self.assertGreater(profile["bootstrap"]["built_at"], 1)

    def test_timeframe_profiles_and_locks_are_independent(self) -> None:
        engine = FeatureEngine()
        bootstrap = {"built_at": now_ms(), "candles": 200, "natr_p50": 0.01, "natr_p90": 0.02}
        for tf in config.TRADE_TIMEFRAMES:
            cfg = engine.default_profile_config(tf)
            self.storage.learning.save_bootstrap_profile("DOGEUSDT", "LONG", bootstrap, cfg, tf)
        self.storage.learning.update_profile("DOGEUSDT", "LONG", "4H", stage="MEDIUM")
        self.assertEqual(self.storage.learning.get_profile("DOGEUSDT", "LONG", "4H")["stage"], "MEDIUM")
        self.assertEqual(self.storage.learning.get_profile("DOGEUSDT", "LONG", "30m")["stage"], "INITIAL")
        s30 = make_signal("DOGEUSDT", "LONG", trade_timeframe="30m", entry_timeframe="15m")
        s4 = make_signal("DOGEUSDT", "LONG", trade_timeframe="4H", entry_timeframe="1H")
        id30 = self.storage.runtime.create_official_signal(s30)
        id4 = self.storage.runtime.create_official_signal(s4)
        self.assertIsNotNone(id30)
        self.assertIsNotNone(id4)
        self.assertTrue(self.storage.runtime.has_symbol_lock("DOGEUSDT", "30m"))
        self.assertTrue(self.storage.runtime.has_symbol_lock("DOGEUSDT", "4H"))
        self.storage.runtime.finalize_signal(int(id30), "TP", s30.tp, 0.1)
        self.assertFalse(self.storage.runtime.has_symbol_lock("DOGEUSDT", "30m"))
        self.assertTrue(self.storage.runtime.has_symbol_lock("DOGEUSDT", "4H"))

    def test_higher_timeframe_entry_mapping_and_rr_bounds(self) -> None:
        feature = FeatureEngine()
        bundle = {
            "5m": synthetic_candles(600, 300_000),
            "15m": synthetic_candles(420, 900_000),
            "30m": synthetic_candles(420, 1_800_000),
            "1H": synthetic_candles(420, 3_600_000),
            "4H": synthetic_candles(320, 14_400_000),
            "1D": synthetic_candles(260, 86_400_000),
        }
        for tf in config.TRADE_TIMEFRAMES:
            bootstrap = feature.bootstrap_profile(bundle[tf], minimum=60)
            cfg = feature.default_profile_config(tf)
            # Deliberately out of range: TPSLEngine must clamp learned RR.
            cfg["rr"] = 99.0
            profile = {"bootstrap": bootstrap, "config": cfg}
            snap = feature.analyze("TESTUSDT", "OKX", bundle, profile, data_quality=99.0, trade_timeframe=tf)
            self.assertEqual(snap.trade_timeframe, tf)
            self.assertIn(snap.entry_timeframe, config.ENTRY_TIMEFRAME_OPTIONS[tf])
            decision = BehaviorEngine().decide(snap, profile)
            plan = TPSLEngine().build_plan(snap, decision, profile, 100.0, 10, tier="INITIAL")
            lo, hi = config.RR_BOUNDS_BY_TIMEFRAME[tf]
            self.assertTrue(plan.valid, f"{tf}: {plan.reject_reason}")
            self.assertGreaterEqual(plan.rr, lo - 1e-9, tf)
            self.assertLessEqual(plan.rr, hi + 1e-9, tf)

    def test_scanner_is_hard_gated_until_startup_profiles_ready(self) -> None:
        q1: queue.Queue[int] = queue.Queue()
        q2: queue.Queue[int] = queue.Queue()
        q3: queue.Queue[dict] = queue.Queue()
        engine = SignalEngine(
            self.storage, None, None, None, None, None, q1, q2, q3,
            RejectLogger(lambda: False),
        )
        self.storage.runtime.set_setting("startup_ready", False)
        self.assertEqual(engine.scan_once(), 0)

    def test_news_filter_parses_high_impact_and_respects_windows(self) -> None:
        original_url = config.NEWS_CALENDAR_URL
        config.NEWS_CALENDAR_URL = "https://calendar.invalid/events"
        self.addCleanup(setattr, config, "NEWS_CALENDAR_URL", original_url)
        event_ms = now_ms() + 60_000

        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "events": [
                        {"timestamp": event_ms, "impact": "high", "title": "CPI"},
                        {"timestamp": event_ms, "impact": "low", "title": "Minor"},
                    ]
                }

        class Session:
            def get(self, url, timeout):
                self.url = url
                self.timeout = timeout
                return Response()

            def close(self):
                return None

        news = NewsFilter(Session())
        news.refresh_if_due()
        self.assertEqual(news.status()["events"], 1)
        blocked, reason = news.is_blocked(at_ms=event_ms - 4 * 60_000)
        self.assertTrue(blocked)
        self.assertIn("CPI", reason)
        blocked, reason = news.is_blocked(market_abnormal=True, at_ms=event_ms + 20 * 60_000)
        self.assertTrue(blocked)
        self.assertIn("تمدید", reason)
        blocked, _ = news.is_blocked(at_ms=event_ms + 20 * 60_000)
        self.assertFalse(blocked)

    def test_no_blocking_70_second_sleep_in_source(self) -> None:
        source = (Path(__file__).parent / "toobit_client.py").read_text(encoding="utf-8")
        trade_source = (Path(__file__).parent / "trade_engine.py").read_text(encoding="utf-8")
        self.assertNotIn("sleep(70", source.replace(" ", ""))
        self.assertNotIn("sleep(70", trade_source.replace(" ", ""))

    def test_panels_work_without_network(self) -> None:
        router = CommandRouter(self.storage)
        for command in ("ترید", "آمار", "پوزیشن", "کوین‌ها", "سلامت"):
            text = router.handle(command)
            self.assertIsInstance(text, str)
            self.assertGreater(len(text), 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
