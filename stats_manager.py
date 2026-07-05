from __future__ import annotations

import time
from datetime import datetime

from signal_manager import SignalStore


class StatsManager:
    def __init__(self, store: SignalStore) -> None:
        self.store = store

    def render_stats(self) -> str:
        signals = self.store.all()
        open_items = [s for s in signals if s.status == "باز"]
        closed = [s for s in signals if s.status != "باز"]
        tp = [s for s in closed if s.status == "تیپی خورد"]
        sl = [s for s in closed if s.status == "استاپ خورد"]
        real = [s for s in signals if s.signal_type == "رئال"]
        normal = [s for s in signals if s.signal_type == "نرمال"]
        total_net = sum(s.net_pnl or 0.0 for s in closed)
        today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        today_net = sum(s.net_pnl or 0.0 for s in closed if (s.closed_at or 0) >= today_start)
        win_rate = (len(tp) / len(closed) * 100) if closed else 0.0

        return (
            "📊 آمار ربات\n\n"
            f"کل سیگنال‌ها: {len(signals)}\n"
            f"رئال: {len(real)}\n"
            f"نرمال: {len(normal)}\n"
            f"سیگنال باز: {len(open_items)}\n"
            f"سیگنال بسته: {len(closed)}\n"
            f"✅ تیپی: {len(tp)}\n"
            f"❌ استاپ: {len(sl)}\n"
            f"وین‌ریت: {win_rate:.2f}%\n"
            f"سود یا ضرر کلی: {total_net:.4f} دلار\n"
            f"سود یا ضرر امروز: {today_net:.4f} دلار"
        )
