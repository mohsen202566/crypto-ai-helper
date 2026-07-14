"""آزمایشگاه سناریوهای تک‌تغییری برای Initial و Medium.

سناریوها سیگنال رسمی نیستند، قفل ارز یا آمار رسمی ایجاد نمی‌کنند و تا TP/Stop
خودشان مانیتور می‌شوند. بودجه پردازشی تطبیقی است.
"""
from __future__ import annotations

import hashlib
import logging
import queue
from copy import deepcopy
from typing import Any

import config
from models import Scenario
from storage import Storage
from utils import clamp, json_dumps, now_ms

logger = logging.getLogger("adaptive_bot")


class ScenarioLab:
    def __init__(self, storage: Storage, input_queue: queue.Queue[int]):
        self.storage = storage
        self.input_queue = input_queue

    def process_one(self, timeout: float = 1.0) -> int:
        try:
            signal_id = self.input_queue.get(timeout=timeout)
        except queue.Empty:
            return 0
        try:
            return self.create_for_signal(signal_id)
        finally:
            self.input_queue.task_done()

    def _budget(self) -> int:
        live = self.storage.learning.live_scenario_count()
        remaining = max(0, config.MAX_LIVE_SCENARIOS - live)
        if remaining <= 0:
            return 0
        if live > config.MAX_LIVE_SCENARIOS * 0.75:
            wanted = 2
        elif live > config.MAX_LIVE_SCENARIOS * 0.50:
            wanted = 4
        else:
            wanted = config.SCENARIOS_DEFAULT_PER_SIGNAL
        return min(wanted, remaining, config.SCENARIOS_MAX_PER_SIGNAL)

    @staticmethod
    def _candidate_changes(signal: dict[str, Any], profile: dict[str, Any]) -> list[dict[str, Any]]:
        cfg = profile.get("config") or {}
        decision = signal.get("decision") or {}
        features = signal.get("features") or {}
        raw = (features.get("raw") or {}).get("selected") or {}
        atr = float((raw.get("atr_natr") or {}).get("atr") or 0)
        entry = float(signal["entry"])
        side = signal["side"]
        tp_dist = abs(float(signal["tp"]) - entry)
        sl_dist = abs(float(signal["sl"]) - entry)
        changes: list[dict[str, Any]] = []

        def prices(new_entry: float, new_tp_dist: float, new_sl_dist: float) -> tuple[float, float, float]:
            if side == "LONG":
                return new_entry, new_entry + new_tp_dist, new_entry - new_sl_dist
            return new_entry, new_entry - new_tp_dist, new_entry + new_sl_dist

        # Persist absolute multipliers. Relative factor patches would become ambiguous
        # after a Champion changes and could accidentally aggregate different tests.
        current_tp_mult = float(cfg.get("tp_atr_multiplier", 1.35))
        current_sl_mult = float(cfg.get("sl_atr_multiplier", 0.90))
        for factor in (0.90, 1.10):
            new_mult = clamp(current_tp_mult * factor, 0.35, 6.0)
            e, tp, sl = prices(entry, tp_dist * factor, sl_dist)
            changes.append({
                "change_key": "tp_atr_multiplier",
                "old_value": current_tp_mult,
                "new_value": new_mult,
                "patch": {"tp_atr_multiplier": new_mult},
                "entry": e, "tp": tp, "sl": sl,
            })
        for factor in (0.90, 1.10):
            new_mult = clamp(current_sl_mult * factor, 0.25, 6.0)
            e, tp, sl = prices(entry, tp_dist, sl_dist * factor)
            changes.append({
                "change_key": "sl_atr_multiplier",
                "old_value": current_sl_mult,
                "new_value": new_mult,
                "patch": {"sl_atr_multiplier": new_mult},
                "entry": e, "tp": tp, "sl": sl,
            })

        # Risk/reward is adaptive, but every variant changes only RR/target geometry.
        trade_tf = str(signal.get("trade_timeframe") or (signal.get("decision") or {}).get("trade_timeframe") or "30m")
        rr_floor, rr_ceiling = config.RR_BOUNDS_BY_TIMEFRAME.get(trade_tf, (1.5, 5.0))
        current_rr = float(signal.get("rr") or cfg.get("rr") or config.RR_DEFAULT_BY_TIMEFRAME.get(trade_tf, config.DEFAULT_RR))
        for factor in (0.90, 1.10):
            new_rr = clamp(current_rr * factor, rr_floor, rr_ceiling)
            e, tp, sl = prices(entry, sl_dist * new_rr, sl_dist)
            changes.append({
                "change_key": "rr",
                "old_value": current_rr,
                "new_value": new_rr,
                "patch": {"rr": new_rr},
                "entry": e, "tp": tp, "sl": sl,
            })

        behavior = str(decision.get("behavior") or "UNKNOWN")
        behavior_tp = float((cfg.get("behavior_tp_factors") or {}).get(behavior, 1.0))
        for factor in (0.90, 1.10):
            new_value = clamp(behavior_tp * factor, 0.5, 2.0)
            e, tp, sl = prices(entry, tp_dist * factor, sl_dist)
            changes.append({
                "change_key": f"behavior_tp_factors.{behavior}",
                "old_value": behavior_tp,
                "new_value": new_value,
                "patch": {f"behavior_tp_factors.{behavior}": new_value},
                "entry": e, "tp": tp, "sl": sl,
            })

        behavior_sl = float((cfg.get("behavior_sl_factors") or {}).get(behavior, 1.0))
        for factor in (0.90, 1.10):
            new_value = clamp(behavior_sl * factor, 0.5, 2.0)
            e, tp, sl = prices(entry, tp_dist, sl_dist * factor)
            changes.append({
                "change_key": f"behavior_sl_factors.{behavior}",
                "old_value": behavior_sl,
                "new_value": new_value,
                "patch": {f"behavior_sl_factors.{behavior}": new_value},
                "entry": e, "tp": tp, "sl": sl,
            })

        current_score = float(decision.get("final_score") or 0)
        tier_floor = config.INITIAL_MIN_SCORE if signal.get("tier") == "INITIAL" else config.MEDIUM_MIN_SCORE
        behavior_prob = float((decision.get("behavior_probabilities") or {}).get(behavior, 0.0))
        old_behavior_bias = float((cfg.get("behavior_bias") or {}).get(behavior, 1.0))
        for factor in (0.85, 1.15):
            new_value = clamp(old_behavior_bias * factor, 0.35, 2.5)
            variant_score = current_score + (factor - 1.0) * max(2.0, behavior_prob * 12.0)
            changes.append({
                "change_key": f"behavior_bias.{behavior}",
                "old_value": old_behavior_bias,
                "new_value": new_value,
                "patch": {f"behavior_bias.{behavior}": new_value},
                "entry": entry, "tp": signal["tp"], "sl": signal["sl"],
                "no_entry": variant_score < tier_floor,
                "variant_score": variant_score,
            })

        entry_type = str(decision.get("entry_type") or "FLEXIBLE")
        old_entry_bias = float((cfg.get("entry_type_bias") or {}).get(entry_type, 1.0))
        for factor in (0.85, 1.15):
            new_value = clamp(old_entry_bias * factor, 0.35, 2.5)
            variant_score = current_score + (factor - 1.0) * 8.0
            changes.append({
                "change_key": f"entry_type_bias.{entry_type}",
                "old_value": old_entry_bias,
                "new_value": new_value,
                "patch": {f"entry_type_bias.{entry_type}": new_value},
                "entry": entry, "tp": signal["tp"], "sl": signal["sl"],
                "no_entry": variant_score < tier_floor,
                "variant_score": variant_score,
            })

        # Test both disabling and re-enabling BTC/ETH context. FeatureEngine keeps
        # the actual context value in raw data even when its live weight is disabled.
        current_context_enabled = bool(cfg.get("btc_eth_weight_enabled", True))
        target_context_enabled = not current_context_enabled
        tool_scores = (features.get("long_scores") if side == "LONG" else features.get("short_scores")) or {}
        weights_for_context = deepcopy(cfg.get("tool_weights") or config.BASE_TOOL_WEIGHTS)
        denom = sum(max(0.0, float(v)) for v in weights_for_context.values()) or 1.0
        actual_context = ((features.get("raw") or {}).get("actual_context_scores") or {}).get(side.lower(), 50.0)
        context_value = float(actual_context) if target_context_enabled else 50.0
        context_variant = sum(
            max(0.0, float(weights_for_context.get(k, 0)))
            * (context_value if k == "btc_eth_context" else float(tool_scores.get(k, 50)))
            for k in weights_for_context
        ) / denom
        changes.append({
            "change_key": "btc_eth_weight_enabled",
            "old_value": current_context_enabled,
            "new_value": target_context_enabled,
            "patch": {"btc_eth_weight_enabled": target_context_enabled},
            "entry": entry, "tp": signal["tp"], "sl": signal["sl"],
            "no_entry": context_variant < tier_floor,
            "variant_score": context_variant,
        })

        # Better entry/pullback scenario, using one absolute profile parameter.
        if atr > 0:
            current_offset = max(0.0, float(cfg.get("entry_atr_offset", 0.0) or 0.0))
            candidates = (0.10, 0.20) if current_offset <= 0 else (current_offset + 0.05, current_offset + 0.10)
            for new_offset in candidates:
                additional_offset = max(0.0, new_offset - current_offset)
                offset = atr * additional_offset
                new_entry = entry - offset if side == "LONG" else entry + offset
                e, tp, sl = prices(new_entry, tp_dist, sl_dist)
                changes.append({
                    "change_key": "entry_atr_offset",
                    "old_value": current_offset,
                    "new_value": new_offset,
                    "patch": {"entry_atr_offset": new_offset},
                    "entry": e, "tp": tp, "sl": sl,
                    "waiting_entry": additional_offset > 0,
                })

        # Soft gate/no-entry counterfactuals use absolute tier thresholds.
        threshold_key = "initial_min_score" if signal.get("tier") == "INITIAL" else "medium_min_score"
        old_threshold = float(cfg.get(threshold_key, tier_floor))
        for delta in (3.0, 6.0):
            threshold = clamp(old_threshold + delta, 35.0, 90.0)
            changes.append({
                "change_key": threshold_key,
                "old_value": old_threshold,
                "new_value": threshold,
                "patch": {threshold_key: threshold},
                "entry": entry, "tp": signal["tp"], "sl": signal["sl"],
                "no_entry": current_score < threshold,
            })

        # Tool-weight single changes. Recompute direction score and decide enter/no-entry.
        tool_scores = (features.get("long_scores") if side == "LONG" else features.get("short_scores")) or {}
        weights = deepcopy(cfg.get("tool_weights") or config.BASE_TOOL_WEIGHTS)
        for tool in sorted(weights):
            old = float(weights[tool])
            for factor in (0.80, 1.20):
                changed = old * factor
                new_weights = dict(weights)
                new_weights[tool] = changed
                denom = sum(max(0.0, v) for v in new_weights.values()) or 1.0
                score = sum(max(0.0, new_weights.get(k, 0)) * float(tool_scores.get(k, 50)) for k in new_weights) / denom
                changes.append({
                    "change_key": f"tool_weights.{tool}",
                    "old_value": old,
                    "new_value": changed,
                    "patch": {f"tool_weights.{tool}": changed},
                    "entry": entry, "tp": signal["tp"], "sl": signal["sl"],
                    "no_entry": score < max(47.0, current_score - 2.0),
                    "variant_score": score,
                })

        # Timeframe alternatives are real single-variable tests when data existed at issuance.
        per_tf = (features.get("raw") or {}).get("per_tf") or {}
        selected_tf = features.get("entry_timeframe") or decision.get("entry_timeframe")
        allowed_entry_tfs = set(config.ENTRY_TIMEFRAME_OPTIONS.get(trade_tf, (selected_tf,)))
        for tf, tf_raw in per_tf.items():
            if tf == selected_tf or tf not in allowed_entry_tfs:
                continue
            alt_entry = float(tf_raw.get("last") or entry)
            alt_atr = float((tf_raw.get("atr_natr") or {}).get("atr") or atr)
            if alt_atr <= 0:
                continue
            rr = float(signal.get("rr") or config.DEFAULT_RR)
            alt_sl_dist = alt_atr * float(cfg.get("sl_atr_multiplier", 0.9))
            alt_tp_dist = max(alt_atr * float(cfg.get("tp_atr_multiplier", 1.35)), alt_sl_dist * rr)
            e, tp, sl = prices(alt_entry, alt_tp_dist, alt_sl_dist)
            changes.append({
                "change_key": "entry_timeframe",
                "old_value": selected_tf,
                "new_value": tf,
                "patch": {"entry_timeframe": tf},
                "entry": e, "tp": tp, "sl": sl,
            })

        # Stable deterministic rotation ensures all mutable families get tested over time.
        seed = int(signal["id"])
        changes.sort(
            key=lambda x: hashlib.sha256(
                f"{seed}:{x['change_key']}:{x['new_value']}".encode("utf-8")
            ).digest()
        )
        return changes

    def create_for_signal(self, signal_id: int) -> int:
        signal = self.storage.runtime.get_signal(signal_id)
        if not signal or signal.get("tier") not in {"INITIAL", "MEDIUM"}:
            return 0
        trade_tf = str(signal.get("trade_timeframe") or (signal.get("decision") or {}).get("trade_timeframe") or "30m")
        profile = self.storage.learning.get_profile(signal["canonical"], signal["side"], trade_tf)
        if not profile:
            return 0
        budget = self._budget()
        if budget <= 0:
            return 0
        created = 0
        candidates = self._candidate_changes(signal, profile)
        challenger_targets: set[str] = set()
        for version in self.storage.learning.profile_versions(
            status="CHALLENGER", canonical=signal["canonical"], side=signal["side"], trade_timeframe=trade_tf
        ):
            payload = version.get("patch") or {}
            patch = payload.get("patch", payload)
            if isinstance(patch, dict):
                challenger_targets.add(json_dumps(patch))
        # Once a patch becomes Challenger, future independent opportunities must test
        # it first; remaining budget continues broad exploration.
        # Python sort is stable: this moves Challenger patches to the front while
        # preserving the deterministic per-signal rotation built above.
        candidates.sort(
            key=lambda item: 0 if json_dumps(item.get("patch") or {}) in challenger_targets else 1
        )
        for change in candidates[:budget]:
            scenario = Scenario(
                id=None,
                parent_signal_id=signal_id,
                canonical=signal["canonical"],
                side=signal["side"],
                created_at=now_ms(),
                status="ACTIVE",
                entry=float(change["entry"]),
                tp=float(change["tp"]),
                sl=float(change["sl"]),
                margin_usdt=float(signal["margin_usdt"]),
                leverage=int(signal["leverage"]),
                change_key=change["change_key"],
                old_value=change["old_value"],
                new_value=change["new_value"],
                patch=change["patch"],
                trade_timeframe=trade_tf,
                no_entry=bool(change.get("no_entry", False)),
            ).to_dict()
            scenario["waiting_entry"] = bool(change.get("waiting_entry", False))
            scenario["entered"] = not scenario["waiting_entry"] and not scenario["no_entry"]
            scenario["notional_usdt"] = float(signal["notional_usdt"])
            scenario["parent_tier"] = signal["tier"]
            scenario["variant_score"] = change.get("variant_score")
            self.storage.learning.create_scenario(scenario)
            created += 1
        return created
