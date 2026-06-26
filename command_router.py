"""Telegram command router for Crypto AI Helper bot.

Locked responsibility:
- Parses user commands and updates/reads StateStore settings.
- Renders command responses through telegram_ui.py data models/renderers.
- May call injected status/reset hooks, but never talks to OKX or Toobit directly.
- Does not analyze markets, decide entries, calculate TP/SL, open/close orders, or manage learning.

Design lock:
- Small, simple, strong.
- Persian commands are the public interface.
- Level 4 / 1H Smart Scalp is the only active strategy command here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from config import (
    CMD_AI,
    CMD_COINS,
    CMD_MAX_POSITIONS,
    CMD_MIN_NET_PROFIT,
    CMD_POSITIONS,
    CMD_SETTINGS,
    CMD_STATS,
    CMD_TRADE,
    CMD_TRADE_CAPITAL,
    CMD_TRADE_DOLLAR,
    CMD_TRADE_LEVERAGE,
    CMD_TRADE_OFF,
    CMD_TRADE_ON,
    LEVERAGE_MAX,
    LEVERAGE_MIN,
    MAX_POSITIONS_MAX,
    MAX_POSITIONS_MIN,
    MIN_NET_PROFIT_MAX,
    MIN_NET_PROFIT_MIN,
    TARGET_HOLD_MINUTES,
    TIMEFRAME,
    TRADE_CAPITAL_MAX,
    TRADE_CAPITAL_MIN,
    TRADE_DOLLAR_MAX,
    TRADE_DOLLAR_MIN,
    WATCHLIST,
)
from state_store import StateStore
from telegram_ui import StatsPanelData, TradePanelData, render_invalid_value, render_stats_panel, render_trade_panel

try:
    import strategy_manager
except Exception:  # pragma: no cover - router must compile without optional project layer.
    strategy_manager = None  # type: ignore


CommandStatus = str
StatusProvider = Callable[[], Mapping[str, Any]]
ResetHook = Callable[[str], Mapping[str, Any] | None]
ReplySender = Callable[[str], None]


@dataclass(frozen=True)
class CommandResult:
    status: CommandStatus
    handled: bool
    text: str
    command: str = ""
    metadata: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return self.status == "OK"


class CommandRouter:
    """Small Telegram command parser.

    The router owns commands only.  It writes settings through StateStore and
    reads status either from StateStore or from an injected status_provider.
    """

    def __init__(
        self,
        store: StateStore,
        *,
        status_provider: StatusProvider | None = None,
        reset_hook: ResetHook | None = None,
        reply_sender: ReplySender | None = None,
    ) -> None:
        self.store = store
        self.status_provider = status_provider
        self.reset_hook = reset_hook
        self.reply_sender = reply_sender

    def handle(self, text: str) -> CommandResult:
        raw = _clean(text)
        normalized = _normalize_command(raw)
        if not normalized:
            return CommandResult(status="IGNORED", handled=False, text="", command="")

        try:
            result = self._handle_normalized(normalized, raw)
        except ValueError as exc:
            result = CommandResult(status="FAILED", handled=True, text=f"❌ {exc}", command=normalized)
        except Exception as exc:
            result = CommandResult(status="FAILED", handled=True, text=f"❌ خطای دستور: {exc}", command=normalized)

        if result.handled and result.text and self.reply_sender is not None:
            self.reply_sender(result.text)
        return result

    def _handle_normalized(self, normalized: str, raw: str) -> CommandResult:
        if normalized in {"راهنما", "help", "/start", "start"}:
            return _ok(normalized, render_help())

        if normalized in {_normalize_command(CMD_TRADE_ON), "trade on", "real on"}:
            self.store.set_real_trade_enabled(True)
            return _ok(normalized, "✅ ترید واقعی فعال شد.\nاز این به بعد اگر همه شروط قفل‌شده پاس شود، مسیر REAL مجاز است.")

        if normalized in {_normalize_command(CMD_TRADE_OFF), "trade off", "real off"}:
            self.store.set_real_trade_enabled(False)
            return _ok(normalized, "✅ ترید واقعی خاموش شد.\nسیگنال‌ها فقط به حالت SIGNAL ذخیره و مانیتور می‌شوند.")

        if normalized in {"اتو سیگنال فعال", "auto on", "signal on"}:
            self.store.set_auto_signal_enabled(True)
            return _ok(normalized, "✅ اتو سیگنال فعال شد.")

        if normalized in {"اتو سیگنال خاموش", "auto off", "signal off"}:
            self.store.set_auto_signal_enabled(False)
            return _ok(normalized, "✅ اتو سیگنال خاموش شد.")

        if normalized in {_normalize_command(CMD_TRADE), _normalize_command(CMD_SETTINGS), "پنل", "وضعیت", "status"}:
            return _ok(normalized, self.render_trade_panel())

        if normalized in {_normalize_command(CMD_STATS), "stats"}:
            return _ok(normalized, self.render_stats_panel())

        if normalized in {_normalize_command(CMD_COINS), "coins", "واچ لیست", "واچ‌لیست"}:
            return _ok(normalized, render_coin_list())

        if normalized in {_normalize_command(CMD_POSITIONS), "positions", "پوزیشن ها", "پوزیشن‌ها"}:
            return _ok(normalized, self.render_positions())

        if normalized in {_normalize_command(CMD_AI), "ai"}:
            return _ok(normalized, render_ai_status())

        if normalized in {"استراتژی", "وضعیت استراتژی", "لیست استراتژی"}:
            return _ok(normalized, render_strategy_status())

        if normalized.startswith("استراتژی لول") or normalized.startswith("strategy level"):
            level = _last_int(raw)
            if level != 4:
                return CommandResult(
                    status="FAILED",
                    handled=True,
                    text="❌ فقط استراتژی لول 4 قفل و فعال است.\nدستور درست: استراتژی لول 4",
                    command=normalized,
                )
            _set_strategy_level_4()
            return _ok(normalized, "✅ استراتژی روی لول 4 تنظیم شد.\n⏱️ تایم‌فریم: 1H\n🎯 هدف: Smart Scalp با کیفیت ورود، نه دنبال‌کردن حرکت")

        # Numeric setting commands.  Persian command name + one numeric value.
        if normalized.startswith(_normalize_command(CMD_TRADE_DOLLAR)):
            value = _required_float(raw, CMD_TRADE_DOLLAR)
            self.store.set_trade_dollar(value)
            return _ok(normalized, f"✅ دلار هر پوزیشن روی {value:.2f} USDT تنظیم شد.")

        if normalized.startswith(_normalize_command(CMD_TRADE_LEVERAGE)):
            value = _required_int(raw, CMD_TRADE_LEVERAGE)
            self.store.set_leverage(value)
            return _ok(normalized, f"✅ لوریج روی {value}x تنظیم شد.")

        if normalized.startswith(_normalize_command(CMD_TRADE_CAPITAL)):
            value = _required_float(raw, CMD_TRADE_CAPITAL)
            self.store.set_trade_capital(value)
            return _ok(normalized, f"✅ سرمایه مجاز ربات روی {value:.2f} USDT تنظیم شد.")

        if normalized.startswith(_normalize_command(CMD_MAX_POSITIONS)):
            value = _required_int(raw, CMD_MAX_POSITIONS)
            self.store.set_max_slots(value)
            return _ok(normalized, f"✅ حداکثر پوزیشن همزمان روی {value} تنظیم شد.")

        if normalized.startswith(_normalize_command(CMD_MIN_NET_PROFIT)):
            value = _required_float(raw, CMD_MIN_NET_PROFIT)
            self.store.set_min_net_profit(value)
            return _ok(normalized, f"✅ حداقل سود خالص روی {value:.2f} USDT تنظیم شد.")

        if normalized in {"ریست آمار", "reset stats"}:
            return self._reset("stats")

        if normalized in {"ریست سیگنال", "ریست سیگنال‌ها", "reset signals"}:
            return self._reset("signals")

        return CommandResult(status="IGNORED", handled=False, text="", command=normalized)

    def _reset(self, kind: str) -> CommandResult:
        if self.reset_hook is not None:
            data = self.reset_hook(kind) or {}
            if data.get("error"):
                return CommandResult(status="FAILED", handled=True, text=f"❌ ریست انجام نشد: {data.get('error')}", command=f"reset:{kind}")
        else:
            _local_reset(self.store, kind)
        label = "آمار" if kind == "stats" else "سیگنال‌ها"
        return _ok(f"reset:{kind}", f"✅ ریست {label} انجام شد.")

    def render_trade_panel(self) -> str:
        snapshot = self.store.snapshot()
        settings = snapshot.settings
        live_status = self._status_snapshot()
        toobit_margin = _first_not_none(live_status.get("toobit_margin_usdt"), live_status.get("margin_usdt"), snapshot.toobit_margin_usdt)
        open_positions = int(_first_not_none(live_status.get("toobit_open_total"), live_status.get("open_positions"), snapshot.used_slots) or 0)
        free_slots = max(0, int(settings.max_slots) - open_positions)
        data = TradePanelData(
            auto_signal_enabled=bool(settings.auto_signal_enabled),
            real_trade_enabled=bool(settings.real_trade_enabled),
            toobit_margin_usdt=None if toobit_margin is None else float(toobit_margin),
            trade_capital_usdt=float(settings.trade_capital_usdt),
            trade_dollar_usdt=float(settings.trade_dollar_usdt),
            leverage=int(settings.leverage),
            min_net_profit_usdt=float(settings.min_net_profit_usdt),
            max_slots=int(settings.max_slots),
            open_positions=open_positions,
            free_slots=free_slots,
        )
        return render_trade_panel(data)

    def render_stats_panel(self) -> str:
        stats = self.store.snapshot().stats
        return render_stats_panel(
            StatsPanelData(
                real_signals=int(stats.real_signals),
                real_monitoring=int(stats.real_monitoring),
                real_tp=int(stats.real_tp),
                real_sl=int(stats.real_sl),
                real_win_rate=float(stats.real_win_rate),
                real_pnl_usdt=float(stats.real_pnl_usdt),
                signal_only_total=int(stats.signal_only_total),
                signal_only_tp=int(stats.signal_only_tp),
                signal_only_sl=int(stats.signal_only_sl),
                signal_only_win_rate=float(stats.signal_only_win_rate),
            )
        )

    def render_positions(self) -> str:
        active = list(self.store.snapshot().active_signals.values())
        if not active:
            return "📭 پوزیشن/سیگنال فعالی وجود ندارد."
        lines = ["📂 پوزیشن‌ها و سیگنال‌های فعال", ""]
        for item in active:
            mode = "🏦 توبیت" if item.mode == "TOOBIT" else "📊 سیگنال"
            direction = "لانگ 🟢" if item.direction == "LONG" else "شورت 🔴"
            lines.extend([
                f"{mode} | {item.symbol}",
                f"جهت: {direction}",
                f"ورود: {item.entry}",
                f"TP: {item.tp}",
                f"SL: {item.sl}",
                f"وضعیت: {item.status}",
                "",
            ])
        return "\n".join(lines).strip()

    def _status_snapshot(self) -> dict[str, Any]:
        if self.status_provider is None:
            return {}
        try:
            data = self.status_provider()
            return dict(data) if isinstance(data, Mapping) else {}
        except Exception:
            return {}


def handle_command(
    text: str,
    *,
    store: StateStore | None = None,
    status_provider: StatusProvider | None = None,
    reset_hook: ResetHook | None = None,
    reply_sender: ReplySender | None = None,
) -> CommandResult:
    router = CommandRouter(store or StateStore(), status_provider=status_provider, reset_hook=reset_hook, reply_sender=reply_sender)
    return router.handle(text)


def render_help() -> str:
    return "\n".join([
        "📌 دستورات ربات",
        "",
        "ترید / وضعیت / تنظیمات",
        "ترید فعال",
        "ترید خاموش",
        "اتو سیگنال فعال",
        "اتو سیگنال خاموش",
        "",
        "ترید دلار 7",
        "ترید لوریج 10",
        "سرمایه ترید 100",
        "حداکثر پوزیشن 1",
        "حداقل سود خالص 0.10",
        "",
        "استراتژی لول 4",
        "آمار",
        "پوزیشن",
        "کوین‌ها",
        "ریست آمار",
        "ریست سیگنال‌ها",
    ])


def render_coin_list() -> str:
    lines = ["🪙 کوین‌های قفل‌شده Level 4 / 1H", ""]
    for symbol, coin in WATCHLIST.items():
        lines.append(f"• {coin.fa_name} | {symbol}")
    return "\n".join(lines)


def render_ai_status() -> str:
    return "\n".join([
        "🧠 وضعیت تصمیم‌گیری",
        "لول فعال: 4",
        f"تایم‌فریم: {TIMEFRAME}",
        f"هدف نگهداری: {TARGET_HOLD_MINUTES[0]} تا {TARGET_HOLD_MINUTES[1]} دقیقه",
        "ورود: کیفیت محور، نه دنبال‌کردن حرکت",
    ])


def render_strategy_status() -> str:
    return "\n".join([
        "📌 استراتژی فعال",
        "Level 4 / 1H Smart Scalp",
        "فقط همین لول برای سیگنال‌های جدید فعال است.",
        "دستور تغییر: استراتژی لول 4",
    ])


def _set_strategy_level_4() -> None:
    if strategy_manager is None:
        return
    for name in ("set_strategy_level", "set_active_strategy", "activate_strategy_level"):
        fn = getattr(strategy_manager, name, None)
        if callable(fn):
            try:
                fn(4)
                return
            except TypeError:
                try:
                    fn(level=4)
                    return
                except TypeError:
                    continue


def _local_reset(store: StateStore, kind: str) -> None:
    state = store.snapshot()
    if kind == "stats":
        # Preserve active records; reset closed counters only.
        from state_store import StatsState

        state.stats = StatsState()
        for record in state.active_signals.values():
            if record.mode == "TOOBIT":
                state.stats.real_signals += 1
                state.stats.real_monitoring += 1
            else:
                state.stats.signal_only_total += 1
                state.stats.signal_only_monitoring += 1
        store.save()
        return
    if kind == "signals":
        state.active_signals.clear()
        store._recalculate_monitoring_counts()  # noqa: SLF001 - command-level maintenance hook.
        store.save()
        return
    raise ValueError("نوع ریست نامعتبر است.")


def _required_float(raw: str, command_name: str) -> float:
    values = _numbers(raw)
    if not values:
        raise ValueError(f"برای دستور «{command_name}» عدد وارد کن.")
    value = float(values[-1])
    _validate_command_range(command_name, value)
    return value


def _required_int(raw: str, command_name: str) -> int:
    value = int(_required_float(raw, command_name))
    _validate_command_range(command_name, value)
    return value


def _validate_command_range(command_name: str, value: float | int) -> None:
    ranges: dict[str, tuple[float | int, float | int]] = {
        CMD_TRADE_DOLLAR: (TRADE_DOLLAR_MIN, TRADE_DOLLAR_MAX),
        CMD_TRADE_LEVERAGE: (LEVERAGE_MIN, LEVERAGE_MAX),
        CMD_TRADE_CAPITAL: (TRADE_CAPITAL_MIN, TRADE_CAPITAL_MAX),
        CMD_MAX_POSITIONS: (MAX_POSITIONS_MIN, MAX_POSITIONS_MAX),
        CMD_MIN_NET_PROFIT: (MIN_NET_PROFIT_MIN, MIN_NET_PROFIT_MAX),
    }
    if command_name not in ranges:
        return
    lo, hi = ranges[command_name]
    if not float(lo) <= float(value) <= float(hi):
        raise ValueError(render_invalid_value(command_name, lo, hi))


def _numbers(raw: str) -> list[float]:
    normalized = _persian_digits_to_english(raw).replace(",", ".")
    out: list[float] = []
    token = ""
    for char in normalized:
        if char.isdigit() or char in {".", "-"}:
            token += char
        else:
            if token not in {"", ".", "-"}:
                try:
                    out.append(float(token))
                except ValueError:
                    pass
            token = ""
    if token not in {"", ".", "-"}:
        try:
            out.append(float(token))
        except ValueError:
            pass
    return out


def _last_int(raw: str) -> int | None:
    values = _numbers(raw)
    return None if not values else int(values[-1])


def _clean(text: str) -> str:
    return str(text or "").strip()


def _normalize_command(text: str) -> str:
    text = _persian_digits_to_english(text)
    text = text.replace("\u200c", " ").replace("_", " ").replace("-", " ")
    text = " ".join(text.strip().split())
    return text.lower()


def _persian_digits_to_english(text: str) -> str:
    table = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
    return text.translate(table)


def _first_not_none(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _ok(command: str, text: str, metadata: dict[str, Any] | None = None) -> CommandResult:
    return CommandResult(status="OK", handled=True, text=text, command=command, metadata=metadata)


__all__ = [
    "CommandResult",
    "CommandRouter",
    "handle_command",
    "render_ai_status",
    "render_coin_list",
    "render_help",
    "render_strategy_status",
]
