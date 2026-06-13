# -*- coding: utf-8 -*-
"""
Paper Trader

وظیفه:
- ثبت پوزیشن‌های آزمایشی
- باز کردن پوزیشن وقتی سیگنال ACTIVE ارسال شد
- بستن پوزیشن هنگام TP/SL توسط signal_tracker
- نگهداری امن دیتا در JSON
"""

import time
import uuid
from typing import Dict, List, Optional, Any

try:
    from data_store import load_json, save_json
except Exception:
    import json
    import os

    DATA_DIR = "data"
    os.makedirs(DATA_DIR, exist_ok=True)

    def load_json(filename: str, default=None):
        path = os.path.join(DATA_DIR, filename)
        if default is None:
            default = {}
        try:
            if not os.path.exists(path):
                return default
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default

    def save_json(filename: str, data):
        path = os.path.join(DATA_DIR, filename)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


PAPER_FILE = "paper_trades.json"


def now_ts() -> int:
    return int(time.time())


def load_paper_state() -> Dict:
    state = load_json(PAPER_FILE, default={})

    if not isinstance(state, dict):
        state = {}

    state.setdefault("open_positions", {})
    state.setdefault("closed_positions", [])
    state.setdefault("stats", {
        "total": 0,
        "tp1": 0,
        "tp2": 0,
        "sl": 0,
        "manual_closed": 0,
    })

    return state


def save_paper_state(state: Dict) -> None:
    save_json(PAPER_FILE, state)


def make_position_id(symbol: str, direction: str) -> str:
    return f"paper_{symbol}_{direction}_{now_ts()}_{uuid.uuid4().hex[:6]}"


def normalize_direction(direction: str) -> str:
    direction = str(direction).upper().strip()

    if direction in ["LONG", "BUY", "لانگ"]:
        return "LONG"

    if direction in ["SHORT", "SELL", "شورت"]:
        return "SHORT"

    return direction


def calculate_pnl_percent(
    direction: str,
    entry: float,
    exit_price: float,
) -> float:
    try:
        entry = float(entry)
        exit_price = float(exit_price)

        if entry <= 0:
            return 0.0

        if direction == "LONG":
            return round(((exit_price - entry) / entry) * 100, 4)

        if direction == "SHORT":
            return round(((entry - exit_price) / entry) * 100, 4)

        return 0.0

    except Exception:
        return 0.0


def has_open_position(
    symbol: str,
    direction: Optional[str] = None,
) -> bool:
    state = load_paper_state()
    open_positions = state.get("open_positions", {})

    symbol = str(symbol).upper().strip()

    for pos in open_positions.values():
        if pos.get("symbol") != symbol:
            continue

        if direction is None:
            return True

        if pos.get("direction") == normalize_direction(direction):
            return True

    return False


def find_open_position(
    symbol: str,
    direction: Optional[str] = None,
    signal_id: Optional[str] = None,
) -> Optional[Dict]:
    state = load_paper_state()
    open_positions = state.get("open_positions", {})

    symbol = str(symbol).upper().strip()
    direction = normalize_direction(direction) if direction else None

    for position_id, pos in open_positions.items():
        if signal_id and pos.get("signal_id") == signal_id:
            item = dict(pos)
            item["position_id"] = position_id
            return item

        if pos.get("symbol") != symbol:
            continue

        if direction and pos.get("direction") != direction:
            continue

        item = dict(pos)
        item["position_id"] = position_id
        return item

    return None

def open_paper_position(
    signal: Dict[str, Any],
    telegram_message_id: Optional[int] = None,
    chat_id: Optional[int] = None,
) -> Optional[Dict]:
    """
    وقتی bot.py یک سیگنال ACTIVE ارسال کرد، این تابع پوزیشن Paper را باز می‌کند.
    """

    if not isinstance(signal, dict):
        return None

    symbol = str(signal.get("symbol", "")).upper().strip()
    direction = normalize_direction(signal.get("direction"))

    if not symbol or direction not in ["LONG", "SHORT"]:
        return None

    if has_open_position(symbol, direction):
        return None

    entry = signal.get("entry") or signal.get("price")
    stop_loss = signal.get("stop_loss")
    tp1 = signal.get("tp1")
    tp2 = signal.get("tp2")

    if entry is None or stop_loss is None or tp1 is None:
        return None

    state = load_paper_state()
    position_id = make_position_id(symbol, direction)

    position = {
        "position_id": position_id,
        "signal_id": signal.get("signal_id"),
        "symbol": symbol,
        "direction": direction,

        "entry": float(entry),
        "stop_loss": float(stop_loss),
        "tp1": float(tp1),
        "tp2": float(tp2) if tp2 is not None else None,

        "score": signal.get("score"),
        "risk_level": signal.get("risk_level"),
        "risk_reward": signal.get("risk_reward"),

        "status": "OPEN",
        "opened_at": now_ts(),

        "telegram_message_id": telegram_message_id,
        "chat_id": chat_id,

        "snapshot": signal.get("snapshot", {}),
        "source": signal.get("source", "auto_signal"),
    }

    state["open_positions"][position_id] = position
    save_paper_state(state)

    return position


