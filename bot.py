from __future__ import annotations

"""
26 - bot.py

Telegram command router and orchestration layer for the locked Movement Hunter bot.

Responsibilities:
- Telegram command routing only.
- Preserve user-facing commands and Persian output.
- Run the REAL/GHOST/REJECT pipeline by calling the proper modules.
- Send reports produced by result_reporter.py.
- Start/stop real trading setting through runtime data store.
- Run auto scan loop and position monitor loop.
- Never contain Paper mode.
- Never contain Setup flow.
- Never make AI decisions itself.
- Never call Toobit directly except through tobit_client.py / real_trade_manager.py / position_monitor.py.

Architecture:
market_data -> analysis_layers -> analysis_engine -> movement_hunter -> trap_engine
-> state_engine -> confidence_engine -> correlation_engine -> coin_learning
-> movement_memory -> movement_predictor -> ai_decision_engine -> tp_sl_engine
-> ghost_manager OR real_trade_manager
-> position_monitor -> result_reporter -> Telegram
"""

import asyncio
import logging
import os
import re
import time
from dataclasses import asdict, dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

try:
    from telegram import Update
    from telegram.constants import ParseMode
    from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
except Exception:  # allows compile/test without telegram package installed
    Update = Any  # type: ignore
    ContextTypes = Any  # type: ignore
    Application = None  # type: ignore
    CommandHandler = None  # type: ignore
    MessageHandler = None  # type: ignore
    filters = None  # type: ignore
    ParseMode = None  # type: ignore

from config import SETTINGS
from data_store import store, save_error
from market_data import get_multi_timeframe_snapshot, get_latest_price
from analysis_engine import analyze_symbol, analyze_multi_timeframe, AnalysisCandidate
from movement_hunter import analyze_movement, MovementHunterResult
from trap_engine import analyze_trap, TrapResult
from state_engine import analyze_state, StateResult
from confidence_engine import analyze_confidence, ConfidenceResult
from correlation_engine import analyze_correlation, CorrelationResult
from coin_learning import summarize_candidate_learning, LearningSummary
from movement_memory import summarize_movement_candidate
from movement_predictor import predict_movement, MovementPredictionResult
from meta_learning import get_meta_learning_summary
from ai_decision_engine import decide, AIDecision, DECISION_REAL, DECISION_GHOST, DECISION_REJECT
from tp_sl_engine import build_tp_sl_plan, apply_tp_sl_to_decision, TPSLPlan
from ghost_manager import create_ghost, monitor_ghost, ghost_stats
from real_trade_manager import open_real_position, RealTradeOpenResult
from tobit_client import get_client
from position_monitor import monitor_all_positions
from result_reporter import reporter, ReportPayload, format_error_report
from stats_manager import record_decision, record_position_event, record_ghost_result, stats_report, detailed_stats_report, clear_stats, manager as stats_manager_instance


LOGGER = logging.getLogger("movement_hunter_bot")

DIRECTION_LONG = "LONG"
DIRECTION_SHORT = "SHORT"

CMD_START = "/start"
CMD_ID = "/id"

PERSIAN_TRUE = {"روشن", "فعال", "on", "ON", "true", "True"}
PERSIAN_FALSE = {"خاموش", "غیرفعال", "off", "OFF", "false", "False"}


@dataclass(frozen=True)
class PipelineResult:
    candidate: AnalysisCandidate
    movement: MovementHunterResult
    trap: TrapResult
    state: StateResult
    confidence: ConfidenceResult
    correlation: CorrelationResult
    learning: LearningSummary
    prediction: MovementPredictionResult
    decision: AIDecision
    plan: Optional[TPSLPlan]
    trade_result: Optional[RealTradeOpenResult] = None
    signal_report: Optional[ReportPayload] = None
    trade_report: Optional[ReportPayload] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "candidate": self.candidate.to_dict(),
            "movement": self.movement.to_dict(),
            "trap": self.trap.to_dict(),
            "state": self.state.to_dict(),
            "confidence": self.confidence.to_dict(),
            "correlation": self.correlation.to_dict(),
            "learning": self.learning.to_dict(),
            "prediction": self.prediction.to_dict(),
            "decision": self.decision.to_dict(),
            "plan": self.plan.to_dict() if self.plan else None,
            "trade_result": self.trade_result.to_dict() if self.trade_result else None,
        }


