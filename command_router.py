"""
command_router.py
Level 4 / 1H Smart Scalp Bot

Telegram command routing layer.

Architecture lock:
- Parses user text and returns a lightweight CommandRoute.
- Does not execute trades, make AI decisions, fetch market data, monitor positions,
  write JSON state, call Toobit, or send Telegram messages.
- bot.py is responsible for executing the route.
- Allowed project imports:
  constants.py, utils.py, telegram_ui.py only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from constants import STATUS_FAILED, STATUS_OK, STRATEGY_LEVEL, SYSTEM_VERSION
from telegram_ui import render_help, render_unknown_command
from utils import normalize_symbol, safe_float, safe_int, safe_str, utc_now_iso


COMMAND_ROUTER_VERSION: str = SYSTEM_VERSION


@dataclass
class CommandRoute:
    action: str
    status: str = STATUS_OK
    args: dict[str, Any] = field(default_factory=dict)
    raw_text: str = ""
    reply_text: str = ""
    reason: str = ""
    created_at: str = field(default_factory=utc_now_iso)
    system_version: str = SYSTEM_VERSION


def normalize_text(text: Any) -> str:
    t = safe_str(text).strip()
    replacements = {"ي": "ی", "ك": "ک", "\u200c": " ", "\n": " ", "\t": " "}
    for old, new in replacements.items():
        t = t.replace(old, new)
    while "  " in t:
        t = t.replace("  ", " ")
    return t.strip()


def text_tokens(text: str) -> list[str]:
    return [x for x in normalize_text(text).split(" ") if x]


def contains_any(text: str, words: set[str]) -> bool:
    normalized = normalize_text(text).lower()
    return any(w.lower() in normalized for w in words)


def first_number(text: str) -> Optional[int]:
    for token in text_tokens(text):
        cleaned = token.replace("x", "").replace("X", "")
        value = safe_int(cleaned, None)
        if value is not None:
            return value
    return None


def first_float(text: str) -> Optional[float]:
    for token in text_tokens(text):
        cleaned = token.replace("$", "").replace(",", ".")
        value = safe_float(cleaned, None)
        if value is not None:
            return value
    return None


def route(action: str, *, text: str = "", args: Optional[Mapping[str, Any]] = None, reply_text: str = "", reason: str = "", status: str = STATUS_OK) -> CommandRoute:
    return CommandRoute(action=action, status=status, args=dict(args or {}), raw_text=text, reply_text=reply_text, reason=reason)


def route_unknown(text: str = "") -> CommandRoute:
    return route("UNKNOWN", text=text, reply_text=render_unknown_command(), reason="unknown_command", status=STATUS_FAILED)


def _attach_context(command_route: CommandRoute, *, user_id: Optional[int] = None, chat_id: Optional[int] = None) -> CommandRoute:
    if user_id is not None:
        command_route.args["user_id"] = user_id
    if chat_id is not None:
        command_route.args["chat_id"] = chat_id
    return command_route


def parse_strategy_level(text: str) -> Optional[int]:
    normalized = normalize_text(text).lower()
    if "استراتژی" not in normalized and "strategy" not in normalized:
        return None
    if "لول" not in normalized and "level" not in normalized:
        return None
    value = first_number(normalized)
    if value is None:
        return None
    if 1 <= value <= 9:
        return value
    return None


def parse_symbol(text: str) -> str:
    for token in text_tokens(text):
        upper = token.upper().replace("/", "").replace("-", "")
        if upper.endswith("USDT") and len(upper) >= 6:
            return normalize_symbol(upper)
    return ""


def parse_runtime_update(text: str) -> Optional[CommandRoute]:
    normalized = normalize_text(text).lower()

    if any(x in normalized for x in [
        "مارجین", "margin", "مبلغ",
        "ترید دلار", "دلار ترید", "حجم ترید", "trade dollar", "trade dollars",
        "سرمایه ترید", "مقدار ترید",
    ]):
        value = first_float(normalized)
        if value is not None and value > 0:
            return route("SET_MARGIN", text=text, args={"margin_usdt": value})

    if any(x in normalized for x in ["لوریج", "leverage", "اهرم"]):
        value = first_number(normalized)
        if value is not None and value > 0:
            return route("SET_LEVERAGE", text=text, args={"leverage": value})

    if any(x in normalized for x in [
        "حداکثر پوزیشن", "max position", "max_positions", "مکس پوزیشن",
        "حداکثر معامله", "حداکثر ترید", "max trades", "max positions",
    ]):
        value = first_number(normalized)
        if value is not None and value > 0:
            return route("SET_MAX_POSITIONS", text=text, args={"max_positions": value})

    if normalized in {"ریست ترید", "ریست تنظیمات ترید", "reset trade", "reset settings"}:
        return route("RESET_TRADE_SETTINGS", text=text)

    return None


def parse_trade_toggle(text: str) -> Optional[CommandRoute]:
    normalized = normalize_text(text).lower()

    if "ترید" in normalized or "trade" in normalized:
        if any(x in normalized for x in ["فعال", "روشن", "on", "enable", "enabled"]):
            if not any(x in normalized for x in ["غیرفعال", "خاموش", "off", "disable", "disabled"]):
                return route("ENABLE_REAL_TRADING", text=text)
        if any(x in normalized for x in ["غیرفعال", "خاموش", "off", "disable", "disabled"]):
            return route("DISABLE_REAL_TRADING", text=text)

    if any(x in normalized for x in ["emergency", "اضطراری", "توقف فوری", "استاپ فوری"]):
        return route("EMERGENCY_STOP", text=text)

    return None


def parse_status_commands(text: str) -> Optional[CommandRoute]:
    normalized = normalize_text(text).lower()

    if normalized in {"راهنما", "help", "/help", "دستورات", "/start", "start"}:
        return route("HELP", text=text, reply_text=render_help())

    if normalized in {"لیست استراتژی", "لیست استراتژی ها", "لیست استراتژی‌ها", "strategy list", "list strategies"}:
        return route("LIST_STRATEGIES", text=text)

    if normalized in {"استراتژی", "وضعیت استراتژی", "strategy", "strategy status"}:
        return route("SHOW_STRATEGY", text=text)

    if normalized in {"وضعیت", "status", "/status"}:
        return route("SHOW_STATUS", text=text)

    if normalized in {"پوزیشن ها", "پوزیشن‌ها", "positions", "open positions", "پوزیشن"}:
        return route("SHOW_POSITIONS", text=text)

    if normalized in {"حذف آمار", "حذف امار", "ریست آمار", "ریست امار", "reset stats"}:
        return route("RESET_STATS", text=text)

    if normalized in {"آمار", "امار", "stats", "statistics"}:
        return route("SHOW_STATS", text=text)

    if normalized in {
        "هوش مصنوعی", "ai", "ai status", "وضعیت هوش مصنوعی",
        "آمار هوشمند", "امار هوشمند", "حافظه ربات",
    }:
        return route("SHOW_AI_STATUS", text=text)

    if normalized in {
        "ترید", "trade",
        "تنظیمات", "تنظیمات ترید", "وضعیت ترید", "trade settings",
        "settings", "trade status", "وضعیت معامله",
    }:
        return route("SHOW_TRADE_SETTINGS", text=text)

    return None


def parse_analysis_command(text: str) -> Optional[CommandRoute]:
    normalized = normalize_text(text).lower()

    if any(x in normalized for x in ["تحلیل", "بررسی", "analyze", "analysis"]):
        symbol = parse_symbol(text)
        if symbol:
            return route("ANALYZE_SYMBOL", text=text, args={"symbol": symbol})
        return route("SCAN_MARKET", text=text)

    if any(x in normalized for x in ["اسکن", "scan", "بازار"]):
        return route("SCAN_MARKET", text=text)

    return None


def parse_position_action(text: str) -> Optional[CommandRoute]:
    normalized = normalize_text(text).lower()
    symbol = parse_symbol(text)

    if any(x in normalized for x in ["بستن", "close", "خروج"]) and symbol:
        return route("REQUEST_CLOSE_POSITION", text=text, args={"symbol": symbol})

    if any(x in normalized for x in ["زیر نظر", "مانیتور", "monitor"]) and symbol:
        return route("WATCH_POSITION", text=text, args={"symbol": symbol})

    return None


def parse_command(text: Any, *, user_id: Optional[int] = None, chat_id: Optional[int] = None) -> CommandRoute:
    raw = safe_str(text)
    normalized = normalize_text(raw)

    if not normalized:
        return _attach_context(route_unknown(raw), user_id=user_id, chat_id=chat_id)

    level = parse_strategy_level(normalized)
    if level is not None:
        return _attach_context(route("SET_STRATEGY_LEVEL", text=raw, args={"level": level}), user_id=user_id, chat_id=chat_id)

    for parser in (
        parse_trade_toggle,
        parse_runtime_update,
        parse_status_commands,
        parse_analysis_command,
        parse_position_action,
    ):
        result = parser(normalized)
        if result is not None:
            result.raw_text = raw
            return _attach_context(result, user_id=user_id, chat_id=chat_id)

    return _attach_context(route_unknown(raw), user_id=user_id, chat_id=chat_id)


def validate_route(command_route: CommandRoute) -> dict[str, Any]:
    errors: list[str] = []
    if command_route.system_version != SYSTEM_VERSION:
        errors.append("INVALID_SYSTEM_VERSION")
    if not command_route.action:
        errors.append("MISSING_ACTION")
    if command_route.status not in {STATUS_OK, STATUS_FAILED}:
        errors.append("INVALID_STATUS")
    if not isinstance(command_route.args, dict):
        errors.append("ARGS_NOT_DICT")
    return {
        "status": STATUS_OK if not errors else STATUS_FAILED,
        "valid": not errors,
        "errors": errors,
        "action": command_route.action,
        "args": command_route.args,
    }


def route_to_dict(command_route: CommandRoute) -> dict[str, Any]:
    return {
        "system_version": command_route.system_version,
        "created_at": command_route.created_at,
        "action": command_route.action,
        "status": command_route.status,
        "args": dict(command_route.args),
        "raw_text": command_route.raw_text,
        "reply_text": command_route.reply_text,
        "reason": command_route.reason,
    }


__all__ = [
    "COMMAND_ROUTER_VERSION",
    "CommandRoute",
    "normalize_text",
    "text_tokens",
    "contains_any",
    "first_number",
    "first_float",
    "route",
    "route_unknown",
    "parse_strategy_level",
    "parse_symbol",
    "parse_runtime_update",
    "parse_trade_toggle",
    "parse_status_commands",
    "parse_analysis_command",
    "parse_position_action",
    "parse_command",
    "validate_route",
    "route_to_dict",
]
