from __future__ import annotations

import importlib
import py_compile
from pathlib import Path

ROOT = Path(__file__).resolve().parent

MODULES = [
    "config","data_store","diagnostics","ai_memory","coin_learning","coin_risk","coin_rotation","sr_learning",
    "analysis","trend_analysis","market_structure","market_sentiment","ai_movement_hunter","ghost_signals",
    "slot_manager","signal_tracker","tobit_client","toobit_safety","real_trade_manager","real_position_sync",
    "market_scanner","scanner","users","coins_fa","reply_manager","recovery_manager","daily_report",
    "command_registry","integration_status","bot",
]

def main() -> int:
    failed = []
    for p in sorted(ROOT.glob("*.py")):
        try:
            py_compile.compile(str(p), doraise=True)
        except Exception as e:
            failed.append(f"compile:{p.name}:{e}")
    for m in MODULES:
        try:
            importlib.import_module(m)
        except Exception as e:
            failed.append(f"import:{m}:{type(e).__name__}:{e}")
    if failed:
        print("FAILED")
        for f in failed:
            print(f)
        return 1
    print("OK: compile/import audit passed")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