def now_ts() -> int:
    return int(time.time())


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except Exception:
        return default


def get_runtime_settings() -> Dict[str, Any]:
    """
    Persistent runtime settings.

    These values are command-controlled and must not be only visual.
    real_trade_manager reads the same runtime_settings section, so changing
    margin/leverage/max_positions here changes real order sizing and safety.
    """
    section = store().section("runtime_settings")

    defaults = {
        "real_trading_enabled": bool(getattr(SETTINGS.trading, "enabled", False)),
        "auto_signal_enabled": bool(os.getenv("AUTO_SIGNAL_ENABLED", "true").lower() in {"1", "true", "yes", "on"}),
        "scan_interval_seconds": safe_int(getattr(SETTINGS.monitor, "scan_interval_seconds", 180), 180),
        "margin_usdt": safe_float(getattr(SETTINGS.trading, "margin_usdt", 5.0), 5.0),
        "trade_margin_usdt": safe_float(getattr(SETTINGS.trading, "margin_usdt", 5.0), 5.0),
        "leverage": safe_int(getattr(SETTINGS.trading, "leverage", 10), 10),
        "max_positions": safe_int(getattr(SETTINGS.trading, "max_positions", 5), 5),
        "daily_loss_lock_enabled": True,
        "daily_loss_locked_until": 0,
        "last_scan_ts": 0,
    }
    changed = False
    for key, value in defaults.items():
        if key not in section:
            section[key] = value
            changed = True
    if changed:
        save_runtime_settings(section)
    return section


def save_runtime_settings(values: Dict[str, Any]) -> None:
    def mutate(section: Dict[str, Any]) -> Dict[str, Any]:
        section.update(values)
        section["updated_at"] = now_ts()
        return section

    try:
        store().update_section("runtime_settings", mutate, save=True)
    except AttributeError:
        section = store().section_ref("runtime_settings")  # type: ignore[attr-defined]
        section.update(values)
        section["updated_at"] = now_ts()
        store().save()


def real_trading_enabled() -> bool:
    return bool(get_runtime_settings().get("real_trading_enabled", False))


def auto_signal_enabled() -> bool:
    return bool(get_runtime_settings().get("auto_signal_enabled", True))


def runtime_margin_usdt() -> float:
    return safe_float(get_runtime_settings().get("margin_usdt", getattr(SETTINGS.trading, "margin_usdt", 0)), 0.0)


def runtime_leverage() -> int:
    return safe_int(get_runtime_settings().get("leverage", getattr(SETTINGS.trading, "leverage", 1)), 1)


def runtime_max_positions() -> int:
    return safe_int(get_runtime_settings().get("max_positions", getattr(SETTINGS.trading, "max_positions", 1)), 1)


def owner_id() -> int:
    return safe_int(os.getenv("OWNER_ID", getattr(SETTINGS.telegram, "owner_id", 0)), 0)


def allowed_user_ids() -> set[int]:
    ids = {owner_id()} if owner_id() else set()
    try:
        configured = getattr(SETTINGS.telegram, "allowed_user_ids", [])
        for item in configured:
            ids.add(safe_int(item))
    except Exception:
        pass
    runtime = store().section("allowed_users")
    for item in runtime.values():
        ids.add(safe_int(item))
    return {i for i in ids if i > 0}


def is_allowed(user_id: int) -> bool:
    allowed = allowed_user_ids()
    return not allowed or int(user_id) in allowed


def extract_symbol(text: str) -> str:
    t = str(text or "").upper()
    t = re.sub(r"[^\w\s]", " ", t)
    words = [w for w in t.split() if w]
    for w in reversed(words):
        if w in {"تحلیل", "سیگنال", "بازار", "بررسی", "LONG", "SHORT"}:
            continue
        if w.endswith("USDT"):
            return w
        if 2 <= len(w) <= 12 and re.match(r"^[A-Z0-9]+$", w):
            return w + "USDT"
    return "BTCUSDT"


