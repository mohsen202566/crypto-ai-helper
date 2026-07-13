from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import config
import indicators as ind
from setup_engine import SetupCandidate


@dataclass
class WatchEvaluation:
    watch_id: str
    state: str
    trigger_score: float  # compatibility/diagnostic only
    confirmed: bool
    entry_price: float
    reason: str
    meta: dict[str, Any] = field(default_factory=dict)


class WatchEngine:
    """Event-driven activation with conditional memory.

    Pressure alone never triggers. A valid path must show price progress and acceptance,
    while stale events are cleared when price loses the trigger area.
    """

    def evaluate(self, s: SetupCandidate, c1: list[dict[str, Any]]) -> WatchEvaluation:
        now = int(time.time())
        if now > s.expires_at:
            return WatchEvaluation(s.setup_id, 'EXPIRED', 0, False, 0, 'واچ منقضی شد')
        if len(c1) < 25:
            return WatchEvaluation(s.setup_id, 'WAITING', 0, False, 0, 'داده 1M کافی نیست')

        live_price = float(c1[-1]['close'])
        if live_price <= 0:
            return WatchEvaluation(s.setup_id, 'WAITING', 0, False, 0, 'قیمت 1M نامعتبر است')
        if (s.side == 'LONG' and live_price <= s.invalidation_price) or (s.side == 'SHORT' and live_price >= s.invalidation_price):
            s.meta['scenario_state'] = 'INVALIDATED'
            return WatchEvaluation(s.setup_id, 'INVALIDATED', 0, False, live_price, 'سطح ابطال سناریو شکسته شد')

        confirmed = [x for x in c1 if int(x.get('confirm', 1)) == 1]
        if len(confirmed) < 25:
            return WatchEvaluation(s.setup_id, 'WAITING', 0, False, live_price, 'کندل تأییدشده 1M کافی نیست')

        closes = ind.closes(confirmed)
        cf = ind.candle_features(confirmed[-1])
        prev_cf = ind.candle_features(confirmed[-2])
        rs = ind.rsi(closes, 7)
        _, _, hist = ind.macd(closes, 6, 13, 4)
        atr = max(float(s.meta.get('atr') or 0), 1e-12)

        consumed = max(0.0, live_price - s.trigger_price) if s.side == 'LONG' else max(0.0, s.trigger_price - live_price)
        consumed_atr = consumed / atr
        if consumed_atr > config.ENTRY_MAX_CONSUMED_ATR:
            s.meta['scenario_state'] = 'LATE'
            return WatchEvaluation(s.setup_id, 'INVALIDATED', 0, False, live_price, 'ورود از محدوده اقتصادی سناریو عبور کرده است', {'late_atr': consumed_atr})

        previous_close = float(confirmed[-2]['close'])
        previous2_close = float(confirmed[-3]['close'])
        price_cross = live_price > s.trigger_price if s.side == 'LONG' else live_price < s.trigger_price
        progress_atr = (
            max(0.0, live_price - s.trigger_price) / atr
            if s.side == 'LONG'
            else max(0.0, s.trigger_price - live_price) / atr
        )
        closes_beyond = sum(
            1 for x in confirmed[-max(2, config.ACTIVATION_ACCEPTANCE_BARS):]
            if (float(x['close']) > s.trigger_price if s.side == 'LONG' else float(x['close']) < s.trigger_price)
        )
        accepted = closes_beyond >= max(1, config.ACTIVATION_ACCEPTANCE_BARS)
        strong_acceptance = price_cross and progress_atr >= config.ACTIVATION_STRONG_PROGRESS_ATR and cf['body_ratio'] >= 0.45

        momentum = (
            rs[-1] > 51 and hist[-1] >= hist[-2]
            if s.side == 'LONG'
            else rs[-1] < 49 and hist[-1] <= hist[-2]
        )
        candle_response = (
            cf['direction'] == 1 and cf['close_location'] >= 0.62 and cf['body_ratio'] >= 0.35
            if s.side == 'LONG'
            else cf['direction'] == -1 and cf['close_location'] <= 0.38 and cf['body_ratio'] >= 0.35
        )
        pressure_shift = (
            cf['direction'] == 1 and prev_cf['direction'] <= 0 and cf['body_ratio'] > prev_cf['body_ratio']
            if s.side == 'LONG'
            else cf['direction'] == -1 and prev_cf['direction'] >= 0 and cf['body_ratio'] > prev_cf['body_ratio']
        )

        # Price progress is mandatory; pressure without progress is treated as possible absorption.
        progress_ok = progress_atr >= config.ACTIVATION_MIN_PROGRESS_ATR
        absorption = (pressure_shift or momentum) and not progress_ok and price_cross

        events = s.meta.setdefault('activation_events', {})
        candle_ts = int(confirmed[-1].get('ts') or 0)
        for name, active in {
            'momentum': momentum,
            'candle_response': candle_response,
            'pressure_shift': pressure_shift,
            'price_cross': price_cross,
            'accepted': accepted or strong_acceptance,
            'progress': progress_ok,
        }.items():
            if active:
                events[name] = {'time': now, 'candle_ts': candle_ts}

        # Losing the trigger area invalidates cached activation evidence, without killing the setup itself.
        lost_trigger = (
            live_price < s.trigger_price - 0.06 * atr if s.side == 'LONG'
            else live_price > s.trigger_price + 0.06 * atr
        )
        if lost_trigger:
            for key in ('price_cross', 'accepted', 'progress', 'candle_response', 'pressure_shift', 'momentum'):
                events.pop(key, None)

        ttl = int(config.ACTIVATION_EVENT_TTL_SECONDS)
        bar_ms = 60_000
        max_bars = int(config.ACTIVATION_EVENT_MAX_BARS)
        recent: dict[str, bool] = {}
        for name, payload in list(events.items()):
            ts = int(payload.get('time') or 0)
            cts = int(payload.get('candle_ts') or 0)
            valid = now - ts <= ttl and (not cts or not candle_ts or candle_ts - cts <= max_bars * bar_ms)
            recent[name] = valid
            if not valid:
                events.pop(name, None)

        recent_pressure = recent.get('pressure_shift', False)
        recent_momentum = recent.get('momentum', False)
        recent_candle = recent.get('candle_response', False)
        recent_acceptance = recent.get('accepted', False)
        recent_progress = recent.get('progress', False)

        path = None
        if price_cross and recent_progress and recent_acceptance:
            if recent_pressure or recent_momentum:
                path = 'PRESSURE_PROGRESS_ACCEPTANCE'
            elif recent_candle:
                path = 'PRICE_RESPONSE_ACCEPTANCE'
            elif s.meta.get('opportunity_pre_ready') and strong_acceptance:
                path = 'PREPARED_FAST_BREAK'

        confirmed_now = path is not None and not absorption
        diagnostic = 100.0 if confirmed_now else 60.0 if price_cross and progress_ok else 35.0 if (recent_pressure or recent_momentum or recent_candle) else 0.0

        if confirmed_now:
            s.meta['scenario_state'] = 'ACTIVATED'
            reason = {
                'PRESSURE_PROGRESS_ACCEPTANCE': 'فشار هم‌جهت همراه با پیشروی و پذیرش قیمت',
                'PRICE_RESPONSE_ACCEPTANCE': 'پاسخ معتبر قیمت همراه با پذیرش سطح',
                'PREPARED_FAST_BREAK': 'فرصت از قبل آماده بود و شکست سریع با پذیرش قوی رخ داد',
            }[path]
            state = 'TRIGGER_CONFIRMED'
        else:
            s.meta['scenario_state'] = 'WAITING_ACTIVATION'
            if absorption:
                reason = 'فشار دیده شد اما قیمت پیشروی نکرد؛ احتمال جذب وجود دارد'
            elif not price_cross and (recent_pressure or recent_momentum or recent_candle):
                reason = 'فعال‌سازی آماده است؛ عبور قیمت از سطح باقی مانده'
            elif price_cross and not progress_ok:
                reason = 'سطح عبور کرده اما پیشروی کافی نیست'
            elif price_cross and progress_ok and not recent_acceptance:
                reason = 'پیشروی انجام شده؛ پذیرش سطح هنوز تأیید نشده'
            else:
                reason = 'در انتظار رویداد فعال‌سازی سناریو'
            state = 'WAITING'

        return WatchEvaluation(
            s.setup_id, state, diagnostic, confirmed_now, live_price, reason,
            {
                'activation_path': path,
                'price_ok': price_cross,
                'progress_ok': progress_ok,
                'acceptance_ok': recent_acceptance,
                'pressure_ok': recent_pressure,
                'momentum_ok': recent_momentum,
                'candle_ok': recent_candle,
                'absorption': absorption,
                'progress_atr': progress_atr,
                'late_atr': consumed_atr,
                'events': dict(events),
                'confirmed_candle_ts': candle_ts,
                'previous_closes': [previous2_close, previous_close],
            },
        )
