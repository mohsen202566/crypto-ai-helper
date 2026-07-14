"""تشخیص احتمالاتی رفتار بازار؛ در شروع نرم و بدون قفل‌های خشک."""
from __future__ import annotations

from typing import Any

from models import Decision, FeatureSnapshot
from utils import clamp

BEHAVIOR_VERSION = "behavior-high-tf-v2"


class BehaviorEngine:
    BEHAVIORS = (
        "TREND_START",
        "TREND_CONTINUATION",
        "PULLBACK",
        "COMPRESSION",
        "TRUE_BREAKOUT",
        "FALSE_BREAKOUT",
        "REVERSAL",
        "RANGE",
        "SHOCK",
        "UNKNOWN",
    )

    @staticmethod
    def _normalize(raw: dict[str, float]) -> dict[str, float]:
        vals = {k: max(0.0, float(v)) for k, v in raw.items()}
        total = sum(vals.values())
        if total <= 0:
            return {k: (1.0 if k == "UNKNOWN" else 0.0) for k in vals}
        return {k: v / total for k, v in vals.items()}

    def classify(self, snapshot: FeatureSnapshot, side: str, profile: dict[str, Any] | None = None) -> tuple[str, dict[str, float], list[str], list[str]]:
        s = snapshot.raw["selected"]
        direction = snapshot.long_scores if side == "LONG" else snapshot.short_scores
        dir_score = direction["weighted"]
        adx = float(s["adx_dmi"]["adx"])
        rel_vol = float(s["relative_volume"]["ratio"])
        natr_state = float(s["atr_natr"]["state"])
        efficiency = float(s["efficiency"])
        rsi_delta = abs(float(s["rsi"]["delta"]))
        hist_delta = abs(float(s["macd"]["hist_delta"])) / max(abs(float(s["macd"]["hist"])), s["last"] * 1e-7)
        last = float(s["last"])
        high = float(s["recent_high"])
        low = float(s["recent_low"])
        edge_distance = min(abs(last - high), abs(last - low)) / max(last, 1e-9)
        trend_slope = abs(float(s["trend_slope"]))

        raw = {
            "TREND_START": max(0, (dir_score - 50) * 1.2 + rsi_delta * 1.5 + min(hist_delta, 3) * 7 + (rel_vol - 1) * 15),
            "TREND_CONTINUATION": max(0, (dir_score - 48) + adx * 0.65 + efficiency * 28),
            "PULLBACK": max(0, adx * 0.45 + (1.2 - rel_vol) * 12 + (dir_score - 45) * 0.7),
            "COMPRESSION": max(0, (0.3 - max(natr_state, -0.5)) * 28 + (24 - adx) * 0.7),
            "TRUE_BREAKOUT": max(0, (dir_score - 50) + (rel_vol - 1) * 25 + (0.002 - edge_distance) * 6000),
            "FALSE_BREAKOUT": max(0, (1.05 - rel_vol) * 12 + (0.0015 - edge_distance) * 4000 + (45 - dir_score) * 0.5),
            "REVERSAL": max(0, (55 - dir_score) * 0.5 + rsi_delta + (22 - adx) * 0.3),
            "RANGE": max(0, (28 - adx) * 1.2 + (0.45 - efficiency) * 30 - trend_slope * 10000),
            "SHOCK": max(0, (natr_state - 1.0) * 25 + (rel_vol - 2.0) * 20),
            "UNKNOWN": 12.0,
        }
        cfg = (profile or {}).get("config") or {}
        behavior_bias = cfg.get("behavior_bias") or {}
        for key in raw:
            raw[key] *= clamp(float(behavior_bias.get(key, 1.0)), 0.35, 2.5)
        probs = self._normalize(raw)
        behavior = max(probs, key=probs.get)
        reasons: list[str] = []
        risks: list[str] = []
        if dir_score >= 60:
            reasons.append(f"هم‌گرایی نرم ابزارهای جهت: {dir_score:.1f}")
        if adx >= 24:
            reasons.append(f"قدرت روند مناسب ADX: {adx:.1f}")
        if rel_vol >= 1.15:
            reasons.append(f"حجم نسبی فعال: {rel_vol:.2f}x")
        if efficiency >= 0.45:
            reasons.append(f"حرکت مفید نسبت به نویز: {efficiency:.2f}")
        if snapshot.data_quality < 80:
            risks.append(f"کیفیت داده متوسط: {snapshot.data_quality:.0f}")
        if probs.get("UNKNOWN", 0) > 0.25:
            risks.append("رفتار بازار هنوز کاملاً تثبیت نشده")
        if probs.get("SHOCK", 0) > 0.25:
            risks.append("نوسان یا حجم خارج از رفتار معمول")
        if rel_vol < 0.75:
            risks.append("حجم نسبی ضعیف")
        return behavior, probs, reasons, risks

    def decide(self, snapshot: FeatureSnapshot, profile: dict[str, Any], forced_side: str | None = None) -> Decision:
        long = snapshot.long_scores["weighted"]
        short = snapshot.short_scores["weighted"]
        side = forced_side or ("LONG" if long >= short else "SHORT")
        scores = snapshot.long_scores if side == "LONG" else snapshot.short_scores
        selected = snapshot.raw["selected"]
        entry_selected = snapshot.raw.get("entry_selected") or selected
        behavior, probs, reasons, risks = self.classify(snapshot, side, profile)

        direction_score = float(scores["weighted"])
        adx = float(selected["adx_dmi"]["adx"])
        rel_vol = float(selected["relative_volume"]["ratio"])
        efficiency = float(selected["efficiency"])
        entry_rel_vol = float(entry_selected["relative_volume"]["ratio"])
        entry_efficiency = float(entry_selected["efficiency"])
        strength = clamp(0.45 * direction_score + 0.30 * clamp(adx * 2, 0, 100) + 0.25 * clamp(rel_vol * 45, 0, 100), 0, 100)
        entry_side = "long" if side == "LONG" else "short"
        entry = clamp(
            0.30 * direction_score
            + 0.30 * float(entry_selected["market_structure"][entry_side])
            + 0.20 * float(entry_selected["relative_volume"][entry_side])
            + 0.20 * entry_efficiency * 100,
            0, 100,
        )
        regime_conf = clamp(max(probs.values()) * 100, 0, 100)
        noise = float(selected["noise"])
        natr = float(selected["atr_natr"]["natr"])
        noise_risk = clamp(35 + noise / max(natr, 1e-7) * 22 - efficiency * 25, 0, 100)
        execution_quality = clamp(snapshot.data_quality - max(0.0, natr - 0.03) * 800, 0, 100)
        entry_type_map = {
            "TREND_START": "EARLY_MOVEMENT",
            "TREND_CONTINUATION": "PULLBACK_CONTINUATION",
            "PULLBACK": "PULLBACK_CONTINUATION",
            "COMPRESSION": "DIRECT_BREAKOUT",
            "TRUE_BREAKOUT": "BREAKOUT_RETEST",
            "FALSE_BREAKOUT": "FAILED_BREAKOUT_REVERSAL",
            "REVERSAL": "LIQUIDITY_SWEEP_REVERSAL",
            "RANGE": "RANGE_EDGE_REVERSAL",
            "SHOCK": "DIRECT_BREAKOUT",
            "UNKNOWN": "FLEXIBLE",
        }
        entry_type = entry_type_map.get(behavior, "FLEXIBLE")
        entry_bias = float(((profile.get("config") or {}).get("entry_type_bias") or {}).get(entry_type, 1.0))
        final = clamp(
            0.25 * direction_score
            + 0.20 * strength
            + 0.25 * entry
            + 0.10 * regime_conf
            + 0.10 * (100 - noise_risk)
            + 0.10 * execution_quality
            + (clamp(entry_bias, 0.35, 2.5) - 1.0) * 8.0,
            0,
            100,
        )
        return Decision(
            canonical=snapshot.canonical,
            side=side,
            direction_score=direction_score,
            strength_score=strength,
            entry_quality=entry,
            regime_confidence=regime_conf,
            noise_risk=noise_risk,
            execution_quality=execution_quality,
            final_score=final,
            behavior=behavior,
            behavior_probabilities=probs,
            entry_type=entry_type,
            trade_timeframe=snapshot.trade_timeframe,
            entry_timeframe=snapshot.entry_timeframe,
            estimated_hold_minutes=snapshot.estimated_hold_minutes,
            reasons=reasons,
            risks=risks,
        )
