"""منطق v16: اسکالپ ۵ دقیقه‌ای فقط شورت.

قانون اصلی:
- لانگ کامل غیرفعال است.
- فقط وقتی بازار SELL باشد و بازه 5m شورت تایید شود، سیگنال فروش ساخته می‌شود.
- پنل، Toobit، مانیتور و نتیجه‌ها در فایل‌های دیگر دست‌نخورده می‌مانند.
"""
from __future__ import annotations

from typing import Any

import config
from symbol_profiles import SymbolProfileManager
from utils import build_signal_id, is_entry_window, price_by_percent, side_to_persian


class ClassicScalpingStrategy:
    def __init__(self, profiles: SymbolProfileManager | None = None) -> None:
        self.profiles = profiles or SymbolProfileManager()

    @staticmethod
    def _vwap_distance_percent(ind: dict[str, Any]) -> float:
        close = float(ind.get("close") or 0)
        vwap = float(ind.get("vwap") or 0)
        if close <= 0 or vwap <= 0:
            return 999.0
        return abs(close - vwap) / close * 100.0

    @staticmethod
    def _bb_position(ind: dict[str, Any]) -> float:
        """موقعیت قیمت داخل بولینگر: 0 نزدیک باند پایین، 1 نزدیک باند بالا."""
        close = float(ind.get("close") or 0)
        upper = float(ind.get("bb_upper") or 0)
        lower = float(ind.get("bb_lower") or 0)
        width = upper - lower
        if close <= 0 or width <= 0:
            return 0.5
        return (close - lower) / width

    @staticmethod
    def _ema_gap_percent(ind: dict[str, Any]) -> float:
        close = float(ind.get("close") or 0)
        if close <= 0:
            return 0.0
        return (float(ind.get("ema_fast") or 0) - float(ind.get("ema_slow") or 0)) / close * 100.0

    def _base_blockers(self, ind: dict[str, Any]) -> list[str]:
        blockers: list[str] = []
        if not is_entry_window(ind["open_time"]):
            blockers.append("کندل زنده خارج از پنجره مجاز ورود است")
        return blockers

    @staticmethod
    def _check_range(profile: dict[str, Any], feature: str, value: float) -> bool:
        return SymbolProfileManager.in_range(profile, feature, value)

    @staticmethod
    def _range_text(profile: dict[str, Any], feature: str) -> str:
        return SymbolProfileManager.range_text(profile, feature)

    def _long_zone(self, symbol: str, ind: dict[str, Any]) -> tuple[bool, list[str], list[str], dict[str, Any]]:
        profile = self.profiles.get_profile(symbol, "LONG")
        reasons: list[str] = []
        blockers: list[str] = self._base_blockers(ind)
        if getattr(config, "REQUIRE_CUSTOM_PROFILE_FOR_SIGNAL", False) and profile.get("using_default"):
            blockers.append(f"بازه اختصاصی معتبر برای لانگ {symbol} وجود ندارد؛ ورود عمومی غیرفعال است")
        close = float(ind.get("close") or 0)
        vwap_distance = self._vwap_distance_percent(ind)
        bb_pos = self._bb_position(ind)
        ema_gap = self._ema_gap_percent(ind)
        volume_multiplier = float(ind.get("volume_multiplier") or 0)
        rsi = float(ind.get("rsi") or 50)
        adx = float(ind.get("adx") or 0)
        atr_percent = float(ind.get("atr_percent") or 0)

        checks = [
            (close > float(ind.get("vwap") or 0), "قیمت بالای VWAP است", "قیمت بالای VWAP نیست"),
            (float(ind.get("ema_fast") or 0) > float(ind.get("ema_slow") or 0), "EMA 9 بالای EMA 21 است", "EMA 9 بالای EMA 21 نیست"),
            (self._check_range(profile, "rsi", rsi), f"RSI داخل بازه لانگ است ({self._range_text(profile, 'rsi')})", "RSI خارج از بازه لانگ است"),
            (self._check_range(profile, "adx", adx), f"ADX داخل بازه روند سالم است ({self._range_text(profile, 'adx')})", "ADX خارج از بازه روند سالم است"),
            (self._check_range(profile, "vwap_distance_percent", vwap_distance), f"فاصله قیمت از VWAP داخل بازه ورود است ({self._range_text(profile, 'vwap_distance_percent')})", "فاصله قیمت از VWAP مناسب نیست"),
            (self._check_range(profile, "volume_multiplier", volume_multiplier), f"حجم زنده داخل بازه تایید است ({self._range_text(profile, 'volume_multiplier')})", "حجم زنده خارج از بازه سالم است"),
            (self._check_range(profile, "atr_percent", atr_percent), f"ATR داخل بازه سالم است ({self._range_text(profile, 'atr_percent')})", "ATR خارج از بازه سالم است"),
            (self._check_range(profile, "bb_position", bb_pos), f"موقعیت بولینگر داخل بازه است ({self._range_text(profile, 'bb_position')})", "قیمت در بولینگر ناحیه مناسبی ندارد"),
            (self._check_range(profile, "ema_gap_percent", ema_gap), f"فاصله EMA9/21 داخل بازه است ({self._range_text(profile, 'ema_gap_percent')})", "فاصله EMA9/21 مناسب نیست"),
        ]
        for ok, reason, block in checks:
            if ok:
                reasons.append(reason)
            else:
                blockers.append(block)
        return not blockers, reasons, blockers, profile

    def _short_zone(self, symbol: str, ind: dict[str, Any]) -> tuple[bool, list[str], list[str], dict[str, Any]]:
        profile = self.profiles.get_profile(symbol, "SHORT")
        reasons: list[str] = []
        blockers: list[str] = self._base_blockers(ind)
        if getattr(config, "REQUIRE_CUSTOM_PROFILE_FOR_SIGNAL", False) and profile.get("using_default"):
            blockers.append(f"بازه اختصاصی معتبر برای شورت {symbol} وجود ندارد؛ ورود عمومی غیرفعال است")
        close = float(ind.get("close") or 0)
        vwap_distance = self._vwap_distance_percent(ind)
        bb_pos = self._bb_position(ind)
        ema_gap = self._ema_gap_percent(ind)
        volume_multiplier = float(ind.get("volume_multiplier") or 0)
        rsi = float(ind.get("rsi") or 50)
        adx = float(ind.get("adx") or 0)
        atr_percent = float(ind.get("atr_percent") or 0)

        checks = [
            (close < float(ind.get("vwap") or 0), "قیمت زیر VWAP است", "قیمت زیر VWAP نیست"),
            (float(ind.get("ema_fast") or 0) < float(ind.get("ema_slow") or 0), "EMA 9 پایین EMA 21 است", "EMA 9 پایین EMA 21 نیست"),
            (self._check_range(profile, "rsi", rsi), f"RSI داخل بازه شورت است ({self._range_text(profile, 'rsi')})", "RSI خارج از بازه شورت است"),
            (self._check_range(profile, "adx", adx), f"ADX داخل بازه روند سالم است ({self._range_text(profile, 'adx')})", "ADX خارج از بازه روند سالم است"),
            (self._check_range(profile, "vwap_distance_percent", vwap_distance), f"فاصله قیمت از VWAP داخل بازه ورود است ({self._range_text(profile, 'vwap_distance_percent')})", "فاصله قیمت از VWAP مناسب نیست"),
            (self._check_range(profile, "volume_multiplier", volume_multiplier), f"حجم زنده داخل بازه تایید است ({self._range_text(profile, 'volume_multiplier')})", "حجم زنده خارج از بازه سالم است"),
            (self._check_range(profile, "atr_percent", atr_percent), f"ATR داخل بازه سالم است ({self._range_text(profile, 'atr_percent')})", "ATR خارج از بازه سالم است"),
            (self._check_range(profile, "bb_position", bb_pos), f"موقعیت بولینگر داخل بازه است ({self._range_text(profile, 'bb_position')})", "قیمت در بولینگر ناحیه مناسبی ندارد"),
            (self._check_range(profile, "ema_gap_percent", ema_gap), f"فاصله EMA9/21 داخل بازه است ({self._range_text(profile, 'ema_gap_percent')})", "فاصله EMA9/21 مناسب نیست"),
        ]
        for ok, reason, block in checks:
            if ok:
                reasons.append(reason)
            else:
                blockers.append(block)
        return not blockers, reasons, blockers, profile

    def evaluate(
        self,
        symbol: str,
        okx_symbol: str,
        toobit_symbol: str,
        ind: dict[str, Any],
        market: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        market = market or {"direction": "RANGE", "summary": "بازار رنج است"}
        direction = str(market.get("direction") or "RANGE").upper()
        if getattr(config, "SHORT_ONLY_MODE", True):
            if direction != "SELL" or not getattr(config, "ALLOW_SHORT_SIGNALS", True):
                return None
            allowed, reasons, blockers, profile = self._short_zone(symbol, ind)
            side = "SELL"
            zone_label = "SHORT_ENTRY_ZONE"
        else:
            if direction not in ("BUY", "SELL"):
                return None
            if direction == "BUY":
                if not getattr(config, "ALLOW_LONG_SIGNALS", False):
                    return None
                allowed, reasons, blockers, profile = self._long_zone(symbol, ind)
                side = "BUY"
                zone_label = "LONG_ENTRY_ZONE"
            else:
                if not getattr(config, "ALLOW_SHORT_SIGNALS", True):
                    return None
                allowed, reasons, blockers, profile = self._short_zone(symbol, ind)
                side = "SELL"
                zone_label = "SHORT_ENTRY_ZONE"

        if not allowed:
            return None

        entry = float(ind["close"])
        tp = price_by_percent(entry, config.FIXED_TP_PERCENT, side, "TP")
        sl = price_by_percent(entry, config.FIXED_SL_PERCENT, side, "SL")

        market_summary = str(market.get("summary") or "جهت کلی بازار تایید شد")
        reasons = [market_summary] + reasons
        profile_source = str(profile.get("source_fa") or "بازه عمومی")
        score_label = "ورود بازه‌ای اختصاصی امروز" if not profile.get("using_default") else "ورود بازه‌ای عمومی"

        return {
            "signal_id": build_signal_id(symbol, side),
            "symbol": symbol,
            "okx_symbol": okx_symbol,
            "toobit_symbol": toobit_symbol,
            "side": side,
            "side_fa": side_to_persian(side),
            "entry": entry,
            "tp": tp,
            "sl": sl,
            "score": 0,
            "score_label": score_label,
            "signal_type": "اسکالپ ۵ دقیقه‌ای فقط شورت",
            "market_direction": direction,
            "market_state": "نزولی / فقط شورت",
            "entry_zone": zone_label,
            "profile_source": profile_source,
            "profile_samples": int(profile.get("samples") or 0),
            "profile_using_default": bool(profile.get("using_default")),
            "reasons": reasons[:8],
            "warnings": blockers[:4],
            "indicators": ind,
            "market_filter": market,
            "created_at": ind.get("open_time"),
            "max_hold_minutes": getattr(config, "SIGNAL_MAX_HOLD_MINUTES", 180),
            "created_utc": None,
            "normal_result": None,
            "real_result": None,
            "telegram_message_id": None,
        }
