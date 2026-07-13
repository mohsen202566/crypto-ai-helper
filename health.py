"""گزارش سلامت اجزای ربات."""
from __future__ import annotations
import time
from storage import Storage

class HealthManager:
    def __init__(self, storage: Storage):
        self.storage = storage
        self.marks: dict[str, int] = {}
    def mark(self, name: str) -> None:
        self.marks[name] = int(time.time())
    @staticmethod
    def age(ts: int) -> str:
        if not ts:
            return "هنوز ثبت نشده"
        sec = max(0, int(time.time()) - ts)
        return f"{sec} ثانیه قبل" if sec < 60 else f"{sec//60} دقیقه قبل"
    def report(self) -> str:
        events = self.storage.active_health_events()
        connected = bool(self.storage.get("toobit_connected", False))
        status = "✅ سالم" if connected and not events else "⚠️ هشدار"
        lines = ["🩺 سلامت ربات 5m رفتارمحور", "", f"وضعیت کلی: {status}",
                 f"OKX Data: {self.age(self.marks.get('okx',0))}",
                 f"Toobit: {'✅ وصل' if connected else '❌ قطع/خطا'} | {self.age(int(self.storage.get('toobit_last_update',0) or 0))}",
                 f"Signal Engine: {self.age(self.marks.get('signal',0))}",
                 f"Monitoring: {self.age(self.marks.get('monitor',0))}",
                 f"Telegram: {self.age(self.marks.get('telegram',0))}"]
        black = self.storage.blacklist_rows()
        if black:
            lines += ["", f"بلک‌لیست موقت: {len(black)} ارز"]
        if events:
            lines += ["", "🚨 خطاهای فعال:"] + [f"{e['severity']} | {e['component']} | {e.get('symbol_id') or '-'} | {e['message']}" for e in events[:8]]
        return "\n".join(lines)