async def send_payload(update: Update, payload: ReportPayload) -> None:
    if not payload.should_send or not payload.text:
        return
    message = getattr(update, "effective_message", None)
    if message is None:
        return
    kwargs: Dict[str, Any] = {}
    if payload.reply_to_message_id:
        kwargs["reply_to_message_id"] = payload.reply_to_message_id
    try:
        await message.reply_text(payload.text, **kwargs)
    except TypeError:
        await message.reply_text(payload.text)
    except Exception as exc:
        LOGGER.exception("send_payload failed: %s", exc)


async def send_text(update: Update, text: str) -> None:
    message = getattr(update, "effective_message", None)
    if message is not None:
        await message.reply_text(text)


class PipelineOrchestrator:
    """
    Runs the full analysis/decision/TP-SL/trade/ghost path.

    The router calls this; it does not contain Telegram logic.
    """

    def __init__(self):
        self.client = get_client()

    def build_candidate(self, symbol: str, timeframe: str = "5m") -> AnalysisCandidate:
        mtf = get_multi_timeframe_snapshot(symbol, timeframes=[timeframe], limit=120)
        snapshot = mtf.snapshots[timeframe]
        candles = [c.to_dict() for c in snapshot.candles]
        return analyze_symbol(symbol=symbol, timeframe=timeframe, candles=candles, market_context=None)

    def run_pipeline(
        self,
        symbol: str,
        timeframe: str = "5m",
        open_positions: Optional[Iterable[Any]] = None,
        execute_real: bool = True,
    ) -> PipelineResult:
        candidate = self.build_candidate(symbol, timeframe=timeframe)

        movement = analyze_movement(candidate)
        trap = analyze_trap(candidate, movement=movement)
        state = analyze_state(candidate, movement=movement, trap=trap)
        learning = summarize_candidate_learning(candidate, movement=movement, trap=trap, state=state)
        confidence = analyze_confidence(candidate, movement=movement, trap=trap, state=state, learning_summary=learning.to_dict())
        correlation = analyze_correlation(candidate, open_positions=open_positions, market_context=candidate.market_context)
        movement_summary = summarize_movement_candidate(candidate, movement=movement, trap=trap, state=state)
        prediction = predict_movement(
            candidate=candidate,
            movement=movement,
            trap=trap,
            state=state,
            confidence=confidence,
            movement_summary=movement_summary,
        )

        meta = get_meta_learning_summary()
        decision = decide(
            candidate=candidate,
            movement=movement,
            trap=trap,
            state=state,
            confidence=confidence,
            correlation=correlation,
            prediction=prediction,
            learning=learning,
            meta=meta,
        )

        plan: Optional[TPSLPlan] = None
        trade_result: Optional[RealTradeOpenResult] = None
        signal_report: Optional[ReportPayload] = None
        trade_report: Optional[ReportPayload] = None

        if decision.decision_type in {DECISION_REAL, DECISION_GHOST}:
            plan = build_tp_sl_plan(
                decision=decision,
                candidate=candidate,
                movement=movement,
                trap=trap,
                state=state,
                confidence=confidence,
                prediction=prediction,
                learning=learning,
            )
            decision = apply_tp_sl_to_decision(decision, plan)

        record_decision(decision)

        if decision.decision_type == DECISION_GHOST and plan:
            create_ghost(
                decision_id=decision.decision_id,
                candidate=candidate,
                entry=plan.entry,
                tp1=plan.tp1,
                tp2=plan.tp2,
                sl=plan.sl,
                movement=movement,
                trap=trap,
                state=state,
                confidence=confidence,
                meta={"decision": decision.to_dict(), "plan": plan.to_dict()},
            )

        if decision.decision_type == DECISION_REAL and plan and execute_real and real_trading_enabled():
            trade_result = open_real_position(
                self.client,
                decision,
                plan,
                analysis_meta={
                    "candidate": candidate.to_dict(),
                    "movement": movement.to_dict(),
                    "trap": trap.to_dict(),
                    "state": state.to_dict(),
                    "confidence": confidence.to_dict(),
                    "correlation": correlation.to_dict(),
                    "prediction": prediction.to_dict(),
                    "learning": learning.to_dict(),
                    "ai_decision": decision.to_dict(),
                },
            )

        if plan:
            signal_report = reporter().signal_report(decision, plan)
        if trade_result:
            trade_report = reporter().trade_open_report(trade_result)

        return PipelineResult(
            candidate=candidate,
            movement=movement,
            trap=trap,
            state=state,
            confidence=confidence,
            correlation=correlation,
            learning=learning,
            prediction=prediction,
            decision=decision,
            plan=plan,
            trade_result=trade_result,
            signal_report=signal_report,
            trade_report=trade_report,
        )


