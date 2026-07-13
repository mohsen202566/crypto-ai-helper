from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any

import config
from market_engine import MarketAnalysis


@dataclass
class SetupCandidate:
    setup_id: str
    symbol_id: str
    side: str
    setup_type: str
    state: str
    score: float  # diagnostic only; never a decision gate
    anchor_price: float
    invalidation_price: float
    trigger_price: float
    expires_at: int
    reasons: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


class SetupEngine:
    """Builds one coherent scenario and gives each concept one owner.

    Context owns direction/structure, this engine owns location and opportunity,
    WatchEngine owns activation, and RiskEngine owns economics.
    """

    def detect(self, m: MarketAnalysis, c5: list[dict[str, Any]]) -> SetupCandidate | None:
        candidate, _, _ = self.detect_with_reason(m, c5)
        return candidate

    @staticmethod
    def _sig(symbol_id: str, side: str, setup_type: str, trigger: float, invalidation: float, candle_ts: int) -> str:
        raw = f"{symbol_id}|{side}|{setup_type}|{trigger:.12g}|{invalidation:.12g}|{candle_ts}"
        return hashlib.sha1(raw.encode('utf-8')).hexdigest()[:16]

    def detect_with_reason(self, m: MarketAnalysis, c5: list[dict[str, Any]]) -> tuple[SetupCandidate | None, str, dict[str, Any]]:
        details: dict[str, Any] = {
            'context': m.primary_direction,
            'regime': m.regime,
            'movement_stage': m.movement_stage,
        }
        if m.hard_veto:
            return None, 'رد زمینه: بازار آشفته/فرسوده یا داده نامعتبر است', details
        if m.primary_direction not in {'LONG', 'SHORT'}:
            return None, 'رد زمینه: جهت ساختاری معتبر وجود ندارد', details
        if len(c5) < 30:
            return None, 'رد داده: تعداد کندل 5M کافی نیست', details

        side = m.primary_direction
        px = float(c5[-1]['close'])
        atr = float(m.features.get('atr') or 0)
        e21 = float(m.features.get('ema21') or 0)
        if px <= 0 or atr <= 0 or e21 <= 0:
            return None, 'رد داده: قیمت، ATR یا EMA21 نامعتبر است', details

        prior20 = c5[-21:-1]
        prior6 = c5[-7:-1]
        hi20 = max(float(x['high']) for x in prior20)
        lo20 = min(float(x['low']) for x in prior20)
        local_hi = max(float(x['high']) for x in prior6)
        local_lo = min(float(x['low']) for x in prior6)
        recent_closes = [float(x['close']) for x in c5[-7:]]
        vr = float(m.features.get('volume_ratio') or 1.0)
        value_distance = abs(px - e21) / atr

        broke_range = (side == 'LONG' and px > hi20) or (side == 'SHORT' and px < lo20)
        breakout_valid = broke_range and vr >= config.SCENARIO_BREAKOUT_MIN_VOLUME_RATIO
        touched_value = any(abs(close - e21) / atr <= config.SCENARIO_VALUE_ZONE_ATR for close in recent_closes)
        still_reachable = value_distance <= config.SCENARIO_MAX_LIVE_DISTANCE_ATR
        pullback_valid = touched_value and still_reachable

        details.update({
            'price': px,
            'atr': atr,
            'ema21': e21,
            'value_distance_atr': round(value_distance, 4),
            'touched_value': touched_value,
            'breakout': broke_range,
            'volume_ratio': round(vr, 3),
        })

        if breakout_valid:
            setup_type = 'COMPRESSION_BREAKOUT'
            trigger = hi20 if side == 'LONG' else lo20
            invalidation = local_lo if side == 'LONG' else local_hi
            obstacle = None
            capacity_atr = config.TARGET_CAPACITY_ATR_BREAKOUT
            identity = 'شکست محدوده با حفظ جهت ساختاری'
        elif pullback_valid:
            setup_type = 'PULLBACK_CONTINUATION'
            trigger = local_hi if side == 'LONG' else local_lo
            invalidation = (
                float(m.features.get('recent_swing_low') or lo20)
                if side == 'LONG'
                else float(m.features.get('recent_swing_high') or hi20)
            )
            obstacle = hi20 if side == 'LONG' else lo20
            capacity_atr = config.TARGET_CAPACITY_ATR_PULLBACK
            identity = 'پولبک به ناحیه ارزش در جهت ساختار'
        else:
            if broke_range and not breakout_valid:
                return None, f'رد سناریوی شکست: حجم برای هویت شکست ضعیف است ({vr:.2f})', details
            if not touched_value:
                return None, f'رد سناریوی پولبک: ناحیه ارزش لمس نشده ({value_distance:.2f} ATR)', details
            return None, f'رد سناریوی پولبک: حرکت از محل فرصت دور شده ({value_distance:.2f} ATR)', details

        if (side == 'LONG' and invalidation >= px) or (side == 'SHORT' and invalidation <= px):
            return None, 'رد سناریو: سطح ابطال در سمت نادرست قیمت است', details
        if (side == 'LONG' and trigger <= invalidation) or (side == 'SHORT' and trigger >= invalidation):
            return None, 'رد سناریو: ترتیب Trigger و Invalidation نامعتبر است', details

        # Opportunity freshness and activation freshness are intentionally separate.
        consumed_atr = max(0.0, px - trigger) / atr if side == 'LONG' else max(0.0, trigger - px) / atr
        if consumed_atr > config.ENTRY_MAX_CONSUMED_ATR:
            return None, f'رد فرصت: بخش زیادی از حرکت مصرف شده ({consumed_atr:.2f} ATR)', details

        diagnostic_quality = 75.0 if breakout_valid else 70.0
        if m.context_15m in {'BULLISH', 'BEARISH'}:
            aligned = (side == 'LONG' and m.context_15m == 'BULLISH') or (side == 'SHORT' and m.context_15m == 'BEARISH')
            diagnostic_quality += 8.0 if aligned else -8.0
        diagnostic_quality -= min(12.0, max(0.0, value_distance - 0.8) * 10.0)
        diagnostic_quality = max(0.0, min(100.0, diagnostic_quality))

        now = int(time.time())
        candle_ts = int(c5[-1].get('ts') or now * 1000)
        scenario_id = self._sig(m.symbol_id, side, setup_type, trigger, invalidation, candle_ts)
        target_capacity_price = px + capacity_atr * atr if side == 'LONG' else px - capacity_atr * atr

        candidate = SetupCandidate(
            setup_id=f'{m.symbol_id}-{scenario_id}',
            symbol_id=m.symbol_id,
            side=side,
            setup_type=setup_type,
            state='READY',
            score=round(diagnostic_quality, 2),
            anchor_price=px,
            invalidation_price=float(invalidation),
            trigger_price=float(trigger),
            expires_at=now + int(config.WATCH_TTL_SECONDS),
            reasons=[identity],
            risks=list(m.contradictions),
            meta={
                'atr': atr,
                'regime': m.regime,
                'obstacle_price': obstacle,
                'target_capacity_price': target_capacity_price,
                'context_side': side,
                'scenario_state': 'OPPORTUNITY_READY',
                'scenario_id': scenario_id,
                'activation_events': {},
                'created_at': now,
                'created_candle_ts': candle_ts,
                'opportunity_pre_ready': (px <= trigger if side == 'LONG' else px >= trigger),
                'entry_consumed_atr_at_creation': consumed_atr,
            },
        )
        return candidate, f'ورود به واچ: {identity}', details
