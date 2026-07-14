"""یادگیری نتیجه‌محور، تشخیص علت Stop و قانون دو Stop واقعی."""
from __future__ import annotations

import logging
import queue
from typing import Any

import config
from models import ProfileStage
from storage import Storage
from utils import clamp, now_ms

logger = logging.getLogger("adaptive_bot")


class LearningEngine:
    def __init__(
        self,
        storage: Storage,
        result_queue: queue.Queue[int],
        notification_queue: queue.Queue[dict[str, Any]],
    ):
        self.storage = storage
        self.result_queue = result_queue
        self.notifications = notification_queue

    def process_one(self, timeout: float = 1.0) -> bool:
        try:
            signal_id = self.result_queue.get(timeout=timeout)
        except queue.Empty:
            return False
        try:
            self.learn_from_result(signal_id)
            return True
        finally:
            self.result_queue.task_done()

    @staticmethod
    def _normalize_probs(values: dict[str, float]) -> dict[str, float]:
        vals = {k: max(0.0, float(v)) for k, v in values.items()}
        total = sum(vals.values()) or 1.0
        return {k: round(v / total * 100, 1) for k, v in vals.items()}

    def diagnose_stop(self, signal: dict[str, Any]) -> tuple[dict[str, float], dict[str, Any]]:
        decision = signal.get("decision") or {}
        features = signal.get("features") or {}
        selected = (features.get("raw") or {}).get("selected") or {}
        entry_quality = float(decision.get("entry_quality") or 50)
        direction = float(decision.get("direction_score") or 50)
        regime = float(decision.get("regime_confidence") or 50)
        sl_pct = abs(float(signal.get("sl") or 0) - float(signal.get("entry") or 0)) / max(float(signal.get("entry") or 1), 1e-12)
        natr = float((selected.get("atr_natr") or {}).get("natr") or sl_pct)
        execution_slippage = abs(float(
            signal.get("entry_slippage_rate")
            or (signal.get("metadata") or {}).get("entry_slippage_rate")
            or 0
        ))
        raw = {
            "EARLY_OR_LATE_ENTRY": 20 + max(0, 58 - entry_quality) * 1.2,
            "STOP_INSIDE_NOISE": 15 + max(0, natr * 1.15 - sl_pct) / max(natr, 1e-6) * 50,
            "WRONG_DIRECTION": 12 + max(0, 58 - direction) * 1.1,
            "WRONG_REGIME": 10 + max(0, 58 - regime) * 0.9,
            "FALSE_BREAKOUT_OR_LIQUIDITY_SWEEP": 12 + (20 if decision.get("behavior") in {"TRUE_BREAKOUT", "TREND_START"} else 0),
            "BTC_ETH_CONTEXT_ERROR": 8,
            "EXECUTION_OR_SLIPPAGE": 5 + execution_slippage * 5000,
            "SHOCK_OR_UNFORECASTABLE": 10 + (20 if decision.get("behavior") == "SHOCK" else 0),
            "UNKNOWN": 12,
        }
        evidence = {
            "entry_quality": entry_quality,
            "direction_score": direction,
            "regime_confidence": regime,
            "sl_percent": sl_pct,
            "natr": natr,
            "behavior": decision.get("behavior"),
            "data_source": signal.get("data_source"),
        }
        return self._normalize_probs(raw), evidence

    def _smoothed_learning_effect(self, before: dict[str, Any], after: dict[str, Any], tier: str) -> tuple[float, str]:
        # One trade can only move the displayed learning impact slightly. Real has more evidence weight.
        weight = {"REAL": 1.0, "MEDIUM": 0.55, "INITIAL": 0.25}.get(tier, 0.2)
        win_delta = (after.get("win_rate", 0) - before.get("win_rate", 0)) * 100
        pnl_scale = max(1.0, abs(before.get("net_pnl", 0)), abs(after.get("net_pnl", 0)))
        pnl_delta = (after.get("net_pnl", 0) - before.get("net_pnl", 0)) / pnl_scale * 100
        effect = clamp((0.55 * win_delta + 0.45 * pnl_delta) * weight, -3.0, 3.0)
        count = int(after.get("count", 0))
        confidence = "کم" if count < 15 else "متوسط" if count < 40 else "بالا"
        return round(effect, 2), confidence

    def learn_from_result(self, signal_id: int) -> None:
        signal = self.storage.runtime.get_signal(signal_id)
        if not signal or signal.get("result") not in {"TP", "STOP"}:
            return
        trade_tf = str(signal.get("trade_timeframe") or (signal.get("decision") or {}).get("trade_timeframe") or "30m")
        profile = self.storage.learning.get_profile(signal["canonical"], signal["side"], trade_tf)
        if not profile:
            return
        before = self.storage.learning.result_metrics(signal["canonical"], signal["side"], signal["tier"], trade_timeframe=trade_tf)
        inserted = self.storage.learning.insert_result(signal)
        if not inserted:
            return
        after = self.storage.learning.result_metrics(signal["canonical"], signal["side"], signal["tier"], trade_timeframe=trade_tf)
        effect, confidence = self._smoothed_learning_effect(before, after, signal["tier"])

        stats = dict(profile.get("stats") or {})
        stats[signal["tier"]] = after
        stats["last_result"] = signal["result"]
        stats["last_signal_id"] = signal_id
        self.storage.learning.update_profile(signal["canonical"], signal["side"], trade_tf, stats=stats)

        diagnosis: dict[str, float] | None = None
        if signal["result"] == "STOP":
            diagnosis, evidence = self.diagnose_stop(signal)
            self.storage.learning.add_stop_diagnosis(signal_id, signal["canonical"], signal["tier"], diagnosis, evidence)

        if signal["tier"] == "REAL":
            real_state = self.storage.learning.record_real_result(signal["canonical"], signal["result"], signal["side"], trade_tf)
            if signal["result"] == "STOP" and int(real_state.get("stop_streak", 0)) == 1:
                current = self.storage.learning.get_profile(signal["canonical"], signal["side"], trade_tf)
                if current and current.get("stage") == ProfileStage.REAL_READY.value:
                    self.storage.learning.update_profile(signal["canonical"], signal["side"], trade_tf, stage=ProfileStage.REAL_WATCH.value)
            elif signal["result"] == "TP":
                current = self.storage.learning.get_profile(signal["canonical"], signal["side"], trade_tf)
                if current and current.get("stage") == ProfileStage.REAL_WATCH.value:
                    self.storage.learning.update_profile(signal["canonical"], signal["side"], trade_tf, stage=ProfileStage.REAL_READY.value)
            if int(real_state.get("stop_streak", 0)) >= config.REAL_DEMOTION_STOP_STREAK:
                self.storage.learning.demote_symbol_to_relearn(
                    signal["canonical"], "دو Stop واقعی متوالی", real_state.get("stop_sides") or [signal["side"]], trade_tf
                )
        elif profile.get("stage") == ProfileStage.MEDIUM_RELEARN.value and signal["tier"] == "MEDIUM":
            self.storage.learning.increment_relearn_count(signal["canonical"], signal["side"], trade_tf)

        # Non-blocking post-result path; the symbol lock is already released.
        hold = int(signal.get("expected_hold_minutes") or 30)
        horizon_min = int(clamp(hold * 3, config.POST_RESULT_MIN_MINUTES, config.POST_RESULT_MAX_MINUTES))
        self.storage.learning.start_post_result(signal, now_ms() + horizon_min * 60_000)

        self.storage.runtime.update_signal(
            signal_id,
            learning_effect_percent=effect,
            learning_confidence=confidence,
            stop_diagnosis=diagnosis,
        )
        self.storage.learning.audit(
            signal["canonical"], signal["side"], "RESULT_LEARNED",
            f"{signal['tier']} {signal['result']} learned", {"effect": effect, "confidence": confidence}, trade_tf,
        )
        self.notifications.put({"type": "result", "signal_id": signal_id})
