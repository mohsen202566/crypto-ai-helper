from __future__ import annotations

import asyncio

import config
from indicators import build_snapshot
from okx_data import OkxDataClient
from range_learning import RangeLearningBrain
from symbols import ACTIVE_SYMBOLS
from target_engine import TargetEngine
from utils import logger, net_profit_after_fees


class HistoricalReplayEngine:
    def __init__(self, storage, okx: OkxDataClient) -> None:
        self.storage = storage
        self.okx = okx
        self.range_brain = RangeLearningBrain()
        self.target_engine = TargetEngine()

    async def run_daily_replay(self) -> None:
        for symbol in ACTIVE_SYMBOLS[:config.REPLAY_SYMBOL_LIMIT]:
            try:
                await asyncio.to_thread(self._replay_symbol, symbol)
            except Exception as exc:
                logger.warning("replay error %s: %s", symbol.name, exc)

    def _replay_symbol(self, symbol) -> None:
        candles = self.okx.get_historical_candles(symbol.okx_inst_id, config.TIMEFRAME_ENTRY, config.REPLAY_MAX_CANDLES)
        if len(candles) < 260:
            return
        observations = 0
        step = 6
        horizon = 24
        for idx in range(205, len(candles) - horizon, step):
            history = candles[:idx]
            snap = build_snapshot(history)
            if not (40 <= snap.rsi14 <= 72 and snap.volume_ratio >= 0.45 and snap.adx14 >= 8):
                continue
            entry = snap.close
            future = candles[idx:idx + horizon]
            high = max(c.high for c in future)
            low = min(c.low for c in future)
            mfe = max(0.0, (high - entry) / entry)
            mae = max(0.0, (entry - low) / entry)
            features = self.range_brain.make_features_key(symbol_name=symbol.name, market_state="REPLAY", alignment="REPLAY", snapshot=snap)
            profile = self.storage.get_range_profile(features)
            plan = self.target_engine.build(entry=entry, snapshot_5m=snap, profile=profile, trade_usdt=self.storage.trade_usdt())
            if not plan.ok:
                continue
            result = "TARGET" if mfe >= plan.target_distance_pct else "MISSED"
            net = plan.estimated_net_profit_usdt if result == "TARGET" else -max(config.MIN_NET_PROFIT_USDT, plan.estimated_fee_usdt * 0.25)
            self.storage.record_observation(source="replay", signal_id=None, features_key=features, symbol_name=symbol.name, result=result, net_profit=net, mfe_pct=mfe, mae_pct=mae, target_distance_pct=plan.target_distance_pct, reason="یادگیری از 7 روز گذشته")
            observations += 1
        self._record_run(symbol.name, observations)

    def _record_run(self, symbol_name: str, observations: int) -> None:
        with self.storage._connect() as conn:
            from utils import now_utc
            conn.execute("INSERT INTO historical_replay_runs(created_at, symbol_name, days, observations, notes) VALUES(?, ?, ?, ?, ?)", (now_utc().isoformat(), symbol_name, config.REPLAY_DAYS, observations, "daily replay"))
