from __future__ import annotations

from dataclasses import replace
from typing import Any

from config import MIN_NET_PROFIT_USDT, MIN_RISK_REWARD, PRICE_TICK_DECIMALS
from guard_types import GuardVerdict
from guard_utils import half_hour_bucket, session_info, weekday_key
from utils import net_profit_for_move, round_price


class AdaptiveFixEngine:
    """Learns from every closed signal and applies a lightweight treatment layer.

    This engine does not change the original AI analysis. It looks at the AI decision,
    searches learned profiles from previous TP/SL results, and applies a treatment only
    when it can make the outgoing signal safer: Real block, caution note, or learned
    TP/SL refinement. Temporary full blocks remain only for explicit safety windows
    such as news/session/cooldown guards.
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
        profiles.sort(key=lambda p: (int(p.get("risk_score") or 0), int(p.get("sl") or 0), int(p.get("samples") or 0)), reverse=True)
        strongest = profiles[0]
        adjusted, notes = self._apply_learned_price_treatment(decision, profiles)
        action = str(strongest.get("recommended_action") or "ALLOW").upper()
        risk = int(strongest.get("risk_score") or 0)
        cause = str(strongest.get("last_cause") or "LEARNED_PATTERN")

        if notes or action != "ALLOW":
            reason_note = self._reason_text(strongest, notes)
            adjusted = replace(adjusted, reason=(adjusted.reason or "") + "\n\n🧠 درمان یادگیری: " + reason_note)
        if action == "WATCH_ONLY":
            adjusted = replace(adjusted, real_allowed=False, signal_type_hint="watch")
            return adjusted, GuardVerdict("REAL_BLOCK", "ADAPTIVE_FIX", f"درمان‌های قبلی برای الگوی مشابه هنوز جواب نداده‌اند ({cause}, level={strongest.get('treatment_level',0)}, risk={risk}). سیگنال حذف نشد؛ فقط Watch و ثبت نتیجه.", 0, {"profile": strongest, "notes": notes})
        if action in {"CAUTION", "REAL_BLOCK"}:
            adjusted = replace(adjusted, real_allowed=False, signal_type_hint="normal")
            level = "REAL_BLOCK" if action == "REAL_BLOCK" or risk >= 45 else "CAUTION"
            return adjusted, GuardVerdict(level, "ADAPTIVE_FIX", f"الگوی مشابه قبلاً ریسک داده ({cause}, risk={risk}). سیگنال حذف نشد؛ فقط محتاط/Normal شد.", 0, {"profile": strongest, "notes": notes})
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
                # Drop symbol and exact session so learned indicator weakness transfers across time/symbol when useful.
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
        tp_pct = float(getattr(decision, "tp_distance_pct", 0.0) or 0.0)
        sl_pct = float(getattr(decision, "sl_distance_pct", 0.0) or 0.0)
        if tp_pct <= 0 or sl_pct <= 0:
            return decision, []
        margin = self.storage.margin_usdt()
        leverage = self.storage.leverage()
        notes: list[str] = []

        # Every stop matters. One exact-feature/symbol SL can test a small TP/SL treatment;
        # broader/global profiles still need more evidence.
        evidence = [p for p in profiles if int(p.get("samples") or 0) >= 2 or str(p.get("scope") or "") in {"FEATURE", "SYMBOL_DIRECTION", "SYMBOL_STATE"}]
        for p in evidence[:4]:
            cause = str(p.get("last_cause") or "")
            avg_mfe = float(p.get("avg_mfe_pct") or 0.0)
            avg_mae = float(p.get("avg_mae_pct") or 0.0)
            rec_tp = float(p.get("recommended_tp_pct") or 0.0)
            rec_sl = float(p.get("recommended_sl_pct") or 0.0)
            sl_count = int(p.get("sl") or 0)
            samples = int(p.get("samples") or 0)
            if sl_count <= 0:
                continue

            # If many losses reached near TP before reversing, TP was too far for this pattern.
            if cause == "TP_TOO_FAR_OR_REVERSAL" or (avg_mfe > 0 and avg_mfe < tp_pct * 0.92 and avg_mfe >= tp_pct * 0.45):
                target = rec_tp or avg_mfe * 0.72
                target = max(target, tp_pct * 0.58)
                target = min(tp_pct, target)
                if self._valid_tp_sl(target, sl_pct, margin, leverage):
                    if target < tp_pct * 0.97:
                        tp_pct = target
                        notes.append(f"TP برای این الگو از داده‌های قبلی کوتاه‌تر شد؛ چون {samples} نمونه نشان داده TP قبلی دور بوده.")

            # If SL hits after normal noise and MAE sits around/beyond SL, stop was too tight for this symbol/pattern.
            if cause in {"SL_HIT_AFTER_NOISE_OR_BAD_RANGE", "SL_TOO_TIGHT", "UNKNOWN_SL_REASON"} or (avg_mae >= sl_pct * 0.90 and avg_mfe >= tp_pct * 0.20):
                target = rec_sl or avg_mae * 1.18
                target = max(sl_pct, min(target, sl_pct * 1.35))
                if self._valid_tp_sl(tp_pct, target, margin, leverage):
                    if target > sl_pct * 1.03:
                        sl_pct = target
                        notes.append(f"SL برای این الگو کمی بازتر شد؛ چون داده‌های قبلی نویز/MAE بیشتری نشان داده‌اند.")
                else:
                    notes.append("AI فهمید SL برای این الگو کوچک است، ولی بازتر کردنش RR/سود خالص را خراب می‌کرد؛ پس Real محتاط شد.")

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
        return replace(decision, tp=tp, sl=sl, tp_distance_pct=tp_pct, sl_distance_pct=sl_pct, risk_reward=risk_reward, estimated_net_profit_usdt=net_profit), notes

    @staticmethod
    def _valid_tp_sl(tp_pct: float, sl_pct: float, margin: float, leverage: int) -> bool:
        if tp_pct <= 0 or sl_pct <= 0:
            return False
        if tp_pct / max(sl_pct, 0.000001) < MIN_RISK_REWARD:
            return False
        if net_profit_for_move(margin, leverage, tp_pct) < MIN_NET_PROFIT_USDT:
            return False
        return True

    @staticmethod
    def _reason_text(profile: dict[str, Any], notes: list[str]) -> str:
        parts = []
        if notes:
            parts.extend(notes)
        parts.append(
            f"حافظه مشابه: {profile.get('scope','-')} | TP {profile.get('tp',0)}/SL {profile.get('sl',0)} از {profile.get('samples',0)} نمونه | تست درمان {profile.get('tests',0)} بار، موفق {profile.get('successes',0)}، شکست {profile.get('failures',0)} | سطح {profile.get('treatment_level',0)} | علت غالب {profile.get('last_cause','-')} | درمان {profile.get('fix_policy','-')} | اقدام {profile.get('recommended_action','ALLOW')}"
        )
        return " ".join(parts)[:900]
