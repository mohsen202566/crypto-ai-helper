"""Position monitor for Crypto AI Helper bot.

Locked responsibility:
- Monitors active TOOBIT and SIGNAL signals until TP/SL result.
- For SIGNAL mode, decides result only from supplied market price.
- For TOOBIT mode, uses injected exchange status / PnL adapters and never calls
  Toobit directly.
- Marks results in StateStore so slots and stats are released immediately after
  a confirmed result.
- Can build Telegram UI result payloads, but does not send Telegram messages.

Design lock:
- Small, simple, strong.
- No market analysis, no AI decision, no order execution, no TP/SL calculation.
- No hidden REAL opening or closing.
"""

from __future__ import annotations

from dataclasses import dataclass
from time import time
from typing import Any, Callable, Iterable, Literal, Mapping

try:
    from state_store import ResultKind, SignalMode, SignalRecord, StateStore
except Exception:  # pragma: no cover - allows isolated compile during file checks.
    ResultKind = Literal["TP", "SL"]  # type: ignore
    SignalMode = Literal["TOOBIT", "SIGNAL"]  # type: ignore
    SignalRecord = Any  # type: ignore
    StateStore = Any  # type: ignore

Direction = Literal["LONG", "SHORT"]
MonitorStatus = Literal["NO_RESULT", "RESULT_RECORDED", "WAITING_REAL_CONFIRM", "ERROR"]
ExchangeChecker = Callable[[Any], Mapping[str, Any]]
ClosedPnlReader = Callable[[Any], Mapping[str, Any]]
ResultCallback = Callable[["MonitorResult"], None]


@dataclass(frozen=True)
class PriceTick:
    symbol: str
    price: float
    timestamp: float = 0.0


@dataclass(frozen=True)
class MonitorResult:
    status: MonitorStatus
    signal_id: str
    symbol: str
    mode: SignalMode
    direction: Direction
    result: ResultKind | None
    entry: float
    exit_price: float | None
    pnl_usdt: float | None
    move_pct: float | None
    duration_minutes: int
    reason: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class ResultPanelPayload:
    mode: SignalMode
    fa_name: str
    symbol: str
    direction: Direction
    result: ResultKind
    entry: float
    exit_price: float
    pnl_usdt: float
    move_pct: float
    duration_minutes: int


