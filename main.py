"""اجرای ربات: پروفایل، اسکن OKX، واچ پویا، توبیت، تلگرام و آمار."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Any
import logging
import sys
import threading
import time

import requests

import config
from engine import AdaptiveLearningManager, AdaptiveStartEngine, Observation, TradePlan
from okx_client import OKXClient
from profiles import ProfileManager, SymbolSpec
from storage import Storage
from toobit_client import ToobitFuturesClient

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)s | %(threadName)s | %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("adaptive_bot")


class TelegramPanel:
    def __init__(self, app: "TradingBotApp") -> None:
        self.app = app
        self.storage = app.storage
        self.token = config.TELEGRAM_BOT_TOKEN
        self.chat_id = config.TELEGRAM_CHAT_ID
        self.offset = int(self.storage.get("telegram_offset", 0) or 0)
        self.session = requests.Session()
        self.lock = threading.RLock()

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def send(self, text: str, reply_to: int | None = None) -> int | None:
        if not self.enabled:
            logger.info("[TELEGRAM_DISABLED] %s", text.replace("\n", " | ")[:300])
            return None
        payload: dict[str, Any] = {
            "chat_id": self.chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if reply_to:
            payload["reply_to_message_id"] = int(reply_to)
        try:
            with self.lock:
                response = self.session.post(
                    f"https://api.telegram.org/bot{self.token}/sendMessage",
                    json=payload,
                    timeout=10,
                )
            response.raise_for_status()
            data = response.json()
            if not data.get("ok"):
                raise RuntimeError(str(data))
            self.storage.clear_health("telegram")
            return int(data.get("result", {}).get("message_id"))
        except Exception as exc:
            self.storage.add_health_event("telegram", "warning", str(exc))
            logger.warning("[TELEGRAM_SEND_FAILED] %s", exc)
            return None

    def poll_once(self) -> None:
        if not self.enabled:
            return
        try:
            response = self.session.get(
                f"https://api.telegram.org/bot{self.token}/getUpdates",
                params={"offset": self.offset + 1, "timeout": 1},
                timeout=6,
            )
            response.raise_for_status()
            data = response.json()
            if not data.get("ok"):
                raise RuntimeError(str(data))
            for update in data.get("result") or []:
                self.offset = max(self.offset, int(update.get("update_id") or 0))
                self.storage.set("telegram_offset", self.offset)
                message = update.get("message") or {}
                if str((message.get("chat") or {}).get("id") or "") != str(self.chat_id):
                    continue
                text = str(message.get("text") or "").strip()
                if text:
                    self.handle(text)
            self.storage.clear_health("telegram")
        except Exception as exc:
            self.storage.add_health_event("telegram", "warning", f"poll: {exc}")

    def handle(self, text: str) -> None:
        command = text.strip()
        low = command.lower()
        try:
            if command in ("پنل", "پنل ترید", "وضعیت", "ترید"):
                self.send(self.panel_trade())
            elif command in ("آمار", "پنل آمار"):
                self.send(self.panel_stats())
            elif command in ("سلامت", "هلس") or low == "health":
                self.send(self.panel_health())
            elif command in ("چک توبیت", "موجودی", "مارجین"):
                self.send(self.app.toobit_snapshot_text(refresh=True))
            elif command in ("پوزیشن", "پوزیشن‌ها"):
                self.send(self.app.positions_text())
            elif command in ("ارزها", "بازار"):
                self.send(self.app.market_text())
            elif command == "مانیتور":
                self.send(self.app.monitor_text())
            elif command in ("یادگیری", "حافظه"):
                self.send(self.app.learning_text())
            elif command in ("ترید فعال", "توبیت روشن"):
                self.storage.set("trading_enabled", True)
                self.send("✅ ترید واقعی توبیت فعال شد.")
            elif command in ("ترید خاموش", "توبیت خاموش"):
                self.storage.set("trading_enabled", False)
                self.send("⛔ ترید واقعی خاموش شد؛ سیگنال عادی و مانیتورینگ ادامه دارد.")
            elif command in ("اتوسیگنال فعال", "اتو سیگنال فعال"):
                self.storage.set("auto_signal_enabled", True)
                self.send("✅ اتوسیگنال واقعی فعال شد.")
            elif command in ("اتوسیگنال خاموش", "اتو سیگنال خاموش"):
                self.storage.set("auto_signal_enabled", False)
                self.send("⛔ اتوسیگنال واقعی خاموش شد؛ سیگنال‌ها عادی ثبت می‌شوند.")
            elif command.startswith("دلار ترید") or command.startswith("ترید دلار"):
                self._set_number(command, "trade_usdt", config.TRADE_USDT_MIN, config.TRADE_USDT_MAX, "دلار هر ترید")
            elif command.startswith("لوریج ترید") or command.startswith("ترید لوریج"):
                self._set_number(command, "leverage", config.LEVERAGE_MIN, config.LEVERAGE_MAX, "لوریج", integer=True)
            elif command.startswith("حداکثر پوزیشن"):
                self._set_number(command, "max_positions", config.MAX_POSITIONS_MIN, config.MAX_POSITIONS_MAX, "حداکثر پوزیشن", integer=True)
            elif command == "حذف آمار":
                self.storage.reset_stats()
                self.send("✅ آمار از این لحظه صفر شد؛ سیگنال‌های فعال حذف نشدند.")
            elif command in ("ریست سود", "ریست سود کل"):
                self.storage.reset_profit()
                self.send("✅ سود/ضرر امروز و کل صفر شد.")
            else:
                self.send(
                    "⚠️ دستور شناخته نشد.\n"
                    "پنل | آمار | سلامت | چک توبیت | پوزیشن | ارزها | مانیتور | یادگیری\n"
                    "ترید فعال/خاموش | اتوسیگنال فعال/خاموش\n"
                    "دلار ترید 10 | لوریج ترید 10 | حداکثر پوزیشن 3"
                )
        except Exception as exc:
            logger.exception("telegram command failed")
            self.send(f"⚠️ خطا در اجرای دستور: {exc}")

    def _set_number(
        self, text: str, key: str, minimum: float, maximum: float, label: str, integer: bool = False
    ) -> None:
        value = float(text.split()[-1])
        if not minimum <= value <= maximum:
            self.send(f"⚠️ {label} باید بین {minimum:g} تا {maximum:g} باشد.")
            return
        final: float | int = int(value) if integer else value
        self.storage.set(key, final)
        self.send(f"✅ {label} روی {final:g} تنظیم شد.")

    @staticmethod
    def _age(timestamp: int) -> str:
        if timestamp <= 0:
            return "هنوز ثبت نشده"
        seconds = max(0, int(time.time()) - timestamp)
        if seconds < 60:
            return f"{seconds} ثانیه قبل"
        if seconds < 3600:
            return f"{seconds // 60} دقیقه قبل"
        return f"{seconds // 3600} ساعت قبل"

    def panel_trade(self) -> str:
        self.storage.roll_profit_day()
        connected = bool(self.storage.get("toobit_connected", False))
        real_open = self.storage.count_real_active()
        max_positions = int(self.storage.get("max_positions", config.MAX_POSITIONS_DEFAULT))
        active = self.storage.get_active_signals()
        learning = self.storage.learning_summary()
        return (
            "⚙️ پنل ترید\n\n"
            f"ترید واقعی: {'✅ روشن' if self.storage.get('trading_enabled', False) else '⛔ خاموش'}\n"
            f"اتوسیگنال: {'✅ فعال' if self.storage.get('auto_signal_enabled', True) else '⛔ غیرفعال'}\n"
            f"توبیت: {'✅ وصل' if connected else '❌ قطع/خطا'} | {self._age(int(self.storage.get('toobit_last_update', 0) or 0))}\n"
            f"موجودی آزاد: {float(self.storage.get('toobit_available_usdt', 0)):.4f} USDT\n"
            f"مارجین: {float(self.storage.get('toobit_margin_usdt', 0)):.4f} USDT\n"
            f"دلار ترید: {float(self.storage.get('trade_usdt', config.TRADE_USDT_DEFAULT)):g}\n"
            f"لوریج: {int(self.storage.get('leverage', config.LEVERAGE_DEFAULT))}x\n"
            f"پوزیشن واقعی: {real_open}/{max_positions}\n"
            f"کل سیگنال فعال: {len(active)}\n"
            f"ارز آماده: {len(self.app.ready_symbols)} از {len(config.SYMBOL_BASES)}\n"
            f"پروفایل: {self._age(int(self.storage.get('profiles_updated_at', 0) or 0))}\n"
            f"یادگیری: {learning['patterns']} الگو | {learning['trial']} آزمایشی | {learning['accepted']} تأییدشده\n"
            f"سود خالص امروز: {float(self.storage.get('profit_today', 0)):.4f} USDT\n"
            f"سود خالص کل: {float(self.storage.get('profit_total', 0)):.4f} USDT"
        )

    def panel_stats(self) -> str:
        stats = self.storage.stats()
        closed = stats["tp"] + stats["sl"]
        win_rate = stats["tp"] / closed * 100.0 if closed else 0.0
        return (
            "📊 پنل آمار\n\n"
            f"سیگنال‌ها: {stats['signals']}\n"
            f"فعال: {stats['open']} | Pending واقعی: {stats['pending']}\n"
            f"TP: {stats['tp']} | SL: {stats['sl']}\n"
            f"مبهم/خارج از یادگیری: {stats.get('ambiguous', 0)}\n"
            f"وین‌ریت بسته‌شده: {win_rate:.2f}%\n"
            f"واقعی: {stats['real']} | عادی: {stats['virtual']}\n"
            f"سود خالص آمار: {stats['net_pnl']:.4f} USDT\n"
            f"سود واقعی: {stats['real_net']:.4f} USDT\n"
            f"سود عادی: {stats['virtual_net']:.4f} USDT"
        )

    def panel_health(self) -> str:
        events = self.storage.active_health_events()
        learning = self.storage.learning_summary()
        lines = [
            "🩺 سلامت ربات",
            "",
            f"ارز معتبر: {len(self.app.symbols)}",
            f"پروفایل آماده: {len(self.app.ready_symbols)}",
            f"واچ فعال: {len(self.app.engine.watches)}",
            f"سیگنال فعال: {len(self.storage.get_active_signals())}",
            f"یادگیری: {learning['patterns']} الگو | بررسی پس‌ازاستاپ: {learning['reviews']}",
        ]
        if not events:
            lines.append("✅ خطای فعال ثبت نشده است.")
        else:
            lines.append("خطاهای فعال:")
            for event in events[:8]:
                symbol = f"/{event['symbol_id']}" if event.get("symbol_id") else ""
                lines.append(f"• {event['component']}{symbol}: {str(event['message'])[:120]}")
        return "\n".join(lines)


class TradingBotApp:
    def __init__(self) -> None:
        self.storage = Storage()
        self.okx = OKXClient()
        self.toobit = ToobitFuturesClient()
        self.engine = AdaptiveStartEngine()
        self.learning = AdaptiveLearningManager(self.storage)
        self.profiles = ProfileManager(self.okx, self.storage)
        self.telegram = TelegramPanel(self)
        self.stop_event = threading.Event()
        self.symbol_lock = threading.RLock()
        self.profile_lock = threading.RLock()
        self.publish_lock = threading.RLock()
        self.symbols: dict[str, SymbolSpec] = {}
        self.ready_symbols: dict[str, SymbolSpec] = {}
        self._reject_cache: dict[tuple[str, str], tuple[str, float]] = {}
        self._last_profile_day = ""
        self._virtual_last_trade_ts: dict[int, int] = {}

    @staticmethod
    def _metrics_text(metrics: dict[str, Any]) -> str:
        # INFO logs stay exact but readable. Full raw metrics are saved with the
        # signal in SQLite; journalctl only needs the values that explain a reject.
        important = (
            "window", "signed_move_pct", "move_ratio", "directionality",
            "range_ratio", "acceleration", "horizon", "expected_minutes",
            "sl_pct", "required_tp_pct", "behavior_capacity_pct",
            "min_profit_tp_pct", "tp_pct", "rr", "tp_net_usdt",
            "plan_quality", "notional", "support_tool", "learning_version",
            "learning_status", "pattern_key", "move_required", "support_required",
            "outcome_source", "outcome_valid_samples", "historical_win_rate",
            "conservative_win_rate", "expected_net_from_outcomes",
        )
        parts: list[str] = []
        for key in important:
            if key not in metrics:
                continue
            value = metrics[key]
            if isinstance(value, float):
                parts.append(f"{key}={value:.6g}")
            else:
                parts.append(f"{key}={value}")
        return " | ".join(parts)

    def log_reject(
        self, stage: str, symbol_id: str, reason: str, metrics: dict[str, Any] | None = None, force: bool = False
    ) -> None:
        key = (stage, symbol_id)
        now = time.time()
        old_reason, old_time = self._reject_cache.get(key, ("", 0.0))
        if not force and old_reason == reason and now - old_time < config.REJECT_LOG_REPEAT_SECONDS:
            return
        self._reject_cache[key] = (reason, now)
        detail = self._metrics_text(metrics or {})
        logger.info(
            "[REJECT] stage=%s symbol=%s reason=%s%s",
            stage, symbol_id, reason, f" | {detail}" if detail else "",
        )

    def resolve_symbols(self) -> dict[str, SymbolSpec]:
        okx_map = self.okx.list_usdt_swaps()
        toobit_map = self.toobit.list_usdt_contracts()
        resolved: dict[str, SymbolSpec] = {}
        for base in config.SYMBOL_BASES:
            okx_symbol = okx_map.get(base)
            toobit_symbol = toobit_map.get(base)
            if not okx_symbol or not toobit_symbol:
                logger.warning(
                    "[SYMBOL_SKIP] base=%s okx=%s toobit=%s action=continue",
                    base, bool(okx_symbol), bool(toobit_symbol),
                )
                continue
            resolved[base] = SymbolSpec(base, okx_symbol, toobit_symbol)
        with self.symbol_lock:
            self.symbols = resolved
        logger.info("[SYMBOLS_READY] valid=%d requested=%d", len(resolved), len(config.SYMBOL_BASES))
        return resolved

    def prepare_profiles(self, force: bool = False) -> None:
        self.storage.set("profile_building", True)
        try:
            symbols = list(self.resolve_symbols().values())
            built = self.profiles.load_or_build(symbols, force=force)
            with self.symbol_lock:
                self.ready_symbols = {
                    symbol_id: self.symbols[symbol_id]
                    for symbol_id in built
                    if symbol_id in self.symbols
                }
            logger.info("[SCAN_READY] symbols=%d", len(self.ready_symbols))
            if self.ready_symbols:
                self.telegram.send(
                    f"✅ پروفایل آماده شد.\nارز معتبر: {len(self.symbols)}\nارز آماده اسکن: {len(self.ready_symbols)}"
                )
            else:
                self.storage.add_health_event("profile", "critical", "هیچ پروفایل آماده‌ای وجود ندارد")
                self.telegram.send("❌ هیچ پروفایل آماده‌ای ساخته نشد؛ اسکن سیگنال شروع نمی‌شود.")
        finally:
            self.storage.set("profile_building", False)

    def _transition_log(self, observation: Observation) -> None:
        if observation.transition == "NEW":
            logger.info(
                "[WATCH_NEW] symbol=%s side=%s window=%ss reason=%s",
                observation.symbol_id, observation.side, observation.window, observation.reason,
            )
        elif observation.transition.startswith("FLIP"):
            logger.info(
                "[WATCH_FLIP] symbol=%s transition=%s window=%ss metrics=%s",
                observation.symbol_id, observation.transition, observation.window,
                self._metrics_text(observation.metrics),
            )
        elif observation.transition == "CANCEL":
            logger.info("[WATCH_CANCEL] symbol=%s reason=%s", observation.symbol_id, observation.reason)

    def scan_loop(self) -> None:
        while not self.stop_event.is_set():
            started = time.time()
            try:
                tickers = self.okx.get_all_swap_tickers()
                with self.symbol_lock:
                    symbols = list(self.ready_symbols.values())
                active_by_symbol = {row["symbol_id"]: row for row in self.storage.get_active_signals()}
                learning_reviews = self.storage.get_open_learning_reviews() if config.LEARNING_ENABLED else []
                for symbol in symbols:
                    row = tickers.get(symbol.okx)
                    if not row:
                        self.log_reject("ticker", symbol.id, "OKX_TICKER_MISSING")
                        continue
                    try:
                        price = float(row.get("last") or 0.0)
                    except (TypeError, ValueError):
                        price = 0.0
                    if price <= 0:
                        self.log_reject("ticker", symbol.id, "OKX_PRICE_INVALID")
                        continue

                    self.learning.process_reviews(symbol.id, price, learning_reviews)
                    active = active_by_symbol.get(symbol.id)
                    if active:
                        if int(active.get("is_real") or 0):
                            self.update_active_excursions(active, price)
                        else:
                            self.monitor_virtual_tick(active, price, symbol.okx)
                        continue
                    profile = self.profiles.get(symbol.id)
                    if not profile:
                        continue
                    learning_gates = self.learning.gates_for_symbol(symbol.id)
                    observation = self.engine.evaluate(
                        symbol, profile, price, learning_gates=learning_gates
                    )
                    self._transition_log(observation)
                    if observation.status == "NEEDS_VOLUME":
                        try:
                            volume = self.okx.recent_quote_volume(symbol.okx, int(observation.window or 60))
                            observation = self.engine.evaluate(
                                symbol, profile, price, volume_quote=volume, append_tick=False,
                                learning_gates=learning_gates,
                            )
                            self._transition_log(observation)
                        except Exception as exc:
                            self.log_reject("volume", symbol.id, "OKX_RECENT_TRADES_FAILED", {"error": str(exc)[:120]})
                            continue
                    if observation.status == "TRIGGER":
                        trade_usdt = float(self.storage.get("trade_usdt", config.TRADE_USDT_DEFAULT))
                        leverage = int(self.storage.get("leverage", config.LEVERAGE_DEFAULT))
                        support_tool = str(observation.metrics.get("support_tool") or "RANGE")
                        learning_adjustments = self.learning.adjustments_for(
                            symbol.id, str(observation.side), int(observation.window or 0), support_tool
                        )
                        plan, reason, metrics = self.engine.build_plan(
                            symbol, profile, observation, price, trade_usdt, leverage,
                            learning_adjustments=learning_adjustments,
                        )
                        if plan is None:
                            self.log_reject("risk", symbol.id, reason, metrics)
                            continue
                        self.publish_signal(plan)
                    elif observation.status in ("REJECT",):
                        self.log_reject("trigger", symbol.id, observation.reason, observation.metrics)
                    elif observation.status == "WATCH" and observation.reason == "PRICE_TRIGGER_WITHOUT_SUPPORT":
                        self.log_reject("support", symbol.id, observation.reason, observation.metrics)
                self.storage.clear_health("okx")
            except Exception as exc:
                logger.warning("[SCAN_ERROR] %s", exc)
                self.storage.add_health_event("okx", "warning", str(exc))
            elapsed = time.time() - started
            self.stop_event.wait(max(0.2, config.SCAN_INTERVAL_SECONDS - elapsed))

    def _mode_for_signal(self) -> tuple[str, bool]:
        enabled = bool(self.storage.get("trading_enabled", False))
        auto = bool(self.storage.get("auto_signal_enabled", True))
        connected = bool(self.storage.get("toobit_connected", False))
        max_positions = int(self.storage.get("max_positions", config.MAX_POSITIONS_DEFAULT))
        room = self.storage.count_real_active() < max_positions
        is_real = enabled and auto and connected and room and self.toobit.has_credentials
        return ("TOOBIT", True) if is_real else ("SIGNAL", False)

    def _signal_text(self, signal_id: int, plan: TradePlan, mode: str, trade_usdt: float, leverage: int) -> str:
        icon = "🟢" if plan.side == "LONG" else "🔴"
        return (
            "🚨 سیگنال شروع حرکت\n\n"
            f"#{signal_id} | {plan.symbol_id}\n"
            f"{icon} جهت: {plan.side}\n"
            f"حالت: {'واقعی توبیت' if mode == 'TOOBIT' else 'سیگنال عادی'}\n"
            f"Entry: {plan.entry:.10g}\n"
            f"TP: {plan.tp:.10g} ({plan.tp_pct:.3f}%)\n"
            f"SL: {plan.sl:.10g} ({plan.sl_pct:.3f}%)\n"
            f"انتظار تقریبی: {plan.expected_minutes} دقیقه\n"
            f"Trigger: {plan.trigger_window} ثانیه | ابزار: {plan.metrics.get('support_tool', 'RANGE')}\n"
            f"یادگیری: v{int(plan.metrics.get('learning_version', 0))} | {plan.metrics.get('learning_status', 'BASE')}\n"
            f"RR: {plan.rr_net:.2f}\n"
            f"مارجین: {trade_usdt:g} USDT | لوریج: {leverage}x\n"
            f"سود خالص تخمینی TP: {plan.estimated_tp_net:.4f} USDT\n"
            f"زیان خالص تخمینی SL: {plan.estimated_sl_net_loss:.4f} USDT\n"
            f"علت: {plan.trigger_reason}"
        )

    def publish_signal(self, plan: TradePlan) -> bool:
        with self.publish_lock:
            if self.storage.has_active_signal(plan.symbol_id):
                self.log_reject("publish", plan.symbol_id, "ACTIVE_SIGNAL_ALREADY_EXISTS", force=True)
                return False
            trade_usdt = float(self.storage.get("trade_usdt", config.TRADE_USDT_DEFAULT))
            leverage = int(self.storage.get("leverage", config.LEVERAGE_DEFAULT))
            mode, is_real = self._mode_for_signal()
            data = {
                "symbol_id": plan.symbol_id,
                "okx_symbol": plan.okx_symbol,
                "toobit_symbol": plan.toobit_symbol,
                "side": plan.side,
                "entry": plan.entry,
                "tp": plan.tp,
                "sl": plan.sl,
                "tp_pct": plan.tp_pct,
                "sl_pct": plan.sl_pct,
                "rr": plan.rr_net,
                "expected_minutes": plan.expected_minutes,
                "trigger_window": plan.trigger_window,
                "trigger_reason": plan.trigger_reason,
                "mode": mode,
                "status": "pending" if is_real else "open",
                "is_real": is_real,
                "trade_usdt": trade_usdt,
                "leverage": leverage,
                "notional": plan.notional,
                "estimated_tp_net": plan.estimated_tp_net,
                "estimated_sl_net_loss": plan.estimated_sl_net_loss,
                "raw": plan.metrics,
            }
            signal_id = self.storage.create_signal(data)
            if signal_id is None:
                self.log_reject("publish", plan.symbol_id, "ACTIVE_SIGNAL_RACE_BLOCKED", force=True)
                return False
            message_id = self.telegram.send(self._signal_text(signal_id, plan, mode, trade_usdt, leverage))
            self.storage.update_signal(signal_id, message_id=message_id)
            self.engine.mark_signal(plan.symbol_id)
            self._virtual_last_trade_ts[signal_id] = int(time.time() * 1000)
            if is_real:
                try:
                    result = self.toobit.open_position(
                        plan.toobit_symbol,
                        plan.side,
                        trade_usdt,
                        leverage,
                        plan.entry,
                        plan.tp,
                        plan.sl,
                        f"asb_{signal_id}_{int(time.time())}",
                    )
                    self.storage.update_signal(
                        signal_id,
                        order_id=result.get("order_id"),
                        quantity=result.get("quantity"),
                    )
                    threading.Thread(
                        target=self.verify_real_order,
                        args=(signal_id,),
                        daemon=True,
                        name=f"verify-{plan.symbol_id}",
                    ).start()
                except Exception as exc:
                    self.storage.update_signal(
                        signal_id, status="open", is_real=0, mode="SIGNAL"
                    )
                    self.storage.add_health_event("toobit_order", "warning", str(exc), plan.symbol_id)
                    self.telegram.send(
                        f"⚠️ سفارش واقعی #{signal_id} باز نشد و سیگنال به حالت عادی تبدیل شد.\nخطا: {exc}",
                        reply_to=message_id,
                    )
            logger.info(
                "[SIGNAL] id=%d symbol=%s side=%s mode=%s entry=%.10g tp=%.10g sl=%.10g expected=%dm",
                signal_id, plan.symbol_id, plan.side, mode, plan.entry, plan.tp, plan.sl, plan.expected_minutes,
            )
            return True

    def verify_real_order(self, signal_id: int) -> None:
        signal = self.storage.get_signal(signal_id)
        if not signal or not int(signal.get("is_real") or 0):
            return
        try:
            opened = self.toobit.wait_and_protect(
                signal["toobit_symbol"], signal["side"], float(signal["tp"]), float(signal["sl"]),
                str(signal.get("quantity") or "0"), config.ORDER_VERIFY_SECONDS,
            )
            if opened:
                positions = self.toobit.get_open_positions(signal["toobit_symbol"])
                entry_real = float(signal["entry"])
                for position in positions:
                    candidate = float(position.get("avgPrice") or position.get("entryPrice") or 0.0)
                    if candidate > 0:
                        entry_real = candidate
                        break
                self.storage.update_signal(
                    signal_id, status="open", opened_at=int(time.time()), entry_real=entry_real
                )
                self.telegram.send(
                    f"✅ پوزیشن واقعی #{signal_id} در توبیت تأیید و TP/SL کنترل شد.\nEntry واقعی: {entry_real:.10g}",
                    reply_to=signal.get("message_id"),
                )
            else:
                self.storage.add_health_event(
                    "toobit_position", "warning", "بعد از ۷۰ ثانیه پوزیشن پیدا نشد؛ بررسی ادامه دارد", signal["symbol_id"]
                )
                self.telegram.send(
                    f"⚠️ وضعیت پوزیشن واقعی #{signal_id} هنوز قطعی نیست؛ مانیتور ادامه می‌دهد.",
                    reply_to=signal.get("message_id"),
                )
        except Exception as exc:
            self.storage.add_health_event("toobit_position", "warning", str(exc), signal["symbol_id"])
            self.telegram.send(
                f"⚠️ بررسی پوزیشن واقعی #{signal_id} خطا داد؛ مانیتور ادامه می‌دهد.\n{exc}",
                reply_to=signal.get("message_id"),
            )

    def update_active_excursions(self, signal: dict[str, Any], price: float) -> None:
        entry = float(signal.get("entry_real") or signal["entry"])
        if entry <= 0:
            return
        if str(signal["side"]) == "LONG":
            favorable = max(0.0, (price - entry) / entry * 100.0)
            adverse = max(0.0, (entry - price) / entry * 100.0)
        else:
            favorable = max(0.0, (entry - price) / entry * 100.0)
            adverse = max(0.0, (price - entry) / entry * 100.0)
        self.storage.update_signal(
            int(signal["id"]),
            mfe_pct=max(float(signal.get("mfe_pct") or 0.0), favorable),
            mae_pct=max(float(signal.get("mae_pct") or 0.0), adverse),
        )

    def monitor_virtual_tick(self, signal: dict[str, Any], price: float, okx_symbol: str) -> None:
        signal_id = int(signal["id"])
        entry = float(signal.get("entry_real") or signal["entry"])
        tp = float(signal["tp"])
        sl = float(signal["sl"])
        side = str(signal["side"])
        if entry <= 0:
            return

        last_ts = self._virtual_last_trade_ts.get(
            signal_id,
            int(signal.get("opened_at") or signal.get("created_at") or time.time()) * 1000,
        )
        result = ""
        result_detail = ""
        close_price = price
        mfe = float(signal.get("mfe_pct") or 0.0)
        mae = float(signal.get("mae_pct") or 0.0)
        newest_ts = last_ts
        try:
            trades = [
                row for row in self.okx.get_recent_trades(okx_symbol, limit=300)
                if int(row.get("ts") or 0) > last_ts and float(row.get("price") or 0.0) > 0
            ]
            groups: dict[int, list[float]] = {}
            for trade in trades:
                ts = int(trade["ts"])
                newest_ts = max(newest_ts, ts)
                groups.setdefault(ts, []).append(float(trade["price"]))
            for ts in sorted(groups):
                prices = groups[ts]
                high = max(prices)
                low = min(prices)
                if side == "LONG":
                    favorable = max(0.0, (high - entry) / entry * 100.0)
                    adverse = max(0.0, (entry - low) / entry * 100.0)
                    tp_hit = high >= tp
                    sl_hit = low <= sl
                else:
                    favorable = max(0.0, (entry - low) / entry * 100.0)
                    adverse = max(0.0, (high - entry) / entry * 100.0)
                    tp_hit = low <= tp
                    sl_hit = high >= sl
                mfe = max(mfe, favorable)
                mae = max(mae, adverse)
                if tp_hit and sl_hit:
                    result = "AMBIGUOUS"
                    result_detail = "OKX_TRADES_SAME_TIMESTAMP_BOTH_LEVELS"
                    close_price = prices[-1]
                    break
                if tp_hit:
                    result = "TP"
                    result_detail = "OKX_TRADES_TP_FIRST"
                    close_price = tp
                    break
                if sl_hit:
                    result = "SL"
                    result_detail = "OKX_TRADES_SL_FIRST"
                    close_price = sl
                    break
        except Exception as exc:
            self.log_reject(
                "virtual_monitor", str(signal["symbol_id"]), "OKX_RECENT_TRADES_MONITOR_FAILED",
                {"error": str(exc)[:120]},
            )

        self._virtual_last_trade_ts[signal_id] = max(newest_ts, int(time.time() * 1000) - 1)
        if not result:
            if side == "LONG":
                favorable = max(0.0, (price - entry) / entry * 100.0)
                adverse = max(0.0, (entry - price) / entry * 100.0)
                result = "TP" if price >= tp else "SL" if price <= sl else ""
            else:
                favorable = max(0.0, (entry - price) / entry * 100.0)
                adverse = max(0.0, (price - entry) / entry * 100.0)
                result = "TP" if price <= tp else "SL" if price >= sl else ""
            mfe = max(mfe, favorable)
            mae = max(mae, adverse)
            if result:
                result_detail = "OKX_TICKER_FALLBACK"
                close_price = tp if result == "TP" else sl

        if not result:
            self.storage.update_signal(signal_id, mfe_pct=mfe, mae_pct=mae)
            return
        if result in ("TP", "SL"):
            gross, costs, net = self.engine.pnl_for_exit(entry, close_price, side, float(signal["notional"]))
        else:
            gross, costs, net = 0.0, 0.0, 0.0
        self.finish_signal(
            signal,
            result=result,
            result_detail=result_detail,
            entry_price=entry,
            close_price=close_price,
            gross=gross,
            fees=costs,
            net=net,
            mfe=mfe,
            mae=mae,
        )

    def finish_signal(
        self,
        signal: dict[str, Any],
        *,
        result: str,
        entry_price: float,
        close_price: float,
        gross: float,
        fees: float,
        net: float,
        mfe: float,
        mae: float,
        closed_at: int | None = None,
        result_detail: str | None = None,
    ) -> None:
        if not self.storage.close_signal(
            int(signal["id"]), close_price=close_price, gross_pnl=gross, fees=fees, net_pnl=net,
            result=result, result_detail=result_detail, mfe_pct=mfe, mae_pct=mae,
            entry_real=entry_price, closed_at=closed_at,
        ):
            return
        self._virtual_last_trade_ts.pop(int(signal["id"]), None)
        fresh = self.storage.get_signal(int(signal["id"])) or signal
        if result == "TP":
            self.learning.on_tp(fresh)
            learning_note = self.learning.result_note(fresh)
        elif result == "SL":
            self.learning.on_sl(fresh)
            learning_note = self.learning.result_note(fresh)
        else:
            learning_note = "نتیجه مبهم؛ وارد یادگیری نشد"
        stats = self.storage.stats()
        closed_count = stats["tp"] + stats["sl"]
        win_rate = stats["tp"] / closed_count * 100.0 if closed_count else 0.0
        started_at = int(fresh.get("opened_at") or fresh.get("created_at") or time.time())
        duration = max(0, int((int(fresh.get("closed_at") or time.time()) - started_at) / 60))
        icon = "✅" if result == "TP" else "❌" if result == "SL" else "⚠️"
        text = (
            f"{icon} نتیجه نهایی: {result}\n\n"
            f"#{fresh['id']} | {fresh['symbol_id']}\n"
            f"جهت: {fresh['side']}\n"
            f"حالت: {'واقعی توبیت' if int(fresh.get('is_real') or 0) else 'سیگنال عادی'}\n"
            f"Entry: {entry_price:.10g}\n"
            f"TP: {float(fresh['tp']):.10g}\n"
            f"SL: {float(fresh['sl']):.10g}\n"
            f"Close: {close_price:.10g}\n"
            f"سود/زیان خام: {gross:.4f} USDT\n"
            f"کارمزد و هزینه: {fees:.4f} USDT\n"
            f"سود/زیان خالص: {net:.4f} USDT\n"
            f"MFE: {mfe:.3f}% | MAE: {mae:.3f}%\n"
            f"ثبت نتیجه: {result_detail or '-'}\n"
            f"زمان انتظار اعلامی: {fresh['expected_minutes']} دقیقه\n"
            f"مدت واقعی: {duration} دقیقه\n"
            f"یادگیری: {learning_note}\n\n"
            f"آمار: TP={stats['tp']} | SL={stats['sl']} | مبهم={stats.get('ambiguous', 0)} | وین‌ریت={win_rate:.2f}%\n"
            f"سود خالص کل آمار: {stats['net_pnl']:.4f} USDT"
        )
        self.telegram.send(text, reply_to=fresh.get("message_id"))
        logger.info(
            "[RESULT] id=%s symbol=%s result=%s detail=%s side=%s entry=%.10g close=%.10g gross=%.6f fees=%.6f net=%.6f",
            fresh["id"], fresh["symbol_id"], result, result_detail, fresh["side"],
            entry_price, close_price, gross, fees, net,
        )
        self.engine.reset_after_close(str(fresh["symbol_id"]))

    @staticmethod
    def _classify_real_close(signal: dict[str, Any], result: dict[str, Any]) -> tuple[str, str]:
        reason = str(result.get("close_reason") or "").upper().replace("-", "_").replace(" ", "_")
        if any(token in reason for token in ("TAKE_PROFIT", "TAKEPROFIT", "TP_TRIGGER", "TP_ORDER")):
            return "TP", f"TOOBIT_REASON:{reason}"
        if any(token in reason for token in ("STOP_LOSS", "STOPLOSS", "SL_TRIGGER", "SL_ORDER")):
            return "SL", f"TOOBIT_REASON:{reason}"
        close_price = float(result["close_price"])
        tp = float(signal["tp"])
        sl = float(signal["sl"])
        tolerance = max(close_price * 0.00035, abs(tp - sl) * 0.04)
        tp_match = abs(close_price - tp) <= tolerance
        sl_match = abs(close_price - sl) <= tolerance
        if tp_match and not sl_match:
            return "TP", "TOOBIT_PRICE_STRICT_TP_MATCH"
        if sl_match and not tp_match:
            return "SL", "TOOBIT_PRICE_STRICT_SL_MATCH"
        return "AMBIGUOUS", f"TOOBIT_CLOSE_UNCERTAIN:{reason or 'NO_REASON'}"

    def real_monitor_loop(self) -> None:
        while not self.stop_event.is_set():
            for signal in self.storage.get_active_signals():
                if not int(signal.get("is_real") or 0):
                    continue
                try:
                    if self.toobit.is_position_open(signal["toobit_symbol"]):
                        if signal["status"] == "pending":
                            positions = self.toobit.get_open_positions(signal["toobit_symbol"])
                            entry = float(signal["entry"])
                            if positions:
                                entry = float(positions[0].get("avgPrice") or positions[0].get("entryPrice") or entry)
                            self.storage.update_signal(
                                int(signal["id"]), status="open", opened_at=int(time.time()), entry_real=entry
                            )
                        continue
                    result = self.toobit.get_closed_position(
                        signal["toobit_symbol"],
                        signal["side"],
                        int(signal.get("opened_at") or signal["created_at"]) * 1000,
                    )
                    if not result:
                        continue
                    close_price = float(result["close_price"])
                    result_kind, result_detail = self._classify_real_close(signal, result)
                    entry_price = float(result["entry_price"])
                    if signal["side"] == "LONG":
                        favorable = max(0.0, (close_price - entry_price) / entry_price * 100.0)
                        adverse = max(0.0, (entry_price - close_price) / entry_price * 100.0)
                    else:
                        favorable = max(0.0, (entry_price - close_price) / entry_price * 100.0)
                        adverse = max(0.0, (close_price - entry_price) / entry_price * 100.0)
                    fresh_signal = self.storage.get_signal(int(signal["id"])) or signal
                    self.finish_signal(
                        fresh_signal,
                        result=result_kind,
                        entry_price=entry_price,
                        close_price=close_price,
                        gross=float(result["gross_pnl"]),
                        fees=float(result["fees"]),
                        net=float(result["net_pnl"]),
                        mfe=max(float(fresh_signal.get("mfe_pct") or 0.0), favorable),
                        mae=max(float(fresh_signal.get("mae_pct") or 0.0), adverse),
                        closed_at=int(result["close_time"] / 1000),
                        result_detail=result_detail,
                    )
                    self.storage.clear_health("real_monitor", signal["symbol_id"])
                except Exception as exc:
                    logger.warning("[REAL_MONITOR_ERROR] symbol=%s error=%s", signal["symbol_id"], exc)
                    self.storage.add_health_event("real_monitor", "warning", str(exc), signal["symbol_id"])
            self.stop_event.wait(config.REAL_MONITOR_INTERVAL_SECONDS)

    def toobit_status_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                balance = self.toobit.get_balance()
                self.storage.set("toobit_connected", True)
                self.storage.set("toobit_available_usdt", balance["available"])
                self.storage.set("toobit_total_usdt", balance["total"])
                self.storage.set("toobit_margin_usdt", balance["margin"])
                self.storage.set("toobit_last_error", "")
                self.storage.set("toobit_last_update", int(time.time()))
                self.storage.clear_health("toobit")
            except Exception as exc:
                self.storage.set("toobit_connected", False)
                self.storage.set("toobit_last_error", str(exc))
                self.storage.set("toobit_last_update", int(time.time()))
                self.storage.add_health_event("toobit", "warning", str(exc))
            self.stop_event.wait(config.TOOBIT_STATUS_INTERVAL_SECONDS)

    def telegram_loop(self) -> None:
        while not self.stop_event.is_set():
            self.telegram.poll_once()
            self.stop_event.wait(config.TELEGRAM_POLL_SECONDS)

    def profile_update_loop(self) -> None:
        timezone = ZoneInfo(config.TIMEZONE)
        while not self.stop_event.is_set():
            now = datetime.now(timezone)
            day_key = now.strftime("%Y-%m-%d")
            due = (
                now.hour > config.PROFILE_DAILY_UPDATE_HOUR
                or (now.hour == config.PROFILE_DAILY_UPDATE_HOUR and now.minute >= config.PROFILE_DAILY_UPDATE_MINUTE)
            )
            if due and self._last_profile_day != day_key:
                self._last_profile_day = day_key
                try:
                    logger.info("[PROFILE_DAILY_UPDATE_START] day=%s", day_key)
                    with self.profile_lock:
                        self.prepare_profiles(force=True)
                    logger.info("[PROFILE_DAILY_UPDATE_DONE] day=%s", day_key)
                except Exception as exc:
                    logger.exception("daily profile update failed")
                    self.storage.add_health_event("profile_update", "warning", str(exc))
            self.stop_event.wait(30)

    def toobit_snapshot_text(self, refresh: bool = False) -> str:
        if refresh:
            try:
                balance = self.toobit.get_balance()
                self.storage.set("toobit_connected", True)
                self.storage.set("toobit_available_usdt", balance["available"])
                self.storage.set("toobit_total_usdt", balance["total"])
                self.storage.set("toobit_margin_usdt", balance["margin"])
                self.storage.set("toobit_last_update", int(time.time()))
            except Exception as exc:
                return f"❌ خطای توبیت: {exc}"
        return (
            "💳 توبیت\n\n"
            f"اتصال: {'✅' if self.storage.get('toobit_connected', False) else '❌'}\n"
            f"آزاد: {float(self.storage.get('toobit_available_usdt', 0)):.4f} USDT\n"
            f"کل: {float(self.storage.get('toobit_total_usdt', 0)):.4f} USDT\n"
            f"مارجین: {float(self.storage.get('toobit_margin_usdt', 0)):.4f} USDT"
        )

    def positions_text(self) -> str:
        active = self.storage.get_active_signals()
        if not active:
            return "📭 سیگنال یا پوزیشن فعالی وجود ندارد."
        lines = ["📌 سیگنال‌های فعال", ""]
        for row in active:
            lines.append(
                f"#{row['id']} {row['symbol_id']} {row['side']} | {'واقعی' if int(row.get('is_real') or 0) else 'عادی'} | "
                f"Entry={float(row['entry']):.8g} TP={float(row['tp']):.8g} SL={float(row['sl']):.8g}"
            )
        return "\n".join(lines)

    def market_text(self) -> str:
        with self.symbol_lock:
            valid = list(self.symbols)
            ready = list(self.ready_symbols)
        skipped = [base for base in config.SYMBOL_BASES if base not in self.symbols]
        return (
            "🌐 بازار و پروفایل\n\n"
            f"درخواست‌شده: {len(config.SYMBOL_BASES)}\n"
            f"معتبر در OKX و توبیت: {len(valid)}\n"
            f"آماده اسکن: {len(ready)}\n"
            f"واچ فعال: {len(self.engine.watches)}\n"
            f"رد نماد: {', '.join(skipped) if skipped else 'ندارد'}"
        )

    def monitor_text(self) -> str:
        active = self.storage.get_active_signals()
        return (
            "👁 مانیتور\n\n"
            f"واچ پویا: {len(self.engine.watches)}\n"
            f"سیگنال فعال: {len(active)}\n"
            f"واقعی: {sum(1 for row in active if int(row.get('is_real') or 0))}\n"
            f"عادی: {sum(1 for row in active if not int(row.get('is_real') or 0))}"
        )

    def learning_text(self) -> str:
        summary = self.storage.learning_summary()
        rows = self.storage.recent_learning_patterns(config.LEARNING_PATTERN_RECENT_LIMIT)
        lines = [
            "🧠 حافظه و یادگیری",
            "",
            f"وضعیت: {'فعال' if config.LEARNING_ENABLED else 'خاموش'}",
            f"الگوها: {summary['patterns']} | آزمایشی: {summary['trial']} | تأییدشده: {summary['accepted']} | قرنطینه: {summary.get('quarantined', 0)}",
            f"نتایج واردشده: TP={summary['tp']} | SL={summary['sl']}",
            f"بررسی مخفی پس‌ازاستاپ: {summary['reviews']}",
        ]
        if rows:
            lines.append("")
            lines.append("آخرین تغییرها:")
            for row in rows:
                factors = row.get("factors") or {}
                changed = ", ".join(
                    f"{key.replace('_factor', '')}={float(value):.2f}"
                    for key, value in factors.items() if abs(float(value) - 1.0) >= 0.005
                ) or "پایه"
                lines.append(
                    f"• {row['symbol_id']} {row['side']} {row['trigger_window']}s/"
                    f"{row['support_tool']}/{row['horizon']}m | {row['status']} v{row['version']} | "
                    f"{row.get('active_reason') or '-'} | {changed}"
                )
        return "\n".join(lines)

    def profile_bootstrap_loop(self) -> None:
        try:
            logger.info("[PROFILE_BOOTSTRAP_THREAD_START]")
            with self.profile_lock:
                self.prepare_profiles(force=False)
        except Exception as exc:
            logger.exception("profile bootstrap failed")
            self.storage.add_health_event("profile_bootstrap", "critical", str(exc))
        finally:
            logger.info("[PROFILE_BOOTSTRAP_THREAD_DONE] ready=%d", len(self.ready_symbols))

    def run(self) -> None:
        # Telegram and panels must answer immediately. Only the signal scanner waits
        # for ready_symbols, which remain empty until profile bootstrap completes.
        self._last_profile_day = datetime.now(ZoneInfo(config.TIMEZONE)).strftime("%Y-%m-%d")
        threads = [
            threading.Thread(target=self.telegram_loop, daemon=True, name="telegram"),
            threading.Thread(target=self.toobit_status_loop, daemon=True, name="toobit-status"),
            threading.Thread(target=self.real_monitor_loop, daemon=True, name="real-monitor"),
            threading.Thread(target=self.scan_loop, daemon=True, name="okx-scan"),
            threading.Thread(target=self.profile_bootstrap_loop, daemon=True, name="profile-bootstrap"),
            threading.Thread(target=self.profile_update_loop, daemon=True, name="profile-update"),
        ]
        for thread in threads:
            thread.start()
        logger.info(
            "[BOT_STARTED] telegram_immediate=true requested=%d profile_status=BUILDING",
            len(config.SYMBOL_BASES),
        )
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            self.stop_event.set()
            for thread in threads:
                thread.join(timeout=4)


if __name__ == "__main__":
    TradingBotApp().run()
