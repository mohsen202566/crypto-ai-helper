from __future__ import annotations

from dataclasses import replace
from typing import Any

from config import MIN_NET_PROFIT_USDT, MIN_RISK_REWARD, PRICE_TICK_DECIMALS
from guard_types import GuardVerdict
from guard_utils import session_info
from utils import net_profit_for_move, round_price


class AdaptiveFixEngine:
    """Treatment-testing layer above the original AI.

    It does not create the original signal and it does not add entry indicators to the
    main decision. It only applies learned fixes from previous TP/SL outcomes:
    safer signal type, tested TP/SL adjustment when the cause was TP/SL, and clearer
    reporting of whether a fix was actually applied or rejected.
    """

    def __init__(self, storage) -> None:
        self.storage = storage

    def apply(self, symbol_name: str, decision):
        if not getattr(decision, "accepted", False) or not getattr(decision, "direction", None):
            return decision, GuardVerdict()

        keys = self.profile_keys(symbol_name, decision)
        profiles = self.storage.get_adaptive_fix_profiles(keys)
        if not profiles:
            return decision, GuardVerdict()

        # Prefer dangerous profiles, but keep exact feature/symbol profiles ahead of broad time profiles
        # when risk is similar. This prevents TIME_GLOBAL from hiding the exact range learning.
        profiles.sort(
            key=lambda p: (
                int(p.get("risk_score") or 0),
                1 if str(p.get("scope") or "") in {"FEATURE", "SYMBOL_DIRECTION", "SYMBOL_STATE"} else 0,
                int(p.get("sl") or 0),
                int(p.get("samples") or 0),
            ),
            reverse=True,
        )
        strongest = profiles[0]
        adjusted, notes = self._apply_learned_price_treatment(decision, profiles)
        action = str(strongest.get("recommended_action") or "ALLOW").upper()
        risk = int(strongest.get("risk_score") or 0)
        cause = self._display_cause(strongest)

        if notes or action != "ALLOW":
            reason_note = self._reason_text(strongest, notes)
            adjusted = replace(adjusted, reason=(adjusted.reason or "") + "\n\n🧠 درمان یادگیری: " + reason_note)

        if action in {"WATCH_ONLY", "REJECT", "BLOCK"}:
            adjusted = replace(adjusted, real_allowed=False, signal_type_hint="reject", decision_label="REJECT", control_mode="reject")
            return adjusted, GuardVerdict(
                "BLOCK",
                "ADAPTIVE_FIX",
                f"الگوی مشابه از نظر Net PnL و درمان‌های قبلی قابل معامله نیست ({cause}, level={strongest.get('treatment_level',0)}, risk={risk}). Watch حذف شده؛ این موقعیت Reject می‌شود.",
                0,
                {"profile": strongest, "notes": notes},
            )
        if action in {"CAUTION", "REAL_BLOCK", "NORMAL_CONTROLLED"}:
            adjusted = replace(adjusted, real_allowed=False, signal_type_hint="normal_controlled", decision_label="NORMAL_CONTROLLED", control_mode="normal_controlled")
            level = "REAL_BLOCK" if action == "REAL_BLOCK" or risk >= 45 else "CAUTION"
            return adjusted, GuardVerdict(
                level,
                "ADAPTIVE_FIX",
                f"الگوی مشابه قبلاً ریسک داده ({cause}, risk={risk}). Watch حذف شده؛ فقط Normal کنترل‌شده مجاز است.",
                0,
                {"profile": strongest, "notes": notes},
            )
        if notes:
            adjusted = replace(adjusted, signal_type_hint="normal_controlled" if not getattr(adjusted, "real_allowed", False) else getattr(adjusted, "signal_type_hint", "normal"), control_mode="normal_controlled" if not getattr(adjusted, "real_allowed", False) else getattr(adjusted, "control_mode", "real"))
        return adjusted, GuardVerdict("ALLOW", "ADAPTIVE_FIX", "", 0, {"profile": strongest, "notes": notes})

    @staticmethod
    def profile_keys(symbol_name: str, decision) -> list[str]:
        direction = str(getattr(decision, "direction", "ANY") or "ANY")
        features_key = str(getattr(decision, "features_key", "") or "")
        state = str(getattr(decision, "market_state", "UNKNOWN") or "UNKNOWN")
        alignment = str(getattr(decision, "alignment", "UNKNOWN") or "UNKNOWN")
        info = session_info()
        keys = []
        if features_key:
            keys.append("FEATURE|" + features_key)
            parts = features_key.split("|")
            if len(parts) >= 12:
                indicator_signature = "|".join([direction, parts[3], parts[4], *parts[5:]])
                keys.append("INDICATOR|" + indicator_signature)
        keys.append("SYMBOL_DIRECTION|" + "|".join([symbol_name, direction]))
        keys.append("SYMBOL_STATE|" + "|".join([symbol_name, direction, state, alignment]))
        keys.append("TIME|" + "|".join([info.name, info.hour_bucket, info.weekday]))
        keys.append("TIME_GLOBAL|" + "|".join([info.name, info.hour_bucket, "ANYDAY"]))
        return list(dict.fromkeys(keys))

    def _apply_learned_price_treatment(self, decision, profiles: list[dict[str, Any]]):
        entry = float(getattr(decision, "entry", 0.0) or 0.0)
        direction = str(getattr(decision, "direction", "") or "")
        if entry <= 0 or direction not in {"LONG", "SHORT"}:
            return decision, []
        original_tp_pct = float(getattr(decision, "tp_distance_pct", 0.0) or 0.0)
        original_sl_pct = float(getattr(decision, "sl_distance_pct", 0.0) or 0.0)
        tp_pct = original_tp_pct
        sl_pct = original_sl_pct
        if tp_pct <= 0 or sl_pct <= 0:
            return decision, []
        margin = self.storage.margin_usdt()
        leverage = self.storage.leverage()
        notes: list[str] = []
        rejected_sl_fix = False
        rejected_tp_fix = False

        evidence = [
            p for p in profiles
            if int(p.get("samples") or 0) >= 2 or str(p.get("scope") or "") in {"FEATURE", "SYMBOL_DIRECTION", "SYMBOL_STATE"}
        ]
        for p in evidence[:5]:
            cause = self._normalize_cause(str(p.get("last_cause") or ""), p)
            avg_mfe = float(p.get("avg_mfe_pct") or 0.0)
            avg_mae = float(p.get("avg_mae_pct") or 0.0)
            rec_tp = float(p.get("recommended_tp_pct") or 0.0)
            rec_sl = float(p.get("recommended_sl_pct") or 0.0)
            sl_count = int(p.get("sl") or 0)
            samples = int(p.get("samples") or 0)
            if sl_count <= 0:
                continue

            # TP correction is allowed only when the forensic cause says TP was unrealistic
            # or MFE history proves price repeatedly came near TP and reversed.
            if cause == "TP_TOO_FAR_OR_REVERSAL" or (avg_mfe > 0 and avg_mfe < tp_pct * 0.92 and avg_mfe >= tp_pct * 0.45):
                target = rec_tp or avg_mfe * 0.72
                target = max(target, tp_pct * 0.58)
                target = min(tp_pct, target)
                if target < tp_pct * 0.97:
                    if self._valid_tp_sl(target, sl_pct, margin, leverage):
                        old = tp_pct
                        tp_pct = target
                        notes.append(f"TP اصلاح شد: {old*100:.3f}% → {tp_pct*100:.3f}%؛ چون {samples} نمونه نشان داده TP قبلی برای این الگو دور بوده.")
                    else:
                        rejected_tp_fix = True

            # SL correction is allowed only when the forensic cause/MAE history says the stop
            # was too tight. If it would break RR/net edge, report rejected fix without claiming it happened.
            stop_tight_causes = {"STOP_TOO_TIGHT", "SL_TOO_TIGHT", "SL_HIT_AFTER_NOISE_OR_BAD_RANGE", "UNKNOWN_SL_REASON"}
            if cause in stop_tight_causes or (avg_mae >= sl_pct * 0.90 and avg_mfe >= tp_pct * 0.20):
                target = rec_sl or avg_mae * 1.18
                target = max(sl_pct, min(target, sl_pct * 1.35))
                if target > sl_pct * 1.03:
                    if self._valid_tp_sl(tp_pct, target, margin, leverage):
                        old = sl_pct
                        sl_pct = target
                        notes.append(f"SL اصلاح شد: {old*100:.3f}% → {sl_pct*100:.3f}%؛ داده‌های قبلی MAE/نویز بیشتری برای این الگو نشان داده‌اند.")
                    else:
                        rejected_sl_fix = True

        tp_changed = abs(tp_pct - original_tp_pct) > 1e-9
        sl_changed = abs(sl_pct - original_sl_pct) > 1e-9
        if rejected_sl_fix and not sl_changed:
            notes.append("SL کوچک تشخیص داده شد، اما بازترکردن آن RR یا سود خالص را خراب می‌کرد؛ پس فقط Real محتاط/بسته شد.")
        if rejected_tp_fix and not tp_changed:
            notes.append("TP دور تشخیص داده شد، اما اصلاح TP بعد از کارمزد ارزش کافی نمی‌داد؛ پس Real بسته و فقط Normal کنترل‌شده یا Reject مجاز است.")

        if not notes:
            return decision, []
        risk_reward = tp_pct / max(sl_pct, 0.000001)
        net_profit = net_profit_for_move(margin, leverage, tp_pct)
        if direction == "LONG":
            tp = round_price(entry * (1.0 + tp_pct), PRICE_TICK_DECIMALS)
            sl = round_price(entry * (1.0 - sl_pct), PRICE_TICK_DECIMALS)
        else:
            tp = round_price(entry * (1.0 - tp_pct), PRICE_TICK_DECIMALS)
            sl = round_price(entry * (1.0 + sl_pct), PRICE_TICK_DECIMALS)
        return replace(
            decision,
            tp=tp,
            sl=sl,
            tp_distance_pct=tp_pct,
            sl_distance_pct=sl_pct,
            risk_reward=risk_reward,
            estimated_net_profit_usdt=net_profit,
        ), notes

    @staticmethod
    def _valid_tp_sl(tp_pct: float, sl_pct: float, margin: float, leverage: int) -> bool:
        if tp_pct <= 0 or sl_pct <= 0:
            return False
        if tp_pct / max(sl_pct, 0.000001) < MIN_RISK_REWARD:
            return False
        if net_profit_for_move(margin, leverage, tp_pct) < MIN_NET_PROFIT_USDT:
            return False
        return True

    @classmethod
    def _display_cause(cls, profile: dict[str, Any]) -> str:
        return cls._normalize_cause(str(profile.get("last_cause") or "LEARNED_PATTERN"), profile)

    @staticmethod
    def _normalize_cause(cause: str, profile: dict[str, Any]) -> str:
        cause = (cause or "LEARNED_PATTERN").upper()
        tp = int(profile.get("tp") or 0)
        sl = int(profile.get("sl") or 0)
        risk = int(profile.get("risk_score") or 0)
        failures = int(profile.get("failures") or 0)
        successes = int(profile.get("successes") or 0)
        if cause == "TP_OK" and (sl > tp or risk >= 40 or failures > successes):
            return "TREATMENT_FAILED_OR_HIGH_RISK_PATTERN"
        if cause in {"SL_TOO_TIGHT", "SL_HIT_AFTER_NOISE_OR_BAD_RANGE"}:
            return "STOP_TOO_TIGHT"
        return cause

    @classmethod
    def _reason_text(cls, profile: dict[str, Any], notes: list[str]) -> str:
        parts = []
        if notes:
            parts.extend(notes)
        exact_samples = int(profile.get("samples") or 0) if str(profile.get("scope") or "") == "FEATURE" else 0
        cause = cls._display_cause(profile)
        parts.append(
            f"حافظه مشابه: {profile.get('scope','-')} | نمونه دقیق بازه {exact_samples} | "
            f"TP {profile.get('tp',0)}/SL {profile.get('sl',0)} از {profile.get('samples',0)} نمونه | "
            f"تست درمان {profile.get('tests',0)} بار، موفق {profile.get('successes',0)}، شکست {profile.get('failures',0)} | "
            f"سطح {profile.get('treatment_level',0)} | علت غالب {cause} | درمان {profile.get('fix_policy','-')} | اقدام {profile.get('recommended_action','ALLOW')}"
        )
        return " ".join(parts)[:900]
