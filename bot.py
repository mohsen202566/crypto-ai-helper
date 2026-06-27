"""Main bot loop for Crypto AI Helper bot.

Locked responsibility:
- Owns the lightweight 5-second runtime loop.
- Pulls market prices from OKX by default.
- Sends market snapshots to the existing strategy layer for the final decision.
- Registers SIGNAL_ONLY when real trading is off, slots are full, or the strategy/TP-SL
  result says the trade should be monitored only.
- Sends REAL requests only through the project trade/exchange layer when available.
- Reads active monitoring records from StateStore every cycle and closes TP/SL hits.
- Reads Telegram command names from config.py and applies them directly to StateStore.
- Reads Telegram command names from config.py and handles them directly.

Design lock:
- Small, simple, strong.
- No TP/SL calculation here.
- No indicator/AI/probability logic here.
- No command_router.py or position_monitor.py dependency.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import threading
import time
import traceback
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Protocol

try:
    import config as bot_config
    from config import (
        DEFAULT_AUTO_SIGNAL_ENABLED,
        DEFAULT_REAL_TRADE_ENABLED,
        WATCHLIST,
        TIMEFRAME,
        ENTRY_TIMEFRAME,
        TREND_FILTER_TIMEFRAME,
        get_coin,
    )
except Exception:  # pragma: no cover - keeps isolated compile checks possible.
    bot_config = None  # type: ignore
    DEFAULT_AUTO_SIGNAL_ENABLED = True
    DEFAULT_REAL_TRADE_ENABLED = False
    WATCHLIST = ("DOGEUSDT", "XRPUSDT", "SOLUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "INJUSDT")
    TIMEFRAME = "30m"
    ENTRY_TIMEFRAME = "15m"
    TREND_FILTER_TIMEFRAME = "1h"

    def get_coin(symbol: str) -> Any:  # type: ignore
        return symbol

try:
    from state_store import StateStore
except Exception:  # pragma: no cover
    StateStore = Any  # type: ignore

# NOTE:
# Monitoring is handled below using StateStore records and current prices.
# No external position_monitor.py file is imported or required.

try:
    import telegram_ui
except Exception:  # pragma: no cover
    telegram_ui = None  # type: ignore

try:
    import strategy_manager
except Exception:  # pragma: no cover
    strategy_manager = None  # type: ignore

try:
    import real_trade_manager
except Exception:  # pragma: no cover
    real_trade_manager = None  # type: ignore

try:
    import toobit_client as tobit_client
except Exception:  # pragma: no cover - status panel must still work without Toobit deps.
    tobit_client = None  # type: ignore

try:
    from telegram import Update
    from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters
except Exception:  # pragma: no cover - py_compile must work even before requirements install.
    Update = Any  # type: ignore
    Application = Any  # type: ignore
    ApplicationBuilder = None  # type: ignore
    CommandHandler = None  # type: ignore
    ContextTypes = Any  # type: ignore
    MessageHandler = None  # type: ignore
    filters = None  # type: ignore


BOT_VERSION = "level4_fast_15m30m_loop_v1"
DEFAULT_SCAN_INTERVAL_SECONDS = 5.0
DEFAULT_HTTP_TIMEOUT_SECONDS = 10.0
LOGGER = logging.getLogger("crypto_bot")



class MarketDataProvider(Protocol):
    def get_prices(self, symbols: Iterable[str]) -> Mapping[str, float]:
        """Return latest prices keyed by symbol."""


Notifier = Callable[[Any], None]
DecisionProvider = Callable[[str, Mapping[str, Any]], Any]


@dataclass(frozen=True)
class LoopConfig:
    scan_interval_seconds: float = DEFAULT_SCAN_INTERVAL_SECONDS
    watchlist: tuple[str, ...] = tuple(str(item).upper() for item in WATCHLIST)
    stop_on_error: bool = False

    @classmethod
    def from_env(cls) -> "LoopConfig":
        interval = _safe_float(os.getenv("BOT_SCAN_INTERVAL_SECONDS"), DEFAULT_SCAN_INTERVAL_SECONDS)
        return cls(scan_interval_seconds=max(1.0, interval))


@dataclass
class CycleReport:
    cycle_started_at: float
    scanned_symbols: list[str] = field(default_factory=list)
    decisions: int = 0
    signal_registered: int = 0
    real_requested: int = 0
    monitor_results: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class MonitorResult:
    signal_id: str
    symbol: str
    direction: str
    mode: str
    result: str
    entry: float
    exit_price: float
    tp: float
    sl: float
    pnl_usdt: float
    close_reason: str


class StateStoreMonitor:
    """Monitor active StateStore records without any external monitor file.

    SIGNAL records are closed by latest OKX price hitting local TP/SL.
    TOOBIT records are synced from exchange callbacks/toobit_client state and only
    marked closed when a TP/SL hit can be identified safely from the current price.
    """

    def __init__(
        self,
        store: StateStore,
        *,
        exchange_checker: Callable[[Any], Mapping[str, Any] | bool | None] | None = None,
        closed_pnl_reader: Callable[[Any], Mapping[str, Any] | float | int | None] | None = None,
        result_callback: Callable[[Any], None] | None = None,
    ) -> None:
        self.store = store
        self.exchange_checker = exchange_checker
        self.closed_pnl_reader = closed_pnl_reader
        self.result_callback = result_callback
        self.last_error = ""

    def check_once(self, prices: Mapping[str, Any] | None = None) -> list[MonitorResult]:
        prices = prices or {}
        snapshot = self.store.snapshot()
        active = dict(getattr(snapshot, "active_signals", {}) or {})
        results: list[MonitorResult] = []
        for signal_id, record in active.items():
            try:
                status = str(getattr(record, "status", "") or "").upper()
                if status not in {"PENDING_OPEN", "MONITORING"}:
                    continue
                mode = str(getattr(record, "mode", "") or "").upper()
                if mode == "TOOBIT":
                    result = self._check_toobit(signal_id, record, prices)
                else:
                    result = self._check_signal(signal_id, record, prices)
                if result is not None:
                    results.append(result)
                    self._emit(result)
            except Exception as exc:
                self.last_error = f"{signal_id}:{exc}"
                LOGGER.exception("state_monitor_record_error signal_id=%s", signal_id)
        return results

    def _check_signal(self, signal_id: str, record: Any, prices: Mapping[str, Any]) -> MonitorResult | None:
        hit = self._local_price_hit(record, prices)
        if hit is None:
            return None
        result, exit_price = hit
        pnl = self._estimate_signal_pnl(record, exit_price=exit_price, result=result)
        closed = self.store.mark_result(
            signal_id,
            result=result,  # type: ignore[arg-type]
            exit_price=exit_price,
            pnl_usdt=pnl,
            close_reason=f"SIGNAL_{result}_HIT",
        )
        return self._build_result(closed, result=result, exit_price=exit_price, pnl_usdt=pnl, reason=f"SIGNAL_{result}_HIT")

    def _check_toobit(self, signal_id: str, record: Any, prices: Mapping[str, Any]) -> MonitorResult | None:
        status = str(getattr(record, "status", "") or "").upper()
        exists_payload = self._safe_exchange_check(record)
        exists = self._payload_bool(exists_payload, "exists")

        if status == "PENDING_OPEN":
            if exists is True:
                self.store.confirm_real_open(signal_id)
            elif exists is False:
                # Keep this defensive: only cancel pending records when exchange clearly says no position.
                self.store.cancel_unconfirmed_real(signal_id, "پوزیشن در توبیت تایید نشد")
            return None

        if status != "MONITORING":
            return None

        # First use an explicit closed-PNL reader if real_trade_manager provides one.
        pnl_payload = self._safe_closed_pnl(record)
        confirmed = self._payload_bool(pnl_payload, "confirmed")
        if confirmed is True:
            result = self._payload_result(pnl_payload) or self._infer_result_from_price(record, prices) or "TP"
            exit_price = self._payload_float(pnl_payload, "exit_price", "price", default=self._fallback_exit_price(record, prices, result))
            pnl = self._payload_float(pnl_payload, "pnl_usdt", "pnl", default=0.0)
            reason = str(self._payload_get(pnl_payload, "reason", "close_reason") or f"TOOBIT_{result}_CONFIRMED")
            closed = self.store.mark_result(signal_id, result=result, exit_price=exit_price, pnl_usdt=pnl, close_reason=reason)  # type: ignore[arg-type]
            return self._build_result(closed, result=result, exit_price=exit_price, pnl_usdt=pnl, reason=reason)

        # If Toobit position is gone and the current price clearly crossed local TP/SL,
        # close the local record so the panel/stat state does not stay stuck.
        if exists is False:
            hit = self._local_price_hit(record, prices)
            if hit is None:
                return None
            result, exit_price = hit
            closed = self.store.mark_result(
                signal_id,
                result=result,  # type: ignore[arg-type]
                exit_price=exit_price,
                pnl_usdt=0.0,
                close_reason=f"TOOBIT_{result}_INFERRED_FROM_PRICE",
            )
            return self._build_result(closed, result=result, exit_price=exit_price, pnl_usdt=0.0, reason=f"TOOBIT_{result}_INFERRED_FROM_PRICE")
        return None

    def _local_price_hit(self, record: Any, prices: Mapping[str, Any]) -> tuple[str, float] | None:
        symbol = str(getattr(record, "symbol", "") or "").upper().strip()
        price = _safe_price(prices.get(symbol) or prices.get(symbol.lower()))
        if price <= 0:
            return None
        direction = str(getattr(record, "direction", "") or "").upper()
        tp = _safe_price(getattr(record, "tp", 0.0))
        sl = _safe_price(getattr(record, "sl", 0.0))
        if tp <= 0 or sl <= 0:
            return None
        if direction == "LONG":
            if price >= tp:
                return "TP", price
            if price <= sl:
                return "SL", price
        elif direction == "SHORT":
            if price <= tp:
                return "TP", price
            if price >= sl:
                return "SL", price
        return None

    def _infer_result_from_price(self, record: Any, prices: Mapping[str, Any]) -> str | None:
        hit = self._local_price_hit(record, prices)
        return None if hit is None else hit[0]

    def _fallback_exit_price(self, record: Any, prices: Mapping[str, Any], result: str) -> float:
        hit = self._local_price_hit(record, prices)
        if hit is not None:
            return hit[1]
        return _safe_price(getattr(record, "tp" if result == "TP" else "sl", 0.0)) or _safe_price(getattr(record, "entry", 0.0))

    def _estimate_signal_pnl(self, record: Any, *, exit_price: float, result: str) -> float:
        entry = _safe_price(getattr(record, "entry", 0.0))
        if entry <= 0:
            return 0.0
        move_pct = abs(float(exit_price) - entry) / entry
        return round(move_pct if result == "TP" else -move_pct, 6)

    def _safe_exchange_check(self, record: Any) -> Mapping[str, Any] | bool | None:
        if callable(self.exchange_checker):
            try:
                return self.exchange_checker(record)
            except Exception as exc:
                self.last_error = f"exchange_checker:{exc}"
        return _toobit_position_exists(record)

    def _safe_closed_pnl(self, record: Any) -> Mapping[str, Any] | float | int | None:
        if not callable(self.closed_pnl_reader):
            return None
        try:
            return self.closed_pnl_reader(record)
        except Exception as exc:
            self.last_error = f"closed_pnl_reader:{exc}"
            return None

    def _build_result(self, record: Any, *, result: str, exit_price: float, pnl_usdt: float, reason: str) -> MonitorResult:
        return MonitorResult(
            signal_id=str(getattr(record, "signal_id", "")),
            symbol=str(getattr(record, "symbol", "") or "").upper(),
            direction=str(getattr(record, "direction", "") or "").upper(),
            mode=str(getattr(record, "mode", "") or ""),
            result=result,
            entry=_safe_price(getattr(record, "entry", 0.0)),
            exit_price=float(exit_price),
            tp=_safe_price(getattr(record, "tp", 0.0)),
            sl=_safe_price(getattr(record, "sl", 0.0)),
            pnl_usdt=float(pnl_usdt),
            close_reason=reason,
        )

    def _emit(self, result: MonitorResult) -> None:
        if callable(self.result_callback):
            self.result_callback(result)

    @staticmethod
    def _payload_bool(payload: Any, key: str) -> bool | None:
        if isinstance(payload, bool):
            return payload
        if isinstance(payload, Mapping) and key in payload:
            value = payload.get(key)
            if isinstance(value, bool):
                return value
            if value is None:
                return None
            text = str(value).lower().strip()
            if text in {"1", "true", "yes", "open", "exists"}:
                return True
            if text in {"0", "false", "no", "closed", "missing"}:
                return False
        return None

    @staticmethod
    def _payload_get(payload: Any, *keys: str) -> Any:
        if isinstance(payload, Mapping):
            for key in keys:
                if key in payload and payload.get(key) is not None:
                    return payload.get(key)
        return None

    def _payload_float(self, payload: Any, *keys: str, default: float = 0.0) -> float:
        value = self._payload_get(payload, *keys)
        return _safe_float(value, default)

    def _payload_result(self, payload: Any) -> str | None:
        value = str(self._payload_get(payload, "result", "kind", "side") or "").upper()
        return value if value in {"TP", "SL"} else None


def build_result_panel_payload(result: Any, fa_name: str = "") -> dict[str, Any]:
    title = "🎯 TP خورد" if str(getattr(result, "result", "")).upper() == "TP" else "🛑 SL خورد"
    symbol = str(getattr(result, "symbol", "") or "")
    direction = str(getattr(result, "direction", "") or "")
    pnl = _safe_float(getattr(result, "pnl_usdt", 0.0), 0.0)
    text = (
        f"{title}\n"
        f"کوین: {fa_name or symbol} ({symbol})\n"
        f"جهت: {direction}\n"
        f"خروج: {_safe_float(getattr(result, 'exit_price', 0.0), 0.0):.8g}\n"
        f"PNL: {pnl:.4f} USDT\n"
        f"دلیل: {getattr(result, 'close_reason', '')}"
    )
    return {"type": "position_result", "text": text, "result": result}


class OKXMarketDataProvider:
    """Small OKX public ticker adapter.

    It intentionally returns only latest prices. Strategy/indicator work remains in
    strategy_manager.py and downstream files.
    """

    def __init__(self, base_url: str | None = None, timeout: float = DEFAULT_HTTP_TIMEOUT_SECONDS) -> None:
        self.base_url = (base_url or os.getenv("OKX_BASE_URL") or "https://www.okx.com").rstrip("/")
        self.timeout = timeout

    def get_prices(self, symbols: Iterable[str]) -> Mapping[str, float]:
        prices: dict[str, float] = {}
        for symbol in symbols:
            clean_symbol = str(symbol).upper().strip()
            if not clean_symbol:
                continue
            for inst_id in _okx_inst_id_candidates(clean_symbol):
                price = self._fetch_last_price(inst_id)
                if price > 0:
                    prices[clean_symbol] = price
                    break
        return prices

    def _fetch_last_price(self, inst_id: str) -> float:
        query = urllib.parse.urlencode({"instId": inst_id})
        url = f"{self.base_url}/api/v5/market/ticker?{query}"
        req = urllib.request.Request(url, headers={"User-Agent": f"CryptoAIHelper/{BOT_VERSION}"})
        with urllib.request.urlopen(req, timeout=self.timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        data = payload.get("data") or []
        if not data:
            return 0.0
        return _safe_price(data[0].get("last"))

    def get_candles(self, symbol: str, *, bar: str = TIMEFRAME, limit: int = 100) -> list[list[str]]:
        """Return OKX candles for the 15m/30m strategy profile.

        The strategy layer fails closed without enough 30m main candles and
        15m entry-confirmation candles.  bot.py only fetches and passes data;
        it does not calculate indicators or make TP/SL decisions.
        """
        clean_symbol = str(symbol).upper().strip()
        if not clean_symbol:
            return []
        for inst_id in _okx_inst_id_candidates(clean_symbol):
            query = urllib.parse.urlencode({"instId": inst_id, "bar": _okx_bar(bar), "limit": int(limit)})
            url = f"{self.base_url}/api/v5/market/candles?{query}"
            req = urllib.request.Request(url, headers={"User-Agent": f"CryptoAIHelper/{BOT_VERSION}"})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as response:
                    payload = json.loads(response.read().decode("utf-8"))
            except Exception as exc:
                LOGGER.warning("okx_candles_error symbol=%s inst_id=%s error=%s", clean_symbol, inst_id, exc)
                continue
            data = payload.get("data") or [] if isinstance(payload, Mapping) else []
            if data:
                return data
        return []


class EmptyMarketDataProvider:
    """Safe placeholder used only if OKX is explicitly disabled or unavailable."""

    def get_prices(self, symbols: Iterable[str]) -> Mapping[str, float]:
        return {}

    def get_candles(self, symbol: str, *, bar: str = TIMEFRAME, limit: int = 100) -> list[list[str]]:
        return []


class BotRuntime:
    """Single-file runtime coordinator used by bot.py."""

    def __init__(
        self,
        *,
        store: StateStore,
        market_data: MarketDataProvider,
        decision_provider: DecisionProvider | None = None,
        notifier: Notifier | None = None,
        monitor: StateStoreMonitor | None = None,
        config: LoopConfig | None = None,
    ) -> None:
        self.store = store
        self.market_data = market_data
        self.decision_provider = decision_provider or _default_decision_provider
        self.notifier = notifier or _default_notifier
        self.monitor = monitor or StateStoreMonitor(
            store,
            exchange_checker=_default_exchange_checker,
            closed_pnl_reader=_default_closed_pnl_reader,
            result_callback=self._on_monitor_result,
        )
        self.config = config or LoopConfig.from_env()
        self._running = False
        self.last_report: CycleReport | None = None
        self.last_error: str = ""

    def run_forever(self) -> None:
        self._running = True
        _install_stop_handlers(lambda: self.stop())
        while self._running:
            started = time.time()
            report = self.run_once()
            self.last_report = report
            if report.errors:
                self.last_error = "; ".join(report.errors[-5:])
            elapsed = time.time() - started
            sleep_for = max(0.0, self.config.scan_interval_seconds - elapsed)
            if sleep_for:
                time.sleep(sleep_for)

    def stop(self) -> None:
        self._running = False

    def run_once(self) -> CycleReport:
        report = CycleReport(cycle_started_at=time.time())
        symbols = self._active_watchlist()
        settings = self.store.snapshot().settings
        LOGGER.info(
            "cycle_start symbols=%d auto_signal=%s real_trade=%s",
            len(symbols),
            getattr(settings, "auto_signal_enabled", DEFAULT_AUTO_SIGNAL_ENABLED),
            getattr(settings, "real_trade_enabled", DEFAULT_REAL_TRADE_ENABLED),
        )

        try:
            prices = dict(self.market_data.get_prices(symbols))
        except Exception as exc:
            report.errors.append(f"market_data_error:{exc}")
            LOGGER.exception("market_data_error")
            if self.config.stop_on_error:
                raise
            prices = {}

        monitor_results = self._monitor_positions(prices)
        report.monitor_results = len(monitor_results)

        shared_market = self._shared_strategy_market()
        for symbol in symbols:
            report.scanned_symbols.append(symbol)
            price = _safe_price(prices.get(symbol))
            if price <= 0:
                LOGGER.info("scan_skip symbol=%s reason=price_missing", symbol)
                continue
            try:
                handled = self._handle_symbol(symbol, price, prices, shared_market)
                if handled == "REAL":
                    report.real_requested += 1
                elif handled == "SIGNAL":
                    report.signal_registered += 1
                if handled:
                    report.decisions += 1
            except Exception as exc:
                report.errors.append(f"{symbol}:{exc}")
                LOGGER.exception("symbol_error symbol=%s", symbol)
                if self.config.stop_on_error:
                    raise
        LOGGER.info(
            "cycle_done scanned=%d decisions=%d signals=%d real=%d monitor=%d errors=%d",
            len(report.scanned_symbols),
            report.decisions,
            report.signal_registered,
            report.real_requested,
            report.monitor_results,
            len(report.errors),
        )
        return report

    def _active_watchlist(self) -> list[str]:
        symbols: list[str] = []
        for raw in self.config.watchlist:
            symbol = str(raw).upper().strip()
            if not symbol:
                continue
            try:
                get_coin(symbol)
            except Exception:
                continue
            symbols.append(symbol)
        return symbols

    def _monitor_positions(self, prices: Mapping[str, float]) -> list[Any]:
        try:
            return list(self.monitor.check_once(prices))
        except Exception as exc:
            self.last_error = f"state_monitor_error:{exc}"
            LOGGER.exception("state_monitor_error")
            return []

    def _shared_strategy_market(self) -> dict[str, Any]:
        candles_getter = getattr(self.market_data, "get_candles", None)
        if not callable(candles_getter):
            return {}
        try:
            btc_candles = candles_getter("BTCUSDT", bar=TREND_FILTER_TIMEFRAME, limit=80)
        except Exception as exc:
            LOGGER.warning("btc_candles_error error=%s", exc)
            btc_candles = []
        return {"btc_candles": btc_candles}

    def _symbol_strategy_market(
        self,
        *,
        symbol: str,
        price: float,
        prices: Mapping[str, float],
        shared_market: Mapping[str, Any],
    ) -> dict[str, Any]:
        settings = self.store.snapshot().settings
        market: dict[str, Any] = {
            "price": price,
            "prices": dict(prices),
            "symbol": symbol,
            "real_trade_enabled": bool(getattr(settings, "real_trade_enabled", DEFAULT_REAL_TRADE_ENABLED)),
            "trade_dollar_usdt": float(getattr(settings, "trade_dollar_usdt", 0.0) or 0.0),
            "trade_margin_usdt": float(getattr(settings, "trade_dollar_usdt", 0.0) or 0.0),
            "leverage": int(getattr(settings, "leverage", 1) or 1),
            "min_net_profit_usdt": float(getattr(settings, "min_net_profit_usdt", 0.0) or 0.0),
            **dict(shared_market),
        }
        candles_getter = getattr(self.market_data, "get_candles", None)
        if callable(candles_getter):
            try:
                candles_30m = candles_getter(symbol, bar=TIMEFRAME, limit=100)
            except Exception as exc:
                LOGGER.warning("symbol_main_candles_error symbol=%s bar=%s error=%s", symbol, TIMEFRAME, exc)
                candles_30m = []
            try:
                candles_15m = candles_getter(symbol, bar=ENTRY_TIMEFRAME, limit=100)
            except Exception as exc:
                LOGGER.warning("symbol_entry_candles_error symbol=%s bar=%s error=%s", symbol, ENTRY_TIMEFRAME, exc)
                candles_15m = []

            market["candles_30m"] = candles_30m
            market["candles_15m"] = candles_15m
            market["entry_candles"] = candles_15m
            market["candles"] = candles_30m  # compatibility alias for strategy_manager
        return market

    def _handle_symbol(self, symbol: str, price: float, prices: Mapping[str, float], shared_market: Mapping[str, Any] | None = None) -> str:
        settings = self.store.snapshot().settings
        if not getattr(settings, "auto_signal_enabled", DEFAULT_AUTO_SIGNAL_ENABLED):
            LOGGER.info("scan_skip symbol=%s reason=auto_signal_off", symbol)
            return ""
        if self.store.has_active_symbol(symbol):
            LOGGER.info("scan_skip symbol=%s reason=active_signal_exists", symbol)
            return ""

        market = self._symbol_strategy_market(symbol=symbol, price=price, prices=prices, shared_market=shared_market or {})
        decision = self.decision_provider(symbol, market)
        if not _decision_is_actionable(decision):
            LOGGER.info(
                "no_trade symbol=%s action=%s reason=%s candles=%d btc_candles=%d",
                symbol,
                getattr(decision, "action", getattr(decision, "decision", None)),
                getattr(decision, "reason", ""),
                len(market.get("candles") or []),
                len(market.get("btc_candles") or []),
            )
            return ""

        requested_real = _decision_requests_real(decision)
        can_open_real = bool(requested_real and self.store.can_open_real(symbol))

        if can_open_real:
            LOGGER.info("real_request symbol=%s direction=%s confidence=%s", symbol, _direction(decision), getattr(decision, "confidence", ""))
            result = _open_real(decision)
            if _open_result_ok(result):
                self._notify_signal(decision, mode="TOOBIT", raw_result=result)
                LOGGER.info("real_opened symbol=%s", symbol)
                return "REAL"
            LOGGER.warning("real_failed symbol=%s result=%s", symbol, result)
            return ""

        record = self._register_signal_only(decision, symbol=symbol, price=price)
        self._notify_signal(decision, mode="SIGNAL", raw_result=record)
        LOGGER.info("signal_registered symbol=%s direction=%s confidence=%s", symbol, _direction(decision), getattr(decision, "confidence", ""))
        return "SIGNAL"

    def _register_signal_only(self, decision: Any, *, symbol: str, price: float) -> Any:
        direction = _direction(decision)
        tp, sl = _tp_sl(decision)
        signal_id = _signal_id(decision, symbol)
        return self.store.register_signal(
            signal_id=signal_id,
            symbol=symbol,
            direction=direction,
            requested_mode="SIGNAL",
            entry=_entry(decision, fallback=price),
            tp=tp,
            sl=sl,
        )

    def _notify_signal(self, decision: Any, *, mode: str, raw_result: Any) -> None:
        payload = _build_signal_payload(decision, mode=mode, raw_result=raw_result)
        self.notifier(payload)

    def _on_monitor_result(self, result: Any) -> None:
        try:
            payload = build_result_panel_payload(result, fa_name=_fa_name(str(result.symbol)))
        except Exception:
            payload = result
        self.notifier(_build_result_payload(payload))


def _default_decision_provider(symbol: str, market: Mapping[str, Any]) -> Any:
    if strategy_manager is None:
        return None
    for name in ("decide", "analyze_symbol", "build_decision", "evaluate_symbol", "get_decision"):
        fn = getattr(strategy_manager, name, None)
        if callable(fn):
            try:
                return fn(symbol=symbol, market=market)
            except TypeError:
                try:
                    return fn(symbol, market)
                except TypeError:
                    return fn(symbol)
    return None


def _default_exchange_checker(record: Any) -> Mapping[str, Any]:
    if real_trade_manager is not None:
        fn = getattr(real_trade_manager, "exchange_position_checker", None)
        if callable(fn):
            return fn(record)
    return _toobit_position_exists(record)


def _toobit_position_exists(record: Any) -> Mapping[str, Any]:
    symbol = str(getattr(record, "symbol", "") or "").upper().strip()
    if not symbol:
        return {"exists": None, "error": "symbol_missing"}
    if tobit_client is None:
        return {"exists": None, "error": "toobit_client_missing"}
    try:
        client = tobit_client.get_client()
        positions = client.get_open_positions(symbol)
        return {"exists": bool(positions), "open_positions": len(positions)}
    except Exception as exc:
        return {"exists": None, "error": str(exc)}


def _default_closed_pnl_reader(record: Any) -> Mapping[str, Any]:
    if real_trade_manager is None:
        return {"confirmed": False, "pnl_usdt": None, "error": "real_trade_manager_missing"}
    fn = getattr(real_trade_manager, "closed_pnl_reader", None)
    if not callable(fn):
        return {"confirmed": False, "pnl_usdt": None, "error": "closed_pnl_reader_missing"}
    return fn(record)


def _open_real(decision: Any) -> Any:
    if real_trade_manager is None:
        return {"status": "FAILED", "error": "real_trade_manager_missing"}
    fn = getattr(real_trade_manager, "open_real_trade", None)
    if not callable(fn):
        return {"status": "FAILED", "error": "open_real_trade_missing"}
    return fn(decision)


def _open_result_ok(result: Any) -> bool:
    status = str(getattr(result, "status", "") or (result.get("status") if isinstance(result, Mapping) else "")).upper()
    return status in {"OK", "RECOVERED", "SUCCESS"}


def _decision_is_actionable(decision: Any) -> bool:
    if decision is None:
        return False
    action = str(getattr(decision, "action", "") or getattr(decision, "decision", "") or "").upper()
    if action in {"NO_TRADE", "HOLD", "REJECT", "NONE"}:
        return False
    direction = _direction(decision, allow_empty=True)
    if direction not in {"LONG", "SHORT"}:
        return False
    try:
        tp, sl = _tp_sl(decision)
        return tp > 0 and sl > 0
    except Exception:
        return False


def _decision_requests_real(decision: Any) -> bool:
    mode = str(getattr(decision, "mode", "") or "").upper()
    if mode in {"TOOBIT", "REAL"}:
        return True
    return bool(getattr(decision, "real", False) or getattr(decision, "is_real", False))


def _direction(decision: Any, *, allow_empty: bool = False) -> str:
    value = str(getattr(decision, "direction", "") or "").upper()
    if value in {"LONG", "BUY"}:
        return "LONG"
    if value in {"SHORT", "SELL"}:
        return "SHORT"
    if allow_empty:
        return ""
    raise ValueError("decision direction must be LONG or SHORT")


def _entry(decision: Any, *, fallback: float) -> float:
    value = _safe_price(getattr(decision, "entry", 0.0))
    return value if value > 0 else fallback


def _tp_sl(decision: Any) -> tuple[float, float]:
    plan = getattr(decision, "tp_sl", None)
    if plan is not None:
        tp = _safe_price(getattr(plan, "tp1", 0.0) or getattr(plan, "tp", 0.0))
        sl = _safe_price(getattr(plan, "sl", 0.0))
        if tp > 0 and sl > 0:
            return tp, sl
    tp = _safe_price(getattr(decision, "tp", 0.0) or getattr(decision, "tp1", 0.0))
    sl = _safe_price(getattr(decision, "sl", 0.0) or getattr(decision, "stop_loss", 0.0))
    if tp <= 0 or sl <= 0:
        raise ValueError("decision TP/SL missing")
    return tp, sl


def _signal_id(decision: Any, symbol: str) -> str:
    raw = str(getattr(decision, "signal_id", "") or "").strip()
    if raw:
        return raw
    return f"{symbol}_{int(time.time() * 1000)}"


def _safe_float(value: Any, fallback: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _safe_price(value: Any) -> float:
    number = _safe_float(value, 0.0)
    return number if number > 0 else 0.0


def _fa_name(symbol: str) -> str:
    try:
        coin = get_coin(symbol)
        return str(getattr(coin, "fa_name", "") or getattr(coin, "name_fa", "") or symbol)
    except Exception:
        return symbol


def _build_signal_payload(decision: Any, *, mode: str, raw_result: Any) -> Any:
    if telegram_ui is not None:
        for name in ("build_signal_payload", "format_signal", "render_signal"):
            fn = getattr(telegram_ui, name, None)
            if callable(fn):
                try:
                    return fn(decision=decision, mode=mode, result=raw_result)
                except TypeError:
                    try:
                        return fn(decision, mode, raw_result)
                    except TypeError:
                        pass
    return {"type": "signal", "mode": mode, "decision": decision, "result": raw_result}


def _build_result_payload(result_payload: Any) -> Any:
    if telegram_ui is not None:
        for name in ("build_result_payload", "format_result", "render_result"):
            fn = getattr(telegram_ui, name, None)
            if callable(fn):
                try:
                    return fn(result_payload)
                except TypeError:
                    pass
    return {"type": "result", "result": result_payload}


def _payload_to_text(payload: Any) -> str:
    """Return the clean Telegram text from strings, dict payloads, or command result objects."""
    if payload is None:
        return ""
    if isinstance(payload, str):
        return payload

    for attr in ("text", "message", "body", "caption"):
        value = getattr(payload, attr, None)
        if value:
            return str(value)

    if isinstance(payload, Mapping):
        for key in ("text", "message", "body", "caption"):
            value = payload.get(key)
            if value:
                return str(value)
        try:
            return json.dumps(payload, ensure_ascii=False, default=str, indent=2)
        except Exception:
            return str(payload)

    return str(payload)


class TelegramNotifier:
    """Thread-safe Telegram sender used by the runtime loop."""

    def __init__(self, application: Application, default_chat_id: str | int | None = None) -> None:
        self.application = application
        self.default_chat_id = default_chat_id
        self.last_chat_id: str | int | None = default_chat_id
        self.loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop

    def set_chat_id(self, chat_id: str | int) -> None:
        self.last_chat_id = chat_id

    def __call__(self, payload: Any) -> None:
        chat_id = self.last_chat_id or self.default_chat_id
        if not chat_id:
            return
        text = _payload_to_text(payload).strip()
        if not text:
            return
        text = text[:3900]
        if self.loop and self.loop.is_running():
            asyncio.run_coroutine_threadsafe(self.application.bot.send_message(chat_id=chat_id, text=text), self.loop)


def _default_notifier(payload: Any) -> None:
    return None


def _install_stop_handlers(stop: Callable[[], None]) -> None:
    def _handler(_signum: int, _frame: Any) -> None:
        stop()

    try:
        signal.signal(signal.SIGTERM, _handler)
        signal.signal(signal.SIGINT, _handler)
    except Exception:
        pass


def _okx_bar(bar: str) -> str:
    """Normalize internal timeframe names to OKX bar values."""
    value = str(bar or "").strip()
    mapping = {
        "15M": "15m",
        "30M": "30m",
        "1H": "1H",
        "1h": "1H",
        "60M": "1H",
    }
    return mapping.get(value, mapping.get(value.upper(), value or "30m"))


def _okx_inst_id_candidates(symbol: str) -> list[str]:
    clean = symbol.upper().replace("-", "").replace("_", "").strip()
    if clean.endswith("USDT") and len(clean) > 4:
        base = clean[:-4]
        return [f"{base}-USDT-SWAP", f"{base}-USDT"]
    if clean.endswith("USD") and len(clean) > 3:
        base = clean[:-3]
        return [f"{base}-USD-SWAP", f"{base}-USD"]
    return [symbol]


def _build_market_data_provider() -> MarketDataProvider:
    provider_name = str(os.getenv("MARKET_DATA_PROVIDER", "OKX")).upper().strip()
    if provider_name in {"EMPTY", "NONE", "DISABLED"}:
        return EmptyMarketDataProvider()
    return OKXMarketDataProvider()


def _strip_env_value(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _load_env_file(path: str) -> bool:
    """Load simple KEY=VALUE pairs from a .env file without overriding real env vars."""
    try:
        with open(path, "r", encoding="utf-8") as env_file:
            for raw_line in env_file:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                if not key:
                    continue
                os.environ.setdefault(key, _strip_env_value(value))
        return True
    except FileNotFoundError:
        return False
    except Exception:
        return False


def _load_project_env() -> None:
    """Load .env from the working directory and from the bot.py directory."""
    candidates = [
        os.path.join(os.getcwd(), ".env"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
    ]
    seen: set[str] = set()
    for path in candidates:
        if path in seen:
            continue
        seen.add(path)
        _load_env_file(path)


def _env_first(*names: str) -> str:
    for name in names:
        value = os.getenv(name)
        if value:
            value = value.strip()
            if value:
                return value
    return ""


def create_runtime(
    *,
    store: StateStore | None = None,
    market_data: MarketDataProvider | None = None,
    decision_provider: DecisionProvider | None = None,
    notifier: Notifier | None = None,
    monitor: StateStoreMonitor | None = None,
    config: LoopConfig | None = None,
) -> BotRuntime:
    if store is None:
        store = StateStore()
    if market_data is None:
        market_data = _build_market_data_provider()
    return BotRuntime(
        store=store,
        market_data=market_data,
        decision_provider=decision_provider,
        notifier=notifier,
        monitor=monitor,
        config=config,
    )


def _runtime_status_text(runtime: BotRuntime) -> str:
    settings = runtime.store.snapshot().settings
    report = runtime.last_report
    lines = [
        "✅ ربات روشن است",
        f"نسخه: {BOT_VERSION}",
        f"Auto Signal: {getattr(settings, 'auto_signal_enabled', DEFAULT_AUTO_SIGNAL_ENABLED)}",
        f"Real Trade: {getattr(settings, 'real_trade_enabled', DEFAULT_REAL_TRADE_ENABLED)}",
        f"Watchlist: {', '.join(runtime.config.watchlist)}",
    ]
    if report:
        lines.extend(
            [
                f"آخرین اسکن: {len(report.scanned_symbols)} کوین",
                f"سیگنال‌ها: {report.signal_registered}",
                f"REAL: {report.real_requested}",
                f"Monitor: {report.monitor_results}",
            ]
        )
        if report.errors:
            lines.append("خطاهای آخر: " + " | ".join(report.errors[-3:]))
    if runtime.last_error:
        lines.append("آخرین خطا: " + runtime.last_error)
    return "\n".join(lines)


def _build_status_provider(runtime: BotRuntime) -> Callable[[], Mapping[str, Any]]:
    """Build live status for Telegram command panels.

    The provider reads live Toobit wallet/open-position data when available and
    falls back safely to StateStore.
    """

    def _provider() -> Mapping[str, Any]:
        snapshot = runtime.store.snapshot()
        settings = snapshot.settings
        data: dict[str, Any] = {
            "margin_usdt": getattr(snapshot, "toobit_margin_usdt", None),
            "open_positions": getattr(snapshot, "used_slots", 0),
        }

        client = None
        if tobit_client is not None:
            try:
                client = tobit_client.get_client()
            except Exception as exc:
                data["toobit_status_error"] = str(exc)

        if client is not None:
            live_margin = None
            live_positions = None
            try:
                margin = client.get_wallet_margin_usdt()
                if margin is not None:
                    live_margin = float(margin)
                    data["toobit_margin_usdt"] = live_margin
            except Exception as exc:
                data["toobit_margin_error"] = str(exc)

            try:
                positions = client.get_open_positions()
                live_positions = len(positions)
                data["toobit_open_total"] = live_positions
                data["open_positions"] = live_positions
            except Exception as exc:
                data["toobit_positions_error"] = str(exc)

            if live_margin is not None or live_positions is not None:
                try:
                    runtime.store.sync_toobit_status(
                        margin_usdt=live_margin if live_margin is not None else getattr(snapshot, "toobit_margin_usdt", None),
                        open_positions=int(live_positions if live_positions is not None else getattr(snapshot, "toobit_open_positions", 0) or 0),
                    )
                except Exception as exc:
                    data["state_sync_error"] = str(exc)

        if data.get("toobit_margin_usdt") is None and data.get("margin_usdt") is None:
            # Last-resort display fallback: avoid "نامشخص" when live Toobit is
            # unavailable, while keeping the real configured wallet read separate.
            data["margin_usdt"] = float(getattr(settings, "trade_capital_usdt", 0.0) or 0.0)

        return data

    return _provider



# Telegram command handling ---------------------------------------------------

def _cmd(name: str, fallback: str) -> str:
    return str(getattr(bot_config, name, fallback) if bot_config is not None else fallback)


def _normalize_command_text(text: str) -> str:
    value = (text or "").strip()
    while value.startswith("/"):
        value = value[1:].strip()
    return " ".join(value.split()).lower()


def _parse_command_number(text: str, prefix: str) -> str:
    raw = (text or "").strip()
    variants = {prefix, "/" + prefix}
    for item in variants:
        if raw.startswith(item):
            return raw[len(item):].strip()
    return ""


def _format_bool(value: Any) -> str:
    return "روشن ✅" if bool(value) else "خاموش ❌"


def _format_settings_panel(runtime: BotRuntime) -> str:
    snapshot = runtime.store.snapshot()
    settings = snapshot.settings
    return "\n".join([
        _cmd("TITLE_TRADE_PANEL", "⚙️ وضعیت ربات"),
        f"Auto Signal: {_format_bool(getattr(settings, 'auto_signal_enabled', DEFAULT_AUTO_SIGNAL_ENABLED))}",
        f"Real Trade: {_format_bool(getattr(settings, 'real_trade_enabled', DEFAULT_REAL_TRADE_ENABLED))}",
        f"سرمایه ترید: {float(getattr(settings, 'trade_capital_usdt', 0.0) or 0.0):.4g} USDT",
        f"مارجین هر معامله: {float(getattr(settings, 'trade_dollar_usdt', 0.0) or 0.0):.4g} USDT",
        f"لوریج: {int(getattr(settings, 'leverage', 1) or 1)}x",
        f"حداکثر پوزیشن: {int(getattr(settings, 'max_slots', 0) or 0)}",
        f"اسلات استفاده‌شده: {int(getattr(snapshot, 'used_slots', 0) or 0)}",
        f"اسلات آزاد: {int(getattr(snapshot, 'free_slots', 0) or 0)}",
        f"حداقل سود خالص: {float(getattr(settings, 'min_net_profit_usdt', 0.0) or 0.0):.4g} USDT",
    ])


def _format_stats_panel(runtime: BotRuntime) -> str:
    stats = runtime.store.snapshot().stats
    return "\n".join([
        _cmd("TITLE_STATS_PANEL", "📊 آمار ربات"),
        f"Real TP/SL: {int(getattr(stats, 'real_tp', 0))}/{int(getattr(stats, 'real_sl', 0))}",
        f"Real WinRate: {float(getattr(stats, 'real_win_rate', 0.0)):.2f}%",
        f"Real PNL: {float(getattr(stats, 'real_pnl_usdt', 0.0)):.4f} USDT",
        f"Signal TP/SL: {int(getattr(stats, 'signal_only_tp', 0))}/{int(getattr(stats, 'signal_only_sl', 0))}",
        f"Signal WinRate: {float(getattr(stats, 'signal_only_win_rate', 0.0)):.2f}%",
        f"Signal Monitoring: {int(getattr(stats, 'signal_only_monitoring', 0))}",
        f"Real Monitoring: {int(getattr(stats, 'real_monitoring', 0))}",
    ])


def _format_positions_panel(runtime: BotRuntime) -> str:
    active = dict(getattr(runtime.store.snapshot(), "active_signals", {}) or {})
    if not active:
        return "پوزیشن/سیگنال فعال نداریم."
    lines = ["📌 پوزیشن‌های فعال"]
    for item in active.values():
        lines.append(
            f"{getattr(item, 'symbol', '')} | {getattr(item, 'direction', '')} | "
            f"{getattr(item, 'mode', '')}/{getattr(item, 'status', '')} | "
            f"Entry={_safe_float(getattr(item, 'entry', 0.0), 0.0):.8g} | "
            f"TP={_safe_float(getattr(item, 'tp', 0.0), 0.0):.8g} | "
            f"SL={_safe_float(getattr(item, 'sl', 0.0), 0.0):.8g}"
        )
    return "\n".join(lines)


def _format_coins_panel() -> str:
    watchlist = getattr(bot_config, "WATCHLIST", WATCHLIST) if bot_config is not None else WATCHLIST
    symbols = list(watchlist.keys()) if isinstance(watchlist, Mapping) else [str(x) for x in watchlist]
    lines = ["🪙 کوین‌های فعال"]
    for symbol in symbols:
        try:
            coin = get_coin(symbol)
            fa = str(getattr(coin, "fa_name", "") or symbol)
            lines.append(f"{symbol} - {fa}")
        except Exception:
            lines.append(str(symbol))
    return "\n".join(lines)


def _format_help_panel() -> str:
    return "\n".join([
        "📋 دستورات ربات",
        f"{_cmd('CMD_TRADE', 'ترید')} : نمایش پنل ترید",
        f"{_cmd('CMD_TRADE_ON', 'ترید فعال')} / {_cmd('CMD_TRADE_OFF', 'ترید خاموش')}",
        f"{_cmd('CMD_TRADE_DOLLAR', 'ترید دلار')} 5",
        f"{_cmd('CMD_TRADE_LEVERAGE', 'ترید لوریج')} 10",
        f"{_cmd('CMD_TRADE_CAPITAL', 'سرمایه ترید')} 100",
        f"{_cmd('CMD_MAX_POSITIONS', 'حداکثر پوزیشن')} 1",
        f"{_cmd('CMD_MIN_NET_PROFIT', 'حداقل سود خالص')} 0.1",
        f"{_cmd('CMD_STATS', 'آمار')} | {_cmd('CMD_POSITIONS', 'پوزیشن')} | {_cmd('CMD_COINS', 'کوین‌ها')} | {_cmd('CMD_SETTINGS', 'تنظیمات')}",
        "هوش مصنوعی فعال / هوش مصنوعی خاموش",
    ])


def _handle_config_command_text(text: str, runtime: BotRuntime) -> str:
    raw = (text or "").strip()
    normalized = _normalize_command_text(raw)
    if not normalized:
        return ""

    cmd_trade = _cmd("CMD_TRADE", "ترید").lower()
    cmd_trade_on = _cmd("CMD_TRADE_ON", "ترید فعال").lower()
    cmd_trade_off = _cmd("CMD_TRADE_OFF", "ترید خاموش").lower()
    cmd_trade_dollar = _cmd("CMD_TRADE_DOLLAR", "ترید دلار")
    cmd_trade_leverage = _cmd("CMD_TRADE_LEVERAGE", "ترید لوریج")
    cmd_trade_capital = _cmd("CMD_TRADE_CAPITAL", "سرمایه ترید")
    cmd_max_positions = _cmd("CMD_MAX_POSITIONS", "حداکثر پوزیشن")
    cmd_min_net_profit = _cmd("CMD_MIN_NET_PROFIT", "حداقل سود خالص")
    cmd_stats = _cmd("CMD_STATS", "آمار").lower()
    cmd_ai = _cmd("CMD_AI", "هوش مصنوعی").lower()
    cmd_coins = _cmd("CMD_COINS", "کوین‌ها").lower()
    cmd_settings = _cmd("CMD_SETTINGS", "تنظیمات").lower()
    cmd_positions = _cmd("CMD_POSITIONS", "پوزیشن").lower()

    try:
        if normalized in {"help", "راهنما", "دستورات", "commands"}:
            return _format_help_panel()

        if normalized in {cmd_trade, "trade", "panel", "پنل"}:
            return _format_settings_panel(runtime)

        if normalized in {cmd_trade_on, "trade on", "ترید روشن"}:
            runtime.store.set_real_trade_enabled(True)
            return "✅ ترید واقعی فعال شد.\n" + _format_settings_panel(runtime)

        if normalized in {cmd_trade_off, "trade off", "ترید غیرفعال", "ترید غیر فعال"}:
            runtime.store.set_real_trade_enabled(False)
            return "✅ ترید واقعی خاموش شد.\n" + _format_settings_panel(runtime)

        for prefix, setter, label, cast in (
            (cmd_trade_dollar, runtime.store.set_trade_dollar, "مارجین هر معامله", float),
            (cmd_trade_leverage, runtime.store.set_leverage, "لوریج", int),
            (cmd_trade_capital, runtime.store.set_trade_capital, "سرمایه ترید", float),
            (cmd_max_positions, runtime.store.set_max_slots, "حداکثر پوزیشن", int),
            (cmd_min_net_profit, runtime.store.set_min_net_profit, "حداقل سود خالص", float),
        ):
            prefix_norm = prefix.lower()
            if normalized == prefix_norm or normalized.startswith(prefix_norm + " "):
                value_text = _parse_command_number(raw, prefix)
                if not value_text:
                    return f"❌ مقدار وارد نشده. مثال: {prefix} 5"
                value = cast(float(value_text) if cast is int else value_text)
                setter(value)
                suffix = "x" if cast is int and prefix == cmd_trade_leverage else ""
                return f"✅ {label} تنظیم شد: {value}{suffix}\n" + _format_settings_panel(runtime)

        if normalized in {cmd_stats, "stats", "آمار ربات"}:
            return _format_stats_panel(runtime)

        if normalized in {cmd_positions, "positions", "پوزیشن‌ها", "پوزیشن ها"}:
            return _format_positions_panel(runtime)

        if normalized in {cmd_coins, "coins", "کوین ها", "واچ لیست", "واچ‌لیست"}:
            return _format_coins_panel()

        if normalized in {cmd_settings, "settings", "setting", "تنظیمات ربات", "وضعیت"}:
            return _format_settings_panel(runtime)

        if normalized in {cmd_ai, "ai"}:
            return f"هوش مصنوعی/اتو سیگنال: {_format_bool(getattr(runtime.store.snapshot().settings, 'auto_signal_enabled', DEFAULT_AUTO_SIGNAL_ENABLED))}"
        if normalized in {cmd_ai + " فعال", cmd_ai + " روشن", "ai on"}:
            runtime.store.set_auto_signal_enabled(True)
            return "✅ هوش مصنوعی/اتو سیگنال روشن شد."
        if normalized in {cmd_ai + " خاموش", cmd_ai + " غیرفعال", cmd_ai + " غیر فعال", "ai off"}:
            runtime.store.set_auto_signal_enabled(False)
            return "✅ هوش مصنوعی/اتو سیگنال خاموش شد."

    except Exception as exc:
        return f"❌ خطا در اجرای دستور:\n{exc}"

    return ""



async def _telegram_message_handler(update: Update, context: Any) -> None:
    runtime: BotRuntime = context.application.bot_data["runtime"]
    notifier: TelegramNotifier = context.application.bot_data["notifier"]
    if update.effective_chat:
        notifier.set_chat_id(update.effective_chat.id)

    text = (getattr(getattr(update, "message", None), "text", "") or "").strip()
    normalized = text.replace("/", "").strip().lower()

    try:
        if normalized in {"start", "status", "وضعیت"}:
            await update.message.reply_text(_format_settings_panel(runtime)[:3900])
            return

        if normalized in {"ping", "تست"}:
            await update.message.reply_text("✅ ربات جواب می‌دهد")
            return

        command_response = _handle_config_command_text(text, runtime).strip()
        if command_response:
            await update.message.reply_text(command_response[:3900])
            return

        await update.message.reply_text("دستور نامشخص است. برای دیدن لیست دستورها «راهنما» را بفرست.")
    except Exception as exc:
        runtime.last_error = f"telegram_handler_error:{exc}"
        await update.message.reply_text("❌ خطا در پردازش دستور:\n" + str(exc))


def _start_runtime_thread(runtime: BotRuntime) -> threading.Thread:
    thread = threading.Thread(target=runtime.run_forever, name="bot-runtime-loop", daemon=True)
    thread.start()
    return thread


def build_telegram_application(runtime: BotRuntime) -> Application:
    _load_project_env()

    if ApplicationBuilder is None or MessageHandler is None or CommandHandler is None or filters is None:
        raise RuntimeError("python-telegram-bot is not installed. Install requirements.txt first.")

    token = _env_first("TELEGRAM_BOT_TOKEN", "BOT_TOKEN")
    if not token:
        raise RuntimeError("Telegram bot token is missing. Set TELEGRAM_BOT_TOKEN or BOT_TOKEN in .env")

    default_chat_id = _env_first("TELEGRAM_CHAT_ID", "OWNER_ID") or None

    application = ApplicationBuilder().token(token).build()
    notifier = TelegramNotifier(application, default_chat_id=default_chat_id)
    application.bot_data["runtime"] = runtime
    application.bot_data["notifier"] = notifier
    runtime.notifier = notifier

    # Explicit slash commands plus plain Persian text commands.
    application.add_handler(CommandHandler("start", _telegram_message_handler))
    application.add_handler(CommandHandler("status", _telegram_message_handler))
    application.add_handler(CommandHandler("ping", _telegram_message_handler))
    application.add_handler(CommandHandler("trade", _telegram_message_handler))
    application.add_handler(CommandHandler("help", _telegram_message_handler))
    application.add_handler(CommandHandler("stats", _telegram_message_handler))
    application.add_handler(CommandHandler("positions", _telegram_message_handler))
    application.add_handler(CommandHandler("coins", _telegram_message_handler))
    application.add_handler(CommandHandler("settings", _telegram_message_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _telegram_message_handler))
    application.add_error_handler(_telegram_error_handler)
    return application


async def _telegram_error_handler(update: object, context: Any) -> None:
    runtime: BotRuntime | None = context.application.bot_data.get("runtime")
    if runtime is not None:
        runtime.last_error = "telegram_error:" + "".join(traceback.format_exception_only(type(context.error), context.error)).strip()


def _configure_logging() -> None:
    level_name = str(os.getenv("BOT_LOG_LEVEL", "INFO")).upper().strip()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s:%(name)s:%(message)s",
    )


def main() -> None:
    _load_project_env()
    _configure_logging()
    runtime = create_runtime()
    application = build_telegram_application(runtime)
    notifier: TelegramNotifier = application.bot_data["notifier"]
    runtime_thread = _start_runtime_thread(runtime)

    async def _post_init(app: Application) -> None:
        notifier.bind_loop(asyncio.get_running_loop())

    application.post_init = _post_init
    try:
        application.run_polling(allowed_updates=Update.ALL_TYPES)
    finally:
        runtime.stop()
        runtime_thread.join(timeout=5)


if __name__ == "__main__":
    main()


__all__ = [
    "BOT_VERSION",
    "DEFAULT_SCAN_INTERVAL_SECONDS",
    "BotRuntime",
    "CycleReport",
    "EmptyMarketDataProvider",
    "LoopConfig",
    "MarketDataProvider",
    "OKXMarketDataProvider",
    "TelegramNotifier",
    "build_telegram_application",
    "_load_project_env",
    "_build_status_provider",
    "_configure_logging",
    "create_runtime",
    "main",
]
