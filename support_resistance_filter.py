"""فیلتر حمایت/مقاومت چندساعته برای سیگنال‌های ۲ تا ۳ ساعته.

فقط سطح‌هایی مهم‌اند که در کندل‌های 1H/4H باعث برگشت واقعی چندساعته شده‌اند.
این فایل داخل حلقه ورود فقط بعد از ساخته‌شدن کاندید سیگنال اجرا می‌شود تا سرعت ربات کم نشود.
"""
from __future__ import annotations

from typing import Any

import config
from utils import format_num, logger


class SupportResistanceFilter:
    def __init__(self) -> None:
        pass

    @staticmethod
    def _pct(a: float, b: float) -> float:
        if b <= 0:
            return 999.0
        return abs(a - b) / b * 100.0

    @staticmethod
    def _safe_float(x: Any, default: float = 0.0) -> float:
        try:
            return float(x)
        except Exception:
            return default

    def _pivot_events(self, candles: list[dict[str, Any]], timeframe: str) -> list[dict[str, Any]]:
        if len(candles) < 20:
            return []
        swing = int(getattr(config, "SR_PIVOT_SWING_CANDLES", 2))
        lookahead = int(getattr(config, "SR_REACTION_LOOKAHEAD_CANDLES_4H", 4) if timeframe == "4H" else getattr(config, "SR_REACTION_LOOKAHEAD_CANDLES_1H", 6))
        min_reaction = float(getattr(config, "SR_MIN_REACTION_PERCENT", 0.60))
        events: list[dict[str, Any]] = []
        n = len(candles)
        for i in range(swing, max(swing, n - lookahead - 1)):
            row = candles[i]
            low = self._safe_float(row.get("low"))
            high = self._safe_float(row.get("high"))
            if low <= 0 or high <= 0:
                continue
            window = candles[i - swing : i + swing + 1]
            lows = [self._safe_float(x.get("low")) for x in window]
            highs = [self._safe_float(x.get("high")) for x in window]
            future = candles[i + 1 : i + lookahead + 1]
            if not future:
                continue

            # حمایت: کف محلی که بعدش قیمت چندساعته برگشته بالا.
            if low <= min(lows):
                future_high = max(self._safe_float(x.get("high")) for x in future)
                reaction = (future_high - low) / low * 100.0 if low > 0 else 0.0
                if reaction >= min_reaction:
                    events.append({
                        "type": "support",
                        "price": low,
                        "reaction_percent": reaction,
                        "timeframe": timeframe,
                        "time": row.get("open_time"),
                        "index": i,
                    })

            # مقاومت: سقف محلی که بعدش قیمت چندساعته برگشته پایین.
            if high >= max(highs):
                future_low = min(self._safe_float(x.get("low")) for x in future)
                reaction = (high - future_low) / high * 100.0 if high > 0 else 0.0
                if reaction >= min_reaction:
                    events.append({
                        "type": "resistance",
                        "price": high,
                        "reaction_percent": reaction,
                        "timeframe": timeframe,
                        "time": row.get("open_time"),
                        "index": i,
                    })
        return events

    def _cluster(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        width_pct = float(getattr(config, "SR_ZONE_WIDTH_PERCENT", 0.30))
        clusters: list[dict[str, Any]] = []
        for ev in sorted(events, key=lambda x: float(x.get("price") or 0)):
            price = float(ev["price"])
            typ = str(ev["type"])
            placed = False
            for c in clusters:
                if c["type"] != typ:
                    continue
                if self._pct(price, float(c["mid"])) <= width_pct:
                    c["prices"].append(price)
                    c["events"].append(ev)
                    c["mid"] = sum(c["prices"]) / len(c["prices"])
                    c["zone_min"] = min(c["prices"]) * (1 - width_pct / 100.0)
                    c["zone_max"] = max(c["prices"]) * (1 + width_pct / 100.0)
                    placed = True
                    break
            if not placed:
                clusters.append({
                    "type": typ,
                    "mid": price,
                    "zone_min": price * (1 - width_pct / 100.0),
                    "zone_max": price * (1 + width_pct / 100.0),
                    "prices": [price],
                    "events": [ev],
                })

        out: list[dict[str, Any]] = []
        min_touches = int(getattr(config, "SR_MIN_TOUCHES", 2))
        min_strength = float(getattr(config, "SR_MIN_STRENGTH", 4.5))
        for c in clusters:
            events2 = c["events"]
            touches = len(events2)
            max_reaction = max(float(e.get("reaction_percent") or 0) for e in events2) if events2 else 0.0
            avg_reaction = sum(float(e.get("reaction_percent") or 0) for e in events2) / max(1, touches)
            h4_touches = sum(1 for e in events2 if e.get("timeframe") == "4H")
            h1_touches = sum(1 for e in events2 if e.get("timeframe") == "1H")
            strength = touches * 1.4 + min(avg_reaction, 2.5) + h4_touches * 1.6 + h1_touches * 0.4
            c.update({
                "touches": touches,
                "h1_touches": h1_touches,
                "h4_touches": h4_touches,
                "max_reaction_percent": max_reaction,
                "avg_reaction_percent": avg_reaction,
                "strength": round(strength, 2),
                "is_strong": touches >= min_touches and strength >= min_strength,
            })
            if c["is_strong"]:
                out.append(c)
        return sorted(out, key=lambda x: (-float(x.get("strength") or 0), float(x.get("mid") or 0)))

    def build_levels(self, candles_1h: list[dict[str, Any]], candles_4h: list[dict[str, Any]]) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        events.extend(self._pivot_events(candles_1h, "1H"))
        events.extend(self._pivot_events(candles_4h, "4H"))
        return self._cluster(events)

    def check_path(
        self,
        *,
        symbol: str,
        side: str,
        entry: float,
        tp: float,
        candles_1h: list[dict[str, Any]],
        candles_4h: list[dict[str, Any]],
    ) -> tuple[bool, str, dict[str, Any]]:
        """بررسی می‌کند بین ورود تا TP مانع قوی چندساعته هست یا نه."""
        try:
            levels = self.build_levels(candles_1h, candles_4h)
        except Exception as exc:
            logger.warning("ساخت حمایت/مقاومت %s ناموفق بود؛ برای جلوگیری از کرش، این فیلتر عبور داده شد: %s", symbol, exc)
            return True, "حمایت/مقاومت قابل محاسبه نبود؛ ربات بدون توقف ادامه داد", {"levels": []}

        side = str(side or "").upper()
        barriers: list[dict[str, Any]] = []
        if side == "BUY":
            lo, hi = min(entry, tp), max(entry, tp)
            for lv in levels:
                if lv.get("type") != "resistance":
                    continue
                zmin = float(lv.get("zone_min") or 0)
                zmax = float(lv.get("zone_max") or 0)
                mid = float(lv.get("mid") or 0)
                # مقاومت اگر داخل مسیر تا TP باشد یا قیمت همین الان داخل ناحیه مقاومت باشد، مانع است.
                if (lo < zmin < hi) or (zmin <= entry <= zmax) or (lo < mid < hi):
                    barriers.append(lv)
        else:
            lo, hi = min(entry, tp), max(entry, tp)
            for lv in levels:
                if lv.get("type") != "support":
                    continue
                zmin = float(lv.get("zone_min") or 0)
                zmax = float(lv.get("zone_max") or 0)
                mid = float(lv.get("mid") or 0)
                # حمایت اگر داخل مسیر تا TP باشد یا قیمت همین الان داخل ناحیه حمایت باشد، مانع است.
                if (lo < zmax < hi) or (zmin <= entry <= zmax) or (lo < mid < hi):
                    barriers.append(lv)

        meta = {"levels_count": len(levels), "barriers": barriers[:3]}
        if barriers:
            b = sorted(barriers, key=lambda x: abs(float(x.get("mid") or 0) - entry))[0]
            kind = "مقاومت" if side == "BUY" else "حمایت"
            msg = (
                f"{kind} قوی چندساعته بین ورود و TP قرار دارد؛ "
                f"ناحیه {format_num(b.get('zone_min'), 6)} تا {format_num(b.get('zone_max'), 6)} "
                f"| قدرت {format_num(b.get('strength'), 2)} | لمس‌ها {b.get('touches')}"
            )
            meta["blocked_by"] = b
            return False, msg, meta

        nearest_support = None
        nearest_resistance = None
        supports = [x for x in levels if x.get("type") == "support"]
        resistances = [x for x in levels if x.get("type") == "resistance"]
        below = [x for x in supports if float(x.get("mid") or 0) < entry]
        above = [x for x in resistances if float(x.get("mid") or 0) > entry]
        if below:
            nearest_support = max(below, key=lambda x: float(x.get("mid") or 0))
        if above:
            nearest_resistance = min(above, key=lambda x: float(x.get("mid") or 0))
        meta["nearest_support"] = nearest_support
        meta["nearest_resistance"] = nearest_resistance
        return True, "مسیر تا TP از حمایت/مقاومت چندساعته قوی تمیز است", meta