_default_orchestrator: Optional[PipelineOrchestrator] = None


def orchestrator() -> PipelineOrchestrator:
    global _default_orchestrator
    if _default_orchestrator is None:
        _default_orchestrator = PipelineOrchestrator()
    return _default_orchestrator


async def require_access(update: Update) -> bool:
    user = getattr(update, "effective_user", None)
    uid = int(getattr(user, "id", 0) or 0)
    if is_allowed(uid):
        return True
    await send_text(update, "⛔️ دسترسی نداری.")
    return False


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_access(update):
        return
    text = (
        "🤖 ربات Movement Hunter فعال است\n"
        "معماری: REAL / GHOST / REJECT\n"
        "Paper و Setup در این نسخه وجود ندارد.\n\n"
        "دستورات:\n"
        "تحلیل BTC\n"
        "سیگنال BTC\n"
        "بررسی بازار\n"
        "وضعیت / وضعیت ترید\n"
        "ترید فعال / ترید خاموش\n"
        "ترید دلار 10 / سرمایه ترید 10\n"
        "ترید لوریج 10\n"
        "حداکثر پوزیشن 3\n"
        "قفل ضرر خاموش\n"
        "آمار / آمار 7 روز / آمار کل\n"
        "آمار هوشمند\n"
        "حذف آمار"
    )
    await send_text(update, text)


async def id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = getattr(update, "effective_user", None)
    await send_text(update, f"ID: {getattr(user, 'id', 0)}")


async def trade_status(update: Update) -> None:
    settings = get_runtime_settings()
    lock_enabled = bool(settings.get("daily_loss_lock_enabled", True))
    locked_until = safe_int(settings.get("daily_loss_locked_until", 0), 0)
    lock_state = "خاموش" if not lock_enabled else ("فعال" if locked_until > now_ts() else "آماده")
    text = (
        "⚙️ وضعیت ترید\n"
        f"ترید واقعی: {'روشن ✅' if real_trading_enabled() else 'خاموش ❌'}\n"
        f"سیگنال خودکار: {'روشن ✅' if auto_signal_enabled() else 'خاموش ❌'}\n"
        f"قفل ضرر: {lock_state}\n"
        f"اسکن: {settings.get('scan_interval_seconds', 180)} ثانیه\n"
        f"سرمایه هر ترید: {runtime_margin_usdt():.2f}$\n"
        f"لوریج: {runtime_leverage()}x\n"
        f"حداکثر پوزیشن: {runtime_max_positions()}"
    )
    await send_text(update, text)



def parse_first_number(text: str, default: Optional[float] = None) -> Optional[float]:
    m = re.search(r"(\d+(?:[\\.,]\d+)?)", str(text or ""))
    if not m:
        return default
    try:
        return float(m.group(1).replace(",", "."))
    except Exception:
        return default


def percent(part: float, total: float) -> float:
    return (part / total * 100.0) if total else 0.0


