"""منطق کلاسیک بازه‌ای بدون امتیاز.

قانون اصلی:
- بازار فقط سه حالت دارد: BUY، SELL، RANGE.
- در RANGE هیچ سیگنالی صادر نمی‌شود.
- امتیازدهی حذف شده؛ سیگنال فقط وقتی صادر می‌شود که همه بازه‌های سالم ورود برقرار باشند.
"""
from __future__ import annotations

from typing import Any

import config
from utils import build_signal_id, is_entry_window, price_by_percent, side_to_persian


class ClassicScalpingStrategy:
    def _atr_valid(self, ind: dict[str, Any]) -> bool:
        atr_percent = float(ind.get("atr_percent", 0))
        return config.ATR_MIN_PERCENT <= atr_percent <= config.ATR_MAX_PERCENT

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

    def _base_blockers(self, ind: dict[str, Any]) -> list[str]:
        blockers: list[str] = []
        if not is_entry_window(ind["open_time"]):
            blockers.append("کندل زنده خارج از پنجره مجاز ورود است")
        if not self._atr_valid(ind):
            blockers.append("ATR خارج از بازه سالم است")
        return blockers

    def _long_zone(self, ind: dict[str, Any]) -> tuple[bool, list[str], list[str]]:
        reasons: list[str] = []
        blockers: list[str] = self._base_blockers(ind)
        close = float(ind.get("close") or 0)
        vwap_distance = self._vwap_distance_percent(ind)
        bb_pos = self._bb_position(ind)
        volume_multiplier = float(ind.get("volume_multiplier") or 0)
        rsi = float(ind.get("rsi") or 50)
        adx = float(ind.get("adx") or 0)

        checks = [
            (close > float(ind.get("vwap") or 0), "قیمت بالای VWAP است", "قیمت بالای VWAP نیست"),
            (float(ind.get("ema_fast") or 0) > float(ind.get("ema_slow") or 0), "EMA 9 بالای EMA 21 است", "EMA 9 بالای EMA 21 نیست"),
            (config.ZONE_LONG_RSI_MIN <= rsi <= config.ZONE_LONG_RSI_MAX, f"RSI داخل بازه سالم لانگ است ({config.ZONE_LONG_RSI_MIN:g}-{config.ZONE_LONG_RSI_MAX:g})", "RSI خارج از بازه سالم لانگ است"),
            (config.ZONE_ADX_MIN <= adx <= config.ZONE_ADX_MAX, f"ADX داخل بازه روند سالم است ({config.ZONE_ADX_MIN:g}-{config.ZONE_ADX_MAX:g})", "ADX خیلی ضعیف یا خیلی داغ است"),
            (config.ZONE_VWAP_DISTANCE_MIN_PERCENT <= vwap_distance <= config.ZONE_VWAP_DISTANCE_MAX_PERCENT, "فاصله قیمت از VWAP داخل بازه ورود سالم است", "فاصله قیمت از VWAP یا خیلی کم است یا حرکت دیر شده"),
            (config.ZONE_VOLUME_MULTIPLIER_MIN <= volume_multiplier <= config.ZONE_VOLUME_MULTIPLIER_MAX, "حجم زنده داخل بازه تایید سالم است", "حجم زنده خارج از بازه سالم است"),
            (bb_pos < config.ZONE_BB_LONG_MAX_POSITION, "قیمت به باند بالایی بولینگر نچسبیده است", "قیمت نزدیک باند بالایی است؛ احتمال خستگی حرکت"),
        ]
        for ok, reason, block in checks:
            if ok:
                reasons.append(reason)
            else:
                blockers.append(block)
        return not blockers, reasons, blockers

    def _short_zone(self, ind: dict[str, Any]) -> tuple[bool, list[str], list[str]]:
        reasons: list[str] = []
        blockers: list[str] = self._base_blockers(ind)
        close = float(ind.get("close") or 0)
        vwap_distance = self._vwap_distance_percent(ind)
        bb_pos = self._bb_position(ind)
        volume_multiplier = float(ind.get("volume_multiplier") or 0)
        rsi = float(ind.get("rsi") or 50)
        adx = float(ind.get("adx") or 0)

        checks = [
            (close < float(ind.get("vwap") or 0), "قیمت زیر VWAP است", "قیمت زیر VWAP نیست"),
            (float(ind.get("ema_fast") or 0) < float(ind.get("ema_slow") or 0), "EMA 9 پایین EMA 21 است", "EMA 9 پایین EMA 21 نیست"),
            (config.ZONE_SHORT_RSI_MIN <= rsi <= config.ZONE_SHORT_RSI_MAX, f"RSI داخل بازه سالم شورت است ({config.ZONE_SHORT_RSI_MIN:g}-{config.ZONE_SHORT_RSI_MAX:g})", "RSI خارج از بازه سالم شورت است"),
            (config.ZONE_ADX_MIN <= adx <= config.ZONE_ADX_MAX, f"ADX داخل بازه روند سالم است ({config.ZONE_ADX_MIN:g}-{config.ZONE_ADX_MAX:g})", "ADX خیلی ضعیف یا خیلی داغ است"),
            (config.ZONE_VWAP_DISTANCE_MIN_PERCENT <= vwap_distance <= config.ZONE_VWAP_DISTANCE_MAX_PERCENT, "فاصله قیمت از VWAP داخل بازه ورود سالم است", "فاصله قیمت از VWAP یا خیلی کم است یا حرکت دیر شده"),
            (config.ZONE_VOLUME_MULTIPLIER_MIN <= volume_multiplier <= config.ZONE_VOLUME_MULTIPLIER_MAX, "حجم زنده داخل بازه تایید سالم است", "حجم زنده خارج از بازه سالم است"),
            (bb_pos > config.ZONE_BB_SHORT_MIN_POSITION, "قیمت به باند پایینی بولینگر نچسبیده است", "قیمت نزدیک باند پایینی است؛ احتمال خستگی حرکت"),
        ]
        for ok, reason, block in checks:
            if ok:
                reasons.append(reason)
            else:
                blockers.append(block)
        return not blockers, reasons, blockers

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
        if direction not in ("BUY", "SELL"):
            return None

        if direction == "BUY":
            allowed, reasons, blockers = self._long_zone(ind)
            side = "BUY"
            zone_label = "LONG_ENTRY_ZONE"
        else:
            allowed, reasons, blockers = self._short_zone(ind)
            side = "SELL"
            zone_label = "SHORT_ENTRY_ZONE"

        if not allowed:
            return None

        entry = float(ind["close"])
        tp = price_by_percent(entry, config.FIXED_TP_PERCENT, side, "TP")
        sl = price_by_percent(entry, config.FIXED_SL_PERCENT, side, "SL")

        market_summary = str(market.get("summary") or "جهت کلی بازار تایید شد")
        reasons = [market_summary] + reasons

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
            "score_label": "ندارد؛ ورود بازه‌ای",
            "signal_type": "ورود بازه‌ای",
            "market_direction": direction,
            "market_state": "صعودی" if direction == "BUY" else "نزولی / شورت",
            "entry_zone": zone_label,
            "reasons": reasons[:8],
            "warnings": blockers[:4],
            "indicators": ind,
            "market_filter": market,
            "created_at": ind.get("open_time"),
            "created_utc": None,
            "normal_result": None,
            "real_result": None,
            "telegram_message_id": None,
        }
