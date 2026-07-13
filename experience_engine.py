from __future__ import annotations


class ExperienceEngine:
    def analyze(self, signal, result):
        mfe = float(result.get("mfe_r", 0) or 0)
        mae = float(result.get("mae_r", 0) or 0)
        outcome = result.get("outcome")
        adverse = abs(mae)
        if outcome == "TP":
            cause = "CLEAN_WIN" if adverse < 0.5 else "HIGH_MAE_WIN"
            direction = "DIRECTION_CORRECT"
        elif outcome == "EXPIRED":
            cause = "NO_RESOLUTION_WITHIN_TIME_LIMIT"
            direction = "DIRECTION_AMBIGUOUS"
        elif mfe < 0.2:
            cause = "DIRECTION_ERROR"
            direction = "DIRECTION_WRONG"
        elif result.get("post_sl_reached_tp"):
            cause = "ENTRY_TOO_EARLY_OR_STOP_TOO_TIGHT"
            direction = "DIRECTION_CORRECT"
        elif float(signal.get("freshness_score", 100) or 100) < 50:
            cause = "ENTRY_TOO_LATE"
            direction = "DIRECTION_CORRECT"
        else:
            cause = "NO_FOLLOW_THROUGH"
            direction = "DIRECTION_AMBIGUOUS"
        return {
            "signal_id": signal["id"],
            "outcome": outcome,
            "primary_cause": cause,
            "direction_label": direction,
            "mfe_r": mfe,
            "mae_r": mae,
            "net_pnl": result.get("net_pnl", 0),
            "model_version": signal.get("model_version", "v1.0"),
        }