def build_ai_status_text() -> str:
    """
    Full AI report for command: هوش مصنوعی

    Includes REAL/GHOST TP1/TP2/SL/AI_EXIT, metadata counts, Movement Hunter
    pump/dump outcome counts, and meta-learning status.
    """
    sm = stats_manager_instance()
    real = sm.summary(days=None, source_type="REAL")
    ghost = sm.summary(days=None, source_type="GHOST")
    all_summary = sm.summary(days=None, source_type="ALL")

    try:
        gs = ghost_stats()
        ghost_total = safe_int(getattr(gs, "total", 0), 0)
        ghost_open = safe_int(getattr(gs, "open_count", 0), 0)
        ghost_closed = safe_int(getattr(gs, "closed_count", 0), 0)
        ghost_tp1 = safe_int(getattr(gs, "tp1_count", 0), 0)
        ghost_tp2 = safe_int(getattr(gs, "tp2_count", 0), 0)
        ghost_ai_exit = safe_int(getattr(gs, "ai_exit_count", 0), 0)
        ghost_sl = safe_int(getattr(gs, "sl_count", 0), 0)
        ghost_wr = safe_float(getattr(gs, "win_rate", 0), 0)
    except Exception:
        ghost_total = ghost.total_events
        ghost_open = 0
        ghost_closed = ghost.closed_count
        ghost_tp1 = ghost.tp1_count
        ghost_tp2 = ghost.tp2_count
        ghost_ai_exit = ghost.ai_exit_count
        ghost_sl = ghost.sl_count
        ghost_wr = ghost.win_rate

    learning_section = store().section("learning")
    movement_section = store().section("movement_memory")
    meta_section = store().section("meta_learning")
    ghosts_section = store().section("ghosts")

    movement_items = [v for v in movement_section.values() if isinstance(v, dict)]
    pump_total = pump_ok = dump_total = dump_ok = 0
    fresh_count = late_count = 0

    for rec in movement_items:
        direction = str(
            rec.get("direction")
            or rec.get("predicted_direction")
            or rec.get("side")
            or rec.get("signal_direction")
            or ""
        ).upper()
        result = str(
            rec.get("result")
            or rec.get("outcome")
            or rec.get("final_result")
            or rec.get("movement_result")
            or ""
        ).upper()
        freshness = str(rec.get("freshness") or rec.get("move_freshness") or rec.get("phase") or "").upper()

        win = result in {"TP1", "TP2", "AI_EXIT", "SUCCESS", "WIN", "MOVE_SUCCESS", "CORRECT"}
        if direction == "LONG":
            pump_total += 1
            if win:
                pump_ok += 1
        elif direction == "SHORT":
            dump_total += 1
            if win:
                dump_ok += 1

        if "FRESH" in freshness or "START" in freshness or "EARLY" in freshness:
            fresh_count += 1
        if "LATE" in freshness or "EXHAUST" in freshness:
            late_count += 1

    meta_samples = len(meta_section)
    learning_samples = len(learning_section)
    movement_samples = len(movement_items)

    try:
        meta = get_meta_learning_summary()
        if hasattr(meta, "to_dict") and callable(meta.to_dict):
            meta_dict = meta.to_dict()
        elif isinstance(meta, dict):
            meta_dict = meta
        else:
            meta_dict = {}
    except Exception:
        meta_dict = {}

    strong_layers = meta_dict.get("strong_layers") or meta_dict.get("best_layers") or []
    weak_layers = meta_dict.get("weak_layers") or meta_dict.get("worst_layers") or []
    if isinstance(strong_layers, dict):
        strong_layers = list(strong_layers.keys())
    if isinstance(weak_layers, dict):
        weak_layers = list(weak_layers.keys())

    real_wins = real.tp1_count + real.tp2_count + real.ai_exit_count
    ghost_wins = ghost_tp1 + ghost_tp2 + ghost_ai_exit
    all_wins = all_summary.tp1_count + all_summary.tp2_count + all_summary.ai_exit_count

    return (
        "🤖 وضعیت هوش مصنوعی\\n\\n"
        "📊 REAL\\n"
        f"کل: {real.total_events} | بسته: {real.closed_count}\\n"
        f"TP1: {real.tp1_count} | TP2: {real.tp2_count} | AI Exit: {real.ai_exit_count} | SL: {real.sl_count}\\n"
        f"WinRate: {real.win_rate:.2f}% | بردها: {real_wins}\\n"
        f"PnL واقعی تاییدشده: {real.confirmed_pnl_usdt:+.4f}$\\n\\n"
        "👻 GHOST\\n"
        f"کل: {ghost_total} | باز: {ghost_open} | بسته: {ghost_closed}\\n"
        f"TP1: {ghost_tp1} | TP2: {ghost_tp2} | AI Exit: {ghost_ai_exit} | SL: {ghost_sl}\\n"
        f"WinRate: {ghost_wr:.2f}% | بردها: {ghost_wins}\\n\\n"
        "🎯 TP/SL کلی\\n"
        f"TP1: {all_summary.tp1_count} | TP2: {all_summary.tp2_count} | AI Exit: {all_summary.ai_exit_count} | SL: {all_summary.sl_count}\\n"
        f"WinRate کل: {all_summary.win_rate:.2f}% | Long WR: {all_summary.long_win_rate:.2f}% | Short WR: {all_summary.short_win_rate:.2f}%\\n\\n"
        "🧠 Movement Hunter\\n"
        f"تشخیص پامپ درست: {pump_ok}/{pump_total} ({percent(pump_ok, pump_total):.1f}%)\\n"
        f"تشخیص دامپ درست: {dump_ok}/{dump_total} ({percent(dump_ok, dump_total):.1f}%)\\n"
        f"Fresh/Early: {fresh_count} | Late/Exhaustion: {late_count}\\n\\n"
        "🧬 Metadata / Learning\\n"
        f"Learning samples: {learning_samples}\\n"
        f"Movement memory: {movement_samples}\\n"
        f"Ghost records: {len(ghosts_section)}\\n"
        f"Meta records: {meta_samples}\\n"
        f"لایه‌های قوی: {', '.join(map(str, strong_layers[:4])) if strong_layers else '-'}\\n"
        f"لایه‌های ضعیف: {', '.join(map(str, weak_layers[:4])) if weak_layers else '-'}"
    )


