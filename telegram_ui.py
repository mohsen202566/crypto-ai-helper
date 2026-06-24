"""
telegram_ui.py
Level 4 / 1H Smart Scalp Bot

Persian Telegram text rendering layer.

Architecture lock:
- Builds user-facing Persian text only.
- Does not make AI decisions, place orders, fetch market data, monitor positions,
  write JSON state, or call Toobit.
- It may format already-built decisions/events/stats.
- Allowed project imports:
  constants.py, utils.py, models.py, stats_engine.py, strategy_manager.py only.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from constants import (
    DIRECTION_LONG,
    DIRECTION_SHORT,
    MODE_GHOST,
    MODE_REAL,
    MODE_REJECT,
    STATUS_FAILED,
    STATUS_OK,
    STRATEGY_LEVEL,
    SYSTEM_VERSION,
)
from models import AIDecision, MonitorEvent, TradeOutcome, TradePosition, TPSLPlan
from stats_engine import build_stats_snapshot
import strategy_manager
from utils import normalize_direction, normalize_symbol, safe_float, safe_int, safe_str, utc_now_iso


TELEGRAM_UI_VERSION: str = SYSTEM_VERSION


# =============================================================================
# Safe strategy adapters
# =============================================================================

def _safe_strategy_state() -> dict[str, Any]:
    """Read strategy state through whichever public API exists."""
    for name in ("get_strategy_state", "load_strategy_state", "get_current_strategy_state"):
        fn = getattr(strategy_manager, name, None)
        if callable(fn):
            try:
                data = fn()
                return dict(data or {}) if isinstance(data, Mapping) else {}
            except Exception:
                return {}
    return {}


def _safe_trade_runtime(state: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    fn = getattr(strategy_manager, "get_trade_runtime_config", None)
    if callable(fn):
        try:
            data = fn(state)
            return dict(data or {}) if isinstance(data, Mapping) else {}
        except TypeError:
            try:
                data = fn()
                return dict(data or {}) if isinstance(data, Mapping) else {}
            except Exception:
                pass
        except Exception:
            pass

    s = dict(state or _safe_strategy_state())
    return {
        "margin_usdt": s.get("margin_usdt", s.get("trade_margin_usdt", 0.0)),
        "leverage": s.get("leverage", 1),
        "max_positions": s.get("max_positions", 0),
        "real_trading_enabled": s.get("real_trading_enabled", s.get("trade_enabled", False)),
    }


def _safe_real_trading_enabled(state: Optional[Mapping[str, Any]] = None) -> bool:
    fn = getattr(strategy_manager, "is_real_trading_enabled", None)
    if callable(fn):
        try:
            return bool(fn(state))
        except TypeError:
            try:
                return bool(fn())
            except Exception:
                pass
        except Exception:
            pass
    data = dict(state or _safe_strategy_state())
    return bool(data.get("real_trading_enabled", data.get("trade_enabled", False)))


def _margin_range_payload(data: Mapping[str, Any]) -> dict[str, Any]:
    """Return normalized AI margin range fields for display only."""
    d = dict(data or {})
    rt = dict(d.get("runtime") or {}) if isinstance(d.get("runtime"), Mapping) else {}

    margin = d.get("margin_usdt", rt.get("margin_usdt"))
    min_margin = d.get("min_margin_usdt", rt.get("min_margin_usdt", margin))
    max_margin = d.get("max_margin_usdt", rt.get("max_margin_usdt", margin))

    min_v = safe_float(min_margin, None)
    max_v = safe_float(max_margin, None)
    margin_v = safe_float(margin, None)

    if min_v is None and margin_v is not None:
        min_v = margin_v
    if max_v is None and margin_v is not None:
        max_v = margin_v
    if min_v is None:
        min_v = 0.0
    if max_v is None:
        max_v = min_v
    if min_v > max_v:
        max_v = min_v

    mode = safe_str(d.get("position_sizing_mode", rt.get("position_sizing_mode", ""))).upper()
    dynamic = d.get("dynamic_position_sizing_enabled", rt.get("dynamic_position_sizing_enabled", None))
    if dynamic is None:
        dynamic_bool = bool(max_v > min_v)
    else:
        dynamic_bool = bool(dynamic)
    if not mode:
        mode = "AI_DYNAMIC" if dynamic_bool else "FIXED"

    return {
        "min_margin_usdt": min_v,
        "max_margin_usdt": max_v,
        "dynamic_position_sizing_enabled": dynamic_bool,
        "position_sizing_mode": mode,
    }


def render_margin_range_lines(data: Mapping[str, Any]) -> list[str]:
    """Build Persian display lines for AI min/max margin range."""
    payload = _margin_range_payload(data)
    mode = safe_str(payload.get("position_sizing_mode")).upper()
    dynamic = bool(payload.get("dynamic_position_sizing_enabled"))
    mode_text = "AI_DYNAMIC ✅" if dynamic or mode == "AI_DYNAMIC" else "FIXED / ثابت"
    return [
        "🎯 مدیریت سرمایه AI",
        f"حداقل مارجین: {fmt_usdt(payload.get('min_margin_usdt'))}",
        f"حداکثر مارجین: {fmt_usdt(payload.get('max_margin_usdt'))}",
        f"حالت حجم‌دهی: {mode_text}",
    ]


# =============================================================================
# Generic formatting helpers
# =============================================================================

def fmt_float(value: Any, digits: int = 2, default: str = "-") -> str:
    v = safe_float(value, None)
    if v is None:
        return default
    return f"{v:.{digits}f}"


def fmt_price(value: Any, default: str = "-") -> str:
    v = safe_float(value, None)
    if v is None:
        return default
    if abs(v) >= 100:
        return f"{v:.2f}"
    if abs(v) >= 1:
        return f"{v:.4f}"
    return f"{v:.8f}".rstrip("0").rstrip(".")


def fmt_usdt(value: Any, default: str = "-") -> str:
    v = safe_float(value, None)
    if v is None:
        return default
    sign = "+" if v > 0 else ""
    return f"{sign}{v:.2f}$"


def fmt_pct(value: Any, default: str = "-") -> str:
    v = safe_float(value, None)
    if v is None:
        return default
    return f"{v:.1f}%"


def direction_fa(direction: str) -> str:
    d = normalize_direction(direction)
    if d == DIRECTION_LONG:
        return "لانگ 🟢"
    if d == DIRECTION_SHORT:
        return "شورت 🔴"
    return "نامشخص"


def mode_fa(mode: str) -> str:
    m = safe_str(mode).upper()
    if m == MODE_REAL:
        return "REAL / واقعی"
    if m == MODE_GHOST:
        return "GHOST / آموزشی"
    if m == MODE_REJECT:
        return "REJECT / رد شده"
    return m or "نامشخص"


def status_fa(status: str) -> str:
    s = safe_str(status).upper()
    mapping = {
        "OK": "اوکی ✅",
        "FAILED": "خطا ❌",
        "ACTIVE_REAL": "فعال واقعی 🟢",
        "ACTIVE_GHOST": "فعال گوست 👻",
        "PENDING_REAL_CONFIRM": "در انتظار تایید واقعی ⏳",
        "PARTIAL_TP1": "TP1 خورده / رانر فعال ✅",
        "CLOSING": "در حال بستن ⏳",
        "CLOSED": "بسته شده ✅",
    }
    return mapping.get(s, s or "نامشخص")


def short_reasons(reason_codes: Any, limit: int = 5) -> str:
    if not reason_codes:
        return "-"
    if isinstance(reason_codes, str):
        items = [reason_codes]
    else:
        try:
            items = [safe_str(x) for x in list(reason_codes)]
        except Exception:
            items = [safe_str(reason_codes)]
    items = [x for x in items if x]
    return "، ".join(items[:limit]) if items else "-"


def header(title: str) -> str:
    return f"📌 {title}"


def footer_time() -> str:
    return f"⏱ {utc_now_iso()}"


# =============================================================================
# Strategy / status text
# =============================================================================

def render_strategy_status(state: Optional[Mapping[str, Any]] = None) -> str:
    state_data = dict(state or _safe_strategy_state())
    runtime = _safe_trade_runtime(state_data)
    level = safe_int(state_data.get("active_level", state_data.get("level")), STRATEGY_LEVEL) or STRATEGY_LEVEL
    trade_on = _safe_real_trading_enabled(state_data)

    margin_lines = render_margin_range_lines(runtime)
    lines = [
        header("وضعیت استراتژی Level 4"),
        f"سطح فعال: Level {level}",
        f"ترید واقعی: {'فعال ✅' if trade_on else 'غیرفعال / سیگنال‌ها GHOST می‌شوند ⚠️'}",
        f"مارجین فعلی/پایه: {fmt_usdt(runtime.get('margin_usdt'))}",
        *margin_lines,
        f"لوریج: {safe_int(runtime.get('leverage'), 1)}x",
        f"حداکثر پوزیشن‌ها: {safe_int(runtime.get('max_positions', runtime.get('max_concurrent_real_positions')), 0)}",
        "",
        "قانون: فقط همین Level برای تصمیم‌های جدید فعال است.",
    ]
    return "\n".join(lines)


def render_trade_runtime(runtime: Optional[Mapping[str, Any]] = None) -> str:
    """Render full trade/Toobit status when real_trade_manager payload is provided."""
    data = dict(runtime or _safe_trade_runtime())

    if "runtime" in data or "balance" in data or "toobit_open_positions" in data:
        rt = dict(data.get("runtime") or data)
        balance = dict(data.get("balance") or {})
        positions = list(data.get("toobit_open_positions") or [])
        local_positions = list(data.get("local_positions") or [])
        connected = bool(data.get("toobit_connected"))
        trade_on = bool(data.get("real_trading_enabled", rt.get("real_trading_enabled", False)))
        margin = data.get("margin_usdt", rt.get("margin_usdt"))
        margin_range_lines = render_margin_range_lines(data)
        leverage = data.get("leverage", rt.get("leverage"))
        max_real = data.get("max_concurrent_real_positions", rt.get("max_concurrent_real_positions", rt.get("max_positions")))
        max_total = data.get("max_concurrent_total_positions", rt.get("max_concurrent_total_positions", max_real))
        watchlist = data.get("watchlist") or rt.get("watchlist") or []

        # Closed REAL PnL/statistics for the trade status panel.
        # render_trade_runtime() is a rendering layer only, so it reads the
        # already aggregated snapshot from stats_engine and never writes state.
        real_stats: dict[str, Any] = {}
        try:
            snapshot = build_stats_snapshot()
            real_section = snapshot.get("real", {}) if isinstance(snapshot, Mapping) else {}
            if isinstance(real_section, Mapping):
                real_stats = dict(real_section)
        except Exception:
            real_stats = {}

        real_closed_total = safe_int(real_stats.get("total"), 0) or 0
        real_wins = safe_int(real_stats.get("wins"), 0) or 0
        real_losses = safe_int(real_stats.get("losses"), 0) or 0
        real_tp1 = safe_int(real_stats.get("tp1"), 0) or 0
        real_tp2 = safe_int(real_stats.get("tp2"), 0) or 0
        real_sl = safe_int(real_stats.get("sl"), 0) or 0
        real_ai_exit = safe_int(real_stats.get("ai_exit"), 0) or 0
        real_pnl = safe_float(real_stats.get("pnl_usdt"), 0.0) or 0.0
        real_confirmed_pnl = safe_float(real_stats.get("confirmed_pnl_usdt"), None)
        real_unconfirmed_pnl = safe_float(real_stats.get("unconfirmed_pnl_usdt"), None)

        lines = [
            "⚙️ وضعیت ترید و Toobit",
            f"اتصال Toobit: {'وصل ✅' if connected else 'قطع / نامشخص ❌'}",
            f"ترید واقعی: {'روشن ✅' if trade_on else 'خاموش ❌'}",
            "سیگنال خودکار: روشن ✅",
            f"سرمایه پایه/فعلی هر ترید: {fmt_usdt(margin)}",
            *margin_range_lines,
            f"لوریج: {safe_int(leverage, 1)}x",
            f"حداکثر پوزیشن REAL: {safe_int(max_real, 0)}",
            f"اسلات REAL استفاده‌شده: {safe_int(data.get('effective_real_open', data.get('toobit_open_total', data.get('local_real_open', 0))), 0)} / {safe_int(max_real, 0)}",
            f"اسلات REAL آزاد: {safe_int(data.get('available_real_slots'), 0)}" + (" ⚠️ بالاتر از سقف" if bool(data.get('real_slots_over_limit')) else ""),
            f"حداکثر کل پوزیشن: {safe_int(max_total, 0)}",
            f"Margin Mode: {safe_str(data.get('margin_mode', rt.get('margin_mode', 'ISOLATED'))).upper()}",
            "",
            "💰 کیف پول Toobit",
            f"موجودی کل: {fmt_usdt(balance.get('balance'), default='نامشخص')}",
            f"قابل استفاده: {fmt_usdt(balance.get('available'), default='نامشخص')}",
            f"وضعیت موجودی: {safe_str(balance.get('status'), '-')}",
            f"خطای موجودی: {safe_str(balance.get('error'), '-')}" if balance.get("error") else "",
            "",
            f"📌 پوزیشن‌های باز Toobit: {safe_int(data.get('toobit_open_total'), len(positions))}",
            f"PnL باز Toobit: {fmt_usdt(data.get('toobit_pnl_usdt'))}",
            "",
            "📊 سود/ضرر بسته‌شده REAL",
            f"PnL کل بسته‌شده: {fmt_usdt(real_pnl)}",
            f"PnL تاییدشده: {fmt_usdt(real_confirmed_pnl)}" if real_confirmed_pnl is not None else "PnL تاییدشده: -",
            f"PnL تخمینی/تاییدنشده: {fmt_usdt(real_unconfirmed_pnl)}" if real_unconfirmed_pnl is not None else "PnL تخمینی/تاییدنشده: -",
            f"تعداد نتایج REAL: {real_closed_total}",
            f"برد: {real_wins} | باخت: {real_losses} | WinRate: {fmt_pct(real_stats.get('win_rate'))}",
            f"TP1: {real_tp1} | TP2: {real_tp2} | SL: {real_sl} | AI Exit: {real_ai_exit}",
        ]
        if positions:
            for p in positions[:10]:
                lines.append(
                    f"• {normalize_symbol(p.get('symbol'))} {normalize_direction(p.get('direction'))} | "
                    f"qty:{fmt_float(p.get('quantity'), 6)} | entry:{fmt_price(p.get('entry'))} | "
                    f"mark:{fmt_price(p.get('mark'))} | PnL:{fmt_usdt(p.get('pnl_usdt'))} | lev:{safe_int(p.get('leverage'), 0)}x"
                )
        else:
            lines.append("پوزیشن باز Toobit پیدا نشد.")

        effective_real = safe_int(data.get('effective_real_open', data.get('toobit_open_total', data.get('local_real_open', 0))), 0) or 0
        toobit_real = safe_int(data.get('toobit_open_total'), len(positions)) or 0
        local_real = safe_int(data.get('local_real_open'), 0) or 0
        local_ghost = safe_int(data.get('local_ghost_open'), 0) or 0
        local_total = safe_int(data.get('local_open_total'), len(local_positions)) or 0

        lines.extend([
            "",
            "📂 وضعیت داخلی/واقعی پوزیشن‌ها",
            f"REAL واقعی Toobit: {toobit_real}",
            f"REAL موثر برای سقف اسلات: {effective_real} / {safe_int(max_real, 0)}",
            f"پوزیشن‌های داخلی ربات: {local_total}",
            f"REAL داخلی ربات: {local_real} | GHOST داخلی: {local_ghost}",
        ])
        if toobit_real != local_real:
            lines.append("⚠️ اختلاف داخلی/Toobit: برای REAL، عدد Toobit منبع اصلی است.")

        if watchlist:
            lines.extend(["", "🔎 ارزهای فعال:", ", ".join(str(x) for x in watchlist)])

        errors = data.get("errors") or []
        if errors:
            lines.extend(["", "⚠️ هشدارها:", short_reasons(errors, limit=6)])
        return "\n".join(lines)

    margin_lines = render_margin_range_lines(data)
    lines = [
        header("تنظیمات ترید"),
        f"مارجین پایه/فعلی: {fmt_usdt(data.get('margin_usdt'))}",
        *margin_lines,
        f"لوریج: {safe_int(data.get('leverage'), 1)}x",
        f"حداکثر پوزیشن: {safe_int(data.get('max_positions', data.get('max_concurrent_real_positions')), 0)}",
        f"وضعیت ترید واقعی: {'فعال ✅' if bool(data.get('real_trading_enabled')) else 'غیرفعال ⚠️'}",
    ]
    return "\n".join(lines)


def render_ai_status(summary: Optional[Mapping[str, Any]] = None) -> str:
    data = dict(summary or {})
    lines = [
        "🧠 وضعیت هوش مصنوعی و یادگیری",
        f"Learning samples: {safe_int(data.get('total_records'), 0)}",
        f"Coin buckets: {safe_int(data.get('coin_buckets'), 0)}",
        f"Condition buckets: {safe_int(data.get('condition_buckets'), 0)}",
        f"Wins: {safe_int(data.get('wins'), 0)} | Losses: {safe_int(data.get('losses'), 0)}",
        f"WinRate: {fmt_pct(data.get('win_rate'))}",
        f"TP2: {safe_int(data.get('tp2'), 0)}",
        f"Updated: {safe_str(data.get('updated_at'), '-')}",
        "",
        "یادگیری بر اساس REAL و GHOST و به‌صورت coin/direction/condition ذخیره می‌شود.",
    ]
    return "\n".join(lines)


def render_reset_stats_result(result: Any) -> str:
    ok = bool(getattr(result, 'recorded', False)) or safe_str(getattr(result, 'status', '')).upper() == STATUS_OK
    return "✅ آمار و حافظه یادگیری پاک شد. پوزیشن‌های باز حذف نشدند." if ok else "❌ حذف آمار انجام نشد."


# =============================================================================
# Signal / AI decision text
# =============================================================================

def render_tp_sl(plan: Optional[TPSLPlan]) -> str:
    if plan is None:
        return "TP/SL: ناموجود"

    lines = [
        "🎯 پلن TP/SL",
        f"Entry: {fmt_price(plan.entry)}",
        f"TP1: {fmt_price(plan.tp1)}",
        f"TP2: {fmt_price(plan.tp2)}" if plan.tp2 else "TP2: -",
        f"SL: {fmt_price(plan.sl)}",
        f"RR: {fmt_float(plan.rr, 2)}",
        f"سود خالص تخمینی TP1: {fmt_usdt(plan.tp1_net_profit_estimate)}",
        f"اعتبار پلن: {'معتبر ✅' if plan.valid else 'نامعتبر ❌'}",
    ]
    if plan.reason_codes:
        lines.append(f"دلایل: {short_reasons(plan.reason_codes)}")
    return "\n".join(lines)


def render_ai_decision(decision: AIDecision, *, compact: bool = False) -> str:
    symbol = normalize_symbol(decision.symbol)
    mode = safe_str(decision.mode).upper()
    icon = "🟢" if mode == MODE_REAL else "👻" if mode == MODE_GHOST else "⛔"
    title = "سیگنال Level 4" if mode != MODE_REJECT else "سیگنال رد شد"

    lines = [
        f"{icon} {title}",
        f"Symbol: {symbol}",
        f"Direction: {direction_fa(decision.direction)}",
        f"Mode: {mode_fa(mode)}",
        f"Score: {fmt_pct(decision.score)} | Confidence: {fmt_pct(decision.confidence)}",
        f"Entry: {fmt_price(decision.entry)}",
    ]

    if mode == MODE_REJECT:
        lines.append(f"علت رد: {decision.reject_reason or short_reasons(decision.reason_codes)}")
    else:
        lines.append(render_tp_sl(decision.tp_sl))

    if not compact:
        meta = decision.metadata if isinstance(decision.metadata, Mapping) else {}
        raw_scores = meta.get("component_scores")
        if isinstance(raw_scores, Mapping):
            lines.extend([
                "",
                "🧠 امتیاز لایه‌ها",
                f"Structure: {fmt_pct(raw_scores.get('structure'))}",
                f"Momentum: {fmt_pct(raw_scores.get('momentum'))}",
                f"Liquidity: {fmt_pct(raw_scores.get('liquidity'))}",
                f"Context: {fmt_pct(raw_scores.get('context'))}",
                f"Reversal Safety: {fmt_pct(raw_scores.get('reversal'))}",
                f"Timing: {fmt_pct(raw_scores.get('timing'))}",
                f"TP/SL: {fmt_pct(raw_scores.get('tp_sl'))}",
            ])
        lines.append(f"دلایل: {short_reasons(decision.reason_codes)}")

    return "\n".join(lines)


# =============================================================================
# Position / monitor text
# =============================================================================

def render_position(position: TradePosition, *, compact: bool = False) -> str:
    lines = [
        f"📍 پوزیشن {mode_fa(position.mode)}",
        f"Symbol: {normalize_symbol(position.symbol)}",
        f"Direction: {direction_fa(position.direction)}",
        f"Status: {status_fa(position.status)}",
        f"Entry: {fmt_price(position.entry)}",
        f"Current: {fmt_price(position.current_price)}",
        f"TP1: {fmt_price(position.tp1)} | TP2: {fmt_price(position.tp2)} | SL: {fmt_price(position.sl)}",
    ]

    if position.tp1_hit:
        lines.append(f"TP1: خورده ✅ | Runner: {fmt_float(position.runner_quantity, 6)}")
        if position.protected_sl:
            lines.append(f"SL محافظتی: {fmt_price(position.protected_sl)}")

    if not compact:
        lines.extend([
            f"Quantity: {fmt_float(position.quantity, 6)}",
            f"Highest: {fmt_price(position.highest_price)} | Lowest: {fmt_price(position.lowest_price)}",
            f"Level: {safe_int(position.level, STRATEGY_LEVEL)}",
        ])

    return "\n".join(lines)


def render_positions_list(positions: list[TradePosition], *, title: str = "پوزیشن‌ها") -> str:
    if not positions:
        return f"{header(title)}\nپوزیشن فعالی وجود ندارد."

    lines = [header(title)]
    for idx, position in enumerate(positions, start=1):
        lines.append(
            f"{idx}) {normalize_symbol(position.symbol)} | {direction_fa(position.direction)} | "
            f"{mode_fa(position.mode)} | {status_fa(position.status)} | Entry {fmt_price(position.entry)}"
        )
    return "\n".join(lines)


def render_outcome(outcome: TradeOutcome) -> str:
    event = safe_str(outcome.event).upper()
    if event == "TP1":
        icon = "✅"
        title = "TP1 HIT"
    elif event == "TP2":
        icon = "🏁"
        title = "TP2 HIT"
    elif event == "SL":
        icon = "❌"
        title = "STOP LOSS"
    elif "AI_EXIT" in event:
        icon = "🧠"
        title = "AI EXIT"
    else:
        icon = "ℹ️"
        title = event

    lines = [
        f"{icon} {title}",
        f"Symbol: {normalize_symbol(outcome.symbol)}",
        f"Direction: {direction_fa(outcome.direction)}",
        f"Mode: {mode_fa(outcome.mode)}",
        f"Entry: {fmt_price(outcome.entry)}",
        f"Exit: {fmt_price(outcome.exit_price)}",
        f"PnL: {fmt_usdt(outcome.pnl_usdt)} ({fmt_pct(outcome.pnl_pct)})",
        f"PnL واقعی: {'تایید شده ✅' if outcome.pnl_confirmed else 'تخمینی / تایید نشده ⚠️'}",
        f"MFE: {fmt_pct(outcome.mfe_pct)} | MAE: {fmt_pct(outcome.mae_pct)}",
    ]
    return "\n".join(lines)


def render_monitor_event(event: MonitorEvent) -> str:
    if event.outcome is not None:
        return render_outcome(event.outcome)

    lines = [
        f"ℹ️ رویداد مانیتور: {safe_str(event.event).upper()}",
        f"Symbol: {normalize_symbol(event.symbol)}",
        f"Direction: {direction_fa(event.direction)}",
        f"Mode: {mode_fa(event.mode)}",
        f"Status: {status_fa(event.status)}",
    ]
    if event.status == STATUS_FAILED:
        err = ""
        if event.close_result is not None:
            err = event.close_result.error
        if not err and isinstance(event.metadata, Mapping):
            err = safe_str(event.metadata.get("error"))
        lines.append(f"خطا: {err or '-'}")
    return "\n".join(lines)


# =============================================================================
# Stats text
# =============================================================================

def render_stats_block(stats: Mapping[str, Any], title: str) -> str:
    lines = [
        header(title),
        f"Total: {safe_int(stats.get('total'), 0)}",
        f"Wins: {safe_int(stats.get('wins'), 0)} | Losses: {safe_int(stats.get('losses'), 0)}",
        f"Win Rate: {fmt_pct(stats.get('win_rate'))}",
        f"TP1: {safe_int(stats.get('tp1'), 0)} | TP2: {safe_int(stats.get('tp2'), 0)} | SL: {safe_int(stats.get('sl'), 0)}",
        f"TP2 Rate: {fmt_pct(stats.get('tp2_rate'))}",
        f"PnL: {fmt_usdt(stats.get('pnl_usdt'))}",
    ]
    return "\n".join(lines)


def render_stats_snapshot(snapshot: Optional[Mapping[str, Any]] = None) -> str:
    snap = dict(snapshot or build_stats_snapshot())

    lines = [
        "📊 آمار Level 4",
        "",
        render_stats_block(snap.get("global", {}), "کل"),
        "",
        render_stats_block(snap.get("real", {}), "REAL"),
        "",
        render_stats_block(snap.get("ghost", {}), "GHOST"),
    ]

    ai_exit = snap.get("ai_exit", {})
    if isinstance(ai_exit, Mapping):
        lines.extend([
            "",
            "🧠 AI Exit",
            f"تعداد: {safe_int(ai_exit.get('ai_exit_count'), 0)}",
            f"خروج در سود: {safe_int(ai_exit.get('ai_exit_profit_count'), 0)}",
            f"خروج در ضرر: {safe_int(ai_exit.get('ai_exit_loss_count'), 0)}",
            f"درصد خروج سودده: {fmt_pct(ai_exit.get('ai_exit_profit_rate'))}",
        ])

    positions = snap.get("positions", {})
    if isinstance(positions, Mapping):
        lines.extend([
            "",
            "📍 پوزیشن‌های داخلی ربات",
            f"باز داخلی: {safe_int(positions.get('open_total'), 0)}",
            f"REAL داخلی فعال: {safe_int(positions.get('active_real'), 0)}",
            f"GHOST داخلی فعال: {safe_int(positions.get('active_ghost'), 0)}",
            f"Partial TP1 داخلی: {safe_int(positions.get('partial_tp1'), 0)}",
        ])

    return "\n".join(lines)


# =============================================================================
# Command/help/error text
# =============================================================================

def render_help() -> str:
    lines = [
        "📚 راهنمای ربات Level 4",
        "",
        "وضعیت و کنترل ترید:",
        "• ترید — نمایش کامل وضعیت Toobit، موجودی، پوزیشن‌ها، لوریج و تنظیمات",
        "• وضعیت ترید — همان خروجی کامل ترید",
        "• ترید فعال — روشن کردن ترید واقعی",
        "• ترید خاموش — خاموش کردن ترید واقعی؛ سیگنال‌های بعدی GHOST می‌شوند",
        "• ترید دلار 7 / دلار ترید 7 / حجم ترید 7 — تنظیم مارجین ثابت هر ترید",
        "• حداقل 5 — تنظیم حداقل مارجین AI از 1 تا 1000 دلار",
        "• حداکثر 15 — تنظیم حداکثر مارجین AI از 1 تا 1000 دلار",
        "• ترید دلار حداقل 5 / ترید دلار حداکثر 15 — همان تنظیم بازه مارجین AI",
        "• لوریج 10 — تنظیم لوریج ثابت",
        "• حداکثر پوزیشن 3 — تنظیم سقف پوزیشن REAL",
        "• ریست ترید — برگشت تنظیمات ترید به پیش‌فرض",
        "• توقف فوری — خاموش کردن اضطراری ترید واقعی",
        "",
        "استراتژی:",
        "• استراتژی لول 4 — فعال‌سازی Level 4",
        "• وضعیت استراتژی — نمایش سطح فعال",
        "• لیست استراتژی — نمایش لول‌ها و وضعیت فعال/غیرفعال",
        "",
        "تحلیل و سیگنال:",
        "• بررسی DOGEUSDT / تحلیل DOGEUSDT — تحلیل یک نماد",
        "• اسکن / بررسی — اسکن واچ‌لیست Level 4",
        "",
        "آمار و هوش مصنوعی:",
        "• آمار — آمار REAL/GHOST/TP/SL/AI Exit",
        "• حذف آمار — پاک کردن آمار و حافظه یادگیری؛ پوزیشن باز حذف نمی‌شود",
        "• هوش مصنوعی — وضعیت یادگیری AI",
        "",
        "پوزیشن:",
        "• پوزیشن ها — نمایش پوزیشن‌های باز داخلی ربات",
        "• بستن DOGEUSDT — بستن پوزیشن REAL همان نماد با تایید Toobit",
        "",
        "نکته: دستورات تنظیمی واقعی ذخیره و اجرا می‌شوند؛ نمایشی نیستند.",
    ]
    return "\n".join(lines)

def render_error(message: str, *, title: str = "خطا") -> str:
    return f"❌ {title}\n{safe_str(message, 'خطای نامشخص')}"


def render_ok(message: str) -> str:
    return f"✅ {safe_str(message, 'انجام شد')}"


def render_unknown_command() -> str:
    return "دستور نامشخص است. برای راهنما بنویس: راهنما"


def validate_rendered_text(text: str, *, max_len: int = 4096) -> dict[str, Any]:
    errors: list[str] = []
    if not isinstance(text, str) or not text.strip():
        errors.append("EMPTY_TEXT")
    if len(text) > max_len:
        errors.append("TEXT_TOO_LONG")
    return {
        "status": STATUS_OK if not errors else STATUS_FAILED,
        "valid": not errors,
        "errors": errors,
        "length": len(text) if isinstance(text, str) else 0,
    }


__all__ = [
    "TELEGRAM_UI_VERSION",
    "fmt_float",
    "fmt_price",
    "fmt_usdt",
    "fmt_pct",
    "direction_fa",
    "mode_fa",
    "status_fa",
    "short_reasons",
    "header",
    "footer_time",
    "render_strategy_status",
    "render_margin_range_lines",
    "render_trade_runtime",
    "render_ai_status",
    "render_reset_stats_result",
    "render_tp_sl",
    "render_ai_decision",
    "render_position",
    "render_positions_list",
    "render_outcome",
    "render_monitor_event",
    "render_stats_block",
    "render_stats_snapshot",
    "render_help",
    "render_error",
    "render_ok",
    "render_unknown_command",
    "validate_rendered_text",
]
