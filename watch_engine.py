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
    trigger_score: float
    confirmed: bool
    entry_price: float
    reason: str
    meta: dict[str, Any] = field(default_factory=dict)


class WatchEngine:
    """Scenario activation without duplicate confirmations.

    Each path answers one independent question. Missing data is reported as missing,
    not silently converted to a negative signal. Cached events are conditional and expire.
    """

    def evaluate(self, s: SetupCandidate, c1: list[dict[str, Any]]) -> WatchEvaluation:
        now = int(time.time())
        origin = float(s.meta.get("origin_price") or s.anchor_price or 0)
        atr_created = float(s.meta.get("atr_at_creation") or s.meta.get("atr") or 0)
        base_meta = {
            "trigger": float(s.trigger_price), "invalidation": float(s.invalidation_price),
            "origin": origin, "atr": atr_created, "atr_at_creation": atr_created,
        }
        if now > s.expires_at:
            return WatchEvaluation(s.setup_id, "EXPIRED", 0, False, 0, "واچ منقضی شد", base_meta)
        if len(c1) < 25:
            return WatchEvaluation(s.setup_id, "WAITING", 0, False, 0, "داده 1M کافی نیست", {**base_meta, "data_ok": False})

        live_price = float(c1[-1].get("close") or 0)
        atr = atr_created
        if live_price <= 0 or origin <= 0 or atr <= 0:
            s.meta["scenario_state"] = "DATA_INVALID"
            return WatchEvaluation(s.setup_id, "INVALIDATED", 0, False, live_price, "Snapshot سناریو ناقص است: قیمت مبدأ یا ATR زمان ایجاد نامعتبر است", {**base_meta, "data_ok": False})
        if (s.side == "LONG" and live_price <= s.invalidation_price) or (s.side == "SHORT" and live_price >= s.invalidation_price):
            s.meta["scenario_state"] = "INVALIDATED"
            return WatchEvaluation(s.setup_id, "INVALIDATED", 0, False, live_price, "سطح ابطال سناریو شکسته شد", {**base_meta, "data_ok": True})

        confirmed = [x for x in c1 if int(x.get("confirm", 1)) == 1]
        if len(confirmed) < 25:
            return WatchEvaluation(s.setup_id, "WAITING", 0, False, live_price, "کندل تأییدشده 1M کافی نیست", {**base_meta, "data_ok": False})

        closes = ind.closes(confirmed)
        cf = ind.candle_features(confirmed[-1]); prev_cf = ind.candle_features(confirmed[-2])
        rs = ind.rsi(closes, 7); _, _, hist = ind.macd(closes, 6, 13, 4)
        if len(rs) < 2 or len(hist) < 2:
            return WatchEvaluation(s.setup_id, "WAITING", 0, False, live_price, "اندیکاتور فعال‌سازی آماده نیست", {**base_meta, "data_ok": False})

        signed_from_trigger = (live_price - s.trigger_price) if s.side == "LONG" else (s.trigger_price - live_price)
        trigger_distance_atr = signed_from_trigger / atr
        progress_atr = max(0.0, trigger_distance_atr)
        signed_from_origin = (live_price - origin) if s.side == "LONG" else (origin - live_price)
        origin_progress_atr = signed_from_origin / atr
        # Entry consumption is measured only after the activation level is crossed.
        # A negative trigger distance means the market has not activated yet; it is
        # not silently displayed as a meaningless zero in diagnostics.
        consumed_atr = progress_atr
        if consumed_atr > config.ENTRY_MAX_CONSUMED_ATR:
            s.meta["scenario_state"] = "LATE"
            return WatchEvaluation(s.setup_id, "INVALIDATED", 0, False, live_price, "ورود از محدوده اقتصادی سناریو عبور کرده است", {**base_meta, "data_ok": True, "late_atr": consumed_atr})

        price_cross = signed_from_trigger > 0
        tolerance = config.ACTIVATION_TOUCH_TOLERANCE_ATR * atr
        touched = (live_price >= s.trigger_price - tolerance) if s.side == "LONG" else (live_price <= s.trigger_price + tolerance)
        closes_beyond = sum(1 for x in confirmed[-max(2, config.ACTIVATION_ACCEPTANCE_BARS):] if (float(x["close"]) > s.trigger_price if s.side == "LONG" else float(x["close"]) < s.trigger_price))
        accepted = closes_beyond >= max(1, config.ACTIVATION_ACCEPTANCE_BARS)
        strong_acceptance = price_cross and progress_atr >= config.ACTIVATION_STRONG_PROGRESS_ATR and cf["body_ratio"] >= 0.45
        progress_ok = progress_atr >= config.ACTIVATION_MIN_PROGRESS_ATR

        momentum = (rs[-1] > 51 and hist[-1] >= hist[-2]) if s.side == "LONG" else (rs[-1] < 49 and hist[-1] <= hist[-2])
        candle_response = (cf["direction"] == 1 and cf["close_location"] >= 0.62 and cf["body_ratio"] >= 0.35) if s.side == "LONG" else (cf["direction"] == -1 and cf["close_location"] <= 0.38 and cf["body_ratio"] >= 0.35)
        pressure_shift = (cf["direction"] == 1 and prev_cf["direction"] <= 0 and cf["body_ratio"] > prev_cf["body_ratio"]) if s.side == "LONG" else (cf["direction"] == -1 and prev_cf["direction"] >= 0 and cf["body_ratio"] > prev_cf["body_ratio"])
        directional_response = candle_response and touched
        absorption = price_cross and (pressure_shift or momentum) and not progress_ok

        events = s.meta.setdefault("activation_events", {})
        candle_ts = int(confirmed[-1].get("ts") or 0)
        current_events = {"momentum": momentum, "candle_response": candle_response, "pressure_shift": pressure_shift, "price_cross": price_cross, "accepted": accepted or strong_acceptance, "progress": progress_ok}
        for name, active in current_events.items():
            if active: events[name] = {"time": now, "candle_ts": candle_ts}

        lost_trigger = live_price < s.trigger_price - tolerance if s.side == "LONG" else live_price > s.trigger_price + tolerance
        if lost_trigger:
            for key in ("price_cross", "accepted", "progress"):
                events.pop(key, None)

        recent = {}
        for name, payload in list(events.items()):
            age_ok = now - int(payload.get("time") or 0) <= int(config.ACTIVATION_EVENT_TTL_SECONDS)
            bars_ok = not candle_ts or not int(payload.get("candle_ts") or 0) or candle_ts - int(payload.get("candle_ts") or 0) <= int(config.ACTIVATION_EVENT_MAX_BARS) * 60000
            recent[name] = age_ok and bars_ok
            if not recent[name]: events.pop(name, None)

        evidence = recent.get("pressure_shift", False) or recent.get("momentum", False) or recent.get("candle_response", False)
        path = None
        # Path A: clean break and acceptance. No duplicate pressure gate.
        if price_cross and recent.get("progress", False) and recent.get("accepted", False):
            path = "BREAK_PROGRESS_ACCEPTANCE"
        # Path B: strong prepared break may activate before two closed bars.
        elif s.meta.get("opportunity_pre_ready") and strong_acceptance and evidence:
            path = "PREPARED_FAST_BREAK"
        # Path C: rejection/reclaim around trigger with directional response and momentum.
        elif touched and directional_response and (recent.get("momentum", False) or recent.get("pressure_shift", False)) and progress_atr >= config.ACTIVATION_REJECTION_PROGRESS_ATR:
            path = "TRIGGER_REJECTION_RESPONSE"

        confirmed_now = path is not None and not absorption
        diagnostic = 100.0 if confirmed_now else 60.0 if price_cross and progress_ok else 35.0 if evidence else 0.0
        if confirmed_now:
            s.meta["scenario_state"] = "ACTIVATED"
            reasons = {"BREAK_PROGRESS_ACCEPTANCE":"شکست، پیشروی و پذیرش سطح", "PREPARED_FAST_BREAK":"فرصت آماده و شکست سریع معتبر", "TRIGGER_REJECTION_RESPONSE":"واکنش معتبر در سطح فعال‌سازی"}
            state="TRIGGER_CONFIRMED"; reason=reasons[path]
        else:
            s.meta["scenario_state"] = "WAITING_ACTIVATION"; state="WAITING"
            if absorption: reason="فشار دیده شد اما قیمت پیشروی نکرد؛ احتمال جذب"
            elif not touched: reason="قیمت هنوز به محدوده فعال‌سازی نرسیده"
            elif touched and not price_cross and evidence: reason="شواهد فعال‌سازی حاضر است؛ عبور/بازپس‌گیری سطح باقی مانده"
            elif price_cross and not progress_ok: reason="سطح عبور کرده اما پیشروی کافی نیست"
            elif price_cross and progress_ok and not accepted: reason="پیشروی انجام شده؛ پذیرش یا پاسخ معتبر هنوز کامل نیست"
            else: reason="در انتظار یکی از مسیرهای مستقل فعال‌سازی"

        meta={**base_meta, "data_ok":True, "activation_path":path, "live_price":live_price, "price_ok":price_cross, "touch_ok":touched, "progress_ok":progress_ok, "acceptance_ok":recent.get("accepted",False), "pressure_ok":recent.get("pressure_shift",False), "momentum_ok":recent.get("momentum",False), "candle_ok":recent.get("candle_response",False), "absorption":absorption, "progress_atr":progress_atr, "late_atr":consumed_atr, "trigger_distance_atr":trigger_distance_atr, "origin_progress_atr":origin_progress_atr, "rsi":float(rs[-1]), "macd_hist":float(hist[-1]), "body_ratio":float(cf["body_ratio"]), "close_location":float(cf["close_location"]), "events":dict(events), "confirmed_candle_ts":candle_ts}
        return WatchEvaluation(s.setup_id,state,diagnostic,confirmed_now,live_price,reason,meta)
