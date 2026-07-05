from __future__ import annotations

from dataclasses import dataclass

from ai_brain import AIBrain, AnalysisInput
from config import REPLAY_DAYS, REPLAY_MAX_CANDLES, TIMEFRAME_1D, TIMEFRAME_1H, TIMEFRAME_4H, TIMEFRAME_ENTRY
from okx_data import Candle, OkxDataClient
from storage import Storage
from symbols import MarketSymbol
from utils import direction_profit_pct, net_profit_for_move, now_utc


@dataclass(frozen=True)
class ReplayResult:
    symbol_name: str
    observations: int
    missed: int


class HistoricalReplayEngine:
    def __init__(self, storage: Storage, okx: OkxDataClient) -> None:
        self.storage = storage
        self.okx = okx
        self.brain = AIBrain(storage)

    def run_symbol(self, symbol: MarketSymbol, days: int = REPLAY_DAYS) -> ReplayResult:
        candles = self.okx.get_historical_candles(symbol.okx_inst_id, TIMEFRAME_ENTRY, limit=min(REPLAY_MAX_CANDLES, max(320, days * 288)))
        if len(candles) < 260:
            return ReplayResult(symbol.name, 0, 0)
        observations = 0
        missed = 0
        for index in range(220, len(candles) - 36, 6):
            past_5m = candles[:index]
            future = candles[index:index + 36]
            candles_by_tf = {
                TIMEFRAME_ENTRY: past_5m[-300:],
                TIMEFRAME_1H: aggregate_candles(past_5m, 60)[-220:],
                TIMEFRAME_4H: aggregate_candles(past_5m, 240)[-220:],
                TIMEFRAME_1D: aggregate_candles(past_5m, 1440)[-220:],
            }
            try:
                decision = self.brain.analyze(AnalysisInput(symbol.name, candles_by_tf, live_price=past_5m[-1].close))
            except Exception:
                continue
            if decision.accepted and decision.direction:
                result, mfe, mae, exit_price = simulate_result(decision.direction, decision.entry, decision.tp, decision.sl, future)
                move_pct = direction_profit_pct(decision.direction, decision.entry, exit_price)
                net = net_profit_for_move(self.storage.margin_usdt(), self.storage.leverage(), move_pct)
                self.storage.record_observation(source="replay", signal_id=None, features_key=decision.features_key, symbol_name=symbol.name, direction=decision.direction, result=result, net_profit=net, mfe_pct=mfe, mae_pct=mae, tp_distance_pct=decision.tp_distance_pct, sl_distance_pct=decision.sl_distance_pct, reason="HISTORICAL_REPLAY")
                observations += 1
            else:
                long_mfe = max((c.high - past_5m[-1].close) / past_5m[-1].close for c in future)
                short_mfe = max((past_5m[-1].close - c.low) / past_5m[-1].close for c in future)
                if max(long_mfe, short_mfe) >= 0.012:
                    missed += 1
                    direction = "LONG" if long_mfe >= short_mfe else "SHORT"
                    with self.storage._connect() as conn:
                        conn.execute("INSERT INTO missed_opportunities(created_at, symbol_name, direction, features_key, future_mfe_pct, reason) VALUES(?, ?, ?, ?, ?, ?)", (now_utc().isoformat(), symbol.name, direction, decision.features_key, max(long_mfe, short_mfe), decision.reason))
        with self.storage._connect() as conn:
            conn.execute("INSERT INTO historical_replay_runs(created_at, symbol_name, days, observations, notes) VALUES(?, ?, ?, ?, ?)", (now_utc().isoformat(), symbol.name, days, observations, f"missed={missed}"))
        return ReplayResult(symbol.name, observations, missed)


def aggregate_candles(candles: list[Candle], minutes: int) -> list[Candle]:
    bucket_ms = minutes * 60 * 1000
    groups: dict[int, list[Candle]] = {}
    for c in candles:
        key = c.ts // bucket_ms * bucket_ms
        groups.setdefault(key, []).append(c)
    out: list[Candle] = []
    for key in sorted(groups):
        group = groups[key]
        if not group:
            continue
        out.append(Candle(ts=key, open=group[0].open, high=max(c.high for c in group), low=min(c.low for c in group), close=group[-1].close, volume=sum(c.volume for c in group), confirmed=True))
    return out


def simulate_result(direction: str, entry: float, tp: float, sl: float, future: list[Candle]) -> tuple[str, float, float, float]:
    best = entry
    worst = entry
    for c in future:
        if direction == "LONG":
            best = max(best, c.high)
            worst = min(worst, c.low)
            if c.low <= sl:
                mfe = max(0.0, (best - entry) / entry)
                mae = max(0.0, (entry - worst) / entry)
                return "SL", mfe, mae, sl
            if c.high >= tp:
                mfe = max(0.0, (best - entry) / entry)
                mae = max(0.0, (entry - worst) / entry)
                return "TP", mfe, mae, tp
        else:
            best = min(best, c.low)
            worst = max(worst, c.high)
            if c.high >= sl:
                mfe = max(0.0, (entry - best) / entry)
                mae = max(0.0, (worst - entry) / entry)
                return "SL", mfe, mae, sl
            if c.low <= tp:
                mfe = max(0.0, (entry - best) / entry)
                mae = max(0.0, (worst - entry) / entry)
                return "TP", mfe, mae, tp
    exit_price = future[-1].close if future else entry
    mfe = max(0.0, abs(best - entry) / entry)
    mae = max(0.0, abs(worst - entry) / entry)
    result = "TP" if direction_profit_pct(direction, entry, exit_price) > 0 else "SL"
    return result, mfe, mae, exit_price
