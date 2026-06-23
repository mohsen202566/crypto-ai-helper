from __future__ import annotations

"""
26 - bot.py

Telegram router and orchestration layer for the simplified Level 1 / 5M bot.

Locked goals:
- 10 selected coins only for auto scan:
  DOGE, XRP, SOL, ADA, AVAX, LINK, INJ, PEPE, WIF, BONK
- Telegram command routing only.
- AI is final decision maker through ai_decision_engine.py.
- Technical analysis is raw sensor/candidate only.
- Pattern Start / movement prediction is handled by movement_predictor.py.
- Learning is handled by coin_learning.py and movement_memory.py.
- TP/SL is handled by tp_sl_engine.py.
- REAL orders only through real_trade_manager.py.
- GHOST records only through ghost_manager.py.
- REAL position results only through position_monitor.py and result_reporter.py.
- No paper mode.
- No setup flow.
- No trap/state/confidence/correlation/meta/movement_hunter dependency.
- No direct Toobit order logic in this file.
"""

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

try:
    from telegram import Update
    from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
except Exception:  # lets py_compile pass on VPS/dev machines without telegram installed
    Update = Any  # type: ignore
    ContextTypes = Any  # type: ignore
    Application = None  # type: ignore
    CommandHandler = None  # type: ignore
    MessageHandler = None  # type: ignore
    filters = None  # type: ignore

from config import SETTINGS
from data_store import store, save_error, save_position
from market_data import get_multi_timeframe_snapshot, get_latest_price
from analysis_engine import analyze_symbol, AnalysisCandidate
from coin_learning import summarize_candidate_learning, LearningSummary
from movement_memory import summarize_movement_candidate
from movement_predictor import predict_movement, MovementPredictionResult
from ai_decision_engine import decide, AIDecision, DECISION_REAL, DECISION_GHOST, DECISION_REJECT
from tp_sl_engine import build_tp_sl_plan, apply_tp_sl_to_decision, TPSLPlan
from ghost_manager import create_ghost, monitor_ghost, ghost_stats, manager as ghost_manager_instance
from real_trade_manager import open_real_position, RealTradeOpenResult
from tobit_client import get_client
from position_monitor import monitor_all_positions
from result_reporter import reporter, ReportPayload, format_error_report
from stats_manager import (
    record_decision,
    record_position_event,
    record_ghost_result,
    stats_report,
    detailed_stats_report,
    clear_stats,
    manager as stats_manager_instance,
)


LOGGER = logging.getLogger("level1_5m_bot")

DIRECTION_LONG = "LONG"
DIRECTION_SHORT = "SHORT"

LEVEL1_SYMBOLS: Tuple[str, ...] = (
    "DOGEUSDT",
    "XRPUSDT",
    "SOLUSDT",
    "ADAUSDT",
    "AVAXUSDT",
    "LINKUSDT",
    "INJUSDT",
    "PEPEUSDT",
    "WIFUSDT",
    "BONKUSDT",
)

PERSIAN_TRUE = {"روشن", "فعال", "on", "ON", "true", "True"}
PERSIAN_FALSE = {"خاموش", "غیرفعال", "off", "OFF", "false", "False"}

PERSIAN_SYMBOL_ALIASES: Dict[str, str] = {
    "دوج": "DOGEUSDT",
    "دوج کوین": "DOGEUSDT",
    "داج": "DOGEUSDT",
    "داج کوین": "DOGEUSDT",
    "doge": "DOGEUSDT",
    "dog": "DOGEUSDT",
    "dogecoin": "DOGEUSDT",

    "ریپل": "XRPUSDT",
    "ایکس آر پی": "XRPUSDT",
    "ایکس ار پی": "XRPUSDT",
    "xrp": "XRPUSDT",
    "ripple": "XRPUSDT",

    "سولانا": "SOLUSDT",
    "سول": "SOLUSDT",
    "sol": "SOLUSDT",
    "solana": "SOLUSDT",

    "کاردانو": "ADAUSDT",
    "ادا": "ADAUSDT",
    "آدا": "ADAUSDT",
    "ada": "ADAUSDT",
    "cardano": "ADAUSDT",

    "آوالانچ": "AVAXUSDT",
    "اوالانچ": "AVAXUSDT",
    "آواکس": "AVAXUSDT",
    "اواکس": "AVAXUSDT",
    "avax": "AVAXUSDT",
    "avalanche": "AVAXUSDT",

    "لینک": "LINKUSDT",
    "چین لینک": "LINKUSDT",
    "چینلینک": "LINKUSDT",
    "link": "LINKUSDT",
    "chainlink": "LINKUSDT",

    "اینجکتیو": "INJUSDT",
    "اینج": "INJUSDT",
    "inj": "INJUSDT",
    "injective": "INJUSDT",

    "پپه": "PEPEUSDT",
    "پپه کوین": "PEPEUSDT",
    "pepe": "PEPEUSDT",

    "ویف": "WIFUSDT",
    "داگ ویف": "WIFUSDT",
    "داگ ویف هت": "WIFUSDT",
    "wif": "WIFUSDT",

    "بونک": "BONKUSDT",
    "بونک کوین": "BONKUSDT",
    "bonk": "BONKUSDT",
}

ASSET_COMMAND_PREFIXES = ("تحلیل", "سیگنال")


