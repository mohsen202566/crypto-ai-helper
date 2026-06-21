from __future__ import annotations

"""
Reply Manager.

Responsibilities:
- Persist Telegram message/thread metadata for signals.
- Build reply payloads for SETUP / ENTRY / TP1 / TP2 / SL / CANCELLED.
- Avoid importing bot.py.
- Let bot.py send replies using this stored metadata.

This is the missing bridge between tracker results and Telegram replies.
"""

import time
from typing import Any, Dict, List, Optional

from data_store import load_dict, save_json
from diagnostics import safe
import signal_tracker
import ai_movement_hunter
import coins_fa


REPLY_FILE = "reply_map"


def _ts() -> int:
    return int(time.time())


def _empty_state() -> Dict[str, Any]:
    return {
        "version": 1,
        "updated_at": _ts(),
        "signals": {},
        "pending_replies": [],
        "sent_replies": [],
        "queued_results": {},
    }


@safe(default={})
def load_replies() -> Dict[str, Any]:
    st = load_dict(REPLY_FILE)
    if not st:
        st = _empty_state()
        save_json(REPLY_FILE, st)
    for k, v in _empty_state().items():
        st.setdefault(k, v)
    return st


@safe(default=False)
def save_replies(st: Dict[str, Any], make_backup: bool = False) -> bool:
    st["updated_at"] = _ts()
    st["pending_replies"] = st.get("pending_replies", [])[-1000:]
    st["sent_replies"] = st.get("sent_replies", [])[-1000:]
    return save_json(REPLY_FILE, st, make_backup=make_backup)


@safe(default=True)
def register_signal_message(
    signal_id: str,
    chat_id: int,
    message_id: int,
    symbol: str = "",
    direction: str = "",
    signal_type: str = "",
) -> bool:
    st = load_replies()
    st.setdefault("signals", {})[str(signal_id)] = {
        "signal_id": str(signal_id),
        "chat_id": int(chat_id),
        "message_id": int(message_id),
        "symbol": str(symbol).upper(),
        "direction": str(direction).upper(),
        "type": signal_type,
        "created_at": _ts(),
        "updated_at": _ts(),
    }
    save_replies(st)
    return True


@safe(default={})
def get_signal_message(signal_id: str) -> Dict[str, Any]:
    return load_replies().get("signals", {}).get(str(signal_id), {})


@safe(default=True)
def queue_reply(signal_id: str, event: str, text: str, force_chat_id: Optional[int] = None, force_reply_to: Optional[int] = None) -> bool:
    st = load_replies()
    meta = st.get("signals", {}).get(str(signal_id), {})
    chat_id = force_chat_id if force_chat_id is not None else meta.get("chat_id")
    reply_to = force_reply_to if force_reply_to is not None else meta.get("message_id")
    if not chat_id:
        return False
    st.setdefault("pending_replies", []).append({
        "id": f"reply_{int(time.time())}_{len(st.get('pending_replies', []))}",
        "signal_id": str(signal_id),
        "event": event,
        "chat_id": int(chat_id),
        "reply_to_message_id": int(reply_to) if reply_to else None,
        "text": text,
        "created_at": _ts(),
    })
    save_replies(st)
    return True


@safe(default=[])
def pop_pending_replies(limit: int = 20) -> List[Dict[str, Any]]:
    st = load_replies()
    rows = st.get("pending_replies", [])[:limit]
    st["pending_replies"] = st.get("pending_replies", [])[limit:]
    st.setdefault("sent_replies", []).extend([{**r, "popped_at": _ts()} for r in rows])
    save_replies(st)
    return rows


@safe(default="")
def setup_message_fa(decision: Dict[str, Any]) -> str:
    symbol = coins_fa.display_symbol(decision.get("symbol", ""))
    direction = "لانگ" if decision.get("direction") == "LONG" else "شورت"
    conf = round(float(decision.get("confidence", 0)) * 100, 1)
    return (
        f"🟡 ستاپ آماده / منتظر فعال‌سازی ورود\n"
        f"ارز: {symbol}\n"
        f"جهت احتمالی: {direction}\n"
        f"ورود احتمالی: {decision.get('entry')}\n"
        f"TP1: {decision.get('tp1')} | TP2: {decision.get('tp2')}\n"
        f"SL: {decision.get('sl')}\n"
        f"اعتماد AI: {conf}%"
    )


@safe(default="")
def active_signal_message_fa(decision: Dict[str, Any]) -> str:
    symbol = coins_fa.display_symbol(decision.get("symbol", ""))
    direction = "لانگ" if decision.get("direction") == "LONG" else "شورت"
    conf = round(float(decision.get("confidence", 0)) * 100, 1)
    return (
        f"✅ ورود فعال شد\n"
        f"ارز: {symbol}\n"
        f"جهت: {direction}\n"
        f"ورود: {decision.get('entry')}\n"
        f"TP1: {decision.get('tp1')} | TP2: {decision.get('tp2')}\n"
        f"SL: {decision.get('sl')}\n"
        f"اعتماد AI: {conf}%"
    )


@safe(default="")
def ghost_message_fa(decision: Dict[str, Any]) -> str:
    symbol = coins_fa.display_symbol(decision.get("symbol", ""))
    direction = "لانگ" if decision.get("direction") == "LONG" else "شورت"
    return (
        f"👻 سیگنال مخفی برای یادگیری\n"
        f"ارز: {symbol}\n"
        f"جهت: {direction}\n"
        f"ورود فرضی: {decision.get('entry')}"
    )


@safe(default="")
def result_message_from_signal(signal: Dict[str, Any]) -> str:
    return signal_tracker.result_message_fa(signal)



@safe(default=0)
def queue_recent_results_once(limit: int = 100) -> int:
    """
    Queue result replies for recently closed signals exactly once.
    Bot loop sends pending replies later.
    """
    st = load_replies()
    queued = st.setdefault("queued_results", {})
    count = 0
    for sig in signal_tracker.closed_signals(limit):
        sid = str(sig.get("signal_id", ""))
        if not sid or sid in queued:
            continue
        meta = st.get("signals", {}).get(sid, {})
        if not meta:
            continue
        text = result_message_from_signal(sig)
        chat_id = meta.get("chat_id")
        reply_to = meta.get("message_id")
        if not chat_id:
            continue
        st.setdefault("pending_replies", []).append({
            "id": f"reply_{int(time.time())}_{len(st.get('pending_replies', []))}",
            "signal_id": sid,
            "event": str(sig.get("result", "RESULT")),
            "chat_id": int(chat_id),
            "reply_to_message_id": int(reply_to) if reply_to else None,
            "text": text,
            "created_at": _ts(),
        })
        queued[sid] = {"queued_at": _ts(), "result": sig.get("result")}
        count += 1
    save_replies(st)
    return count


@safe(default=True)
def queue_result_if_known(signal_id: str) -> bool:
    closed = signal_tracker.closed_signals(200)
    for s in closed:
        if str(s.get("signal_id")) == str(signal_id):
            return queue_reply(signal_id, str(s.get("result", "RESULT")), result_message_from_signal(s))
    return False


@safe(default=True)
def initialize() -> bool:
    st = load_replies()
    save_replies(st)
    return True
