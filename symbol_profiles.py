"""خواندن و نمایش بازه‌های اختصاصی روزانه برای هر ارز و هر جهت.

این فایل فقط یک لایه سبک روی بازه‌های ورود است:
- اگر data/symbol_profiles.json بازه معتبر امروز داشته باشد، همان را می‌دهد.
- اگر فایل نباشد، خراب باشد، یا نمونه کافی نباشد، بازه عمومی config برگردانده می‌شود.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import config
from utils import format_num, normalize_symbol


PROFILE_FILE = config.DATA_DIR / "symbol_profiles.json"


_FEATURE_LABELS = {
    "rsi": "RSI",
    "adx": "ADX",
    "volume_multiplier": "Volume",
    "vwap_distance_percent": "VWAP Distance",
    "atr_percent": "ATR",
    "bb_position": "BB Position",
    "ema_gap_percent": "EMA 9/21 Gap",
}


class SymbolProfileManager:
    def __init__(self, path: Path = PROFILE_FILE) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._mtime: float | None = None
        self._data: dict[str, Any] = {}

    @staticmethod
    def resolve_symbol(raw: str) -> str | None:
        cleaned = normalize_symbol(str(raw or ""))
        if not cleaned:
            return None
        if cleaned in config.WATCHLIST:
            return cleaned
        if not cleaned.endswith("USDT"):
            candidate = f"{cleaned}USDT"
            if candidate in config.WATCHLIST:
                return candidate
        for sym in config.WATCHLIST:
            base = sym.replace("USDT", "")
            if cleaned == base:
                return sym
        return None

    @staticmethod
    def side_key(side: str) -> str:
        s = str(side or "").upper()
        if s in ("BUY", "LONG", "خرید", "لانگ"):
            return "LONG"
        return "SHORT"

    @staticmethod
    def default_ranges(side_key: str) -> dict[str, dict[str, float]]:
        if side_key == "LONG":
            return {
                "rsi": {"min": config.ZONE_LONG_RSI_MIN, "max": config.ZONE_LONG_RSI_MAX},
                "adx": {"min": config.ZONE_ADX_MIN, "max": config.ZONE_ADX_MAX},
                "volume_multiplier": {"min": config.ZONE_VOLUME_MULTIPLIER_MIN, "max": config.ZONE_VOLUME_MULTIPLIER_MAX},
                "vwap_distance_percent": {"min": config.ZONE_VWAP_DISTANCE_MIN_PERCENT, "max": config.ZONE_VWAP_DISTANCE_MAX_PERCENT},
                "atr_percent": {"min": config.ATR_MIN_PERCENT, "max": config.ATR_MAX_PERCENT},
                "bb_position": {"min": -2.0, "max": config.ZONE_BB_LONG_MAX_POSITION},
                "ema_gap_percent": {"min": 0.0, "max": 10.0},
            }
        return {
            "rsi": {"min": config.ZONE_SHORT_RSI_MIN, "max": config.ZONE_SHORT_RSI_MAX},
            "adx": {"min": config.ZONE_ADX_MIN, "max": config.ZONE_ADX_MAX},
            "volume_multiplier": {"min": config.ZONE_VOLUME_MULTIPLIER_MIN, "max": config.ZONE_VOLUME_MULTIPLIER_MAX},
            "vwap_distance_percent": {"min": config.ZONE_VWAP_DISTANCE_MIN_PERCENT, "max": config.ZONE_VWAP_DISTANCE_MAX_PERCENT},
            "atr_percent": {"min": config.ATR_MIN_PERCENT, "max": config.ATR_MAX_PERCENT},
            "bb_position": {"min": config.ZONE_BB_SHORT_MIN_POSITION, "max": 3.0},
            "ema_gap_percent": {"min": -10.0, "max": 0.0},
        }

    @staticmethod
    def _is_valid_range(item: Any) -> bool:
        if not isinstance(item, dict):
            return False
        try:
            lo = float(item.get("min"))
            hi = float(item.get("max"))
            return lo <= hi
        except Exception:
            return False

    def _load_if_changed(self) -> None:
        with self._lock:
            try:
                mtime = self.path.stat().st_mtime
            except FileNotFoundError:
                self._mtime = None
                self._data = {}
                return
            except Exception:
                return

            if self._mtime == mtime and self._data:
                return

            try:
                with self.path.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    self._data = data
                    self._mtime = mtime
            except Exception:
                self._data = {}
                self._mtime = None

    def raw_data(self) -> dict[str, Any]:
        self._load_if_changed()
        with self._lock:
            return json.loads(json.dumps(self._data, ensure_ascii=False)) if self._data else {}

    def get_profile(self, symbol: str, side: str) -> dict[str, Any]:
        side_key = self.side_key(side)
        default = {
            "symbol": symbol,
            "side": side_key,
            "enabled": False,
            "using_default": True,
            "source": "DEFAULT",
            "source_fa": "بازه عمومی",
            "samples": 0,
            "ranges": self.default_ranges(side_key),
            "reason": "profile_missing_or_invalid",
        }

        resolved = self.resolve_symbol(symbol) or symbol
        self._load_if_changed()
        data = self._data or {}
        symbols = data.get("symbols") if isinstance(data.get("symbols"), dict) else data
        item = symbols.get(resolved, {}) if isinstance(symbols, dict) else {}
        profile = item.get(side_key, {}) if isinstance(item, dict) else {}
        if not isinstance(profile, dict):
            return default

        ranges = profile.get("ranges") or {}
        required = ["rsi", "adx", "volume_multiplier", "vwap_distance_percent", "atr_percent", "bb_position"]
        if not profile.get("enabled") or profile.get("using_default") or not isinstance(ranges, dict):
            return default | {"reason": str(profile.get("reason") or default["reason"]), "samples": int(profile.get("samples") or 0)}
        if any(not self._is_valid_range(ranges.get(f)) for f in required):
            return default | {"reason": "range_invalid", "samples": int(profile.get("samples") or 0)}

        # اگر ema_gap داخل فایل نبود، بازه عمومی همان جهت را برایش بگذار.
        merged_ranges = self.default_ranges(side_key)
        for key, val in ranges.items():
            if self._is_valid_range(val):
                merged_ranges[key] = {"min": float(val["min"]), "max": float(val["max"])}

        return {
            "symbol": resolved,
            "side": side_key,
            "enabled": True,
            "using_default": False,
            "source": "DAILY_CUSTOM",
            "source_fa": "بازه اختصاصی امروز",
            "samples": int(profile.get("samples") or 0),
            "good_moves": int(profile.get("good_moves") or profile.get("samples") or 0),
            "combined_samples": int(profile.get("combined_samples") or 0),
            "target_move_percent": float(profile.get("target_move_percent") or data.get("target_move_percent") or config.ROLLING_OPTIMIZER_TARGET_MOVE_PERCENT),
            "generated_utc": str(data.get("generated_utc") or ""),
            "generated_date_utc": str(data.get("generated_date_utc") or ""),
            "ranges": merged_ranges,
        }

    @staticmethod
    def in_range(profile: dict[str, Any], feature: str, value: float) -> bool:
        r = (profile.get("ranges") or {}).get(feature) or {}
        try:
            return float(r.get("min")) <= float(value) <= float(r.get("max"))
        except Exception:
            return False

    @staticmethod
    def range_text(profile: dict[str, Any], feature: str) -> str:
        r = (profile.get("ranges") or {}).get(feature) or {}
        suffix = ""
        if feature in ("vwap_distance_percent", "atr_percent", "ema_gap_percent"):
            suffix = "%"
        if feature == "volume_multiplier":
            return f"{format_num(r.get('min'), 2)}x تا {format_num(r.get('max'), 2)}x"
        return f"{format_num(r.get('min'), 2)}{suffix} تا {format_num(r.get('max'), 2)}{suffix}"

    def format_profile_message(self, raw_symbol: str) -> str:
        symbol = self.resolve_symbol(raw_symbol or "")
        if not symbol:
            return "❌ نماد پیدا نشد. مثال درست: بازه SOL یا بازه XRP"

        data = self.raw_data()
        generated = data.get("generated_utc") or "-"
        days = data.get("days") or config.ROLLING_OPTIMIZER_DAYS
        target = data.get("target_move_percent") or config.ROLLING_OPTIMIZER_TARGET_MOVE_PERCENT
        timeframe = data.get("timeframe") or config.TIMEFRAME

        lines = [
            f"📊 بازه امروز {symbol}",
            "",
            f"آخرین آپدیت: {generated}",
            f"دوره بررسی: {days} روز گذشته",
            f"تایم‌فریم: {timeframe}",
            f"هدف حرکت خوب: {format_num(target, 2)}٪",
            "",
        ]

        for side_key, title, icon in (("LONG", "لانگ", "🟢"), ("SHORT", "شورت", "🔴")):
            profile = self.get_profile(symbol, side_key)
            status = "اختصاصی فعال ✅" if not profile.get("using_default") else "عمومی / پیش‌فرض ⚠️"
            samples = int(profile.get("samples") or 0)
            lines.append(f"{icon} {title} {symbol}")
            lines.append(f"وضعیت: {status}")
            if profile.get("using_default"):
                reason = profile.get("reason") or "نمونه کافی یا فایل معتبر وجود ندارد"
                lines.append(f"دلیل: {reason}")
            else:
                lines.append(f"نمونه‌های خوب: {samples}")
                if profile.get("combined_samples"):
                    lines.append(f"نمونه داخل بازه ترکیبی: {profile.get('combined_samples')}")

            for feature in ["rsi", "adx", "volume_multiplier", "vwap_distance_percent", "atr_percent", "bb_position", "ema_gap_percent"]:
                label = _FEATURE_LABELS.get(feature, feature)
                lines.append(f"• {label}: {self.range_text(profile, feature)}")
            if side_key == "LONG":
                lines.append("• شرط ثابت EMA: EMA9 بالاتر از EMA21")
                lines.append("• شرط ثابت VWAP: قیمت بالای VWAP")
            else:
                lines.append("• شرط ثابت EMA: EMA9 پایین‌تر از EMA21")
                lines.append("• شرط ثابت VWAP: قیمت زیر VWAP")
            lines.append("")

        lines.append("این همان بازه‌ای است که امروز strategy برای این ارز استفاده می‌کند.")
        return "\n".join(lines).strip()