@dataclass(frozen=True)
class PipelineResult:
    candidate: AnalysisCandidate
    learning: LearningSummary
    prediction: MovementPredictionResult
    decision: AIDecision
    plan: Optional[TPSLPlan]
    trade_result: Optional[RealTradeOpenResult] = None
    signal_report: Optional[ReportPayload] = None
    trade_report: Optional[ReportPayload] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "candidate": self.candidate.to_dict() if hasattr(self.candidate, "to_dict") else {},
            "learning": self.learning.to_dict() if hasattr(self.learning, "to_dict") else {},
            "prediction": self.prediction.to_dict() if hasattr(self.prediction, "to_dict") else {},
            "decision": self.decision.to_dict() if hasattr(self.decision, "to_dict") else {},
            "plan": self.plan.to_dict() if self.plan and hasattr(self.plan, "to_dict") else None,
            "trade_result": self.trade_result.to_dict() if self.trade_result and hasattr(self.trade_result, "to_dict") else None,
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


def safe_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    s = str(value).strip().lower()
    if s in {"1", "true", "yes", "on", "فعال", "روشن"}:
        return True
    if s in {"0", "false", "no", "off", "خاموش", "غیرفعال", "غيرفعال"}:
        return False
    return default


def normalize_symbol_safe(symbol: str) -> str:
    raw = str(symbol or "").upper().strip().replace("/", "").replace("-", "").replace("_", "")
    if not raw:
        return ""
    try:
        from symbol_mapper import normalize_symbol as _normalize_symbol
        mapped = _normalize_symbol(raw)
        if mapped:
            raw = str(mapped).upper().replace("/", "").replace("-", "").replace("_", "")
    except Exception:
        pass
    if raw.endswith("USDT"):
        return raw
    if 2 <= len(raw) <= 12 and re.match(r"^[A-Z0-9]+$", raw):
        return raw + "USDT"
    return raw


def allowed_level1_symbol(symbol: str) -> str:
    normalized = normalize_symbol_safe(symbol)
    if normalized in LEVEL1_SYMBOLS:
        return normalized
    return ""


def level1_scan_symbols() -> List[str]:
    runtime = get_runtime_settings()
    configured = runtime.get("level1_symbols")
    if isinstance(configured, list):
        symbols = [allowed_level1_symbol(str(x)) for x in configured]
        symbols = [s for s in symbols if s]
        if symbols:
            return list(dict.fromkeys(symbols))[:10]
    return list(LEVEL1_SYMBOLS)


def extract_symbol(text: str, default: str = "DOGEUSDT") -> str:
    original = str(text or "").strip()
    compact = original.lower().replace("‌", " ").replace("\u200c", " ")
    compact = re.sub(r"\s+", " ", compact).strip()

    asset_part = compact
    for prefix in ASSET_COMMAND_PREFIXES:
        if asset_part.startswith(prefix):
            asset_part = asset_part[len(prefix):].strip()

    for alias in sorted(PERSIAN_SYMBOL_ALIASES, key=len, reverse=True):
        if asset_part == alias:
            return PERSIAN_SYMBOL_ALIASES[alias]

    if original.lower().strip().startswith(ASSET_COMMAND_PREFIXES):
        for alias in sorted(PERSIAN_SYMBOL_ALIASES, key=len, reverse=True):
            if alias in asset_part:
                return PERSIAN_SYMBOL_ALIASES[alias]

    t = original.upper()
    t = re.sub(r"[^\w\s]", " ", t)
    words = [w for w in t.split() if w]
    for w in reversed(words):
        if w in {"تحلیل", "سیگنال", "بازار", "بررسی", "LONG", "SHORT", "وضعیت", "ترید"}:
            continue
        if re.match(r"^[A-Z0-9]{2,15}$", w):
            symbol = normalize_symbol_safe(w)
            return symbol if symbol else default

    return default


def is_direct_asset_query(text: str) -> bool:
    compact = str(text or "").strip().lower().replace("‌", " ").replace("\u200c", " ")
    compact = re.sub(r"\s+", " ", compact).strip()
    if not compact or len(compact) > 30:
        return False
    if compact.startswith(("وضعیت", "ترید", "آمار", "بررسی", "موجودی", "بالانس", "پوزیشن", "توبیت", "بستن", "هوش")):
        return False
    symbol = extract_symbol(compact, default="")
    return bool(symbol and allowed_level1_symbol(symbol))


def get_runtime_settings() -> Dict[str, Any]:
    section = store().section("runtime_settings")

    default_real_enabled = bool(getattr(SETTINGS.trading, "enabled", False))
    if "real_trading_enabled" not in section and "trade_enabled" in section:
        default_real_enabled = safe_bool(section.get("trade_enabled"), default_real_enabled)
    elif "real_trading_enabled" in section:
        default_real_enabled = safe_bool(section.get("real_trading_enabled"), default_real_enabled)

    defaults = {
        # Keep both names forever so old code/data and new code always agree.
        "real_trading_enabled": default_real_enabled,
        "trade_enabled": default_real_enabled,
        "auto_signal_enabled": bool(os.getenv("AUTO_SIGNAL_ENABLED", "true").lower() in {"1", "true", "yes", "on"}),
        "scan_interval_seconds": safe_int(getattr(SETTINGS.monitor, "scan_interval_seconds", 20), 20),
        "ghost_monitor_interval_seconds": safe_int(getattr(SETTINGS.monitor, "ghost_monitor_interval_seconds", 3), 3),
        "margin_usdt": safe_float(getattr(SETTINGS.trading, "margin_usdt", 5.0), 5.0),
        "trade_margin_usdt": safe_float(getattr(SETTINGS.trading, "margin_usdt", 5.0), 5.0),
        "leverage": safe_int(getattr(SETTINGS.trading, "leverage", 10), 10),
        "max_positions": safe_int(getattr(SETTINGS.trading, "max_positions", 5), 5),
        "daily_loss_lock_enabled": True,
        "daily_loss_locked_until": 0,
        "last_scan_ts": 0,
        "level1_symbols": list(LEVEL1_SYMBOLS),
    }

    changed = False
    for key, value in defaults.items():
        if key not in section:
            section[key] = value
            changed = True

    # Root fix: real_trading_enabled and legacy trade_enabled must never diverge.
    real_enabled = safe_bool(section.get("real_trading_enabled"), default_real_enabled)
    legacy_enabled = safe_bool(section.get("trade_enabled"), real_enabled)
    if real_enabled != legacy_enabled:
        # Prefer explicit new key when present; otherwise use legacy.
        unified = real_enabled if "real_trading_enabled" in section else legacy_enabled
        section["real_trading_enabled"] = unified
        section["trade_enabled"] = unified
        changed = True

    if changed:
        save_runtime_settings(section)
    return section

