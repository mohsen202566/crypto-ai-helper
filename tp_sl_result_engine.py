from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TpSlResult:
    status: str
    result_source: str
    description: str


class TpSlResultEngine:
    def classify(self, *, status: str, signal_type: str, real_status: str, real_pnl_available: bool) -> TpSlResult:
        if status == "EXIT":
            source = "toobit_real" if signal_type == "real" and real_status == "opened" else "normal"
            return TpSlResult(status=status, result_source=source, description="خروج هوشمند AI")
        if signal_type == "real" and real_status == "opened" and real_pnl_available:
            return TpSlResult(status=status, result_source="toobit_real", description="TP/SL واقعی توبیت")
        if signal_type == "real" and real_status == "opened":
            return TpSlResult(status=status, result_source="normal_on_real", description="TP/SL عادی روی سیگنال واقعی")
        if signal_type == "normal":
            return TpSlResult(status=status, result_source="normal", description="TP/SL عادی ربات")
        return TpSlResult(status=status, result_source="ghost_or_failed", description="نتیجه Ghost/Failed")
