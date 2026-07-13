from __future__ import annotations
from dataclasses import dataclass, replace
import config
from setup_engine import SetupCandidate
from decision_engine import TradeDecision

@dataclass
class RiskPlan:
    entry: float; tp: float; sl: float; sl_pct: float; tp_pct: float; gross_rr: float; net_rr: float
    trade_usdt: float; leverage: int; notional_usdt: float; estimated_net_profit: float
    estimated_net_loss: float; estimated_cost_win: float; estimated_cost_loss: float
    valid: bool; reason: str

    def rebased(self, new_entry: float) -> 'RiskPlan':
        if new_entry <= 0:
            return self
        tp = new_entry * (1 + self.tp_pct/100) if self.tp > self.entry else new_entry * (1 - self.tp_pct/100)
        sl = new_entry * (1 - self.sl_pct/100) if self.sl < self.entry else new_entry * (1 + self.sl_pct/100)
        return replace(self, entry=new_entry, tp=tp, sl=sl)

class RiskEngine:
    def build(self, s: SetupCandidate, d: TradeDecision, entry: float, trade_usdt: float, leverage: int) -> RiskPlan:
        if entry <= 0 or not (config.TRADE_USDT_MIN <= trade_usdt <= config.TRADE_USDT_MAX) or not (config.LEVERAGE_MIN <= leverage <= config.LEVERAGE_MAX):
            return RiskPlan(entry,0,0,0,0,0,0,trade_usdt,leverage,max(0,trade_usdt*leverage),0,0,0,0,False,'ورودی ریسک نامعتبر است')

        atr = float(s.meta.get('atr') or 0)
        if atr <= 0:
            return RiskPlan(entry,0,0,0,0,0,0,trade_usdt,leverage,trade_usdt*leverage,0,0,0,0,False,'ATR نامعتبر است')

        atr_pct = atr / entry * 100
        structural_pct = abs(entry - s.invalidation_price) / entry * 100
        structure_with_buffer = structural_pct + atr_pct * config.STOP_STRUCTURE_BUFFER_ATR
        noise_floor = atr_pct * config.STOP_ATR_FLOOR_MULT
        execution_floor = config.STOP_EXECUTION_FLOOR_PCT + config.ENTRY_SLIPPAGE_PCT + config.SL_SLIPPAGE_PCT
        sl_pct = max(config.MIN_SL_PCT, structure_with_buffer, noise_floor, execution_floor)

        max_sl = config.ADAPTIVE_MAX_SL_PCT if s.setup_type == 'COMPRESSION_BREAKOUT' else config.NORMAL_MAX_SL_PCT
        if sl_pct > max_sl:
            return RiskPlan(entry,0,0,sl_pct,0,0,0,trade_usdt,leverage,trade_usdt*leverage,0,0,0,0,False,'استاپ واقعی سناریو بیش از حد دور است')

        sl = entry*(1-sl_pct/100) if s.side=='LONG' else entry*(1+sl_pct/100)
        fee = 2*config.TOOBIT_FUTURES_TAKER_FEE_PCT
        win_cost_pct = fee + config.ENTRY_SLIPPAGE_PCT + config.TP_SLIPPAGE_PCT
        loss_cost_pct = fee + config.ENTRY_SLIPPAGE_PCT + config.SL_SLIPPAGE_PCT

        obstacle = s.meta.get('obstacle_price')
        capacity = s.meta.get('target_capacity_price')
        target_candidates = []
        buffer_abs = atr * config.TARGET_OBSTACLE_BUFFER_ATR
        if obstacle:
            obs = float(obstacle)
            target_candidates.append(obs - buffer_abs if s.side=='LONG' else obs + buffer_abs)
        if capacity:
            target_candidates.append(float(capacity))
        valid_targets = [x for x in target_candidates if (x > entry if s.side=='LONG' else x < entry)]
        if not valid_targets:
            return RiskPlan(entry,0,sl,sl_pct,0,0,0,trade_usdt,leverage,trade_usdt*leverage,0,0,0,0,False,'فضای واقعی تا تارگت کافی نیست')

        tp = min(valid_targets) if s.side=='LONG' else max(valid_targets)
        tp_pct = abs(tp-entry)/entry*100
        gross_rr = tp_pct/sl_pct if sl_pct else 0
        notional = trade_usdt*leverage
        win_cost = notional*win_cost_pct/100
        loss_cost = notional*loss_cost_pct/100
        net_profit = notional*tp_pct/100-win_cost
        net_loss = notional*sl_pct/100+loss_cost
        net_rr = net_profit/net_loss if net_loss else 0
        # User rule: the absolute minimum acceptable net profit is 0.05 USDT after fees/slippage.
        # Leverage changes notional, profit, loss and costs proportionally; it does not improve RR.
        min_profit = config.MIN_NET_PROFIT_USDT
        profit_ok = net_profit >= min_profit
        rr_ok = net_rr >= config.MIN_NET_RR_ABSOLUTE
        valid = profit_ok and rr_ok and tp > 0 and sl > 0
        if valid:
            reason = 'معتبر'
        elif not profit_ok and not rr_ok:
            reason = f'سود خالص {net_profit:.4f} کمتر از {min_profit:.4f} و NetRR {net_rr:.2f} کمتر از {config.MIN_NET_RR_ABSOLUTE:.2f} است'
        elif not profit_ok:
            reason = f'سود خالص {net_profit:.4f} کمتر از حداقل {min_profit:.4f} USDT است'
        else:
            reason = f'NetRR {net_rr:.2f} کمتر از حداقل {config.MIN_NET_RR_ABSOLUTE:.2f} است'
        return RiskPlan(entry,tp,sl,sl_pct,tp_pct,gross_rr,net_rr,trade_usdt,leverage,notional,net_profit,net_loss,win_cost,loss_cost,valid,reason)
