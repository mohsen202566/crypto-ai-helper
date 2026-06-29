"""منطق کلاسیک ورود سریع بدون AI."""
from __future__ import annotations

from typing import Any

import config
from utils import build_signal_id, is_entry_window, price_by_percent, side_to_persian


class ClassicScalpingStrategy:
    def _atr_valid(self, ind: dict[str, Any]) -> bool:
        atr_percent = float(ind.get("atr_percent", 0))
        return config.ATR_MIN_PERCENT <= atr_percent <= config.ATR_MAX_PERCENT

    def _volume_score(self, multiplier: float) -> int:
        if multiplier >= 1.60:
            return 25
        if multiplier >= 1.30:
            return 22
        if multiplier >= 1.10:
            return 17
        if multiplier >= 1.00:
            return 8
        return 0

    def _score_buy(self, ind: dict[str, Any]) -> tuple[int, list[str], list[str]]:
        score = 0
        reasons: list[str] = []
        warnings: list[str] = []
        close = ind["close"]

        if close > ind["vwap"]:
            score += 20
            reasons.append("قیمت بالای VWAP است")
        else:
            warnings.append("قیمت هنوز بالای VWAP تثبیت نشده")

        if ind["rsi"] > 50:
            rsi_score = 20 if ind["rsi"] >= 53 and ind["rsi"] >= ind["rsi_prev"] else 14
            score += rsi_score
            reasons.append("RSI مومنتوم صعودی دارد")
        else:
            warnings.append("RSI برای خرید ضعیف است")

        v_score = self._volume_score(ind["volume_multiplier"])
        score += v_score
        if v_score >= 17:
            reasons.append("حجم زنده کندل قوی‌تر از میانگین است")
        else:
            warnings.append("حجم زنده هنوز کافی نیست")

        ema_positive = ind["ema_fast"] >= ind["ema_slow"] or (ind["ema_fast"] - ind["ema_slow"] > ind["ema_fast_prev"] - ind["ema_slow_prev"])
        if ema_positive:
            score += 15
            reasons.append("EMA 9 و EMA 21 هم‌جهت خرید هستند")
        else:
            warnings.append("EMA هنوز کاملاً هم‌جهت نیست")

        if close >= ind["bb_mid"]:
            score += 10
            reasons.append("فشار قیمت در نیمه بالایی بولینگر است")
        else:
            warnings.append("فشار بولینگر برای خرید متوسط است")

        if ind["adx"] >= 18:
            score += 5
            reasons.append("ADX قدرت روند قابل قبول دارد")
        else:
            warnings.append("ADX متوسط یا ضعیف است")

        if self._atr_valid(ind):
            score += 5
            reasons.append("ATR با TP/SL ثابت هماهنگ است")
        else:
            warnings.append("ATR با TP/SL ثابت هماهنگ نیست")
        return score, reasons, warnings

    def _score_sell(self, ind: dict[str, Any]) -> tuple[int, list[str], list[str]]:
        score = 0
        reasons: list[str] = []
        warnings: list[str] = []
        close = ind["close"]

        if close < ind["vwap"]:
            score += 20
            reasons.append("قیمت زیر VWAP است")
        else:
            warnings.append("قیمت هنوز زیر VWAP تثبیت نشده")

        if ind["rsi"] < 50:
            rsi_score = 20 if ind["rsi"] <= 47 and ind["rsi"] <= ind["rsi_prev"] else 14
            score += rsi_score
            reasons.append("RSI مومنتوم نزولی دارد")
        else:
            warnings.append("RSI برای فروش ضعیف است")

        v_score = self._volume_score(ind["volume_multiplier"])
        score += v_score
        if v_score >= 17:
            reasons.append("حجم زنده کندل قوی‌تر از میانگین است")
        else:
            warnings.append("حجم زنده هنوز کافی نیست")

        ema_negative = ind["ema_fast"] <= ind["ema_slow"] or (ind["ema_fast"] - ind["ema_slow"] < ind["ema_fast_prev"] - ind["ema_slow_prev"])
        if ema_negative:
            score += 15
            reasons.append("EMA 9 و EMA 21 هم‌جهت فروش هستند")
        else:
            warnings.append("EMA هنوز کاملاً هم‌جهت نیست")

        if close <= ind["bb_mid"]:
            score += 10
            reasons.append("فشار قیمت در نیمه پایینی بولینگر است")
        else:
            warnings.append("فشار بولینگر برای فروش متوسط است")

        if ind["adx"] >= 18:
            score += 5
            reasons.append("ADX قدرت روند قابل قبول دارد")
        else:
            warnings.append("ADX متوسط یا ضعیف است")

        if self._atr_valid(ind):
            score += 5
            reasons.append("ATR با TP/SL ثابت هماهنگ است")
        else:
            warnings.append("ATR با TP/SL ثابت هماهنگ نیست")
        return score, reasons, warnings

    def evaluate(self, symbol: str, okx_symbol: str, toobit_symbol: str, ind: dict[str, Any]) -> dict[str, Any] | None:
        if not is_entry_window(ind["open_time"]):
            return None
        if not self._atr_valid(ind):
            return None

        buy_mandatory = ind["close"] > ind["vwap"] and ind["rsi"] > 50 and ind["volume_multiplier"] >= config.MIN_PROJECTED_VOLUME_MULTIPLIER
        sell_mandatory = ind["close"] < ind["vwap"] and ind["rsi"] < 50 and ind["volume_multiplier"] >= config.MIN_PROJECTED_VOLUME_MULTIPLIER

        candidates: list[dict[str, Any]] = []
        if buy_mandatory:
            score, reasons, warnings = self._score_buy(ind)
            candidates.append({"side": "BUY", "score": score, "reasons": reasons, "warnings": warnings})
        if sell_mandatory:
            score, reasons, warnings = self._score_sell(ind)
            candidates.append({"side": "SELL", "score": score, "reasons": reasons, "warnings": warnings})

        if not candidates:
            return None

        best = max(candidates, key=lambda x: x["score"])
        fast_allowed = best["score"] >= config.ALLOW_FAST_ENTRY_SCORE and ind["volume_multiplier"] >= config.FAST_VOLUME_MULTIPLIER
        normal_allowed = best["score"] >= config.MIN_SIGNAL_SCORE
        if not normal_allowed and not fast_allowed:
            return None

        side = best["side"]
        entry = ind["close"]
        tp = price_by_percent(entry, config.FIXED_TP_PERCENT, side, "TP")
        sl = price_by_percent(entry, config.FIXED_SL_PERCENT, side, "SL")
        signal_type = "ورود سریع" if fast_allowed and not normal_allowed else "ورود عادی"

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
            "score": int(best["score"]),
            "signal_type": signal_type,
            "reasons": best["reasons"][:6],
            "warnings": best["warnings"][:4],
            "indicators": ind,
            "created_at": ind.get("open_time"),
            "created_utc": None,
            "normal_result": None,
            "real_result": None,
            "telegram_message_id": None,
        }