async def handle_trade_toggle(update: Update, text: str) -> bool:
    normalized = str(text or "").strip().lower()
    compact = normalized.replace("‌", " ").replace("\u200c", " ")
    settings_update: Dict[str, Any] = {}

    if compact in {"ترید", "وضعیت", "وضعیت ترید", "trade", "trade status"}:
        await trade_status(update)
        return True

    if "ترید فعال" in compact or "ترید روشن" in compact or "trade on" in compact:
        settings_update.update({"real_trading_enabled": True})
        save_runtime_settings(settings_update)
        await send_text(update, "✅ ترید واقعی فعال شد. این تغییر واقعی و ذخیره شد.")
        return True

    if "ترید خاموش" in compact or "ترید غیرفعال" in compact or "trade off" in compact:
        settings_update.update({"real_trading_enabled": False})
        save_runtime_settings(settings_update)
        await send_text(update, "❌ ترید واقعی خاموش شد. این تغییر واقعی و ذخیره شد.")
        return True

    if "قفل ضرر خاموش" in compact:
        settings_update.update({
            "daily_loss_lock_enabled": False,
            "daily_loss_locked_until": 0,
            "daily_loss_unlocked_at": now_ts(),
        })
        save_runtime_settings(settings_update)
        await send_text(update, "🔓 قفل ضرر خاموش و آزاد شد.")
        return True

    if "قفل ضرر روشن" in compact:
        settings_update.update({"daily_loss_lock_enabled": True})
        save_runtime_settings(settings_update)
        await send_text(update, "🔒 قفل ضرر روشن شد.")
        return True

    if compact.startswith("ترید دلار") or compact.startswith("سرمایه ترید"):
        value = parse_first_number(compact)
        if value is None or value <= 0:
            await send_text(update, "❌ مقدار سرمایه ترید نامعتبر است. مثال: سرمایه ترید 10")
            return True
        value = max(1.0, min(float(value), 1_000_000.0))
        settings_update.update({"margin_usdt": value, "trade_margin_usdt": value})
        save_runtime_settings(settings_update)
        await send_text(update, f"✅ سرمایه هر ترید روی {value:.2f}$ تنظیم و ذخیره شد.")
        return True

    if compact.startswith("ترید لوریج") or compact.startswith("لوریج ترید") or compact.startswith("لوریج"):
        value = parse_first_number(compact)
        if value is None or value <= 0:
            await send_text(update, "❌ مقدار لوریج نامعتبر است. مثال: ترید لوریج 10")
            return True
        lev = max(1, min(int(value), 125))
        settings_update.update({"leverage": lev})
        save_runtime_settings(settings_update)
        await send_text(update, f"✅ لوریج روی {lev}x تنظیم و ذخیره شد.")
        return True

    if compact.startswith("حداکثر پوزیشن") or compact.startswith("ماکس پوزیشن") or compact.startswith("max positions"):
        value = parse_first_number(compact)
        if value is None or value <= 0:
            await send_text(update, "❌ مقدار حداکثر پوزیشن نامعتبر است. مثال: حداکثر پوزیشن 3")
            return True
        max_pos = max(1, min(int(value), 100))
        settings_update.update({"max_positions": max_pos})
        save_runtime_settings(settings_update)
        await send_text(update, f"✅ حداکثر پوزیشن روی {max_pos} تنظیم و ذخیره شد.")
        return True

    if "سیگنال خودکار روشن" in compact:
        save_runtime_settings({"auto_signal_enabled": True})
        await send_text(update, "✅ سیگنال خودکار روشن شد.")
        return True

    if "سیگنال خودکار خاموش" in compact:
        save_runtime_settings({"auto_signal_enabled": False})
        await send_text(update, "❌ سیگنال خودکار خاموش شد.")
        return True

    return False