def save_runtime_settings(values: Dict[str, Any]) -> None:
    values = dict(values or {})

    # Single source of truth with backward compatibility:
    # "real_trading_enabled" is the new canonical key, "trade_enabled" is legacy.
    if "real_trading_enabled" in values:
        unified = safe_bool(values.get("real_trading_enabled"), False)
        values["real_trading_enabled"] = unified
        values["trade_enabled"] = unified
    elif "trade_enabled" in values:
        unified = safe_bool(values.get("trade_enabled"), False)
        values["real_trading_enabled"] = unified
        values["trade_enabled"] = unified

    if "margin_usdt" in values and "trade_margin_usdt" not in values:
        values["trade_margin_usdt"] = safe_float(values.get("margin_usdt"), 0.0)
    elif "trade_margin_usdt" in values and "margin_usdt" not in values:
        values["margin_usdt"] = safe_float(values.get("trade_margin_usdt"), 0.0)

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
    settings = get_runtime_settings()
    real_value = safe_bool(settings.get("real_trading_enabled"), False)
    legacy_value = safe_bool(settings.get("trade_enabled"), real_value)
    if real_value != legacy_value:
        # Auto-heal any old/corrupted runtime data immediately.
        real_value = bool(real_value)
        save_runtime_settings({"real_trading_enabled": real_value})
    return bool(real_value)

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
    try:
        runtime = store().section("allowed_users")
        for item in runtime.values():
            ids.add(safe_int(item))
    except Exception:
        pass
    return {i for i in ids if i > 0}


def is_allowed(user_id: int) -> bool:
    allowed = allowed_user_ids()
    return not allowed or int(user_id) in allowed


def force_real_decision_to_ghost(decision: AIDecision, reason: str = "REAL_TRADING_DISABLED_TO_GHOST") -> AIDecision:
    if getattr(decision, "decision_type", "") != DECISION_REAL:
        return decision

    data = decision.to_dict()
    data["decision_type"] = DECISION_GHOST
    data["should_trade_real"] = False
    data["should_create_ghost"] = True
    data["should_reject"] = False

    reasons = list(data.get("reason_codes", ()) or ())
    warnings = list(data.get("warnings", ()) or ())
    if reason not in reasons:
        reasons.append(reason)
    if reason not in warnings:
        warnings.append(reason)

    data["reason_codes"] = tuple(reasons)
    data["warnings"] = tuple(warnings)
    return AIDecision(**data)


async def send_payload(update: Update, payload: ReportPayload) -> Any:
    if not payload or not getattr(payload, "should_send", False) or not getattr(payload, "text", ""):
        return None
    message = getattr(update, "effective_message", None)
    if message is None:
        return None
    kwargs: Dict[str, Any] = {}
    if getattr(payload, "reply_to_message_id", 0):
        kwargs["reply_to_message_id"] = payload.reply_to_message_id
    try:
        return await message.reply_text(payload.text, **kwargs)
    except TypeError:
        return await message.reply_text(payload.text)
    except Exception as exc:
        LOGGER.exception("send_payload failed: %s", exc)
        return None


async def send_text(update: Update, text: str) -> None:
    message = getattr(update, "effective_message", None)
    if message is not None:
        await message.reply_text(text)


def active_position_count() -> int:
    closed = {"CLOSED", "TP2", "AI_EXIT", "SL", "FAILED", "REJECTED"}
    try:
        positions = store().section("positions")
        count = 0
        for item in positions.values():
            if not isinstance(item, dict):
                continue
            status = str(item.get("status", "")).upper()
            if status not in closed:
                count += 1
        return count
    except Exception:
        return 0


def real_capacity_available() -> bool:
    max_pos = runtime_max_positions()
    if max_pos <= 0:
        return True

    internal_count = active_position_count()
    exchange_count = 0
    try:
        summary = toobit_summary()
        if isinstance(summary, dict):
            exchange_count = safe_int(summary.get("open_positions_count", 0), 0)
    except Exception:
        exchange_count = 0

    return max(internal_count, exchange_count) < max_pos


def attach_signal_message_to_position(trade_result: Optional[RealTradeOpenResult], message_obj: Any) -> None:
    if not trade_result or message_obj is None:
        return

    msg_id = safe_int(getattr(message_obj, "message_id", 0), 0)
    if msg_id <= 0:
        return

    try:
        trade_id = str(getattr(trade_result, "trade_id", "") or "")
        if not trade_id:
            return
        positions = store().section("positions")
        rec = positions.get(trade_id)
        if isinstance(rec, dict):
            rec["signal_message_id"] = msg_id
            meta = rec.get("meta") if isinstance(rec.get("meta"), dict) else {}
            meta["signal_message_id"] = msg_id
            rec["meta"] = meta
            save_position(trade_id, rec)
    except Exception as exc:
        try:
            save_error("attach_signal_message_to_position", str(exc), {"trade_result": trade_result.to_dict()})
        except Exception:
            pass


