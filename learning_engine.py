from __future__ import annotations

from storage import Storage
from utils import direction_profit_pct


class LearningEngine:
    def __init__(self, storage: Storage) -> None:
        self.storage = storage

    def learn_from_closed_signal(self, signal_id: int) -> None:
        signal = self.storage.signal_dict(signal_id)
        if not signal:
            return
        status = str(signal.get("status"))
        if status not in {"TP", "SL"}:
            return
        entry = float(signal.get("entry") or 0)
        exit_price = float(signal.get("exit_price") or (signal.get("tp") if status == "TP" else signal.get("sl")) or 0)
        direction = str(signal.get("direction"))
        margin = float(signal.get("margin_usdt") or 0)
        leverage = int(signal.get("leverage") or 1)
        profit_pct = direction_profit_pct(direction, entry, exit_price)
        approx_net = margin * leverage * profit_pct
        real_pnl = signal.get("real_pnl")
        net_profit = float(real_pnl) if real_pnl is not None else approx_net
        features_key = str(signal.get("features_key") or "")
        self.storage.record_observation(
            source=str(signal.get("signal_type") or "normal"),
            signal_id=signal_id,
            features_key=features_key,
            symbol_name=str(signal.get("symbol_name")),
            direction=direction,
            result=status,
            net_profit=net_profit,
            mfe_pct=float(signal.get("mfe_pct") or 0),
            mae_pct=float(signal.get("mae_pct") or 0),
            tp_distance_pct=float(signal.get("tp_distance_pct") or 0),
            sl_distance_pct=float(signal.get("sl_distance_pct") or 0),
            reason=self._failure_reason(signal) if status == "SL" else "TP_OK",
        )
        self.storage.update_shadow_results(signal_id, direction, float(signal.get("best_price") or entry), float(signal.get("worst_price") or entry))
        self._maybe_capital_suggestion()
        self._maybe_indicator_request()

    @staticmethod
    def _failure_reason(signal: dict) -> str:
        mae = float(signal.get("mae_pct") or 0)
        sl_dist = float(signal.get("sl_distance_pct") or 0)
        mfe = float(signal.get("mfe_pct") or 0)
        tp_dist = float(signal.get("tp_distance_pct") or 0)
        market_state = str(signal.get("market_state") or "")
        if sl_dist > 0 and mae >= sl_dist * 0.95 and mfe < tp_dist * 0.25:
            return "DIRECTION_OR_ENTRY_WRONG"
        if mfe >= tp_dist * 0.60:
            return "TP_TOO_FAR_OR_REVERSAL"
        if market_state in {"CLIMAX", "FAKE_BREAKOUT_RISK"}:
            return "CLIMAX_OR_FAKE_BREAKOUT"
        if sl_dist > 0 and mae <= sl_dist * 1.05:
            return "SL_HIT_AFTER_NOISE_OR_BAD_RANGE"
        return "UNKNOWN_SL_REASON"

    def _maybe_capital_suggestion(self) -> None:
        stats = self.storage.all_stats()
        if int(stats.get("closed", 0)) < 30:
            return
        win_rate = float(stats.get("win_rate", 0.0))
        pnl = float(stats.get("pnl", 0.0))
        margin = self.storage.margin_usdt()
        leverage = self.storage.leverage()
        if win_rate >= 55 and pnl > 0 and leverage > 5:
            message = f"AI: نتیجه مثبت است. برای کم‌شدن فشار ضرر، تست لوریج {max(1, leverage-2)} با دلار {margin:.2f} منطقی است؛ اجرا فقط با دستور شما."
        elif pnl <= 0 and leverage > 3:
            message = f"AI: سود خالص ضعیف است. پیشنهاد بررسی لوریج {max(1, leverage-2)} یا کاهش دلار تا تثبیت بازه‌ها؛ اجرا فقط با دستور شما."
        else:
            return
        with self.storage._connect() as conn:
            recent = conn.execute("SELECT message FROM capital_suggestions ORDER BY id DESC LIMIT 1").fetchone()
            if not recent or str(recent["message"]) != message:
                conn.execute("INSERT INTO capital_suggestions(created_at, level, message) VALUES(datetime('now'), 'info', ?)", (message,))

    def _maybe_indicator_request(self) -> None:
        stats = self.storage.all_stats()
        if int(stats.get("sl", 0)) < 20:
            return
        with self.storage._connect() as conn:
            rows = conn.execute("SELECT reason FROM range_observations WHERE result='SL' ORDER BY id DESC LIMIT 40").fetchall()
            fake = sum(1 for r in rows if "FAKE" in str(r["reason"]) or "CLIMAX" in str(r["reason"]))
            if fake >= 12:
                msg = "در SLهای اخیر شکست فیک/کلایمکس زیاد است. پیشنهاد AI: اضافه‌کردن Bollinger Width یا Donchian فقط برای تشخیص رنج/شکست، نه سیگنال مستقیم."
                recent = conn.execute("SELECT reason FROM indicator_requests ORDER BY id DESC LIMIT 1").fetchone()
                if not recent or str(recent["reason"]) != msg:
                    conn.execute("INSERT INTO indicator_requests(created_at, indicator, reason) VALUES(datetime('now'), 'BOLLINGER_WIDTH_OR_DONCHIAN', ?)", (msg,))