class PositionMonitor:
    """Monitors active records owned by StateStore.

    The monitor intentionally receives prices and exchange adapters from outside.
    It does not fetch OKX data, does not talk to Toobit directly, and does not
    execute orders. Its only write action is StateStore.mark_result().
    """

    def __init__(
        self,
        store: StateStore,
        *,
        exchange_checker: ExchangeChecker | None = None,
        closed_pnl_reader: ClosedPnlReader | None = None,
        result_callback: ResultCallback | None = None,
    ) -> None:
        self.store = store
        self.exchange_checker = exchange_checker
        self.closed_pnl_reader = closed_pnl_reader
        self.result_callback = result_callback

    def check_once(self, prices: Mapping[str, float] | Iterable[PriceTick]) -> list[MonitorResult]:
        price_map = _normalize_prices(prices)
        active = list(self.store.snapshot().active_signals.values())
        results: list[MonitorResult] = []
        for record in active:
            price = price_map.get(str(record.symbol).upper())
            try:
                result = self.check_record(record, price)
            except Exception as exc:
                result = _error_result(record, str(exc))
            if result.status != "NO_RESULT":
                results.append(result)
                if result.status == "RESULT_RECORDED" and self.result_callback is not None:
                    self.result_callback(result)
        return results

    def check_record(self, record: SignalRecord, price: float | None) -> MonitorResult:
        mode = str(record.mode).upper()
        if mode == "SIGNAL":
            return self._check_signal_only(record, price)
        if mode == "TOOBIT":
            return self._check_real(record, price)
        return _error_result(record, f"unknown_signal_mode:{mode}")

    def _check_signal_only(self, record: SignalRecord, price: float | None) -> MonitorResult:
        if price is None or price <= 0:
            return _no_result(record, "price_missing")
        hit = detect_tp_sl(record.direction, price=price, tp=record.tp, sl=record.sl)
        if hit is None:
            return _no_result(record, "tp_sl_not_hit")
        pnl = estimate_pnl_usdt(record, price)
        return self._record_result(record, result=hit, exit_price=price, pnl_usdt=pnl, reason="signal_tp_sl_hit")

    def _check_real(self, record: SignalRecord, price: float | None) -> MonitorResult:
        if self.exchange_checker is None:
            return _no_result(record, "exchange_checker_missing")

        exchange = dict(self.exchange_checker(record))
        if exchange.get("error"):
            return MonitorResult(
                status="ERROR",
                signal_id=record.signal_id,
                symbol=record.symbol,
                mode=record.mode,
                direction=record.direction,
                result=None,
                entry=record.entry,
                exit_price=None,
                pnl_usdt=None,
                move_pct=None,
                duration_minutes=_duration_minutes(record),
                reason=str(exchange.get("error")),
                raw={"exchange": exchange},
            )

        is_open = _truthy_any(exchange, "exists", "open", "position_exists", "found")
        if is_open:
            return _no_result(record, "real_position_still_open", raw={"exchange": exchange})

        exit_price = _extract_exit_price(exchange, fallback=price or 0.0)
        pnl_data: dict[str, Any] = {}
        if self.closed_pnl_reader is not None:
            pnl_data = dict(self.closed_pnl_reader(record))
            if pnl_data.get("error") and exit_price <= 0:
                return MonitorResult(
                    status="ERROR",
                    signal_id=record.signal_id,
                    symbol=record.symbol,
                    mode=record.mode,
                    direction=record.direction,
                    result=None,
                    entry=record.entry,
                    exit_price=None,
                    pnl_usdt=None,
                    move_pct=None,
                    duration_minutes=_duration_minutes(record),
                    reason=str(pnl_data.get("error")),
                    raw={"exchange": exchange, "pnl": pnl_data},
                )
            exit_price = _extract_exit_price(pnl_data, fallback=exit_price)

        if exit_price <= 0:
            return _no_result(record, "real_closed_but_exit_price_missing", raw={"exchange": exchange, "pnl": pnl_data})

        result_kind = infer_result_kind(record, exit_price)
        pnl_usdt = _extract_pnl(pnl_data)
        if pnl_usdt is None:
            pnl_usdt = estimate_pnl_usdt(record, exit_price)

        return self._record_result(
            record,
            result=result_kind,
            exit_price=exit_price,
            pnl_usdt=pnl_usdt,
            reason="real_position_closed_confirmed",
            raw={"exchange": exchange, "pnl": pnl_data},
        )

    def _record_result(
        self,
        record: SignalRecord,
        *,
        result: ResultKind,
        exit_price: float,
        pnl_usdt: float,
        reason: str,
        raw: dict[str, Any] | None = None,
    ) -> MonitorResult:
        self.store.mark_result(record.signal_id, result=result, exit_price=exit_price, pnl_usdt=pnl_usdt)
        return MonitorResult(
            status="RESULT_RECORDED",
            signal_id=record.signal_id,
            symbol=record.symbol,
            mode=record.mode,
            direction=record.direction,
            result=result,
            entry=record.entry,
            exit_price=exit_price,
            pnl_usdt=pnl_usdt,
            move_pct=move_pct(record.direction, record.entry, exit_price),
            duration_minutes=_duration_minutes(record),
            reason=reason,
            raw=raw or {},
        )


def detect_tp_sl(direction: Direction, *, price: float, tp: float, sl: float) -> ResultKind | None:
    if direction == "LONG":
        if price >= tp:
            return "TP"
        if price <= sl:
            return "SL"
        return None
    if direction == "SHORT":
        if price <= tp:
            return "TP"
        if price >= sl:
            return "SL"
        return None
    raise ValueError("direction باید LONG یا SHORT باشد.")


def infer_result_kind(record: SignalRecord, exit_price: float) -> ResultKind:
    direct = detect_tp_sl(record.direction, price=exit_price, tp=record.tp, sl=record.sl)
    if direct is not None:
        return direct
    tp_distance = abs(exit_price - record.tp)
    sl_distance = abs(exit_price - record.sl)
    return "TP" if tp_distance <= sl_distance else "SL"


