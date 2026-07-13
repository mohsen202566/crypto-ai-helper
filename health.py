from __future__ import annotations

import time


class HealthManager:
    EXPECTED_MAX_AGE = {
        "scan": 180,
        "watch": 30,
        "monitor": 30,
        "telegram": 30,
        "toobit": 60,
        "learning": 7200,
    }

    def __init__(self, storage):
        self.storage = storage

    def mark(self, component: str, symbol_id: str | None = None) -> None:
        self.storage.set(f"health_{component}_ts", int(time.time()))

    def report(self) -> str:
        events = self.storage.active_health_events()
        labels = (
            ("scan", "اسکن"),
            ("watch", "واچ"),
            ("monitor", "مانیتور"),
            ("telegram", "تلگرام"),
            ("toobit", "توبیت"),
            ("learning", "یادگیری"),
        )
        lines = ["🩺 پنل سلامت", ""]
        now = int(time.time())
        for component, label in labels:
            ts = int(self.storage.get(f"health_{component}_ts", 0) or 0)
            if not ts:
                lines.append(f"{label}: ⚠️ هنوز ثبت نشده")
                continue
            age = max(0, now - ts)
            max_age = self.EXPECTED_MAX_AGE.get(component, 120)
            icon = "✅" if age <= max_age else "⚠️"
            state = "فعال" if age <= max_age else "قدیمی/متوقف"
            lines.append(f"{label}: {icon} {state} | {age} ثانیه قبل")
        if not events:
            lines.extend(["", "✅ مشکل فعالی ثبت نشده."])
        else:
            lines.extend(["", "🚨 مشکلات فعال:"])
            for event in events[:10]:
                lines.append(f"{event['severity']} | {event['component']} | {event.get('symbol_id') or '-'} | {event['message']}")
        return "\n".join(lines)
