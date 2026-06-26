"""Main bot loop for Crypto AI Helper bot.

Locked responsibility:
- Owns the lightweight 5-second runtime loop.
- Pulls market prices from OKX by default, or from an injected market adapter in tests.
- Sends market snapshots to the existing strategy layer for the final decision.
- Registers SIGNAL_ONLY when real trading is off, slots are full, or the strategy/TP-SL
  result says the trade should be monitored only.
- Sends REAL requests only through real_trade_manager.py.
- Runs position_monitor.py every cycle so TP/SL results are detected and recorded fast.
- Builds Telegram/UI payloads through telegram_ui.py only.

Design lock:
- Small, simple, strong.
- OKX price feed is the default; EmptyMarketDataProvider is only a test fallback.
- No Toobit direct calls here.
- No TP/SL calculation here.
- No indicator/AI/probability logic here.
- No extra runtime coordinator files.
"""

from __future__ import annotations

import json
import os
import signal
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Protocol

try:
    from config import DEFAULT_AUTO_SIGNAL_ENABLED, DEFAULT_REAL_TRADE_ENABLED, WATCHLIST, get_coin
except Exception:  # pragma: no cover - keeps isolated compile checks possible.
    DEFAULT_AUTO_SIGNAL_ENABLED = True
    DEFAULT_REAL_TRADE_ENABLED = False
    WATCHLIST = ("DOGEUSDT", "XRPUSDT", "SOLUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "INJUSDT")

    def get_coin(symbol: str) -> Any:  # type: ignore
        return symbol

try:
    from state_store import StateStore
except Exception:  # pragma: no cover
    StateStore = Any  # type: ignore

try:
    from position_monitor import PositionMonitor, PriceTick, MonitorResult, build_result_panel_payload
except Exception:  # pragma: no cover
    PositionMonitor = Any  # type: ignore
    PriceTick = Any  # type: ignore
    MonitorResult = Any  # type: ignore

    def build_result_panel_payload(result: Any, fa_name: str = "") -> Any:  # type: ignore
        return result

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


BOT_VERSION = "simple_okx_level4_loop_v2"
DEFAULT_SCAN_INTERVAL_SECONDS = 5.0
DEFAULT_OKX_BASE_URL = "https://www.okx.com"


class MarketDataProvider(Protocol):
    def get_prices(self, symbols: Iterable[str]) -> Mapping[str, float]:
        """Return latest prices keyed by project symbols such as DOGEUSDT."""


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


class OKXMarketDataProvider:
    """Small OKX public price adapter for USDT perpetual symbols.

    It intentionally uses only stdlib urllib so the bot does not need a new
    dependency on the VPS.  Input/output symbols stay in project format:
    DOGEUSDT -> OKX DOGE-USDT-SWAP -> DOGEUSDT.
    """

    def __init__(self, *, base_url: str | None = None, timeout_seconds: float = 6.0) -> None:
        self.base_url = (base_url or os.getenv("OKX_BASE_URL") or DEFAULT_OKX_BASE_URL).rstrip("/")
        self.timeout_seconds = max(1.0, float(timeout_seconds))

    def get_prices(self, symbols: Iterable[str]) -> Mapping[str, float]:
        wanted = [_normalize_symbol(symbol) for symbol in symbols]
        wanted = [symbol for symbol in wanted if symbol]
        if not wanted:
            return {}

        prices = self._get_swap_tickers(wanted)
        missing = [symbol for symbol in wanted if symbol not in prices]
        for symbol in missing:
            price = self._get_one_ticker(symbol)
            if price > 0:
                prices[symbol] = price
        return prices

    def _get_swap_tickers(self, wanted: list[str]) -> dict[str, float]:
        url = f"{self.base_url}/api/v5/market/tickers?instType=SWAP"
        payload = self._read_json(url)
        wanted_set = set(wanted)
        prices: dict[str, float] = {}
        for row in payload.get("data", []) if isinstance(payload, Mapping) else []:
            symbol = _project_symbol_from_okx_inst_id(str(row.get("instId", "")))
            if symbol in wanted_set:
                price = _safe_price(row.get("last") or row.get("markPx") or row.get("askPx") or row.get("bidPx"))
                if price > 0:
                    prices[symbol] = price
        return prices

    def _get_one_ticker(self, symbol: str) -> float:
        inst_id = _okx_swap_inst_id(symbol)
        query = urllib.parse.urlencode({"instId": inst_id})
        url = f"{self.base_url}/api/v5/market/ticker?{query}"
        payload = self._read_json(url)
        rows = payload.get("data", []) if isinstance(payload, Mapping) else []
        if not rows:
            return 0.0
        row = rows[0]
        return _safe_price(row.get("last") or row.get("markPx") or row.get("askPx") or row.get("bidPx"))

    def _read_json(self, url: str) -> Mapping[str, Any]:
        request = urllib.request.Request(url, headers={"User-Agent": "crypto-ai-helper/1.0"})
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:  # noqa: S310 - OKX public HTTPS endpoint.
            raw = response.read().decode("utf-8")
        data = json.loads(raw)
        return data if isinstance(data, Mapping) else {}


class EmptyMarketDataProvider:
    """Test fallback only. Production create_runtime() uses OKXMarketDataProvider."""

    def get_prices(self, symbols: Iterable[str]) -> Mapping[str, float]:
        return {}


class BotRuntime:
    """Single-file runtime coordinator used by bot.py."""

    def __init__(
        self,
        *,
        store: StateStore,
        market_data: MarketDataProvider,
        decision_provider: DecisionProvider | None = None,
        notifier: Notifier | None = None,
        monitor: PositionMonitor | None = None,
        config: LoopConfig | None = None,
    ) -> None:
        self.store = store
        self.market_data = market_data
        self.decision_provider = decision_provider or _default_decision_provider
        self.notifier = notifier or _default_notifier
        self.monitor = monitor or PositionMonitor(
            store,
            exchange_checker=_default_exchange_checker,
            closed_pnl_reader=_default_closed_pnl_reader,
            result_callback=self._on_monitor_result,
        )
        self.config = config or LoopConfig.from_env()
        self._running = False

    def run_forever(self) -> None:
        self._running = True
        _install_stop_handlers(lambda: self.stop())
        while self._running:
            started = time.time()
            self.run_once()
            elapsed = time.time() - started
            sleep_for = max(0.0, self.config.scan_interval_seconds - elapsed)
            if sleep_for:
                time.sleep(sleep_for)

    def stop(self) -> None:
        self._running = False

    def run_once(self) -> CycleReport:
        report = CycleReport(cycle_started_at=time.time())
        symbols = self._active_watchlist()

        try:
            prices = dict(self.market_data.get_prices(symbols))
        except Exception as exc:
            report.errors.append(f"market_data_error:{exc}")
            if self.config.stop_on_error:
                raise
            prices = {}

        monitor_results = self._monitor_positions(prices)
        report.monitor_results = len(monitor_results)

        for symbol in symbols:
            report.scanned_symbols.append(symbol)
            price = _safe_price(prices.get(symbol))
            if price <= 0:
                continue
            try:
                handled = self._handle_symbol(symbol, price, prices)
                if handled == "REAL":
                    report.real_requested += 1
                elif handled == "SIGNAL":
                    report.signal_registered += 1
                if handled:
                    report.decisions += 1
            except Exception as exc:
                report.errors.append(f"{symbol}:{exc}")
                if self.config.stop_on_error:
                    raise
        return report

    def _active_watchlist(self) -> list[str]:
        symbols: list[str] = []
        for raw in self.config.watchlist:
            symbol = _normalize_symbol(raw)
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
        except Exception:
            return []

    def _handle_symbol(self, symbol: str, price: float, prices: Mapping[str, float]) -> str:
        snapshot = self.store.snapshot()
        settings = getattr(snapshot, "settings", None)
        if not _setting_bool(settings, "auto_signal_enabled", DEFAULT_AUTO_SIGNAL_ENABLED):
            return ""
        if self.store.has_active_symbol(symbol):
            return ""

        market = {"price": price, "prices": dict(prices), "symbol": symbol, "source": "OKX"}
        decision = self.decision_provider(symbol, market)
        if not _decision_is_actionable(decision):
            return ""

        requested_real = _decision_requests_real(decision)
        real_enabled = _setting_bool(settings, "real_trade_enabled", DEFAULT_REAL_TRADE_ENABLED)
        can_open_real = bool(real_enabled and requested_real and self.store.can_open_real(symbol))

        if can_open_real:
            result = _open_real(decision)
            if _open_result_ok(result):
                self._notify_signal(decision, mode="REAL", raw_result=result)
                return "REAL"
            return ""

        record = self._register_signal_only(decision, symbol=symbol, price=price)
        self._notify_signal(decision, mode="SIGNAL", raw_result=record)
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
            payload = build_result_panel_payload(result, fa_name=_fa_name(str(_get_value(result, "symbol", ""))))
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
    if real_trade_manager is None:
        return {"exists": False, "error": "real_trade_manager_missing"}
    fn = getattr(real_trade_manager, "exchange_position_checker", None)
    if not callable(fn):
        return {"exists": False, "error": "exchange_position_checker_missing"}
    return fn(record)


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
    status = str(_get_value(result, "status", "")).upper()
    return status in {"OK", "RECOVERED", "SUCCESS"}


def _decision_is_actionable(decision: Any) -> bool:
    if decision is None:
        return False
    action = str(_get_value(decision, "action", _get_value(decision, "decision", ""))).upper()
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
    mode = str(_get_value(decision, "mode", "")).upper()
    if mode in {"REAL", "TOOBIT"}:
        return True
    return bool(_get_value(decision, "real", False) or _get_value(decision, "is_real", False))


def _direction(decision: Any, *, allow_empty: bool = False) -> str:
    value = str(_get_value(decision, "direction", "")).upper()
    if value in {"LONG", "BUY"}:
        return "LONG"
    if value in {"SHORT", "SELL"}:
        return "SHORT"
    if allow_empty:
        return ""
    raise ValueError("decision direction must be LONG or SHORT")


def _entry(decision: Any, *, fallback: float) -> float:
    value = _safe_price(_get_value(decision, "entry", 0.0))
    return value if value > 0 else fallback


def _tp_sl(decision: Any) -> tuple[float, float]:
    plan = _get_value(decision, "tp_sl", None)
    if plan is not None:
        tp = _safe_price(_get_value(plan, "tp1", 0.0) or _get_value(plan, "tp", 0.0))
        sl = _safe_price(_get_value(plan, "sl", 0.0))
        if tp > 0 and sl > 0:
            return tp, sl
    tp = _safe_price(_get_value(decision, "tp", 0.0) or _get_value(decision, "tp1", 0.0))
    sl = _safe_price(_get_value(decision, "sl", 0.0) or _get_value(decision, "stop_loss", 0.0))
    if tp <= 0 or sl <= 0:
        raise ValueError("decision TP/SL missing")
    return tp, sl


def _signal_id(decision: Any, symbol: str) -> str:
    raw = str(_get_value(decision, "signal_id", "")).strip()
    if raw:
        return raw
    return f"{symbol}_{int(time.time() * 1000)}"


def _get_value(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _setting_bool(settings: Any, name: str, default: bool) -> bool:
    value = _get_value(settings, name, default)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}
    return bool(value)


def _safe_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _safe_price(value: Any) -> float:
    number = _safe_float(value, 0.0)
    return number if number > 0 else 0.0


def _normalize_symbol(symbol: Any) -> str:
    return str(symbol).upper().replace("-", "").replace("_", "").strip()


def _okx_swap_inst_id(symbol: str) -> str:
    normalized = _normalize_symbol(symbol)
    if normalized.endswith("USDT"):
        base = normalized[:-4]
        return f"{base}-USDT-SWAP"
    return normalized


def _project_symbol_from_okx_inst_id(inst_id: str) -> str:
    parts = inst_id.upper().split("-")
    if len(parts) >= 2 and parts[1] == "USDT":
        return f"{parts[0]}USDT"
    return _normalize_symbol(inst_id)


def _fa_name(symbol: str) -> str:
    try:
        coin = get_coin(symbol)
        return str(_get_value(coin, "fa_name", "") or _get_value(coin, "name_fa", "") or symbol)
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


def _default_notifier(payload: Any) -> None:
    # Telegram sending is intentionally not owned by this file.
    # The real bot should inject a notifier from its existing telegram layer.
    return None


def _install_stop_handlers(stop: Callable[[], None]) -> None:
    def _handler(_signum: int, _frame: Any) -> None:
        stop()

    try:
        signal.signal(signal.SIGTERM, _handler)
        signal.signal(signal.SIGINT, _handler)
    except Exception:
        pass


def create_runtime(
    *,
    store: StateStore | None = None,
    market_data: MarketDataProvider | None = None,
    decision_provider: DecisionProvider | None = None,
    notifier: Notifier | None = None,
    monitor: PositionMonitor | None = None,
    config: LoopConfig | None = None,
) -> BotRuntime:
    if store is None:
        store = StateStore()
    if market_data is None:
        market_data = OKXMarketDataProvider()
    return BotRuntime(
        store=store,
        market_data=market_data,
        decision_provider=decision_provider,
        notifier=notifier,
        monitor=monitor,
        config=config,
    )


def main() -> None:
    runtime = create_runtime()
    runtime.run_forever()


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
    "create_runtime",
    "main",
]