def estimate_pnl_usdt(record: SignalRecord, exit_price: float) -> float:
    quantity = float(getattr(record, "quantity", 0.0) or 0.0)
    if quantity <= 0:
        # SIGNAL_ONLY records in state_store do not own quantity. Keep PnL neutral
        # instead of inventing leverage or margin inside the monitor.
        return 0.0
    if record.direction == "LONG":
        return (exit_price - record.entry) * quantity
    if record.direction == "SHORT":
        return (record.entry - exit_price) * quantity
    return 0.0


def move_pct(direction: Direction, entry: float, exit_price: float) -> float:
    if entry <= 0:
        return 0.0
    raw = (exit_price - entry) / entry * 100.0
    return raw if direction == "LONG" else -raw


def build_result_panel_payload(result: MonitorResult, fa_name: str = "") -> ResultPanelPayload:
    if result.result is None or result.exit_price is None or result.pnl_usdt is None or result.move_pct is None:
        raise ValueError("MonitorResult هنوز نتیجه کامل ندارد.")
    return ResultPanelPayload(
        mode=result.mode,
        fa_name=fa_name or result.symbol,
        symbol=result.symbol,
        direction=result.direction,
        result=result.result,
        entry=result.entry,
        exit_price=result.exit_price,
        pnl_usdt=result.pnl_usdt,
        move_pct=result.move_pct,
        duration_minutes=result.duration_minutes,
    )


def _normalize_prices(prices: Mapping[str, float] | Iterable[PriceTick]) -> dict[str, float]:
    if isinstance(prices, Mapping):
        return {str(symbol).upper(): float(price) for symbol, price in prices.items() if float(price) > 0}
    out: dict[str, float] = {}
    for tick in prices:
        if tick.price > 0:
            out[tick.symbol.upper()] = float(tick.price)
    return out


def _duration_minutes(record: SignalRecord) -> int:
    started = float(getattr(record, "opened_at", None) or getattr(record, "created_at", time()) or time())
    return max(0, int((time() - started) // 60))


def _no_result(record: SignalRecord, reason: str, raw: dict[str, Any] | None = None) -> MonitorResult:
    return MonitorResult(
        status="NO_RESULT",
        signal_id=record.signal_id,
        symbol=record.symbol,
        mode=record.mode,
        direction=record.direction,
        result=None,
        entry=record.entry,
        exit_price=None,
        pnl_usdt=None,
        move_pct=None,
        duration_minutes=_duration_minutes(record),
        reason=reason,
        raw=raw or {},
    )


def _error_result(record: SignalRecord, reason: str) -> MonitorResult:
    return MonitorResult(
        status="ERROR",
        signal_id=record.signal_id,
        symbol=record.symbol,
        mode=record.mode,
        direction=record.direction,
        result=None,
        entry=record.entry,
        exit_price=None,
        pnl_usdt=None,
        move_pct=None,
        duration_minutes=_duration_minutes(record),
        reason=reason,
        raw={},
    )


def _truthy_any(data: Mapping[str, Any], *keys: str) -> bool:
    for key in keys:
        if key in data:
            return bool(data.get(key))
    return False


def _extract_exit_price(data: Mapping[str, Any], *, fallback: float = 0.0) -> float:
    for key in ("exit_price", "close_price", "closed_price", "avgExitPrice", "price", "mark", "lastPrice"):
        value = data.get(key)
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number > 0:
            return number
    return float(fallback or 0.0)


def _extract_pnl(data: Mapping[str, Any]) -> float | None:
    for key in ("pnl_usdt", "realizedPnl", "realizedPNL", "realizedProfit", "pnl", "profit"):
        if key not in data:
            continue
        try:
            return float(data.get(key))
        except (TypeError, ValueError):
            continue
    return None


__all__ = [
    "ClosedPnlReader",
    "ExchangeChecker",
    "MonitorResult",
    "PositionMonitor",
    "PriceTick",
    "ResultCallback",
    "ResultPanelPayload",
    "build_result_panel_payload",
    "detect_tp_sl",
    "estimate_pnl_usdt",
    "infer_result_kind",
    "move_pct",
]