def close_paper_position(
    symbol: str,
    direction: str,
    exit_price: float,
    result: str,
    signal_id: Optional[str] = None,
) -> Optional[Dict]:
    """
    توسط signal_tracker هنگام TP1/TP2/SL صدا زده می‌شود.
    """

    direction = normalize_direction(direction)

    state = load_paper_state()
    open_positions = state.get("open_positions", {})

    target_id = None
    target_pos = None

    for position_id, pos in open_positions.items():
        if signal_id and pos.get("signal_id") == signal_id:
            target_id = position_id
            target_pos = pos
            break

        if (
            pos.get("symbol") == str(symbol).upper().strip()
            and pos.get("direction") == direction
        ):
            target_id = position_id
            target_pos = pos
            break

    if not target_id or not target_pos:
        return None

    entry = float(target_pos.get("entry", 0))
    exit_price = float(exit_price)

    pnl_percent = calculate_pnl_percent(
        direction=direction,
        entry=entry,
        exit_price=exit_price,
    )

    closed = dict(target_pos)
    closed.update({
        "status": "CLOSED",
        "result": result,
        "exit_price": exit_price,
        "pnl_percent": pnl_percent,
        "closed_at": now_ts(),
    })

    del open_positions[target_id]

    state.setdefault("closed_positions", [])
    state["closed_positions"].append(closed)

    stats = state.setdefault("stats", {
        "total": 0,
        "tp1": 0,
        "tp2": 0,
        "sl": 0,
        "manual_closed": 0,
    })

    stats["total"] = int(stats.get("total", 0)) + 1

    result_key = str(result).lower()

    if result_key in ["tp1", "tp"]:
        stats["tp1"] = int(stats.get("tp1", 0)) + 1
    elif result_key == "tp2":
        stats["tp2"] = int(stats.get("tp2", 0)) + 1
    elif result_key == "sl":
        stats["sl"] = int(stats.get("sl", 0)) + 1
    else:
        stats["manual_closed"] = int(stats.get("manual_closed", 0)) + 1

    save_paper_state(state)

    return closed


def close_paper_position_by_signal_id(
    signal_id: str,
    exit_price: float,
    result: str,
) -> Optional[Dict]:
    state = load_paper_state()

for pos in state.get("open_positions", {}).values():
        if pos.get("signal_id") == signal_id:
            return close_paper_position(
                symbol=pos.get("symbol"),
                direction=pos.get("direction"),
                exit_price=exit_price,
                result=result,
                signal_id=signal_id,
            )

    return None


def get_open_positions() -> List[Dict]:
    state = load_paper_state()

    positions = []

    for position_id, pos in state.get("open_positions", {}).items():
        item = dict(pos)
        item["position_id"] = position_id
        positions.append(item)

    return positions


def get_closed_positions(limit: int = 20) -> List[Dict]:
    state = load_paper_state()
    closed = state.get("closed_positions", [])

    if not isinstance(closed, list):
        return []

    return closed[-limit:]


def get_paper_stats() -> Dict:
    state = load_paper_state()
    stats = state.get("stats", {})

    total = int(stats.get("total", 0))
    tp1 = int(stats.get("tp1", 0))
    tp2 = int(stats.get("tp2", 0))
    sl = int(stats.get("sl", 0))

    wins = tp1 + tp2
    win_rate = round((wins / total) * 100, 2) if total > 0 else 0

    return {
        "total": total,
        "tp1": tp1,
        "tp2": tp2,
        "sl": sl,
        "manual_closed": int(stats.get("manual_closed", 0)),
        "win_rate": win_rate,
        "open_positions": len(state.get("open_positions", {})),
    }


def format_paper_stats() -> str:
    stats = get_paper_stats()

    return (
        "📊 آمار Paper Trade\n"
        f"کل معاملات بسته‌شده: {stats['total']}\n"
        f"TP1: {stats['tp1']}\n"
        f"TP2: {stats['tp2']}\n"
        f"SL: {stats['sl']}\n"
        f"وین‌ریت: {stats['win_rate']}٪\n"
        f"پوزیشن‌های باز: {stats['open_positions']}"
    )


def format_open_positions() -> str:
    positions = get_open_positions()

    if not positions:
        return "پوزیشن Paper بازی وجود ندارد."

    lines = ["📌 پوزیشن‌های Paper باز:"]

    for pos in positions:
        lines.append(
            f"\n{pos.get('symbol')} | {pos.get('direction')}\n"
            f"Entry: {pos.get('entry')}\n"
            f"SL: {pos.get('stop_loss')}\n"
            f"TP1: {pos.get('tp1')}\n"
            f"TP2: {pos.get('tp2')}"
        )

    return "\n".join(lines)


def reset_paper_trades() -> bool:
    state = {
        "open_positions": {},
        "closed_positions": [],
        "stats": {
            "total": 0,
            "tp1": 0,
            "tp2": 0,
            "sl": 0,
            "manual_closed": 0,
        },
    }

    save_paper_state(state)
    return True