class PipelineOrchestrator:
    def __init__(self):
        self.client = get_client()

    def build_candidate(self, symbol: str, timeframe: str = "5m") -> AnalysisCandidate:
        symbol = allowed_level1_symbol(symbol)
        if not symbol:
            raise ValueError("SYMBOL_NOT_ALLOWED_LEVEL1")

        mtf = get_multi_timeframe_snapshot(symbol, timeframes=[timeframe], limit=160)
        snapshot = mtf.snapshots[timeframe]
        candles = [c.to_dict() for c in snapshot.candles]
        return analyze_symbol(symbol=symbol, timeframe=timeframe, candles=candles, market_context=None)

    def predict_for_candidate(self, candidate: AnalysisCandidate, learning: LearningSummary) -> MovementPredictionResult:
        learning_summary = learning.to_dict() if hasattr(learning, "to_dict") else {}

        movement_summary: Dict[str, Any] = {}
        try:
            ms = summarize_movement_candidate(candidate)
            movement_summary = ms.to_dict() if hasattr(ms, "to_dict") else dict(ms or {})
        except TypeError:
            try:
                ms = summarize_movement_candidate(candidate=candidate)
                movement_summary = ms.to_dict() if hasattr(ms, "to_dict") else dict(ms or {})
            except Exception:
                movement_summary = {}
        except Exception:
            movement_summary = {}

        attempts = [
            lambda: predict_movement(candidate=candidate, learning_summary=learning_summary, movement_summary=movement_summary),
            lambda: predict_movement(candidate=candidate, learning=learning),
            lambda: predict_movement(candidate),
        ]

        last_error: Optional[Exception] = None
        for fn in attempts:
            try:
                return fn()
            except TypeError as exc:
                last_error = exc
                continue
        if last_error:
            raise last_error
        raise RuntimeError("predict_movement_failed")

    def run_pipeline(
        self,
        symbol: str,
        timeframe: str = "5m",
        open_positions: Optional[Iterable[Any]] = None,
        execute_real: bool = True,
        force_real_to_ghost_reason: Optional[str] = None,
    ) -> PipelineResult:
        candidate = self.build_candidate(symbol, timeframe=timeframe)
        learning = summarize_candidate_learning(candidate)
        prediction = self.predict_for_candidate(candidate, learning)

        decision = decide(candidate=candidate, prediction=prediction, learning=learning)

        if decision.decision_type == DECISION_REAL and not real_trading_enabled():
            decision = force_real_decision_to_ghost(decision, "REAL_TRADING_DISABLED_TO_GHOST")
        elif decision.decision_type == DECISION_REAL and force_real_to_ghost_reason:
            decision = force_real_decision_to_ghost(decision, force_real_to_ghost_reason)

        plan: Optional[TPSLPlan] = None
        trade_result: Optional[RealTradeOpenResult] = None
        signal_report: Optional[ReportPayload] = None
        trade_report: Optional[ReportPayload] = None

        if decision.decision_type in {DECISION_REAL, DECISION_GHOST}:
            plan = build_tp_sl_plan(
                decision=decision,
                candidate=candidate,
                prediction=prediction,
                learning=learning,
            )
            decision = apply_tp_sl_to_decision(decision, plan)

        record_decision(decision)
        LOGGER.info(
            "AI decision %s %s ai=%.1f conf=%.1f phase=%s patterns=%s reasons=%s warnings=%s",
            symbol,
            decision.decision_type,
            safe_float(getattr(decision, "ai_score", 0.0)),
            safe_float(getattr(decision, "confidence_score", 0.0)),
            str(getattr(decision, "predicted_phase", "")),
            safe_int(getattr(decision, "pattern_count", 0), 0),
            ",".join(list(getattr(decision, "reason_codes", ()) or ())[:6]),
            ",".join(list(getattr(decision, "warnings", ()) or ())[:6]),
        )

        if decision.decision_type == DECISION_GHOST and plan:
            create_ghost(
                decision=decision,
                candidate=candidate,
                entry=plan.entry,
                tp1=plan.tp1,
                tp2=plan.tp2,
                sl=plan.sl,
                meta={
                    "decision": decision.to_dict(),
                    "plan": plan.to_dict(),
                    "candidate": candidate.to_dict() if hasattr(candidate, "to_dict") else {},
                    "learning": learning.to_dict() if hasattr(learning, "to_dict") else {},
                    "prediction": prediction.to_dict() if hasattr(prediction, "to_dict") else {},
                },
            )

        if decision.decision_type == DECISION_REAL and plan and execute_real and real_trading_enabled():
            trade_result = open_real_position(
                self.client,
                decision,
                plan,
                analysis_meta={
                    "candidate": candidate.to_dict() if hasattr(candidate, "to_dict") else {},
                    "learning": learning.to_dict() if hasattr(learning, "to_dict") else {},
                    "prediction": prediction.to_dict() if hasattr(prediction, "to_dict") else {},
                    "ai_decision": decision.to_dict() if hasattr(decision, "to_dict") else {},
                    "tp_sl_plan": plan.to_dict() if hasattr(plan, "to_dict") else {},
                },
            )

        if plan:
            signal_report = reporter().signal_report(decision, plan)
        if trade_result:
            trade_report = reporter().trade_open_report(trade_result)

        return PipelineResult(
            candidate=candidate,
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
    await send_text(update, (
        "🤖 ربات Level 1 / 5M فعال است\n"
        "معماری: AI + Pattern + Learning → REAL / GHOST / REJECT\n"
        "Paper و Setup حذف شده‌اند.\n\n"
        "ارزهای فعال:\n"
        "DOGE | XRP | SOL | ADA | AVAX | LINK | INJ | PEPE | WIF | BONK\n\n"
        "دستورات:\n"
        "تحلیل DOGE\n"
        "سیگنال XRP\n"
        "بررسی بازار\n"
        "وضعیت / وضعیت ترید\n"
        "ترید فعال / ترید خاموش\n"
        "ترید دلار 10\n"
        "ترید لوریج 10\n"
        "حداکثر پوزیشن 3\n"
        "موجودی / بالانس\n"
        "پوزیشن‌ها\n"
        "توبیت / وضعیت توبیت\n"
        "بستن پوزیشن DOGE\n"
        "آمار / آمار هوشمند\n"
        "هوش مصنوعی"
    ))


async def id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = getattr(update, "effective_user", None)
    await send_text(update, f"ID: {getattr(user, 'id', 0)}")


def format_usdt(value: Any) -> str:
    return f"{safe_float(value):,.4f}$"


def toobit_summary() -> Dict[str, Any]:
    c = get_client()
    if hasattr(c, "account_summary") and callable(c.account_summary):
        return c.account_summary()

    balance = {"ok": False, "error": "account_summary_missing"}
    if hasattr(c, "get_account_balance") and callable(c.get_account_balance):
        try:
            balance = c.get_account_balance("USDT")
        except TypeError:
            balance = c.get_account_balance()

    positions = c.get_open_positions() if hasattr(c, "get_open_positions") else []
    return {
        "ok": bool(balance.get("ok", False)) if isinstance(balance, dict) else False,
        "balance": balance if isinstance(balance, dict) else {"ok": False, "error": "invalid_balance"},
        "open_positions_count": len(positions) if isinstance(positions, list) else 0,
        "open_positions": positions if isinstance(positions, list) else [],
        "total_unrealized_pnl": sum(safe_float(p.get("unrealized_pnl", 0.0)) for p in positions) if isinstance(positions, list) else 0.0,
        "has_credentials": True,
    }


def format_positions(positions: List[Dict[str, Any]], max_rows: int = 10) -> str:
    if not positions:
        return "پوزیشن باز واقعی: 0"

    lines = [f"پوزیشن‌های واقعی باز: {len(positions)}"]
    for p in positions[:max_rows]:
        symbol = str(p.get("symbol", "-"))
        direction = str(p.get("direction", "-"))
        qty = safe_float(p.get("quantity", 0.0))
        entry = safe_float(p.get("entry_price", 0.0))
        mark = safe_float(p.get("mark_price", 0.0))
        pnl = safe_float(p.get("unrealized_pnl", 0.0))
        lev = safe_int(p.get("leverage", 0))
        lines.append(f"• {symbol} {direction} | qty:{qty:g} | entry:{entry:g} | mark:{mark:g} | PnL:{pnl:+.4f}$ | lev:{lev}x")
    return "\n".join(lines)


def format_toobit_status(include_positions: bool = True) -> str:
    try:
        summary = toobit_summary()
        balance = summary.get("balance", {}) if isinstance(summary, dict) else {}
        positions = summary.get("open_positions", []) if isinstance(summary, dict) else []
        if not isinstance(positions, list):
            positions = []

        if balance.get("ok"):
            balance_text = (
                f"موجودی کیف پول: {format_usdt(balance.get('wallet_balance'))}\n"
                f"قابل استفاده: {format_usdt(balance.get('available_balance'))}\n"
                f"PnL باز: {safe_float(summary.get('total_unrealized_pnl', 0.0)):+.4f}$"
            )
            api_line = "اتصال Toobit: وصل ✅"
        else:
            balance_text = f"موجودی واقعی خوانده نشد: {balance.get('error', 'unknown')}"
            api_line = "اتصال Toobit: خطا ⚠️"

        text = (
            "⚙️ وضعیت ترید و Toobit\n"
            f"{api_line}\n"
            f"ترید واقعی: {'روشن ✅' if real_trading_enabled() else 'خاموش ❌'}\n"
            f"سیگنال خودکار: {'روشن ✅' if auto_signal_enabled() else 'خاموش ❌'}\n"
            f"سرمایه هر ترید: {runtime_margin_usdt():.2f}$ | لوریج: {runtime_leverage()}x | حداکثر پوزیشن: {runtime_max_positions()}\n"
            f"ارزهای فعال: {', '.join(level1_scan_symbols())}\n"
            f"{balance_text}\n"
        )
        if include_positions:
            text += "\n" + format_positions(positions)
        return text
    except Exception as exc:
        save_error("toobit_status", str(exc), {})
        return f"⚠️ خطا در خواندن وضعیت Toobit\n{exc}"


async def send_toobit_status(update: Update) -> None:
    await send_text(update, await asyncio.to_thread(format_toobit_status, True))


async def send_toobit_balance(update: Update) -> None:
    def build() -> str:
        summary = toobit_summary()
        balance = summary.get("balance", {}) if isinstance(summary, dict) else {}
        if not balance.get("ok"):
            return f"⚠️ موجودی واقعی Toobit خوانده نشد: {balance.get('error', 'unknown')}"
        return (
            "💰 موجودی واقعی Toobit\n"
            f"کیف پول: {format_usdt(balance.get('wallet_balance'))}\n"
            f"قابل استفاده: {format_usdt(balance.get('available_balance'))}\n"
            f"مارجین/Equity: {format_usdt(balance.get('margin_balance'))}\n"
            f"PnL باز: {safe_float(summary.get('total_unrealized_pnl', 0.0)):+.4f}$"
        )

    await send_text(update, await asyncio.to_thread(build))


async def send_toobit_positions(update: Update) -> None:
    def build() -> str:
        summary = toobit_summary()
        positions = summary.get("open_positions", []) if isinstance(summary, dict) else []
        if not isinstance(positions, list):
            positions = []
        return "📌 " + format_positions(positions)

    await send_text(update, await asyncio.to_thread(build))


async def close_toobit_position_command(update: Update, text: str) -> bool:
    compact = str(text or "").strip().replace("‌", " ")
    if not (compact.startswith("بستن پوزیشن") or compact.startswith("بستن همه پوزیشن")):
        return False

    if not real_trading_enabled():
        await send_text(update, "❌ ترید واقعی خاموش است؛ برای بستن واقعی اول «ترید فعال» را بزن.")
        return True

    def run_close() -> str:
        c = get_client()
        positions = c.get_open_positions()
        if not positions:
            return "پوزیشن باز واقعی وجود ندارد."

        close_all = compact.startswith("بستن همه پوزیشن")
        if not close_all:
            target_symbol = extract_symbol(compact)
            positions_to_close = [p for p in positions if normalize_symbol_safe(str(p.get("symbol", ""))) == target_symbol]
        else:
            target_symbol = "ALL"
            positions_to_close = positions

        if not positions_to_close:
            return f"پوزیشن باز برای {target_symbol} پیدا نشد."

        results = []
        for p in positions_to_close:
            sym = str(p.get("symbol", ""))
            direction = str(p.get("direction", ""))
            qty = safe_float(p.get("quantity", 0.0))
            if not sym or not direction or qty <= 0:
                results.append(f"{sym or '-'}: اطلاعات پوزیشن ناقص بود")
                continue
            try:
                res = c.close_position(sym, direction, qty)
                order_id = res.get("order_id", "-") if isinstance(res, dict) else "-"
                results.append(f"✅ {sym} {direction} بسته شد | order:{order_id}")
            except Exception as exc:
                results.append(f"❌ {sym} {direction} خطا: {exc}")
        return "\n".join(results)

    await send_text(update, await asyncio.to_thread(run_close))
    return True


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
    ghosts_section = store().section("ghosts")

    pattern_pump = 0
    pattern_dump = 0
    early_patterns = 0

    for rec in movement_section.values():
        if not isinstance(rec, dict):
            continue
        direction = str(rec.get("direction") or rec.get("predicted_direction") or rec.get("side") or "").upper()
        phase = str(rec.get("phase") or rec.get("freshness") or rec.get("predicted_phase") or "").upper()
        if direction == "LONG":
            pattern_pump += 1
        elif direction == "SHORT":
            pattern_dump += 1
        if any(x in phase for x in ("PRE", "START", "EARLY", "FRESH")):
            early_patterns += 1

    return (
        "🤖 وضعیت هوش مصنوعی Level 1 / 5M\n\n"
        "📊 REAL\n"
        f"کل: {real.total_events} | بسته: {real.closed_count}\n"
        f"TP1: {real.tp1_count} | TP2: {real.tp2_count} | AI Exit: {real.ai_exit_count} | SL: {real.sl_count}\n"
        f"WinRate: {real.win_rate:.2f}%\n"
        f"PnL واقعی تاییدشده: {real.confirmed_pnl_usdt:+.4f}$\n\n"
        "👻 GHOST\n"
        f"کل: {ghost_total} | باز: {ghost_open} | بسته: {ghost_closed}\n"
        f"TP1: {ghost_tp1} | TP2: {ghost_tp2} | AI Exit: {ghost_ai_exit} | SL: {ghost_sl}\n"
        f"WinRate: {ghost_wr:.2f}%\n\n"
        "🎯 کلی\n"
        f"TP1: {all_summary.tp1_count} | TP2: {all_summary.tp2_count} | AI Exit: {all_summary.ai_exit_count} | SL: {all_summary.sl_count}\n"
        f"WinRate کل: {all_summary.win_rate:.2f}%\n\n"
        "🧬 Pattern / Learning\n"
        f"Learning samples: {len(learning_section)}\n"
        f"Movement memory: {len(movement_section)}\n"
        f"Ghost records: {len(ghosts_section)}\n"
        f"الگوی تشخیص پامپ: {pattern_pump}\n"
        f"الگوی تشخیص دامپ: {pattern_dump}\n"
        f"الگوهای شروع/زودهنگام: {early_patterns}\n"
        f"ارزهای فعال: {', '.join(level1_scan_symbols())}"
    )


async def handle_trade_toggle(update: Update, text: str) -> bool:
    normalized = str(text or "").strip().lower()
    compact = normalized.replace("‌", " ").replace("\u200c", " ")
    settings_update: Dict[str, Any] = {}

    if compact in {"ترید", "وضعیت", "وضعیت ترید", "trade", "trade status"}:
        await send_toobit_status(update)
        return True

    if "ترید فعال" in compact or "ترید روشن" in compact or "trade on" in compact:
        settings_update.update({"real_trading_enabled": True, "trade_enabled": True})
        save_runtime_settings(settings_update)
        verified = real_trading_enabled()
        await send_text(
            update,
            "✅ ترید واقعی فعال شد و در تنظیمات ذخیره شد."
            if verified
            else "⚠️ درخواست فعال‌سازی ثبت شد اما تایید ذخیره‌سازی ناموفق بود؛ لاگ را چک کن."
        )
        return True

    if "ترید خاموش" in compact or "ترید غیرفعال" in compact or "trade off" in compact:
        settings_update.update({"real_trading_enabled": False, "trade_enabled": False})
        save_runtime_settings(settings_update)
        verified = not real_trading_enabled()
        await send_text(
            update,
            "❌ ترید واقعی خاموش شد و در تنظیمات ذخیره شد. از این به بعد سیگنال REAL به GHOST تبدیل می‌شود."
            if verified
            else "⚠️ درخواست خاموش‌سازی ثبت شد اما تایید ذخیره‌سازی ناموفق بود؛ لاگ را چک کن."
        )
        return True

    if "قفل ضرر خاموش" in compact:
        settings_update.update({"daily_loss_lock_enabled": False, "daily_loss_locked_until": 0, "daily_loss_unlocked_at": now_ts()})
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
            await send_text(update, "❌ مقدار سرمایه ترید نامعتبر است. مثال: ترید دلار 10")
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
    if not (t.startswith("تحلیل") or t.startswith("سیگنال") or is_direct_asset_query(t)):
        return False

    symbol = extract_symbol(t)
    if not allowed_level1_symbol(symbol):
        await send_text(update, "❌ این نسخه فقط روی ۱۰ ارز Level 1 کار می‌کند:\n" + ", ".join(LEVEL1_SYMBOLS))
        return True

    await send_text(update, f"🔎 بررسی سریع {symbol} در 5M ...")
    try:
        result = await asyncio.to_thread(orchestrator().run_pipeline, symbol, "5m", None, True)

        if result.signal_report and result.decision.decision_type == DECISION_REAL:
            sent_msg = await send_payload(update, result.signal_report)
            attach_signal_message_to_position(result.trade_result, sent_msg)
        elif result.decision.decision_type == DECISION_GHOST:
            await send_text(update, f"👻 {symbol}: شرایط برای REAL کافی نبود؛ به GHOST رفت و برای یادگیری ثبت شد.")
        else:
            decision = result.decision
            rejects = getattr(decision, "reject_reasons", ()) or ()
            reject_line = " | ".join(list(rejects)[:4]) if rejects else "شرایط کافی نبود"
            await send_text(update, f"❌ {symbol} رد شد\nدلیل: {reject_line}\nAI: {safe_float(getattr(decision, 'ai_score', 0)):.1f}")

        if result.trade_report:
            await send_payload(update, result.trade_report)

    except Exception as exc:
        save_error("bot_analysis", str(exc), {"symbol": symbol})
        await send_payload(update, format_error_report("خطا در تحلیل", exc))

    return True


async def handle_market_overview(update: Update, text: str) -> bool:
    if not str(text or "").strip().startswith("بررسی"):
        return False

    symbols = level1_scan_symbols()
    bullish = 0
    bearish = 0
    neutral = 0
    errors = 0

    await send_text(update, "🔎 بررسی سریع بازار ۱۰ ارز Level 1 شروع شد...")

    for symbol in symbols:
        try:
            candidate = await asyncio.to_thread(orchestrator().build_candidate, symbol, "5m")
            if getattr(candidate, "direction_hint", "") == "LONG":
                bullish += 1
            elif getattr(candidate, "direction_hint", "") == "SHORT":
                bearish += 1
            else:
                neutral += 1
        except Exception:
            errors += 1

    total = max(1, bullish + bearish + neutral)
    if neutral >= bullish and neutral >= bearish:
        summary = "بازار بیشتر رنج/نامشخص است."
    elif bullish > bearish:
        summary = "تمایل کلی بازار صعودی است."
    else:
        summary = "تمایل کلی بازار نزولی است."

    await send_text(update, (
        "📊 بررسی بازار Level 1\n"
        f"صعودی: {bullish} ({bullish / total * 100:.1f}%)\n"
        f"نزولی: {bearish} ({bearish / total * 100:.1f}%)\n"
        f"رنج/خنثی: {neutral} ({neutral / total * 100:.1f}%)\n"
        f"خطا: {errors}\n\n"
        f"جمع‌بندی: {summary}"
    ))
    return True


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_access(update):
        return

    message = getattr(update, "effective_message", None)
    text = str(getattr(message, "text", "") or "").strip()
    if not text:
        return

    compact_text = text.strip().replace("‌", " ")

    if compact_text.startswith(("موجودی", "بالانس", "سرمایه حساب")):
        await send_toobit_balance(update)
        return

    if compact_text.startswith(("پوزیشن‌ها", "پوزیشن ها", "پوزیشنهای باز", "پوزیشن‌های باز")):
        await send_toobit_positions(update)
        return

    if compact_text.startswith(("توبیت", "وضعیت توبیت")):
        await send_toobit_status(update)
        return

    if await close_toobit_position_command(update, text):
        return
    if await handle_trade_toggle(update, text):
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
    LOGGER.info("auto_scan_loop started")
    last_disabled_log = 0

    while True:
        try:
            settings = get_runtime_settings()
            interval = safe_int(settings.get("scan_interval_seconds", 20), 20)
            interval = max(5, interval)

            if not auto_signal_enabled():
                # Keep the loop alive and visible in journalctl; do not silently look stuck.
                if now_ts() - last_disabled_log >= 60:
                    LOGGER.info("auto_scan_loop alive but auto_signal_enabled=false")
                    last_disabled_log = now_ts()
                await asyncio.sleep(interval)
                continue

            symbols = level1_scan_symbols()
            current_settings = get_runtime_settings()
            LOGGER.info(
                "auto_scan_cycle symbols=%s interval=%ss real=%s trade_enabled=%s",
                ",".join(symbols),
                interval,
                real_trading_enabled(),
                safe_bool(current_settings.get("trade_enabled"), False),
            )

            for symbol in symbols:
                try:
                    capacity_full = bool(real_trading_enabled() and not real_capacity_available())
                    force_reason = "REAL_CAPACITY_FULL_TO_GHOST" if capacity_full else None

                    result = await asyncio.to_thread(
                        orchestrator().run_pipeline,
                        symbol,
                        "5m",
                        None,
                        not capacity_full,
                        force_reason,
                    )

                    LOGGER.info(
                        "auto_scan_result %s decision=%s ai=%.1f ghost=%s real=%s reject=%s",
                        symbol,
                        getattr(result.decision, "decision_type", "-"),
                        safe_float(getattr(result.decision, "ai_score", 0.0)),
                        getattr(result.decision, "should_create_ghost", False),
                        getattr(result.decision, "should_trade_real", False),
                        getattr(result.decision, "should_reject", False),
                    )

                    # Auto scan sends only REAL signal reports. GHOST is stored/monitored silently
                    # so it can learn without spamming Telegram.
                    if result.signal_report and result.decision.decision_type == DECISION_REAL:
                        oid = owner_id()
                        if oid:
                            sent_msg = await app.bot.send_message(chat_id=oid, text=result.signal_report.text)
                            attach_signal_message_to_position(result.trade_result, sent_msg)

                    if result.trade_report:
                        oid = owner_id()
                        if oid:
                            await app.bot.send_message(chat_id=oid, text=result.trade_report.text)

                except Exception as exc:
                    LOGGER.exception("auto_scan_symbol failed for %s: %s", symbol, exc)
                    save_error("auto_scan_symbol", str(exc), {"symbol": symbol})

                await asyncio.sleep(0.05)

            await asyncio.sleep(interval)

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOGGER.exception("auto_scan_loop failed: %s", exc)
            save_error("auto_scan_loop", str(exc), {})
            await asyncio.sleep(10)

def build_position_monitor_analysis(pos: Any) -> Any:
    symbol = str(getattr(pos, "symbol", "") or getattr(pos, "exchange_symbol", "") or "")
    symbol = allowed_level1_symbol(symbol) or normalize_symbol_safe(symbol)
    candidate = orchestrator().build_candidate(symbol, timeframe="5m")
    return getattr(candidate, "sensor_snapshot", candidate)


async def position_monitor_loop(app: Any) -> None:
    client = get_client()
    while True:
        try:
            events = await asyncio.to_thread(monitor_all_positions, client, build_position_monitor_analysis)
            for event in events:
                try:
                    record_position_event(event)
                    payload = reporter().position_event_report(event)
                    oid = owner_id()
                    if oid and payload.should_send and payload.text:
                        kwargs = {}
                        if payload.reply_to_message_id:
                            kwargs["reply_to_message_id"] = payload.reply_to_message_id
                        try:
                            await app.bot.send_message(chat_id=oid, text=payload.text, **kwargs)
                        except Exception:
                            await app.bot.send_message(chat_id=oid, text=payload.text)
                except Exception as exc:
                    save_error("position_event_report", str(exc), event.to_dict() if hasattr(event, "to_dict") else {})
            await asyncio.sleep(2)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            save_error("position_monitor_loop", str(exc), {})
            await asyncio.sleep(5)


def latest_price_value(raw: Any) -> float:
    if isinstance(raw, (int, float)):
        return safe_float(raw)
    if isinstance(raw, dict):
        for key in ("price", "last_price", "last", "close", "mark_price", "value"):
            if key in raw:
                price = safe_float(raw.get(key), 0.0)
                if price > 0:
                    return price
    for key in ("price", "last_price", "last", "close", "mark_price", "value"):
        if hasattr(raw, key):
            price = safe_float(getattr(raw, key), 0.0)
            if price > 0:
                return price
    return 0.0


async def ghost_monitor_loop(app: Any) -> None:
    LOGGER.info("ghost_monitor_loop started")
    while True:
        try:
            interval = safe_int(get_runtime_settings().get("ghost_monitor_interval_seconds", 3), 3)
            interval = max(2, interval)

            ghosts = await asyncio.to_thread(ghost_manager_instance().open_ghosts)
            for ghost in ghosts:
                try:
                    raw_price = await asyncio.to_thread(get_latest_price, ghost.symbol)
                    price = latest_price_value(raw_price)
                    if price <= 0:
                        continue

                    result = await asyncio.to_thread(monitor_ghost, ghost, price)
                    if getattr(result, "closed", False):
                        try:
                            record_ghost_result(result)
                        except TypeError:
                            record_ghost_result(result.to_dict())
                        except Exception as exc:
                            save_error("ghost_result_record", str(exc), result.to_dict() if hasattr(result, "to_dict") else {})
                except Exception as exc:
                    payload = ghost.to_dict() if hasattr(ghost, "to_dict") else {"ghost": str(ghost)}
                    save_error("ghost_monitor_symbol", str(exc), payload)

            await asyncio.sleep(interval)

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            save_error("ghost_monitor_loop", str(exc), {})
            await asyncio.sleep(5)


async def post_init(app: Any) -> None:
    LOGGER.info("starting loops: auto_scan, position_monitor, ghost_monitor")
    app.create_task(auto_scan_loop(app))
    app.create_task(position_monitor_loop(app))
    app.create_task(ghost_monitor_loop(app))


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
    LOGGER.info("Level 1 / 5M bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