async def handle_stats(update: Update, text: str) -> bool:
    t = str(text or "").strip()
    if t.startswith("حذف آمار"):
        await send_text(update, clear_stats())
        return True

    if t.startswith("آمار هوشمند"):
        await send_text(update, detailed_stats_report(days=30, source_type="ALL"))
        return True

    if t.startswith("آمار"):
        days = None
        m = re.search(r"(\d+)", t)
        if m:
            days = int(m.group(1))
        elif "کل" in t:
            days = None
        await send_text(update, stats_report(days=days))
        return True

    return False


async def handle_analysis(update: Update, text: str) -> bool:
    t = str(text or "").strip()
    if not (t.startswith("تحلیل") or t.startswith("سیگنال")):
        return False

    symbol = extract_symbol(t)
    await send_text(update, f"🔎 در حال بررسی {symbol} ...")
    try:
        result = await asyncio.to_thread(orchestrator().run_pipeline, symbol, "5m", None, True)
        if result.signal_report:
            await send_payload(update, result.signal_report)
        else:
            decision = result.decision
            reject_line = " | ".join(decision.reject_reasons[:4]) if decision.reject_reasons else "شرایط کافی نبود"
            await send_text(update, f"❌ {symbol} رد شد\nدلیل: {reject_line}\nAI: {decision.ai_score:.1f}")

        if result.trade_report:
            await send_payload(update, result.trade_report)
    except Exception as exc:
        save_error("bot_analysis", str(exc), {"symbol": symbol})
        await send_payload(update, format_error_report("خطا در تحلیل", exc))
    return True


