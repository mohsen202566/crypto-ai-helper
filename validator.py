"""اعتبارسنج مستقل: Promotion، Challenger، Rollback و بازگشت Relearn.

قاعده مهم: هیچ پارامتر آزمایشگاهی مستقیم Champion نمی‌شود. ابتدا با شواهد
تاریخی به CHALLENGER تبدیل می‌شود و سپس باید روی فرصت‌های *بعد از ساخت آن*
دوباره موفقیت خود را ثابت کند.
"""
from __future__ import annotations

import logging
from copy import deepcopy
from typing import Any

import config
from models import ProfileStage
from storage import Storage

logger = logging.getLogger("adaptive_bot")


def _break_even_win_rate(avg_rr: float) -> float:
    rr = max(0.2, float(avg_rr or config.DEFAULT_RR))
    return 1.0 / (1.0 + rr)


def _apply_patch(config_data: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Apply one validated, single-variable patch to a profile config."""
    out = deepcopy(config_data)
    for key, value in patch.items():
        if "." in key:
            head, tail = key.split(".", 1)
            nested = dict(out.get(head) or {})
            nested[tail] = value
            out[head] = nested
        elif key == "tp_distance_factor":
            out["tp_atr_multiplier"] = float(out.get("tp_atr_multiplier", 1.35)) * float(value)
        elif key == "sl_distance_factor":
            out["sl_atr_multiplier"] = float(out.get("sl_atr_multiplier", 0.9)) * float(value)
        elif key == "minimum_score_delta":
            out["medium_min_score"] = float(out.get("medium_min_score", config.MEDIUM_MIN_SCORE)) + float(value)
        else:
            out[key] = value
    return out


def _pareto_improved(metrics: dict[str, Any]) -> bool:
    """Profit and win rate are joint goals; neither may be materially sacrificed."""
    net = float(metrics.get("net_pnl") or 0.0)
    base_net = float(metrics.get("baseline_net_pnl") or 0.0)
    win = float(metrics.get("win_rate") or 0.0)
    base_win = float(metrics.get("baseline_win_rate") or 0.0)
    net_edge = max(0.02, abs(base_net) * 0.05)
    net_tolerance = max(0.02, abs(base_net) * 0.02)
    net_better = net > base_net + net_edge and win >= base_win - 0.02
    win_better = win >= base_win + 0.05 and net >= base_net - net_tolerance
    return net > 0.0 and (net_better or win_better)


def _clearly_degraded(metrics: dict[str, Any]) -> bool:
    net = float(metrics.get("net_pnl") or 0.0)
    base_net = float(metrics.get("baseline_net_pnl") or 0.0)
    win = float(metrics.get("win_rate") or 0.0)
    base_win = float(metrics.get("baseline_win_rate") or 0.0)
    tolerance = max(0.02, abs(base_net) * 0.05)
    return (
        (net < base_net - tolerance and win <= base_win + 0.02)
        or (win < base_win - 0.05 and net <= base_net + tolerance)
        or (net <= 0.0 < base_net and win <= base_win)
    )


class Validator:
    def __init__(self, storage: Storage):
        self.storage = storage

    @staticmethod
    def _stage_pass(metrics: dict[str, Any], min_count: int) -> bool:
        if metrics.get("count", 0) < min_count:
            return False
        breakeven = _break_even_win_rate(metrics.get("avg_rr", config.DEFAULT_RR))
        return (
            metrics.get("net_pnl", 0) > 0
            and metrics.get("win_rate", 0) >= breakeven + config.PROMOTION_WIN_EDGE_OVER_BREAKEVEN
            and metrics.get("profit_factor", 0) >= config.PROMOTION_MIN_PROFIT_FACTOR
        )

    def run_once(self) -> dict[str, int]:
        out = {"stage_promotions": 0, "challengers_created": 0, "config_promotions": 0, "challengers_rejected": 0, "rollbacks": 0}
        scenario_out = self._validate_scenarios()
        for key in ("challengers_created", "config_promotions", "challengers_rejected"):
            out[key] = scenario_out[key]

        for profile in self.storage.learning.profiles(ready=True):
            canonical, side = profile["canonical"], profile["side"]
            trade_tf = str(profile.get("trade_timeframe") or "30m")
            stage = profile["stage"]
            if stage == ProfileStage.INITIAL.value:
                metrics = self.storage.learning.result_metrics(canonical, side, "INITIAL", trade_timeframe=trade_tf)
                if self._stage_pass(metrics, config.PROMOTE_INITIAL_MIN_RESULTS):
                    self.storage.learning.update_profile(canonical, side, trade_tf, stage=ProfileStage.MEDIUM.value)
                    self.storage.learning.add_promotion(canonical, side, stage, ProfileStage.MEDIUM.value, "Initial سودده و پایدار", metrics, trade_tf)
                    out["stage_promotions"] += 1
            elif stage == ProfileStage.MEDIUM.value:
                metrics = self.storage.learning.result_metrics(canonical, side, "MEDIUM", trade_timeframe=trade_tf)
                if self._stage_pass(metrics, config.PROMOTE_MEDIUM_MIN_RESULTS):
                    self.storage.learning.update_profile(canonical, side, trade_tf, stage=ProfileStage.REAL_READY.value)
                    self.storage.learning.add_promotion(canonical, side, stage, ProfileStage.REAL_READY.value, "Medium سودده با وین‌ریت بالاتر از سربه‌سر", metrics, trade_tf)
                    out["stage_promotions"] += 1
            # MEDIUM_RELEARN is released only by the whole-symbol gate below.
            out["rollbacks"] += self._rollback_if_degraded(profile)
        out["stage_promotions"] += self._validate_relearn_symbols()
        return out

    def _validate_relearn_symbols(self) -> int:
        """Release a blocked symbol only after every stop-contributing side is corrected.

        Other directions keep their previous stage, but cannot execute Real while the
        symbol-wide block is active. This avoids punishing an unrelated direction while
        still enforcing the user's whole-symbol two-stop safety rule.
        """
        promoted = 0
        for state in self.storage.learning.real_states(blocked_only=True):
            canonical = state["canonical"]
            trade_tf = str(state.get("trade_timeframe") or "30m")
            demoted_at = int(state.get("demoted_at") or 0)
            required_sides = [s for s in (state.get("relearn_sides") or []) if s in {"LONG", "SHORT"}]
            if not required_sides or demoted_at <= 0:
                continue
            evidence: dict[str, Any] = {}
            ready = True
            for side in required_sides:
                profile = self.storage.learning.get_profile(canonical, side, trade_tf)
                metrics = self.storage.learning.result_metrics(canonical, side, "MEDIUM", since_ms=demoted_at, trade_timeframe=trade_tf)
                corrected = self.storage.learning.has_profile_champion_since(canonical, side, demoted_at, trade_tf)
                evidence[side] = {"corrected": corrected, "metrics": metrics}
                if not profile or profile.get("stage") != ProfileStage.MEDIUM_RELEARN.value:
                    ready = False
                if not corrected or not self._stage_pass(metrics, config.RELEARN_MIN_MEDIUM_RESULTS):
                    ready = False
            if not ready:
                continue
            for side in required_sides:
                self.storage.learning.update_profile(
                    canonical, side, trade_tf, stage=ProfileStage.REAL_READY.value, relearn_result_count=0
                )
                self.storage.learning.add_promotion(
                    canonical, side, ProfileStage.MEDIUM_RELEARN.value, ProfileStage.REAL_READY.value,
                    "درس اصلاحی تمام جهت‌های درگیر اثبات شد و قفل این تایم آزاد شد", evidence[side]["metrics"], trade_tf,
                )
                promoted += 1
            self.storage.learning.clear_real_relearn_block(canonical, trade_tf)
            self.storage.learning.audit(
                canonical, None, "REAL_RELEARN_CLEARED",
                "قفل رئال تایم‌فریم پس از اثبات تمام جهت‌های درگیر آزاد شد", evidence, trade_tf,
            )
        return promoted

    def _validate_scenarios(self) -> dict[str, int]:
        out = {"challengers_created": 0, "config_promotions": 0, "challengers_rejected": 0}

        # Phase 1: only future independent opportunities may confirm a Challenger.
        for version in self.storage.learning.profile_versions(status="CHALLENGER"):
            canonical, side = version["canonical"], version["side"]
            trade_tf = str(version.get("trade_timeframe") or "30m")
            payload = version.get("patch") or {}
            patch = payload.get("patch", payload)
            full_config = payload.get("full_config")
            if not isinstance(patch, dict) or not patch or not isinstance(full_config, dict):
                self.storage.learning.set_profile_version_status(canonical, side, int(version["version"]), "REJECTED", {"reason": "invalid_payload"}, trade_tf)
                out["challengers_rejected"] += 1
                continue
            metrics = self.storage.learning.scenario_patch_metrics_since(
                canonical, side, patch, int(version.get("created_at") or 0), trade_tf
            )
            if int(metrics.get("count") or 0) < config.CHALLENGER_CONFIRM_MIN_RESULTS:
                continue
            profile = self.storage.learning.get_profile(canonical, side, trade_tf)
            if not profile:
                continue
            if _pareto_improved(metrics):
                current_champion = int(profile.get("champion_version") or 1)
                if current_champion != int(version["version"]):
                    self.storage.learning.set_profile_version_status(
                        canonical, side, current_champion, "ARCHIVED", profile.get("stats") or {}, trade_tf
                    )
                self.storage.learning.set_profile_version_status(
                    canonical, side, int(version["version"]), "CHAMPION", metrics, trade_tf
                )
                self.storage.learning.update_profile(
                    canonical,
                    side,
                    trade_tf,
                    config=full_config,
                    champion_version=int(version["version"]),
                    profile_version=int(version["version"]),
                )
                self.storage.learning.add_promotion(
                    canonical,
                    side,
                    profile["stage"],
                    profile["stage"],
                    "Challenger تک‌تغییری روی فرصت‌های آینده تأیید و فعال شد",
                    metrics, trade_tf,
                )
                self.storage.learning.audit(
                    canonical, side, "CHALLENGER_PROMOTED", "نسخه اصلاحی وارد تصمیم‌های بعدی شد", {"version": version["version"], "patch": patch, "metrics": metrics}, trade_tf
                )
                out["config_promotions"] += 1
            elif _clearly_degraded(metrics):
                self.storage.learning.set_profile_version_status(
                    canonical, side, int(version["version"]), "REJECTED", metrics, trade_tf
                )
                self.storage.learning.audit(
                    canonical, side, "CHALLENGER_REJECTED", "تغییر روی فرصت‌های آینده بدتر یا بی‌اثر بود؛ Champion حفظ شد", {"version": version["version"], "patch": patch, "metrics": metrics}, trade_tf
                )
                out["challengers_rejected"] += 1

        # Phase 2: historical LAB evidence may only create one pending Challenger.
        active_keys = {
            (v["canonical"], v["side"], str(v.get("trade_timeframe") or "30m"))
            for v in self.storage.learning.profile_versions(status="CHALLENGER")
        }
        for candidate in self.storage.learning.scenario_candidate_groups(min_count=8):
            canonical, side, patch = candidate["canonical"], candidate["side"], candidate["patch"]
            trade_tf = str(candidate.get("trade_timeframe") or "30m")
            if (canonical, side, trade_tf) in active_keys:
                continue
            if self.storage.learning.patch_already_seen(canonical, side, patch, trade_tf):
                continue
            if not _pareto_improved(candidate):
                continue
            profile = self.storage.learning.get_profile(canonical, side, trade_tf)
            if not profile:
                continue
            parent_version = int(profile.get("champion_version") or 1)
            new_config = _apply_patch(profile.get("config") or {}, patch)
            payload = {"patch": patch, "full_config": new_config, "historical_metrics": candidate}
            new_version = self.storage.learning.create_profile_version(
                canonical, side, parent_version, payload, "CHALLENGER", trade_tf
            )
            self.storage.learning.audit(
                canonical,
                side,
                "CHALLENGER_CREATED",
                "شواهد LAB کافی بود؛ تغییر فقط برای آزمون روی فرصت‌های آینده ثبت شد",
                {"version": new_version, "patch": patch, "metrics": candidate}, trade_tf,
            )
            active_keys.add((canonical, side, trade_tf))
            out["challengers_created"] += 1
        return out

    def _rollback_if_degraded(self, profile: dict[str, Any]) -> int:
        version = int(profile.get("champion_version") or 1)
        if version <= 1:
            return 0
        trade_tf = str(profile.get("trade_timeframe") or "30m")
        current_version = self.storage.learning.profile_version(profile["canonical"], profile["side"], version, trade_tf)
        if not current_version or current_version.get("status") != "CHAMPION":
            return 0
        created_at = int(current_version.get("created_at") or 0)
        stage_tier = "MEDIUM" if profile["stage"] != ProfileStage.INITIAL.value else "INITIAL"
        actual = self.storage.learning.result_metrics(profile["canonical"], profile["side"], stage_tier, since_ms=created_at, trade_timeframe=trade_tf)
        if actual["count"] < 12:
            return 0
        expected = current_version.get("metrics") or {}
        degraded = (
            actual["net_pnl"] < 0
            and actual["win_rate"] + 0.05 < float(expected.get("win_rate", actual["win_rate"] + 0.05))
        )
        if not degraded:
            return 0
        previous = self.storage.learning.latest_prior_version(profile["canonical"], profile["side"], version, trade_tf)
        if not previous:
            return 0
        prior_payload = previous.get("patch") or {}
        prior_config = prior_payload.get("full_config")
        if not isinstance(prior_config, dict):
            return 0
        self.storage.learning.set_profile_version_status(profile["canonical"], profile["side"], version, "ROLLED_BACK", actual, trade_tf)
        self.storage.learning.set_profile_version_status(profile["canonical"], profile["side"], int(previous["version"]), "CHAMPION", previous.get("metrics") or {}, trade_tf)
        self.storage.learning.update_profile(
            profile["canonical"], profile["side"], trade_tf, config=prior_config,
            champion_version=int(previous["version"]), profile_version=int(previous["version"]),
        )
        self.storage.learning.audit(profile["canonical"], profile["side"], "ROLLBACK", "نسخه جدید در نتایج آینده بدتر شد؛ بازگشت به Champion قبلی", actual, trade_tf)
        return 1
