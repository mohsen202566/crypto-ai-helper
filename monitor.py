"""مانیتورینگ نتیجه سیگنال‌ها.
Real از Toobit چک می‌شود؛ Virtual از OKX. نتیجه باید روی پیام سیگنال اصلی ریپلای شود.

این نسخه منطق سیگنال‌دهی را تغییر نمی‌دهد؛ فقط هنگام SL خوردن، علت‌یابی دقیق اضافه می‌کند.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

import config
from okx_client import OKXClient
from storage import Storage
from toobit_client import ToobitFuturesClient, safe_float

logger = logging.getLogger("futures_hunt_2.monitor")


class Monitor:
    def __init__(self, okx: OKXClient, toobit: ToobitFuturesClient, storage: Storage, telegram=None):
        self.okx = okx
        self.toobit = toobit
        self.storage = storage
        self.telegram = telegram

    def _pnl(self, side: str, entry: float, exit_price: float, trade_usdt: float, leverage: int) -> tuple[float, float, float]:
        notional = trade_usdt * leverage
        if side == "LONG":
            gross = notional * ((exit_price - entry) / entry)
        else:
            gross = notional * ((entry - exit_price) / entry)
        fee = notional * ((config.FALLBACK_FEE_PCT_PER_SIDE * 2.0 + config.SLIPPAGE_PCT_PER_SIDE * 2.0) / 100.0)
        return gross, fee, gross - fee

    @staticmethod
    def _raw(sig: dict) -> dict[str, Any]:
        raw = sig.get("raw_json") or sig.get("raw") or {}
        if isinstance(raw, dict):
            return raw
        try:
            parsed = json.loads(raw or "{}")
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _num_from_reason(reason: str, pattern: str, default: float | None = None) -> float | None:
        try:
            m = re.search(pattern, reason or "")
            if not m:
                return default
            return float(m.group(1))
        except Exception:
            return default

    def _reason_metrics(self, sig: dict) -> dict[str, Any]:
        raw = self._raw(sig)
        reason = str(raw.get("reason") or "")
        return {
            "reason": reason,
            "risk_reason": str(raw.get("risk_reason") or ""),
            "dwe": self._num_from_reason(reason, r"DWE=([-+]?\d+(?:\.\d+)?)"),
            "gap": self._num_from_reason(reason, r"gap=([-+]?\d+(?:\.\d+)?)"),
            "conflict": self._num_from_reason(reason, r"conflict=([-+]?\d+(?:\.\d+)?)"),
            "ignition": self._num_from_reason(reason, r"ignition=([-+]?\d+(?:\.\d+)?)"),
            "weakness": self._num_from_reason(reason, r"weakness=([-+]?\d+(?:\.\d+)?)"),
            "trade": self._num_from_reason(reason, r"trade=([-+]?\d+(?:\.\d+)?)"),
            "book": self._num_from_reason(reason, r"book=([-+]?\d+(?:\.\d+)?)"),
            "response_pct": self._num_from_reason(reason, r"response=([-+]?\d+(?:\.\d+)?)%"),
        }

    @staticmethod
    def _risk_pct(side: str, entry: float, sl: float) -> float:
        if entry <= 0:
            return 0.0
        if side.upper() == "LONG":
            return max(0.0, (entry - sl) / entry * 100.0)
        return max(0.0, (sl - entry) / entry * 100.0)

    @staticmethod
    def _tp_pct(side: str, entry: float, tp: float) -> float:
        if entry <= 0:
            return 0.0
        if side.upper() == "LONG":
            return max(0.0, (tp - entry) / entry * 100.0)
        return max(0.0, (entry - tp) / entry * 100.0)

    @staticmethod
    def _max_fav_before_close(candles: list[dict[str, float]], side: str, entry: float, created_ms: int, close_ms: int | None) -> tuple[float, float, int]:
        mfe = 0.0
        mae = 0.0
        bars = 0
        side = side.upper()
        for c in candles or []:
            ts = int(c.get("ts") or 0)
            if ts <= created_ms:
                continue
            if close_ms and ts > close_ms:
                break
            bars += 1
            if side == "LONG":
                mfe = max(mfe, (float(c["high"]) - entry) / entry * 100.0)
                mae = min(mae, (float(c["low"]) - entry) / entry * 100.0)
            else:
                mfe = max(mfe, (entry - float(c["low"])) / entry * 100.0)
                mae = min(mae, (entry - float(c["high"])) / entry * 100.0)
        return mfe, mae, bars

    @staticmethod
    def _post_sl_behavior(candles: list[dict[str, float]], side: str, entry: float, tp: float, sl: float, close_ms: int | None, max_bars: int = 3) -> dict[str, Any]:
        """بعد از SL چند کندل را نگاه می‌کند تا بفهمیم قیمت برگشته یا خلاف جهت ادامه داده.
        اگر دیتای بعد از SL هنوز موجود نباشد، خروجی unknown می‌دهد.
        """
        if not close_ms:
            return {"status": "unknown", "text": "داده کافی بعد از SL هنوز موجود نیست"}
        side = side.upper()
        post = [c for c in candles or [] if int(c.get("ts") or 0) > close_ms][:max_bars]
        if not post:
            return {"status": "unknown", "text": "هنوز کندل بعد از SL برای Post-SL Watch نداریم"}
        max_fav = 0.0
        max_against = 0.0
        touched_tp = False
        for c in post:
            if side == "LONG":
                max_fav = max(max_fav, (float(c["high"]) - entry) / entry * 100.0)
                max_against = min(max_against, (float(c["low"]) - entry) / entry * 100.0)
                touched_tp = touched_tp or float(c["high"]) >= tp
            else:
                max_fav = max(max_fav, (entry - float(c["low"])) / entry * 100.0)
                max_against = min(max_against, (entry - float(c["high"])) / entry * 100.0)
                touched_tp = touched_tp or float(c["low"]) <= tp
        if touched_tp:
            return {"status": "returned_to_tp", "text": "بعد از SL قیمت دوباره در جهت سیگنال برگشت و محدوده TP را لمس کرد"}
        if max_fav > 0:
            return {"status": "returned_partial", "text": "بعد از SL بخشی از حرکت در جهت سیگنال برگشت"}
        return {"status": "continued_against", "text": "بعد از SL هم ادامه معنی‌دار در جهت سیگنال دیده نشد"}

    def _diagnose_sl(self, sig: dict, exit_price: float, mfe: float, mae: float, candles: list[dict[str, float]] | None = None, close_ts_ms: int | None = None) -> str:
        """علت‌یابی SL بر اساس داده‌های ذخیره‌شده و مسیر قیمت.
        تشخیص قطعی مطلق نیست؛ با confidence و شواهد عددی گزارش می‌شود.
        """
        side = str(sig.get("side") or "").upper()
        entry = float(sig.get("entry_real") or sig.get("entry") or 0.0)
        sl = float(sig.get("sl") or 0.0)
        tp = float(sig.get("tp") or 0.0)
        created_ms = int(sig.get("created_at") or time.time()) * 1000
        risk_pct = self._risk_pct(side, entry, sl)
        tp_pct = self._tp_pct(side, entry, tp)
        mfe_r = (float(mfe) / risk_pct) if risk_pct > 0 else 0.0
        mae_r = (abs(float(mae)) / risk_pct) if risk_pct > 0 else 0.0
        reward_r = (tp_pct / risk_pct) if risk_pct > 0 else float(getattr(config, "RISK_REWARD", 1.35))

        close_mfe, close_mae, bars_to_sl = self._max_fav_before_close(candles or [], side, entry, created_ms, close_ts_ms)
        if close_mfe > 0 or close_mae < 0:
            mfe_to_close = close_mfe
            mae_to_close = close_mae
            mfe_r_to_close = close_mfe / risk_pct if risk_pct > 0 else mfe_r
        else:
            mfe_to_close = float(mfe)
            mae_to_close = float(mae)
            mfe_r_to_close = mfe_r

        meta = self._reason_metrics(sig)
        dwe = meta.get("dwe")
        gap = meta.get("gap")
        conflict = meta.get("conflict")
        ignition = meta.get("ignition")
        weakness = meta.get("weakness")
        trade = meta.get("trade")
        book = meta.get("book")
        post = self._post_sl_behavior(candles or [], side, entry, tp, sl, close_ts_ms)

        # امتیازدهی علت‌ها؛ چند علت ممکن است هم‌زمان فعال باشند.
        scores: dict[str, float] = {
            "WRONG_DIRECTION": 0.0,
            "EARLY_ENTRY": 0.0,
            "FAKE_IGNITION": 0.0,
            "WEAK_TREND_CHOP": 0.0,
            "STOP_TOO_TIGHT": 0.0,
            "LATE_ENTRY": 0.0,
            "MARKET_OR_BTC_SHOCK": 0.0,
            "EXECUTION_LIQUIDITY": 0.0,
        }

        # 1) بدون حرکت مثبت = جهت غلط یا ورود زود.
        if mfe_r_to_close < 0.10:
            scores["WRONG_DIRECTION"] += 34
            scores["EARLY_ENTRY"] += 28
        elif mfe_r_to_close < 0.25:
            scores["EARLY_ENTRY"] += 30
            scores["WRONG_DIRECTION"] += 18
        elif mfe_r_to_close < 0.60:
            scores["FAKE_IGNITION"] += 26
            scores["WEAK_TREND_CHOP"] += 13
        else:
            scores["STOP_TOO_TIGHT"] += 32
            scores["FAKE_IGNITION"] += 9

        # 2) سرعت SL خوردن.
        if bars_to_sl and bars_to_sl <= 1 and mfe_r_to_close < 0.25:
            scores["WRONG_DIRECTION"] += 18
            scores["EARLY_ENTRY"] += 18
        elif bars_to_sl and bars_to_sl <= 2 and mfe_r_to_close < 0.45:
            scores["EARLY_ENTRY"] += 12
            scores["FAKE_IGNITION"] += 10

        # 3) برگشت کامل بعد از پالس.
        if mfe_r_to_close >= 0.25:
            retrace_r = 1.0 + mfe_r_to_close  # از بیشترین سود تا SL چقدر برگشته؛ نسبت به ریسک
            if retrace_r >= 1.55:
                scores["FAKE_IGNITION"] += 20
            if mfe_r_to_close >= 0.65:
                scores["STOP_TOO_TIGHT"] += 15

        # 4) ضعف‌های گزارش‌شده از لحظه سیگنال.
        if weakness is not None and weakness >= 2:
            scores["WEAK_TREND_CHOP"] += 22
        if conflict is not None and conflict >= 32:
            scores["WRONG_DIRECTION"] += 16
            scores["WEAK_TREND_CHOP"] += 12
        if gap is not None and gap < 18:
            scores["WRONG_DIRECTION"] += 12
        if ignition is not None and ignition < 88:
            scores["EARLY_ENTRY"] += 12
            scores["FAKE_IGNITION"] += 10

        # 5) جریان سفارش/دفتر اگر از reason در دسترس باشد.
        if trade is not None:
            if (side == "LONG" and trade < -0.05) or (side == "SHORT" and trade > 0.05):
                scores["WRONG_DIRECTION"] += 10
                scores["WEAK_TREND_CHOP"] += 5
        if book is not None:
            if (side == "LONG" and book < -0.08) or (side == "SHORT" and book > 0.08):
                scores["EXECUTION_LIQUIDITY"] += 8
                scores["WRONG_DIRECTION"] += 5

        # 6) SL خیلی کوچک نسبت به هزینه‌ها/نویز احتمالی.
        fee_round_pct = float(getattr(config, "FALLBACK_FEE_PCT_PER_SIDE", 0.06)) * 2.0 + float(getattr(config, "SLIPPAGE_PCT_PER_SIDE", 0.02)) * 2.0
        if risk_pct > 0 and risk_pct <= fee_round_pct * 2.5:
            scores["EXECUTION_LIQUIDITY"] += 18
            scores["STOP_TOO_TIGHT"] += 10
        if risk_pct > 0 and risk_pct < float(getattr(config, "RISK_FALLBACK_MIN_SL_PCT", 0.55)) * 0.65:
            scores["STOP_TOO_TIGHT"] += 12

        # 7) رفتار بعد از SL.
        post_status = post.get("status")
        if post_status == "returned_to_tp":
            scores["STOP_TOO_TIGHT"] += 35
            scores["WRONG_DIRECTION"] = max(0.0, scores["WRONG_DIRECTION"] - 18)
        elif post_status == "returned_partial":
            scores["STOP_TOO_TIGHT"] += 16
            scores["EARLY_ENTRY"] += 8
        elif post_status == "continued_against" and mfe_r_to_close < 0.25:
            scores["WRONG_DIRECTION"] += 18

        # انتخاب علت اصلی و فرعی.
        ordered = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        primary_key, primary_score = ordered[0]
        secondary_key, secondary_score = ordered[1]
        if primary_score < 28 or primary_score - secondary_score < 6:
            primary_key = "MIXED_CAUSE"

        names = {
            "WRONG_DIRECTION": "تشخیص جهت اشتباه / Direction Misread",
            "EARLY_ENTRY": "ورود زود قبل از شروع واقعی حرکت / Early Entry",
            "FAKE_IGNITION": "پالس شروع فیک یا پس‌داده‌شده / Fake Ignition",
            "WEAK_TREND_CHOP": "روند ضعیف یا بازار چاپی / Weak Trend-Chop",
            "STOP_TOO_TIGHT": "استاپ تنگ یا پشت invalidation نامعتبر / Stop Too Tight",
            "LATE_ENTRY": "ورود دیر بعد از مصرف‌شدن حرکت / Late Entry",
            "MARKET_OR_BTC_SHOCK": "شوک بازار یا حرکت خلاف بازار اصلی / Market Shock",
            "EXECUTION_LIQUIDITY": "مشکل اجرا، اسپرد، اسلیپیج یا نقدینگی / Execution-Liquidity",
            "MIXED_CAUSE": "علت ترکیبی؛ چند عامل هم‌زمان قوی بودند / Mixed Cause",
        }
        modules = {
            "WRONG_DIRECTION": "Direction Core / DWE",
            "EARLY_ENTRY": "Entry Timing / IWG / Proof-of-Move",
            "FAKE_IGNITION": "IWG / No-Full-Retrace / Pullback-Fail",
            "WEAK_TREND_CHOP": "Trend Quality / LTSF / Chop Guard",
            "STOP_TOO_TIGHT": "Risk Engine / Stop Band / Origin Invalidation",
            "LATE_ENTRY": "Distance Gate / Entry Timing",
            "MARKET_OR_BTC_SHOCK": "Market Safety / BTC Shock Guard",
            "EXECUTION_LIQUIDITY": "Execution / Liquidity Filter",
            "MIXED_CAUSE": "چند ماژول؛ ابتدا شواهد عددی زیر بررسی شود",
        }
        actions = {
            "WRONG_DIRECTION": "Direction Gap، Conflict و Relative/Flow سمت مخالف سخت‌تر بررسی شود.",
            "EARLY_ENTRY": "صدور سیگنال بدون Proof-of-Move کافی محدودتر شود؛ WATCH از ENTRY جدا بماند.",
            "FAKE_IGNITION": "No-Full-Retrace و Pullback-Fail سخت‌تر شود؛ پالس پس‌داده‌شده سیگنال نشود.",
            "WEAK_TREND_CHOP": "Weakness Count، Directional Efficiency و Chop Guard دقیق‌تر شود.",
            "STOP_TOO_TIGHT": "حداقل فاصله SL با نویز معمول و origin بررسی شود؛ هر SL را جهت غلط حساب نکن.",
            "LATE_ENTRY": "Distance From Origin و دیرشدن ورود سخت‌تر شود.",
            "MARKET_OR_BTC_SHOCK": "محافظ BTC/Market Shock و توقف موقت بازار تقویت شود.",
            "EXECUTION_LIQUIDITY": "اسپرد، اسلیپیج و اختلاف Entry واقعی با سیگنال قبل از ورود چک شود.",
            "MIXED_CAUSE": "برای بهینه‌سازی، اول بیشترین امتیازهای علت را در لاگ‌ها بررسی کن.",
        }
        confidence = int(max(45, min(94, 52 + primary_score * 0.55 - min(18, secondary_score * 0.12)))) if primary_key != "MIXED_CAUSE" else int(max(45, min(74, 50 + primary_score * 0.25)))

        evidence: list[str] = []
        evidence.append(f"MFE_R={mfe_r:.2f}R تا کل مانیتور | MFE_R قبل از SL≈{mfe_r_to_close:.2f}R")
        evidence.append(f"MAE_R={mae_r:.2f}R | فاصله SL≈{risk_pct:.3f}% | RR عملی≈{reward_r:.2f}")
        if bars_to_sl:
            evidence.append(f"تعداد کندل‌های مانیتور تا SL: {bars_to_sl}")
        else:
            evidence.append("زمان‌بندی دقیق تا SL از کندل‌ها قابل تشخیص نبود")
        if mfe_r_to_close < 0.15:
            evidence.append("قیمت تقریباً هیچ حرکت معناداری به نفع سیگنال نداد؛ ورود/جهت مشکوک است")
        elif mfe_r_to_close >= 0.60:
            evidence.append("قیمت ابتدا حرکت قابل‌قبول به نفع سیگنال داشت؛ جهت کاملاً غلط نبوده و SL/برگشت باید بررسی شود")
        else:
            evidence.append("قیمت کمی به نفع سیگنال رفت ولی حرکت دوام نداشت؛ فیک‌پالس یا ضعف روند محتمل است")
        evidence.append(str(post.get("text") or "Post-SL Watch نامشخص"))
        if dwe is not None or gap is not None or conflict is not None or ignition is not None or weakness is not None:
            evidence.append(f"لحظه سیگنال: DWE={dwe if dwe is not None else '?'} gap={gap if gap is not None else '?'} conflict={conflict if conflict is not None else '?'} ignition={ignition if ignition is not None else '?'} weakness={weakness if weakness is not None else '?'}")
        if trade is not None or book is not None:
            evidence.append(f"فشار لحظه سیگنال: trade={trade if trade is not None else '?'} book={book if book is not None else '?'}")

        # فقط شواهد اصلی را در پیام نگه می‌داریم تا تلگرام بیش از حد بلند نشود.
        ev_text = "\n".join(f"- {x}" for x in evidence[:7])
        score_line = ", ".join(f"{k}={v:.0f}" for k, v in ordered[:4])
        secondary = names.get(secondary_key, secondary_key)
        return (
            "\n\n🧠 علت‌یابی استاپ:\n"
            f"علت اصلی: {names.get(primary_key, primary_key)}\n"
            f"علت فرعی: {secondary}\n"
            f"اطمینان تشخیص: {confidence}%\n"
            f"امتیاز علت‌ها: {score_line}\n\n"
            "شواهد:\n"
            f"{ev_text}\n\n"
            f"ماژول مشکوک: {modules.get(primary_key, 'نامشخص')}\n"
            f"اقدام پیشنهادی: {actions.get(primary_key, 'بررسی دستی لازم است')}"
        )

    def _send_result(
        self,
        sig: dict,
        reason: str,
        exit_price: float,
        net: float,
        gross: float,
        fee: float,
        mfe: float = 0.0,
        mae: float = 0.0,
        candles: list[dict[str, float]] | None = None,
        close_ts_ms: int | None = None,
    ):
        if not self.telegram:
            return
        icon = "✅" if reason == "TP" else "❌"
        title = "TP خورد" if reason == "TP" else "SL خورد"
        diagnosis = ""
        if reason == "SL":
            try:
                diagnosis = self._diagnose_sl(sig, exit_price, mfe, mae, candles=candles, close_ts_ms=close_ts_ms)
            except Exception as exc:
                logger.warning("SL_DIAGNOSIS_ERROR id=%s error=%s", sig.get("id"), exc)
                diagnosis = "\n\n🧠 علت‌یابی استاپ: خطا در محاسبه علت؛ لاگ مانیتور بررسی شود."
        text = (
            f"{icon} {title}\n\n"
            f"#{sig['id']} | {sig['symbol_id']} | {sig['side']}\n"
            f"Entry: {sig['entry']:.8g}\n"
            f"Exit: {exit_price:.8g}\n"
            f"PnL خام: {gross:.4f} USDT\n"
            f"کارمزد/اسلیپیج تخمینی: {fee:.4f} USDT\n"
            f"PnL خالص: {net:.4f} USDT\n"
            f"MFE: {mfe:.3f}% | MAE: {mae:.3f}%\n"
            f"close_reason: {reason}"
            f"{diagnosis}"
        )
        self.telegram.send_message(text, reply_to_message_id=sig.get("message_id"))

    def check_virtual(self, sig: dict) -> None:
        candles = self.okx.get_candles(sig["okx_symbol"], limit=120)
        reason, exit_price, ts = self.okx.reached_tp_or_sl(candles, sig["side"], float(sig["tp"]), float(sig["sl"]), int(sig["created_at"]) * 1000)
        if not reason or exit_price is None:
            if time.time() - int(sig["created_at"]) > config.VIRTUAL_MONITOR_MAX_MINUTES * 60:
                self.storage.update_signal(sig["id"], status="closed", closed_at=int(time.time()), close_reason="TIMEOUT")
            return
        trade_usdt = float(self.storage.get("trade_usdt", config.TRADE_USDT_DEFAULT))
        leverage = int(self.storage.get("leverage", config.LEVERAGE_DEFAULT))
        gross, fee, net = self._pnl(sig["side"], float(sig["entry"]), float(exit_price), trade_usdt, leverage)
        mfe, mae = self.okx.max_favorable_adverse(candles, sig["side"], float(sig["entry"]), int(sig["created_at"]) * 1000)
        closed_at = int((ts or int(time.time() * 1000)) / 1000)
        self.storage.update_signal(sig["id"], status="closed", closed_at=closed_at, exit_price=exit_price, gross_pnl=gross, fee_usdt=fee, net_pnl=net, close_reason=reason, mfe=mfe, mae=mae)
        self.storage.add_profit(net)
        logger.info("SIGNAL_CLOSED id=%s symbol=%s mode=virtual result=%s net=%.4f exit=%.8g mfe=%.3f mae=%.3f", sig["id"], sig["symbol_id"], reason, net, exit_price, mfe, mae)
        self._send_result(sig, reason, exit_price, net, gross, fee, mfe, mae, candles=candles, close_ts_ms=ts)

    def check_real(self, sig: dict) -> None:
        # اگر پوزیشن هنوز باز است، نتیجه قطعی نشده. اگر دیگر باز نیست، از order history/آخرین قیمت خروج تقریبی می‌گیریم.
        opened = self.toobit.check_position_opened(sig["toobit_symbol"])
        if opened:
            return
        # پوزیشن بسته شده؛ نتیجه را با نزدیک‌ترین قیمت OKX تخمین می‌زنیم اگر API history دقیق موجود نبود.
        exit_price = self.okx.get_last_price(sig["okx_symbol"])
        reason = "TP" if ((sig["side"] == "LONG" and exit_price >= float(sig["tp"])) or (sig["side"] == "SHORT" and exit_price <= float(sig["tp"]))) else "SL"
        trade_usdt = float(self.storage.get("trade_usdt", config.TRADE_USDT_DEFAULT))
        leverage = int(self.storage.get("leverage", config.LEVERAGE_DEFAULT))
        entry_for_pnl = float(sig.get("entry_real") or sig["entry"])
        gross, fee, net = self._pnl(sig["side"], entry_for_pnl, exit_price, trade_usdt, leverage)
        candles: list[dict[str, float]] = []
        mfe, mae = 0.0, 0.0
        try:
            candles = self.okx.get_candles(sig["okx_symbol"], limit=120)
            mfe, mae = self.okx.max_favorable_adverse(candles, sig["side"], float(sig["entry"]), int(sig["created_at"]) * 1000)
        except Exception as exc:
            logger.warning("REAL_RESULT_CANDLES_ERROR id=%s symbol=%s error=%s", sig.get("id"), sig.get("symbol_id"), exc)
        self.storage.update_signal(sig["id"], status="closed", closed_at=int(time.time()), exit_price=exit_price, gross_pnl=gross, fee_usdt=fee, net_pnl=net, close_reason=reason, mfe=mfe, mae=mae)
        self.storage.add_profit(net)
        logger.info("SIGNAL_CLOSED id=%s symbol=%s mode=real result=%s net=%.4f exit=%.8g mfe=%.3f mae=%.3f", sig["id"], sig["symbol_id"], reason, net, exit_price, mfe, mae)
        self._send_result(sig, reason, exit_price, net, gross, fee, mfe, mae, candles=candles, close_ts_ms=int(time.time() * 1000))

    def tick(self) -> None:
        for sig in self.storage.get_open_signals():
            try:
                if int(sig.get("is_real") or 0):
                    self.check_real(sig)
                else:
                    self.check_virtual(sig)
            except Exception as exc:
                logger.warning("MONITOR_SIGNAL_ERROR id=%s symbol=%s error=%s", sig.get("id"), sig.get("symbol_id"), exc)
                self.storage.add_health_event("monitor", "warning", f"monitor failed: {exc}", sig.get("symbol_id"))
                continue