async def handle_market_overview(update: Update, text: str) -> bool:
    if not str(text or "").strip().startswith("بررسی"):
        return False

    symbols = list(getattr(SETTINGS.market_data, "scan_symbols", ["BTCUSDT", "ETHUSDT"]))[:30]
    bullish = 0
    bearish = 0
    neutral = 0
    errors = 0

    await send_text(update, "🔎 بررسی سریع بازار شروع شد...")

    for symbol in symbols:
        try:
            candidate = await asyncio.to_thread(orchestrator().build_candidate, symbol, "5m")
            if candidate.direction_hint == "LONG":
                bullish += 1
            elif candidate.direction_hint == "SHORT":
                bearish += 1
            else:
                neutral += 1
        except Exception:
            errors += 1

    total = max(1, bullish + bearish + neutral)
    text_out = (
        "📊 بررسی بازار\n"
        f"صعودی: {bullish} ({bullish / total * 100:.1f}%)\n"
        f"نزولی: {bearish} ({bearish / total * 100:.1f}%)\n"
        f"رنج/خنثی: {neutral} ({neutral / total * 100:.1f}%)\n"
        f"خطا: {errors}\n\n"
    )
    if neutral >= bullish and neutral >= bearish:
        text_out += "جمع‌بندی: بازار بیشتر رنج/نامشخص است."
    elif bullish > bearish:
        text_out += "جمع‌بندی: تمایل کلی بازار صعودی است."
    else:
        text_out += "جمع‌بندی: تمایل کلی بازار نزولی است."
    await send_text(update, text_out)
    return True


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_access(update):
        return

    message = getattr(update, "effective_message", None)
    text = str(getattr(message, "text", "") or "").strip()
    if not text:
        return

    if await handle_trade_toggle(update, text):
        return

    if text.startswith("وضعیت ترید"):
        await trade_status(update)
        return

    if await handle_stats(update, text):
        return

    if await handle_market_overview(update, text):
        return

    if await handle_analysis(update, text):
        return

    if text.startswith("هوش مصنوعی"):
        await send_text(update, build_ai_status_text())
        return

    await send_text(update, "دستور شناخته نشد.")


async def auto_scan_loop(app: Any) -> None:
    while True:
        try:
            settings = get_runtime_settings()
            interval = safe_int(settings.get("scan_interval_seconds", 240), 240)
            if auto_signal_enabled():
                symbols = list(getattr(SETTINGS.market_data, "scan_symbols", ["BTCUSDT", "ETHUSDT"]))
                for symbol in symbols:
                    try:
                        result = await asyncio.to_thread(orchestrator().run_pipeline, symbol, "5m", None, True)
                        if result.signal_report and result.decision.decision_type in {DECISION_REAL, DECISION_GHOST}:
                            oid = owner_id()
                            if oid:
                                await app.bot.send_message(chat_id=oid, text=result.signal_report.text)
                        if result.trade_report:
                            oid = owner_id()
                            if oid:
                                await app.bot.send_message(chat_id=oid, text=result.trade_report.text)
                    except Exception as exc:
                        save_error("auto_scan_symbol", str(exc), {"symbol": symbol})
                    await asyncio.sleep(0.2)
            await asyncio.sleep(max(30, interval))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            save_error("auto_scan_loop", str(exc), {})
            await asyncio.sleep(30)


async def position_monitor_loop(app: Any) -> None:
    client = get_client()
    while True:
        try:
            events = await asyncio.to_thread(monitor_all_positions, client, None)
            for event in events:
                try:
                    record_position_event(event)
                    payload = reporter().position_event_report(event)
                    oid = owner_id()
                    if oid and payload.should_send and payload.text:
                        kwargs = {}
                        if payload.reply_to_message_id:
                            kwargs["reply_to_message_id"] = payload.reply_to_message_id
                        await app.bot.send_message(chat_id=oid, text=payload.text, **kwargs)
                except Exception as exc:
                    save_error("position_event_report", str(exc), event.to_dict())
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            save_error("position_monitor_loop", str(exc), {})
            await asyncio.sleep(10)


async def post_init(app: Any) -> None:
    app.create_task(auto_scan_loop(app))
    app.create_task(position_monitor_loop(app))


def build_application() -> Any:
    if Application is None:
        raise RuntimeError("python-telegram-bot is not installed")

    token = os.getenv("BOT_TOKEN", getattr(SETTINGS.telegram, "bot_token", ""))
    if not token:
        raise RuntimeError("BOT_TOKEN is missing")

    app = Application.builder().token(token).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("id", id_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    app = build_application()
    LOGGER.info("Movement Hunter bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
