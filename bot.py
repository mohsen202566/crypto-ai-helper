from __future__ import annotations

import re
import signal
import threading
import time
from typing import Any

import config
from monitor import SignalMonitor
from okx_data import OkxDataClient
from runtime_safety_4h import RuntimeSafety4H
from storage import Storage, StoredSignal
from strategy_4h_simple import SignalPlan, Simple4HStrategy
from telegram_client import TelegramClient
from telegram_ui import render_result, render_signal, render_stats, render_trade_panel
from toobit_client import ToobitClient
from utils import logger, normalize_symbol, safe_float, safe_int, side_to_order_side


class Crypto1HBot:
    def __init__(self) -> None:
        self.storage = Storage()
        self.okx = OkxDataClient()
        self.toobit = ToobitClient()
        self.strategy = Simple4HStrategy()
        self.safety = RuntimeSafety4H(self.storage)
        self.monitor = SignalMonitor(self.storage, self.okx, self.toobit)
        self.telegram = TelegramClient()
        self.stop_event = threading.Event()
        self._toobit_symbols_cache: dict[str, dict[str, Any]] | None = None
        self._toobit_symbols_cache_at = 0.0

    # -------------------------
    # Main loops
    # -------------------------
    def run(self) -> None:
        logger.info("%s شروع شد | symbols=%s", config.BOT_NAME, len(config.WATCHLIST))
        self.telegram.send("✅ ربات 1H Trend Pullback روشن شد.\nبرای پنل بنویس: ترید")
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
        logger.info("ربات متوقف شد")

    def _scan_loop(self) -> None:
        while not self.stop_event.is_set():
            start = time.time()
            try:
                self.scan_once()
            except Exception as exc:
                logger.exception("چرخه اسکن کرش نکرد؛ خطای کلی ثبت شد: %s", exc)
            elapsed = time.time() - start
            sleep_for = max(1.0, float(config.FULL_SCAN_SECONDS) - elapsed)
            self.stop_event.wait(sleep_for)

    def _monitor_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.monitor.check_once(self._send_result)
            except Exception as exc:
                logger.exception("چرخه مانیتورینگ کرش نکرد؛ خطا ثبت شد: %s", exc)
            self.stop_event.wait(max(1, int(config.MONITOR_INTERVAL_SECONDS)))

    def _telegram_loop(self) -> None:
        while not self.stop_event.is_set():
            updates = self.telegram.get_updates()
            for update in updates:
                try:
                    self._handle_update(update)
                except Exception as exc:
                    logger.warning("پردازش پیام تلگرام خطا داد: %s", exc)
            if not self.telegram.enabled:
                self.stop_event.wait(5)

    # -------------------------
    # Scanner
    # -------------------------
    def scan_once(self) -> None:
        watchlist = self.safety.limited_watchlist()
        self.storage.runtime_set("last_scan_started_at", int(time.time()))
        found = 0
        for symbol in watchlist:
            if self.stop_event.is_set():
                break
            symbol = normalize_symbol(symbol)
            if not symbol:
                continue
            if not self.safety.can_scan_coin(symbol):
                continue
            try:
                if self.storage.has_open_symbol(symbol):
                    continue
                plan = self._analyze_symbol(symbol)
                self.safety.clear_coin_error(symbol)
                if plan is None:
                    continue
                found += 1
                self._handle_plan(plan)
            except Exception as exc:
                self.safety.record_coin_error(symbol, exc)
                continue
        self.storage.runtime_set("last_scan_finished_at", int(time.time()))
        self.storage.runtime_set("last_scan_found", found)

    def _analyze_symbol(self, symbol: str) -> SignalPlan | None:
        settings = self.storage.settings()
        toobit_symbol = self._resolve_toobit_symbol(symbol)
        if not toobit_symbol:
            return None
        candles_4h = self.okx.get_candles(symbol, "4H", config.OKX_CANDLE_LIMIT)
        candles_1h = self.okx.get_candles(symbol, "1H", config.OKX_CANDLE_LIMIT)
        return self.strategy.analyze(
            symbol,
            candles_4h,
            candles_1h,
            margin_usdt=float(settings["trade_dollar_usdt"]),
            leverage=int(settings["leverage"]),
            toobit_symbol=toobit_symbol,
            round_trip_fee_usdt=float(config.ROUND_TRIP_FEE_USDT),
        )

    def _handle_plan(self, plan: SignalPlan) -> None:
        settings = self.storage.settings()
        if not settings["real_trade_enabled"]:
            self._emit_normal(plan)
            return
        if plan.estimated_net_profit_usdt < float(settings["min_net_profit_usdt"]):
            self.storage.runtime_set("last_real_block_reason", f"MIN_NET_PROFIT {plan.symbol}: {plan.estimated_net_profit_usdt:.4f}")
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
            client_id = f"c1h_{plan.symbol}_{int(time.time())}"
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
            data = plan.to_legacy_dict()
            data["toobit_symbol"] = toobit_symbol
            data["trade_margin_usdt"] = float(settings["trade_dollar_usdt"])
            data["leverage"] = int(settings["leverage"])
            if result.get("entry_price"):
                data["entry_price"] = float(result["entry_price"])
            if result.get("tp_price"):
                data["tp_price"] = float(result["tp_price"])
            if result.get("sl_price"):
                data["sl_price"] = float(result["sl_price"])
            signal_id = self.storage.add_signal(data, signal_type="real", real_status="opened", order_id=result.get("order_id"))
            msg_id = self.telegram.send(render_signal(signal_id, data, "real"))
            self.storage.update_message_id(signal_id, msg_id)
            return signal_id
        except Exception as exc:
            logger.warning("باز کردن Real برای %s ناموفق بود و Normal صادر شد: %s", plan.symbol, exc)
            self.storage.mark_real_failed(plan.symbol, str(exc))
            return self._emit_normal(plan)

    def _resolve_toobit_symbol(self, symbol: str) -> str | None:
        try:
            exchange_symbols = self._get_toobit_exchange_symbols()
            resolved, _info = self.toobit.validate_symbol(symbol, exchange_symbols)
            return resolved
        except Exception as exc:
            logger.warning("نماد %s در Toobit معتبر نشد و رد شد: %s", symbol, exc)
            return None

    def _get_toobit_exchange_symbols(self) -> dict[str, dict[str, Any]]:
        now = time.time()
        if self._toobit_symbols_cache is not None and now - self._toobit_symbols_cache_at < 3600:
            return self._toobit_symbols_cache
        self._toobit_symbols_cache = self.toobit.get_exchange_symbols()
        self._toobit_symbols_cache_at = now
        return self._toobit_symbols_cache

    # -------------------------
    # Telegram commands
    # -------------------------
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
        if low in {"/start", "start", "پنل", "وضعیت", "ترید"}:
            return self._panel_text()
        if t == "ترید فعال":
            self.storage.set_setting("real_trade_enabled", "1")
            return "✅ ترید واقعی فعال شد. از این به بعد اگر اسلات آزاد باشد سیگنال واجد شرایط به Toobit ارسال می‌شود."
        if t == "ترید خاموش":
            self.storage.set_setting("real_trade_enabled", "0")
            return "⛔ ترید واقعی خاموش شد. سیگنال‌ها فقط عادی ثبت و مانیتور می‌شوند."
        m = re.match(r"^ترید\s+دلار\s+([0-9]+(?:\.[0-9]+)?)$", t)
        if m:
            value = max(1.0, safe_float(m.group(1), config.DEFAULT_TRADE_DOLLAR))
            self.storage.set_setting("trade_dollar_usdt", value)
            return f"✅ دلار هر پوزیشن شد: {value:.2f} USDT"
        m = re.match(r"^ترید\s+لوریج\s+([0-9]+)$", t)
        if m:
            value = max(1, min(125, safe_int(m.group(1), config.DEFAULT_LEVERAGE)))
            self.storage.set_setting("leverage", value)
            return f"✅ لوریج شد: {value}x"
        m = re.match(r"^حداکثر\s+پوزیشن\s+([0-9]+)$", t)
        if m:
            value = max(1, min(20, safe_int(m.group(1), config.DEFAULT_MAX_POSITIONS)))
            self.storage.set_setting("max_positions", value)
            return f"✅ حداکثر پوزیشن شد: {value}"
        m = re.match(r"^سرمایه\s+ترید\s+([0-9]+(?:\.[0-9]+)?)$", t)
        if m:
            value = max(1.0, safe_float(m.group(1), config.DEFAULT_TRADE_CAPITAL))
            self.storage.set_setting("trade_capital_usdt", value)
            return f"✅ سرمایه مجاز ربات شد: {value:.2f} USDT"
        m = re.match(r"^حداقل\s+سود\s+خالص\s+([0-9]+(?:\.[0-9]+)?)$", t)
        if m:
            value = max(0.0, safe_float(m.group(1), config.DEFAULT_MIN_NET_PROFIT_USDT))
            self.storage.set_setting("min_net_profit_usdt", value)
            return f"✅ حداقل سود خالص Real شد: {value:.2f} USDT"
        m = re.match(r"^آمار(?:\s+([0-9]+))?$", t)
        if m:
            days = max(1, min(365, safe_int(m.group(1), 30)))
            return render_stats(self.storage.stats(days), days)
        if t in {"پوزیشن", "پوزیشن‌ها", "پوزیشن ها"}:
            return self.storage.recent_open_positions_text()
        if t in {"کوین‌ها", "کوین ها", "ارزها", "ارزهای فعال"}:
            return "📌 ارزهای فعال:\n" + "\n".join(config.WATCHLIST)
        if t in {"راهنما", "help", "/help"}:
            return self._help_text()
        return "دستور شناخته نشد. برای راهنما بنویس: راهنما"

    def _panel_text(self) -> str:
        settings = self.storage.settings()
        margin = None
        try:
            if self.toobit.has_credentials:
                margin = self.toobit.get_usdt_balance_summary()
        except Exception as exc:
            logger.warning("خواندن مارجین توبیت برای پنل ناموفق بود: %s", exc)
        active = self.storage.active_real_count()
        free = self.storage.free_real_slots(int(settings["max_positions"]))
        return render_trade_panel(settings, active_real=active, free_slots=free, margin_summary=margin)

    @staticmethod
    def _help_text() -> str:
        return "\n".join([
            "راهنما:",
            "ترید / پنل / وضعیت",
            "ترید فعال",
            "ترید خاموش",
            "ترید دلار 10",
            "ترید لوریج 10",
            "حداکثر پوزیشن 3",
            "سرمایه ترید 100",
            "حداقل سود خالص 0.01",
            "آمار یا آمار 7",
            "پوزیشن",
            "کوین‌ها",
        ])

    def _send_result(self, signal: StoredSignal, result) -> int | None:
        return self.telegram.send(render_result(signal, result), reply_to_message_id=signal.message_id)


def main() -> None:
    bot = Crypto1HBot()
    bot.run()


if __name__ == "__main__":
    main()
