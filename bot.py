from __future__ import annotations

import json
import re
import threading
import time
from typing import Any

import config
from monitor import SignalMonitor
from okx_data import OkxDataClient
from runtime_safety import RuntimeSafety
from storage import Storage, StoredSignal
from strategy_ice_5m import ICE5MStrategy, SignalPlan
from telegram_client import TelegramClient
from telegram_ui import render_brain, render_result, render_signal, render_stats, render_trade_panel
from toobit_client import ToobitClient
from utils import logger, normalize_symbol, safe_float, safe_int, side_to_order_side


class Crypto5MICEBot:
    def __init__(self) -> None:
        self.storage = Storage()
        self.okx = OkxDataClient()
        self.toobit = ToobitClient()
        self.strategy = ICE5MStrategy()
        self.safety = RuntimeSafety(self.storage)
        self.monitor = SignalMonitor(self.storage, self.okx, self.toobit)
        self.telegram = TelegramClient()
        self.stop_event = threading.Event()
        self._toobit_symbols_cache: dict[str, dict[str, Any]] | None = None
        self._toobit_symbols_cache_at = 0.0
        self._panel_balance_cache: tuple[float, dict[str, float]] | None = None

    def run(self) -> None:
        logger.info("%s started | symbols=%s", config.BOT_NAME, len(config.WATCHLIST))
        self.telegram.send("✅ ربات ICE-5M روشن شد.\nبرای پنل بنویس: ترید")
        threads = [
            threading.Thread(target=self._scan_loop, name="scan-loop", daemon=True),
            threading.Thread(target=self._monitor_loop, name="monitor-loop", daemon=True),
            threading.Thread(target=self._telegram_loop, name="telegram-loop", daemon=True),
        ]
        for t in threads:
            t.start()
        try:
            while not self.stop_event.is_set():
                time.sleep(1)
        except KeyboardInterrupt:
            self.stop_event.set()
        logger.info("bot stopped")

    def _scan_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                settings = self.storage.settings()
                if settings.get("auto_signal_enabled"):
                    self.scan_once()
                else:
                    self.storage.runtime_set("last_scan_status", "auto_signal_off")
            except Exception as exc:
                logger.exception("scan loop failed without crash: %s", exc)
            self.stop_event.wait(max(1, int(config.FULL_SCAN_SECONDS)))

    def _monitor_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.monitor.check_once(self._send_result)
            except Exception as exc:
                logger.exception("monitor loop failed without crash: %s", exc)
            self.stop_event.wait(max(1, int(config.MONITOR_INTERVAL_SECONDS)))

    def _telegram_loop(self) -> None:
        while not self.stop_event.is_set():
            updates = self.telegram.get_updates()
            for update in updates:
                try:
                    self._handle_update(update)
                except Exception as exc:
                    logger.warning("telegram update failed: %s", exc)
            if not self.telegram.enabled:
                self.stop_event.wait(5)

    def scan_once(self) -> None:
        watchlist = self.safety.limited_watchlist()
        started_at = int(time.time())
        self.storage.runtime_set("last_scan_started_at", started_at)
        summary: dict[str, Any] = {
            "started_at": started_at,
            "total": len(watchlist),
            "scanned": 0,
            "signals": 0,
            "rejected": 0,
            "skipped_open": 0,
            "skipped_cooldown": 0,
            "errors": 0,
            "last_rejects": [],
        }
        reason_counts: dict[str, int] = {}
        for symbol in watchlist:
            if self.stop_event.is_set():
                break
            symbol = normalize_symbol(symbol)
            if not symbol:
                continue
            if not self.safety.can_scan_coin(symbol):
                summary["skipped_cooldown"] += 1
                reason = "رد شد: ارز در کول‌داون خطا است"
                self._record_reject(summary, reason_counts, symbol, reason)
                continue
            try:
                if self.storage.has_open_symbol(symbol):
                    summary["skipped_open"] += 1
                    continue
                summary["scanned"] += 1
                plan = self._analyze_symbol(symbol)
                self.safety.clear_coin_error(symbol)
                if plan is None:
                    reason = self.strategy.last_reject_reason or "رد شد: شرایط ICE کامل نشد"
                    summary["rejected"] += 1
                    self._record_reject(summary, reason_counts, symbol, reason)
                    continue
                summary["signals"] += 1
                self._handle_plan(plan)
            except Exception as exc:
                summary["errors"] += 1
                self.safety.record_coin_error(symbol, exc)
        finished_at = int(time.time())
        summary["finished_at"] = finished_at
        summary["duration_seconds"] = max(0, finished_at - started_at)
        summary["reason_counts"] = sorted([{"reason": r, "count": c} for r, c in reason_counts.items()], key=lambda x: x["count"], reverse=True)[:10]
        summary["last_rejects"] = summary["last_rejects"][-15:]
        self.storage.runtime_set("last_scan_finished_at", finished_at)
        self.storage.runtime_set("last_scan_summary", json.dumps(summary, ensure_ascii=False))
        logger.info("scan summary: %s", summary)

    def _record_reject(self, summary: dict[str, Any], reason_counts: dict[str, int], symbol: str, reason: str) -> None:
        logger.info("scan rejected: %s | %s", symbol, reason)
        self.storage.add_scan_reject(symbol, reason)
        reason_counts[reason] = reason_counts.get(reason, 0) + 1
        summary["last_rejects"].append({"symbol": symbol, "reason": reason})

    def _analyze_symbol(self, symbol: str) -> SignalPlan | None:
        settings = self.storage.settings()
        toobit_symbol = self._resolve_toobit_symbol(symbol)
        candles_15m = self.okx.get_candles(symbol, "15m", config.OKX_CANDLE_LIMIT)
        candles_5m = self.okx.get_candles(symbol, "5m", config.OKX_CANDLE_LIMIT)
        candles_1m = self.okx.get_candles(symbol, "1m", max(80, min(300, config.OKX_CANDLE_LIMIT)))
        order_book = self.okx.get_order_book(symbol, config.ORDERBOOK_DEPTH_LEVELS)
        trades = self.okx.get_trades(symbol, 100)
        return self.strategy.analyze(
            symbol,
            candles_15m,
            candles_5m,
            candles_1m,
            order_book,
            trades,
            margin_usdt=float(settings["trade_dollar_usdt"]),
            leverage=int(settings["leverage"]),
            min_net_profit_usdt=float(settings["min_net_profit_usdt"]),
            toobit_symbol=toobit_symbol,
            round_trip_fee_usdt=float(config.ROUND_TRIP_FEE_USDT),
        )

    def _handle_plan(self, plan: SignalPlan) -> None:
        settings = self.storage.settings()
        if plan.estimated_net_profit_usdt < float(settings["min_net_profit_usdt"]):
            self.storage.runtime_set("last_signal_block_reason", f"MIN_NET_PROFIT {plan.symbol}: {plan.estimated_net_profit_usdt:.4f}")
            return
        if not settings["real_trade_enabled"]:
            self._emit_normal(plan)
            return
        if not self.safety.can_open_real_now(self.toobit, max_positions=int(settings["max_positions"])):
            self.storage.runtime_set("last_real_block_reason", "SLOTS_FULL_WAIT_70S_TOOBIT_RECHECK")
            self._emit_normal(plan)
            return
        self._open_real_or_fallback(plan, settings)

    def _emit_normal(self, plan: SignalPlan) -> int:
        signal_id = self.storage.add_signal(plan, signal_type="normal")
        msg_id = self.telegram.send(render_signal(signal_id, plan, "normal"))
        self.storage.update_message_id(signal_id, msg_id)
        return signal_id

    def _open_real_or_fallback(self, plan: SignalPlan, settings: dict[str, Any]) -> int:
        if not self.toobit.has_credentials:
            self.storage.mark_real_failed(plan.symbol, "Toobit API key/secret is empty")
            return self._emit_normal(plan)
        try:
            exchange_symbols = self._get_toobit_exchange_symbols()
            toobit_symbol, symbol_info = self.toobit.validate_symbol(plan.symbol, exchange_symbols)
            client_id = f"ice5m_{plan.symbol}_{int(time.time())}"
            result = self.toobit.place_market_order(
                symbol=toobit_symbol,
                side=side_to_order_side(plan.direction),
                entry_price=plan.entry_price,
                trade_amount_usdt=float(settings["trade_dollar_usdt"]),
                leverage=int(settings["leverage"]),
                tp_price=plan.tp_price,
                sl_price=plan.sl_price,
                client_order_id=client_id,
                symbol_info=symbol_info,
            )
            if not result.get("opened"):
                self.storage.mark_real_failed(plan.symbol, str(result.get("reason") or "real order not opened"))
                return self._emit_normal(plan)
            order_id = str(result.get("order_id") or result.get("client_order_id") or "")
            signal_id = self.storage.add_signal(plan, signal_type="real", order_id=order_id, client_order_id=client_id)
            msg_id = self.telegram.send(render_signal(signal_id, plan, "real"))
            self.storage.update_message_id(signal_id, msg_id)
            return signal_id
        except Exception as exc:
            logger.warning("real order failed, fallback to normal: %s", exc)
            self.storage.mark_real_failed(plan.symbol, str(exc))
            return self._emit_normal(plan)

    def _resolve_toobit_symbol(self, symbol: str) -> str:
        try:
            exchange_symbols = self._get_toobit_exchange_symbols()
            resolved, _info = self.toobit.validate_symbol(symbol, exchange_symbols)
            return resolved
        except Exception:
            return symbol

    def _get_toobit_exchange_symbols(self) -> dict[str, dict[str, Any]]:
        now = time.time()
        if self._toobit_symbols_cache is not None and now - self._toobit_symbols_cache_at < 3600:
            return self._toobit_symbols_cache
        self._toobit_symbols_cache = self.toobit.get_exchange_symbols()
        self._toobit_symbols_cache_at = now
        return self._toobit_symbols_cache

    def _handle_update(self, update: dict[str, Any]) -> None:
        msg = update.get("message") or {}
        text = str(msg.get("text") or "").strip()
        chat_id = (msg.get("chat") or {}).get("id")
        if not text or chat_id is None:
            return
        if config.OWNER_ID and str(chat_id) != str(config.OWNER_ID) and str(chat_id) != str(config.TELEGRAM_CHAT_ID):
            self.telegram.send("⛔ دسترسی مجاز نیست.", chat_id=chat_id)
            return
        reply = self.handle_command(text)
        self.telegram.send(reply, chat_id=chat_id)

    def handle_command(self, text: str) -> str:
        t = text.strip()
        low = t.lower()
        if low in {"/start", "start", "پنل", "وضعیت", "ترید", "پنل ترید"}:
            return self._panel_text()
        if t in {"ترید روشن", "ترید فعال"}:
            self.storage.set_setting("real_trade_enabled", "1")
            return "✅ ترید واقعی روشن شد. اگر اسلات آزاد باشد، سیگنال واجد شرایط روی Toobit اجرا می‌شود."
        if t == "ترید خاموش":
            self.storage.set_setting("real_trade_enabled", "0")
            return "⛔ ترید واقعی خاموش شد. سیگنال‌ها معمولی ارسال می‌شوند."
        if t == "اتو سیگنال روشن":
            self.storage.set_setting("auto_signal_enabled", "1")
            return "✅ اتو سیگنال روشن شد."
        if t == "اتو سیگنال خاموش":
            self.storage.set_setting("auto_signal_enabled", "0")
            return "⛔ اتو سیگنال خاموش شد."
        if low in {"آمار", "stats"}:
            return render_stats(self.storage.stats())
        if low in {"هوش", "brain"}:
            return render_brain(self.storage.settings(), self._runtime_snapshot())
        if low in {"اسکن", "scan"}:
            self.scan_once()
            return "✅ یک اسکن دستی انجام شد. نتیجه در پنل/آمار ثبت شد."
        if t == "حذف آمار تایید":
            self.storage.reset_stats()
            return "🧹 آمار و سیگنال‌های ذخیره‌شده حذف شد."
        if t == "حذف آمار":
            return "برای حذف آمار بنویس: حذف آمار تایید"
        m = re.match(r"^ترید\s+دلار\s+([0-9]+(?:\.[0-9]+)?)$", t)
        if m:
            v = max(1.0, safe_float(m.group(1), config.DEFAULT_TRADE_DOLLAR))
            self.storage.set_setting("trade_dollar_usdt", v)
            return f"✅ مبلغ هر معامله شد: {v:g} USDT"
        m = re.match(r"^ترید\s+لوریج\s+([0-9]+)$", t)
        if m:
            v = max(1, min(125, safe_int(m.group(1), config.DEFAULT_LEVERAGE)))
            self.storage.set_setting("leverage", v)
            return f"✅ لوریج شد: {v}x"
        m = re.match(r"^حداکثر\s+پوزیشن\s+([0-9]+)$", t)
        if m:
            v = max(1, min(20, safe_int(m.group(1), config.DEFAULT_MAX_POSITIONS)))
            self.storage.set_setting("max_positions", v)
            return f"✅ حداکثر پوزیشن همزمان شد: {v}"
        m = re.match(r"^حداقل\s+سود\s+([0-9]+(?:\.[0-9]+)?)$", t)
        if m:
            v = max(0.0, safe_float(m.group(1), config.DEFAULT_MIN_NET_PROFIT_USDT))
            self.storage.set_setting("min_net_profit_usdt", v)
            return f"✅ حداقل سود خالص شد: {v:g} USDT"
        if low in {"پوزیشن", "positions"}:
            return self._positions_text()
        if low in {"کوین‌ها", "coins", "watchlist"}:
            return "📌 کوین‌های فعال:\n" + ", ".join(config.WATCHLIST)
        return (
            "دستور نامشخص است.\n"
            "دستورات اصلی: ترید، آمار، هوش، ترید روشن، ترید خاموش، اتو سیگنال روشن، اتو سیگنال خاموش"
        )

    def _panel_text(self) -> str:
        balance = self._balance_cached()
        return render_trade_panel(self.storage.settings(), self.storage.stats(), self._runtime_snapshot(), balance)

    def _runtime_snapshot(self) -> dict[str, str]:
        keys = [
            "last_scan_summary", "last_real_block_reason", "last_real_failed", "last_signal_block_reason",
            "last_toobit_open_count", "last_toobit_open_symbols", "last_slot_recheck_error",
        ]
        return {k: self.storage.runtime_get(k, "") for k in keys}

    def _balance_cached(self) -> dict[str, float]:
        now = time.time()
        if self._panel_balance_cache and now - self._panel_balance_cache[0] < int(config.TOOBIT_PANEL_CACHE_SECONDS):
            return self._panel_balance_cache[1]
        if not self.toobit.has_credentials:
            return {}
        try:
            data = self.toobit.get_usdt_balance_summary()
            self._panel_balance_cache = (now, data)
            return data
        except Exception as exc:
            self.storage.runtime_set("last_balance_error", str(exc)[:300])
            return {}

    def _positions_text(self) -> str:
        active = self.storage.active_signals()
        if not active:
            return "پوزیشن/سیگنال باز نداریم."
        lines = ["📌 <b>پوزیشن/سیگنال‌های باز</b>"]
        for s in active[:20]:
            lines.append(f"#{s.id} {s.signal_type.upper()} {s.symbol} {s.direction} | Entry {s.entry_price:.6f} | TP {s.tp_price:.6f} | SL {s.sl_price:.6f}")
        return "\n".join(lines)

    def _send_result(self, signal: StoredSignal, result) -> int | None:
        text = render_result(signal, result)
        msg_id = self.telegram.send(text, reply_to_message_id=signal.message_id)
        if msg_id is None and signal.message_id:
            msg_id = self.telegram.send("نتیجه مربوط به سیگنال #" + str(signal.id) + "\n" + text)
        return msg_id


def main() -> None:
    Crypto5MICEBot().run()


if __name__ == "__main__":
    main()
