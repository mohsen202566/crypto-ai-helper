from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from config import STORAGE_DIR

REPORT_FILE = STORAGE_DIR / "auto_signal_report.json"
MAX_ITEMS = 30


@dataclass
class AutoSignalState:
    active: bool = True
    last_cycle_started_at: float | None = None
    last_check_at: float | None = None
    checked_count: int = 0
    rejected_count: int = 0
    signal_count: int = 0
    last_symbol: str = ""
    last_reason: str = ""
    recent_rejections: list[dict[str, Any]] = field(default_factory=list)
    recent_signals: list[dict[str, Any]] = field(default_factory=list)


class AutoSignalReport:
    def __init__(self, path: Path = REPORT_FILE) -> None:
        self.path = path
        self.path.parent.mkdir(exist_ok=True)
        self.state = self._load()

    def _load(self) -> AutoSignalState:
        if not self.path.exists():
            return AutoSignalState()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            allowed = {item.name for item in fields(AutoSignalState)}
            return AutoSignalState(**{k: v for k, v in data.items() if k in allowed})
        except Exception:
            return AutoSignalState()

    def _save(self) -> None:
        self.path.write_text(json.dumps(asdict(self.state), ensure_ascii=False, indent=2), encoding="utf-8")

    def start_cycle(self) -> None:
        self.state.active = True
        self.state.last_cycle_started_at = time.time()
        self.state.last_check_at = time.time()
        self.state.checked_count = 0
        self.state.rejected_count = 0
        self.state.signal_count = 0
        self.state.last_symbol = ""
        self.state.last_reason = ""
        self._save()

    def record_checked(self, symbol: str) -> None:
        self.state.active = True
        self.state.last_check_at = time.time()
        self.state.checked_count += 1
        self.state.last_symbol = symbol
        self._save()

    def record_rejected(self, symbol: str, reason: str, layer: str = "") -> None:
        now = time.time()
        item = {"time": now, "symbol": symbol, "reason": reason, "layer": layer}
        self.state.active = True
        self.state.last_check_at = now
        self.state.rejected_count += 1
        self.state.last_symbol = symbol
        self.state.last_reason = reason
        self.state.recent_rejections.insert(0, item)
        self.state.recent_rejections = self.state.recent_rejections[:MAX_ITEMS]
        self._save()

    def record_signal(self, symbol: str, signal_type: str, direction: str) -> None:
        now = time.time()
        item = {"time": now, "symbol": symbol, "signal_type": signal_type, "direction": direction}
        self.state.active = True
        self.state.last_check_at = now
        self.state.signal_count += 1
        self.state.last_symbol = symbol
        self.state.last_reason = "سیگنال ساخته شد"
        self.state.recent_signals.insert(0, item)
        self.state.recent_signals = self.state.recent_signals[:MAX_ITEMS]
        self._save()


def _ago(ts: float | None) -> str:
    if not ts:
        return "نامشخص"
    seconds = max(0, int(time.time() - ts))
    if seconds < 60:
        return f"{seconds} ثانیه پیش"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} دقیقه پیش"
    hours = minutes // 60
    return f"{hours} ساعت پیش"


def render_auto_signal_report(path: Path = REPORT_FILE) -> str:
    report = AutoSignalReport(path)
    s = report.state
    lines = [
        "📡 گزارش اتو سیگنال",
        "",
        f"وضعیت: {'فعال' if s.active else 'غیرفعال'}",
        f"آخرین بررسی: {_ago(s.last_check_at)}",
        f"نمادهای بررسی‌شده در دور آخر: {s.checked_count}",
        f"سیگنال ساخته‌شده در دور آخر: {s.signal_count}",
        f"نمادهای ردشده در دور آخر: {s.rejected_count}",
    ]
    if s.last_symbol:
        lines.append(f"آخرین نماد: {s.last_symbol}")
    if s.last_reason:
        lines.append(f"آخرین نتیجه: {s.last_reason}")

    lines.append("")
    lines.append("آخرین رد شدن‌ها:")
    if not s.recent_rejections:
        lines.append("موردی ثبت نشده.")
    else:
        for item in s.recent_rejections[:10]:
            layer = f" | {item.get('layer')}" if item.get("layer") else ""
            lines.append(f"{item.get('symbol')}: {item.get('reason')}{layer}")
    return "\n".join(lines)
