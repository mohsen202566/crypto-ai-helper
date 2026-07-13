"""موتور ساده و تطبیقی: شروع حرکت، جهت همان حرکت، افق و TP/SL رفتاری."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any
import json
import logging
import threading
import time

import config
from profiles import SymbolSpec

logger = logging.getLogger("adaptive_bot.learning")

DEFAULT_LEARNING_FACTORS: dict[str, float] = {
    "price_factor": 1.0,
    "range_factor": 1.0,
    "volume_factor": 1.0,
    "directionality_factor": 1.0,
    "tp_factor": 1.0,
    "sl_factor": 1.0,
}


def learning_pattern_key(symbol_id: str, side: str, window: int, support_tool: str, horizon: int) -> str:
    return f"{symbol_id.upper()}|{side.upper()}|{int(window)}|{support_tool.upper()}|{int(horizon)}"


def normalize_learning_factors(value: dict[str, Any] | None = None) -> dict[str, float]:
    factors = dict(DEFAULT_LEARNING_FACTORS)
    for key, raw in (value or {}).items():
        if key not in factors:
            continue
        try:
            factors[key] = float(raw)
        except (TypeError, ValueError):
            continue
    return factors


@dataclass(frozen=True)
class Tick:
    ts: float
    price: float


@dataclass
class WatchState:
    side: str
    started_at: float
    last_candidate_at: float
    window: int
    last_reason: str = ""


@dataclass(frozen=True)
class Observation:
    status: str
    symbol_id: str
    side: str | None
    window: int | None
    reason: str
    metrics: dict[str, float | int | str | bool]
    transition: str = ""


@dataclass(frozen=True)
class TradePlan:
    symbol_id: str
    okx_symbol: str
    toobit_symbol: str
    side: str
    entry: float
    tp: float
    sl: float
    tp_pct: float
    sl_pct: float
    rr_net: float
    expected_minutes: int
    trigger_window: int
    trigger_reason: str
    notional: float
    estimated_tp_gross: float
    estimated_tp_costs: float
    estimated_tp_net: float
    estimated_sl_gross_loss: float
    estimated_sl_costs: float
    estimated_sl_net_loss: float
    metrics: dict[str, Any] = field(default_factory=dict)


class AdaptiveStartEngine:
    def __init__(self) -> None:
        self.buffers: dict[str, deque[Tick]] = {}
        self.watches: dict[str, WatchState] = {}

    def _buffer(self, symbol_id: str) -> deque[Tick]:
        if symbol_id not in self.buffers:
            self.buffers[symbol_id] = deque(maxlen=180)
        return self.buffers[symbol_id]

    @staticmethod
    def _window_metrics(points: list[Tick], window: int, threshold: dict[str, float]) -> dict[str, float | int]:
        if len(points) < 3:
            return {}
        start = points[0].price
        end = points[-1].price
        if start <= 0:
            return {}
        signed_move = (end - start) / start * 100.0
        path = 0.0
        for previous, current in zip(points, points[1:]):
            if previous.price > 0:
                path += abs(current.price - previous.price) / previous.price * 100.0
        directionality = abs(signed_move) / path if path > 0 else 0.0
        high = max(point.price for point in points)
        low = min(point.price for point in points)
        range_pct = (high - low) / start * 100.0
        half_ts = points[0].ts + (points[-1].ts - points[0].ts) / 2.0
        middle = min(points, key=lambda point: abs(point.ts - half_ts)).price
        first_signed = (middle - start) / start * 100.0
        second_signed = (end - middle) / middle * 100.0 if middle > 0 else 0.0
        side_sign = 1.0 if signed_move >= 0 else -1.0
        acceleration = side_sign * second_signed - side_sign * first_signed
        move_threshold = float(threshold["move_threshold_pct"])
        range_threshold = float(threshold["range_threshold_pct"])
        return {
            "window": window,
            "signed_move_pct": signed_move,
            "abs_move_pct": abs(signed_move),
            "path_pct": path,
            "directionality": directionality,
            "range_pct": range_pct,
            "move_threshold_pct": move_threshold,
            "range_threshold_pct": range_threshold,
            "move_ratio": abs(signed_move) / move_threshold if move_threshold > 0 else 0.0,
            "range_ratio": range_pct / range_threshold if range_threshold > 0 else 0.0,
            "acceleration": acceleration,
        }

    def evaluate(
        self,
        symbol: SymbolSpec,
        profile: dict[str, Any],
        price: float,
        *,
        now: float | None = None,
        volume_quote: float | None = None,
        append_tick: bool = True,
        learning_gates: dict[str, list[dict[str, Any]]] | None = None,
    ) -> Observation:
        now = float(now or time.time())
        buffer = self._buffer(symbol.id)
        if append_tick:
            if not buffer or now > buffer[-1].ts or price != buffer[-1].price:
                buffer.append(Tick(now, float(price)))
        while buffer and now - buffer[0].ts > 125:
            buffer.popleft()
        if not buffer or now - buffer[0].ts < min(config.TRIGGER_WINDOWS_SECONDS) * 0.85:
            return Observation("WARMING", symbol.id, None, None, "LIVE_WINDOW_WARMING", {"buffer_seconds": now - buffer[0].ts if buffer else 0.0})

        observations: list[dict[str, float | int]] = []
        for window in config.TRIGGER_WINDOWS_SECONDS:
            cutoff = now - window
            points = [point for point in buffer if point.ts >= cutoff]
            earlier = [point for point in buffer if point.ts < cutoff]
            if earlier:
                points.insert(0, earlier[-1])
            if not points or points[-1].ts - points[0].ts < window * 0.72:
                continue
            thresholds = profile["windows"][str(window)]
            metrics = self._window_metrics(points, window, thresholds)
            if metrics:
                observations.append(metrics)
        if not observations:
            return Observation("WARMING", symbol.id, None, None, "WINDOWS_INCOMPLETE", {})

        full = [
            item for item in observations
            if float(item["move_ratio"]) >= 1.0
            and float(item["directionality"]) >= config.TRIGGER_MIN_DIRECTIONALITY
        ]
        soft = [
            item for item in observations
            if float(item["move_ratio"]) >= config.WATCH_MOVE_FACTOR
            and float(item["directionality"]) >= config.WATCH_MIN_DIRECTIONALITY
        ]
        chosen: dict[str, float | int] | None = None
        def window_learning_bonus(item: dict[str, float | int]) -> float:
            candidate_side = "LONG" if float(item["signed_move_pct"]) > 0 else "SHORT"
            candidate_window = int(item["window"])
            gates: list[dict[str, Any]] = []
            for tool in ("RANGE", "VOLUME"):
                gates.extend((learning_gates or {}).get(f"{candidate_side}|{candidate_window}|{tool}") or [])
            if not gates:
                return 0.0
            bonuses: list[float] = []
            for gate in gates:
                status = str(gate.get("status") or "BASE").upper()
                factors = normalize_learning_factors(gate.get("factors"))
                fits = (
                    float(item["move_ratio"]) + 1e-9 >= factors["price_factor"]
                    and float(item["directionality"]) + 1e-9
                    >= config.TRIGGER_MIN_DIRECTIONALITY * factors["directionality_factor"]
                )
                status_bonus = {
                    "TRUSTED": 0.22,
                    "ACCEPTED": 0.22,
                    "TRIAL": 0.10,
                    "BASE": 0.0,
                    "QUARANTINED": -0.40,
                }.get(status, 0.0)
                bonuses.append(status_bonus + (0.08 if fits else -0.12))
            return max(bonuses) if bonuses else 0.0

        def window_rank(item: dict[str, float | int]) -> float:
            late_penalty = 0.20 if (
                float(item["move_ratio"]) >= config.LATE_MOVE_RATIO
                and float(item["acceleration"]) <= 0
            ) else 0.0
            return (
                float(item["move_ratio"]) * 0.45
                + float(item["directionality"]) * 0.55
                + window_learning_bonus(item)
                - late_penalty
            )

        if full:
            chosen = max(full, key=window_rank)
        elif soft:
            chosen = max(soft, key=window_rank)

        if chosen is None:
            watch = self.watches.get(symbol.id)
            if watch and now - watch.last_candidate_at > config.WATCH_CANCEL_SECONDS:
                self.watches.pop(symbol.id, None)
                return Observation("NORMAL", symbol.id, None, None, "WATCH_CANCELLED_BEHAVIOR_NORMALIZED", {}, "CANCEL")
            return Observation("NORMAL", symbol.id, None, None, "MOVE_INSIDE_NORMAL_BEHAVIOR", {
                "best_move_ratio": max(float(item["move_ratio"]) for item in observations),
                "best_directionality": max(float(item["directionality"]) for item in observations),
            })

        side = "LONG" if float(chosen["signed_move_pct"]) > 0 else "SHORT"
        previous = self.watches.get(symbol.id)
        transition = ""
        if previous is None:
            transition = "NEW"
            self.watches[symbol.id] = WatchState(side, now, now, int(chosen["window"]))
        else:
            if previous.side != side:
                transition = f"FLIP_{previous.side}_TO_{side}"
                previous.side = side
                previous.started_at = now
            previous.last_candidate_at = now
            previous.window = int(chosen["window"])
        watch = self.watches[symbol.id]
        if now - watch.started_at > config.WATCH_MAX_SECONDS:
            self.watches.pop(symbol.id, None)
            return Observation("NORMAL", symbol.id, None, None, "WATCH_MAX_AGE_REACHED", chosen, "CANCEL")

        learning_gates = learning_gates or {}
        window = int(chosen["window"])
        range_ratio = float(chosen["range_ratio"])
        volume_threshold = float(profile["windows"][str(window)]["volume_threshold_quote"])
        volume_ratio = (
            float(volume_quote) / volume_threshold
            if volume_quote is not None and volume_threshold > 0
            else 0.0
        )

        def gates_for(support_tool: str) -> list[dict[str, Any]]:
            base = {
                "horizon": 0,
                "version": 0,
                "status": "BASE",
                "factors": dict(DEFAULT_LEARNING_FACTORS),
            }
            key = f"{side}|{window}|{support_tool}"
            return [base, *(learning_gates.get(key) or [])]

        def price_direction_ok(gate: dict[str, Any]) -> bool:
            factors = normalize_learning_factors(gate.get("factors"))
            return (
                float(chosen["move_ratio"]) + 1e-9 >= factors["price_factor"]
                and float(chosen["directionality"]) + 1e-9
                >= config.TRIGGER_MIN_DIRECTIONALITY * factors["directionality_factor"]
            )

        range_matches: list[tuple[dict[str, Any], float]] = []
        for gate in gates_for("RANGE"):
            factors = normalize_learning_factors(gate.get("factors"))
            if price_direction_ok(gate) and range_ratio + 1e-9 >= factors["range_factor"]:
                range_matches.append((gate, range_ratio / max(factors["range_factor"], 1e-9)))

        volume_price_matches = [gate for gate in gates_for("VOLUME") if price_direction_ok(gate)]
        volume_matches: list[tuple[dict[str, Any], float]] = []
        if volume_quote is not None:
            for gate in volume_price_matches:
                factors = normalize_learning_factors(gate.get("factors"))
                if volume_ratio + 1e-9 >= factors["volume_factor"]:
                    volume_matches.append((gate, volume_ratio / max(factors["volume_factor"], 1e-9)))

        selected_gate: dict[str, Any] | None = None
        support_tool = ""
        support_score = 0.0
        if range_matches:
            selected_gate, support_score = max(range_matches, key=lambda pair: pair[1])
            support_tool = "RANGE"
        if volume_matches:
            volume_gate, volume_score = max(volume_matches, key=lambda pair: pair[1])
            if selected_gate is None or volume_score > support_score:
                selected_gate, support_score = volume_gate, volume_score
                support_tool = "VOLUME"

        metrics = dict(chosen)
        metrics.update(
            {
                "range_confirmed": bool(range_matches),
                "volume_quote": float(volume_quote or 0.0),
                "volume_threshold_quote": volume_threshold,
                "volume_ratio": volume_ratio,
                "volume_confirmed": bool(volume_matches),
                "support_tool": support_tool,
            }
        )

        if selected_gate is not None:
            factors = normalize_learning_factors(selected_gate.get("factors"))
            metrics.update(
                {
                    "gate_horizon": int(selected_gate.get("horizon") or 0),
                    "gate_version": int(selected_gate.get("version") or 0),
                    "gate_status": str(selected_gate.get("status") or "BASE"),
                    "move_required": factors["price_factor"],
                    "directionality_required": config.TRIGGER_MIN_DIRECTIONALITY * factors["directionality_factor"],
                    "support_required": factors["range_factor"] if support_tool == "RANGE" else factors["volume_factor"],
                }
            )
            if float(chosen["move_ratio"]) >= config.LATE_MOVE_RATIO and float(chosen["acceleration"]) <= 0:
                return Observation("REJECT", symbol.id, side, window, "LATE_EXHAUSTED_MOVE", metrics, transition)
            support = "RANGE_EXPANSION" if support_tool == "RANGE" else "VOLUME_EXPANSION"
            reason = (
                f"شروع حرکت {side} در {window} ثانیه؛ "
                f"حرکت {float(chosen['move_ratio']):.2f} برابر رفتار معمول و تأیید {support}"
            )
            return Observation("TRIGGER", symbol.id, side, window, reason, metrics, transition)

        # A learned soft volume pattern can request volume before the base price gate reaches 1.0.
        if volume_quote is None and volume_price_matches:
            metrics["support_tool"] = "VOLUME"
            return Observation("NEEDS_VOLUME", symbol.id, side, window, "PRICE_TRIGGER_NEEDS_SUPPORT", metrics, transition)

        is_base_full_price = (
            float(chosen["move_ratio"]) >= 1.0
            and float(chosen["directionality"]) >= config.TRIGGER_MIN_DIRECTIONALITY
        )
        if is_base_full_price:
            if float(chosen["move_ratio"]) >= config.LATE_MOVE_RATIO and float(chosen["acceleration"]) <= 0:
                return Observation("REJECT", symbol.id, side, window, "LATE_EXHAUSTED_MOVE", metrics, transition)
            if volume_quote is None:
                return Observation("NEEDS_VOLUME", symbol.id, side, window, "PRICE_TRIGGER_NEEDS_SUPPORT", metrics, transition)
            return Observation("WATCH", symbol.id, side, window, "PRICE_TRIGGER_WITHOUT_SUPPORT", metrics, transition)

        return Observation("WATCH", symbol.id, side, window, "EARLY_MOVE_WATCH", metrics, transition)

    def mark_signal(self, symbol_id: str) -> None:
        self.watches.pop(symbol_id, None)

    def reset_after_close(self, symbol_id: str) -> None:
        """سهمیه یا کول‌داون نیست؛ فقط موج قبلی از حافظه زنده پاک می‌شود."""
        self.watches.pop(symbol_id, None)
        self.buffers.pop(symbol_id, None)

    @staticmethod
    def _price(entry: float, side: str, pct: float, favorable: bool) -> float:
        sign = 1.0 if side == "LONG" else -1.0
        if not favorable:
            sign *= -1.0
        return entry * (1.0 + sign * pct / 100.0)

    @staticmethod
    def _costs(notional: float, entry: float, exit_price: float) -> float:
        qty = notional / entry if entry > 0 else 0.0
        exit_notional = qty * exit_price
        fee_rate = config.TAKER_FEE_PCT_PER_SIDE / 100.0
        slip_rate = config.SLIPPAGE_PCT_PER_SIDE / 100.0
        return (notional + exit_notional) * (fee_rate + slip_rate)

    @classmethod
    def pnl_for_exit(
        cls, entry: float, exit_price: float, side: str, notional: float
    ) -> tuple[float, float, float]:
        move = (exit_price - entry) / entry if side == "LONG" else (entry - exit_price) / entry
        gross = notional * move
        costs = cls._costs(notional, entry, exit_price)
        return gross, costs, gross - costs

    @classmethod
    def _minimum_profit_tp_pct(
        cls, entry: float, side: str, notional: float
    ) -> float:
        """Smallest TP distance that leaves the configured net profit after costs."""
        low, high = 0.0, 8.0
        for _ in range(60):
            middle = (low + high) / 2.0
            exit_price = cls._price(entry, side, middle, favorable=True)
            _, _, net = cls.pnl_for_exit(entry, exit_price, side, notional)
            if net < config.MIN_NET_PROFIT_USDT:
                low = middle
            else:
                high = middle
        return high

    @staticmethod
    def _behavior_mae_to_mfe(stats: dict[str, Any]) -> float:
        """Read the pre-MFE adverse move; old profiles get a conservative fallback."""
        direct = float(stats.get("mae_to_mfe_q55") or 0.0)
        if direct > 0:
            return direct
        # Version-1 profiles measured MAE across the whole horizon, including the
        # reversal after MFE. Discount it until the automatic v2 rebuild finishes.
        old_q70 = float(stats.get("mae_q70") or 0.0)
        old_q75 = float(stats.get("mae_q75") or 0.0)
        return max(0.0, old_q70 * 0.58, old_q75 * 0.52)

    def _outcome_block(
        self,
        profile: dict[str, Any],
        side: str,
        window: int,
        support_tool: str,
        horizon: int,
    ) -> tuple[dict[str, Any] | None, str]:
        outcomes = profile.get("outcomes", {}).get(side, {})
        paths = (
            (str(window), support_tool, "EXACT"),
            (str(window), "ANY", "WINDOW_FALLBACK"),
            ("ANY", "ANY", "SIDE_FALLBACK"),
        )
        fallback: tuple[dict[str, Any], str] | None = None
        for window_key, support_key, source in paths:
            block = (
                outcomes.get(window_key, {})
                .get(support_key, {})
                .get(str(horizon))
            )
            if not block or not block.get("candidates"):
                continue
            if fallback is None:
                fallback = (block, source)
            minimum = config.OUTCOME_MIN_EXACT_SAMPLES if source == "EXACT" else config.OUTCOME_MIN_VALID_SAMPLES
            if int(block.get("samples") or 0) >= minimum:
                return block, source
        return fallback if fallback is not None else (None, "NONE")

    def build_plan(
        self,
        symbol: SymbolSpec,
        profile: dict[str, Any],
        observation: Observation,
        price: float,
        trade_usdt: float,
        leverage: int,
        learning_adjustments: dict[str, dict[str, Any]] | None = None,
    ) -> tuple[TradePlan | None, str, dict[str, Any]]:
        if observation.status != "TRIGGER" or observation.side is None or observation.window is None:
            return None, "NOT_A_CONFIRMED_TRIGGER", {}

        side = observation.side
        window = int(observation.window)
        notional = float(trade_usdt) * int(leverage)
        if price <= 0 or notional <= 0:
            return None, "INVALID_TRADE_INPUT", {"price": price, "notional": notional}

        horizon_rows = profile.get("horizons", {}).get(side, {})
        if not horizon_rows:
            return None, "NO_HISTORICAL_OUTCOME_SAMPLES", {}

        min_profit_tp_pct = self._minimum_profit_tp_pct(price, side, notional)
        move_threshold = float(observation.metrics.get("move_threshold_pct") or 0.0)
        support_tool = str(observation.metrics.get("support_tool") or "RANGE").upper()
        learning_adjustments = learning_adjustments or {}
        accepted: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []

        for horizon in config.HORIZONS_MINUTES:
            stats = horizon_rows.get(str(horizon)) or {}
            samples = int(stats.get("samples") or 0)
            if samples <= 0:
                continue
            adjustment = learning_adjustments.get(str(horizon)) or {}
            factors = normalize_learning_factors(adjustment.get("factors"))
            learning_status = str(adjustment.get("status") or "BASE").upper()
            pattern_key = str(
                adjustment.get("pattern_key")
                or learning_pattern_key(symbol.id, side, window, support_tool, horizon)
            )
            quarantined = learning_status == "QUARANTINED"

            move_ratio = float(observation.metrics.get("move_ratio") or 0.0)
            directionality = float(observation.metrics.get("directionality") or 0.0)
            support_ratio = (
                float(observation.metrics.get("volume_ratio") or 0.0)
                if support_tool == "VOLUME"
                else float(observation.metrics.get("range_ratio") or 0.0)
            )
            support_factor = factors["volume_factor"] if support_tool == "VOLUME" else factors["range_factor"]
            directionality_required = config.TRIGGER_MIN_DIRECTIONALITY * factors["directionality_factor"]
            if (
                move_ratio + 1e-9 < factors["price_factor"]
                or directionality + 1e-9 < directionality_required
                or support_ratio + 1e-9 < support_factor
            ):
                rejected.append({
                    "horizon": horizon,
                    "reason": "LEARNING_PATTERN_FILTER",
                    "hard_capacity_pct": float(stats.get("mfe_q60") or stats.get("mfe_q50") or 0.0),
                    "required_tp_pct": 999.0,
                    "pattern_key": pattern_key,
                    "learning_version": int(adjustment.get("version") or 0),
                    "move_ratio": move_ratio,
                    "move_required": factors["price_factor"],
                    "directionality": directionality,
                    "directionality_required": directionality_required,
                    "support_ratio": support_ratio,
                    "support_required": support_factor,
                })
                continue

            block, outcome_source = self._outcome_block(profile, side, window, support_tool, horizon)
            outcome_rows = list((block or {}).get("candidates") or [])
            if outcome_rows:
                min_tp = min(float(row["tp_pct"]) for row in outcome_rows)
                max_tp = max(float(row["tp_pct"]) for row in outcome_rows)
                min_sl = min(float(row["sl_pct"]) for row in outcome_rows)
                max_sl = max(float(row["sl_pct"]) for row in outcome_rows)
                for row in outcome_rows:
                    tp_pct = float(row["tp_pct"])
                    sl_pct = float(row["sl_pct"])
                    if tp_pct + 1e-9 < max(sl_pct * config.RISK_REWARD_MIN, min_profit_tp_pct):
                        continue
                    if sl_pct < config.MIN_STOP_PCT or sl_pct > config.MAX_STOP_PCT:
                        continue
                    valid_samples = int(row.get("valid_samples") or 0)
                    if valid_samples <= 0:
                        continue
                    tp = self._price(price, side, tp_pct, favorable=True)
                    sl = self._price(price, side, sl_pct, favorable=False)
                    tp_gross, tp_costs, tp_net = self.pnl_for_exit(price, tp, side, notional)
                    sl_gross, sl_costs, sl_net = self.pnl_for_exit(price, sl, side, notional)
                    if tp_net < config.MIN_NET_PROFIT_USDT:
                        continue
                    total_non_ambiguous = max(
                        1,
                        int(row.get("tp_first") or 0)
                        + int(row.get("sl_first") or 0)
                        + int(row.get("no_hit") or 0),
                    )
                    expected_net = (
                        int(row.get("tp_first") or 0) * tp_net
                        - int(row.get("sl_first") or 0) * abs(sl_net)
                    ) / total_non_ambiguous
                    conservative = float(row.get("conservative_win_rate") or 0.0)
                    confident = valid_samples >= config.OUTCOME_MIN_VALID_SAMPLES
                    if quarantined and (
                        outcome_source == "LEGACY"
                        or valid_samples < config.OUTCOME_MIN_EXACT_SAMPLES
                        or conservative < max(0.55, config.OUTCOME_MIN_CONSERVATIVE_WIN_RATE + 0.07)
                    ):
                        continue
                    if confident and conservative < config.OUTCOME_MIN_CONSERVATIVE_WIN_RATE:
                        rejected.append({
                            "horizon": horizon,
                            "reason": "HISTORICAL_TP_FIRST_TOO_WEAK",
                            "hard_capacity_pct": tp_pct,
                            "required_tp_pct": tp_pct,
                            "historical_win_rate": float(row.get("win_rate") or 0.0),
                            "conservative_win_rate": conservative,
                            "outcome_valid_samples": valid_samples,
                        })
                        continue

                    tp_position = (tp_pct - min_tp) / max(max_tp - min_tp, 1e-9)
                    sl_position = (sl_pct - min_sl) / max(max_sl - min_sl, 1e-9)
                    tp_bias = (factors["tp_factor"] - 1.0) * tp_position
                    sl_bias = (factors["sl_factor"] - 1.0) * sl_position
                    quality = float(row.get("quality_score") or 0.0) + 0.20 * (tp_bias + sl_bias)
                    quality += 0.08 * max(-1.0, min(1.0, expected_net / max(abs(sl_net), 1e-9)))
                    accepted.append({
                        "horizon": horizon,
                        "samples": int((block or {}).get("samples") or samples),
                        "tp_pct": tp_pct,
                        "sl_pct": sl_pct,
                        "tp": tp,
                        "sl": sl,
                        "tp_gross": tp_gross,
                        "tp_costs": tp_costs,
                        "tp_net": tp_net,
                        "sl_gross": sl_gross,
                        "sl_costs": sl_costs,
                        "sl_net_loss": abs(sl_net),
                        "rr": tp_pct / sl_pct,
                        "pattern_key": pattern_key,
                        "support_tool": support_tool,
                        "learning_version": int(adjustment.get("version") or 0),
                        "learning_status": learning_status,
                        "learning_factors": factors,
                        "outcome_source": outcome_source,
                        "outcome_valid_samples": valid_samples,
                        "historical_win_rate": float(row.get("win_rate") or 0.0),
                        "conservative_win_rate": conservative,
                        "historical_expectancy_pct": float(row.get("expectancy_pct") or 0.0),
                        "expected_net": expected_net,
                        "quality_score": quality,
                        "tp_first": int(row.get("tp_first") or 0),
                        "sl_first": int(row.get("sl_first") or 0),
                        "no_hit": int(row.get("no_hit") or 0),
                        "ambiguous": int(row.get("ambiguous") or 0),
                        "time_to_mfe_median": float(stats.get("time_to_mfe_median") or horizon),
                    })
                continue

            # Safe compatibility fallback while old/marginal profiles are being rebuilt.
            if quarantined:
                rejected.append({
                    "horizon": horizon,
                    "reason": "LEARNING_PATTERN_QUARANTINED_NO_STRONG_OUTCOME",
                    "pattern_key": pattern_key,
                    "required_tp_pct": 999.0,
                    "hard_capacity_pct": 0.0,
                })
                continue
            mfe_q40 = float(stats.get("mfe_q40") or stats.get("mfe_q50") or 0.0) * 0.92
            mfe_q60 = float(stats.get("mfe_q60") or stats.get("mfe_q50") or 0.0)
            mae_to_mfe = self._behavior_mae_to_mfe(stats)
            sl_pct = max(
                config.MIN_STOP_PCT,
                mae_to_mfe * config.STOP_BEHAVIOR_BUFFER * factors["sl_factor"],
                move_threshold * 0.55,
            )
            required_tp_pct = max(sl_pct * config.RISK_REWARD_MIN, min_profit_tp_pct)
            preferred = mfe_q40 * config.TP_BEHAVIOR_TARGET_FRACTION * factors["tp_factor"]
            hard = mfe_q60 * config.TP_BEHAVIOR_HARD_FRACTION
            if sl_pct > config.MAX_STOP_PCT or required_tp_pct > hard or hard <= 0:
                rejected.append({
                    "horizon": horizon,
                    "reason": "BEHAVIOR_CAPACITY_INSUFFICIENT_FOR_TP",
                    "sl_pct": sl_pct,
                    "required_tp_pct": required_tp_pct,
                    "hard_capacity_pct": hard,
                })
                continue
            tp_pct = min(hard, max(required_tp_pct, preferred))
            tp = self._price(price, side, tp_pct, favorable=True)
            sl = self._price(price, side, sl_pct, favorable=False)
            tp_gross, tp_costs, tp_net = self.pnl_for_exit(price, tp, side, notional)
            sl_gross, sl_costs, sl_net = self.pnl_for_exit(price, sl, side, notional)
            if tp_net < config.MIN_NET_PROFIT_USDT:
                continue
            accepted.append({
                "horizon": horizon,
                "samples": samples,
                "tp_pct": tp_pct,
                "sl_pct": sl_pct,
                "tp": tp,
                "sl": sl,
                "tp_gross": tp_gross,
                "tp_costs": tp_costs,
                "tp_net": tp_net,
                "sl_gross": sl_gross,
                "sl_costs": sl_costs,
                "sl_net_loss": abs(sl_net),
                "rr": tp_pct / sl_pct,
                "pattern_key": pattern_key,
                "support_tool": support_tool,
                "learning_version": int(adjustment.get("version") or 0),
                "learning_status": learning_status,
                "learning_factors": factors,
                "outcome_source": "LEGACY",
                "outcome_valid_samples": 0,
                "historical_win_rate": 0.0,
                "conservative_win_rate": 0.0,
                "historical_expectancy_pct": 0.0,
                "expected_net": 0.0,
                "quality_score": -0.10 - horizon / 10_000.0,
                "tp_first": 0,
                "sl_first": 0,
                "no_hit": 0,
                "ambiguous": 0,
                "time_to_mfe_median": float(stats.get("time_to_mfe_median") or horizon),
            })

        if not accepted:
            if not rejected:
                return None, "NO_HISTORICAL_OUTCOME_SAMPLES", {"notional": notional}
            closest = max(
                rejected,
                key=lambda item: float(item.get("hard_capacity_pct") or 0.0)
                / max(float(item.get("required_tp_pct") or 999.0), 1e-9),
            )
            return None, str(closest.get("reason") or "NO_FEASIBLE_HORIZON"), {
                **observation.metrics,
                **closest,
                "min_profit_tp_pct": min_profit_tp_pct,
                "notional": notional,
            }

        chosen = max(
            accepted,
            key=lambda item: (
                float(item["quality_score"]),
                float(item["expected_net"]),
                float(item["conservative_win_rate"]),
                -int(item["horizon"]),
            ),
        )
        metrics: dict[str, Any] = {
            **observation.metrics,
            "expected_minutes": int(chosen["horizon"]),
            "samples": int(chosen["samples"]),
            "outcome_source": str(chosen["outcome_source"]),
            "outcome_valid_samples": int(chosen["outcome_valid_samples"]),
            "historical_win_rate": float(chosen["historical_win_rate"]),
            "conservative_win_rate": float(chosen["conservative_win_rate"]),
            "historical_expectancy_pct": float(chosen["historical_expectancy_pct"]),
            "expected_net_from_outcomes": float(chosen["expected_net"]),
            "tp_first": int(chosen["tp_first"]),
            "sl_first": int(chosen["sl_first"]),
            "no_hit": int(chosen["no_hit"]),
            "ambiguous": int(chosen["ambiguous"]),
            "tp_pct": float(chosen["tp_pct"]),
            "sl_pct": float(chosen["sl_pct"]),
            "rr": float(chosen["rr"]),
            "tp_net_usdt": float(chosen["tp_net"]),
            "sl_net_loss_usdt": float(chosen["sl_net_loss"]),
            "plan_quality": "OUTCOME" if chosen["outcome_source"] != "LEGACY" else "LEGACY",
            "notional": notional,
            "support_tool": str(chosen["support_tool"]),
            "pattern_key": str(chosen["pattern_key"]),
            "learning_version": int(chosen["learning_version"]),
            "learning_status": str(chosen["learning_status"]),
            "learning_factors": dict(chosen["learning_factors"]),
        }
        plan = TradePlan(
            symbol_id=symbol.id,
            okx_symbol=symbol.okx,
            toobit_symbol=symbol.toobit,
            side=side,
            entry=price,
            tp=float(chosen["tp"]),
            sl=float(chosen["sl"]),
            tp_pct=float(chosen["tp_pct"]),
            sl_pct=float(chosen["sl_pct"]),
            rr_net=float(chosen["rr"]),
            expected_minutes=int(chosen["horizon"]),
            trigger_window=window,
            trigger_reason=observation.reason,
            notional=notional,
            estimated_tp_gross=float(chosen["tp_gross"]),
            estimated_tp_costs=float(chosen["tp_costs"]),
            estimated_tp_net=float(chosen["tp_net"]),
            estimated_sl_gross_loss=abs(float(chosen["sl_gross"])),
            estimated_sl_costs=float(chosen["sl_costs"]),
            estimated_sl_net_loss=float(chosen["sl_net_loss"]),
            metrics=metrics,
        )
        return plan, "PLAN_READY", metrics


class AdaptiveLearningManager:
    """Multi-parameter, two-sided learning with measured trials and rollback."""

    def __init__(self, storage: Any) -> None:
        self.storage = storage
        self._gate_cache: dict[str, tuple[float, dict[str, list[dict[str, Any]]]]] = {}
        self._cache_lock = threading.RLock()

    def _invalidate_symbol(self, symbol_id: str) -> None:
        with self._cache_lock:
            self._gate_cache.pop(str(symbol_id).upper(), None)

    def gates_for_symbol(self, symbol_id: str) -> dict[str, list[dict[str, Any]]]:
        if not config.LEARNING_ENABLED:
            return {}
        symbol_id = str(symbol_id).upper()
        now = time.time()
        with self._cache_lock:
            cached = self._gate_cache.get(symbol_id)
            if cached and now - cached[0] <= config.LEARNING_GATE_CACHE_SECONDS:
                return cached[1]
        mapping: dict[str, list[dict[str, Any]]] = {}
        for row in self.storage.get_symbol_learning_gates(symbol_id):
            key = f"{str(row['side']).upper()}|{int(row['trigger_window'])}|{str(row['support_tool']).upper()}"
            mapping.setdefault(key, []).append({
                "horizon": int(row["horizon"]),
                "version": int(row["version"]),
                "status": str(row["status"]),
                "factors": normalize_learning_factors(row.get("factors")),
            })
        with self._cache_lock:
            self._gate_cache[symbol_id] = (now, mapping)
        return mapping

    @staticmethod
    def _raw(signal: dict[str, Any]) -> dict[str, Any]:
        value = signal.get("raw_json") or signal.get("raw") or {}
        if isinstance(value, dict):
            return dict(value)
        try:
            parsed = json.loads(str(value))
            return dict(parsed) if isinstance(parsed, dict) else {}
        except (TypeError, json.JSONDecodeError):
            return {}

    @staticmethod
    def _clamp(variable: str, value: float) -> float:
        low, high = config.LEARNING_FACTOR_BOUNDS[variable]
        return max(float(low), min(float(high), float(value)))

    def _identity(self, signal: dict[str, Any]) -> dict[str, Any]:
        raw = self._raw(signal)
        support_tool = str(raw.get("support_tool") or "RANGE").upper()
        horizon = int(signal.get("expected_minutes") or raw.get("expected_minutes") or 5)
        pattern_key = str(
            raw.get("pattern_key")
            or learning_pattern_key(
                str(signal["symbol_id"]), str(signal["side"]),
                int(signal["trigger_window"]), support_tool, horizon,
            )
        )
        return {
            "pattern_key": pattern_key,
            "symbol_id": str(signal["symbol_id"]).upper(),
            "side": str(signal["side"]).upper(),
            "trigger_window": int(signal["trigger_window"]),
            "support_tool": support_tool,
            "horizon": horizon,
            "raw": raw,
        }

    def adjustments_for(
        self, symbol_id: str, side: str, trigger_window: int, support_tool: str
    ) -> dict[str, dict[str, Any]]:
        if not config.LEARNING_ENABLED:
            return {}
        return self.storage.get_learning_adjustments(symbol_id, side, trigger_window, support_tool)

    def _base_pattern(self, identity: dict[str, Any]) -> dict[str, Any]:
        existing = self.storage.get_learning_pattern(identity["pattern_key"])
        if existing:
            existing["factors"] = normalize_learning_factors(existing.get("factors"))
            existing["best_factors"] = normalize_learning_factors(existing.get("best_factors"))
            existing["previous_factors"] = normalize_learning_factors(existing.get("previous_factors"))
            existing["stats"] = dict(existing.get("stats") or {})
            if str(existing.get("status")) == "ACCEPTED":
                existing["status"] = "TRUSTED"
            return existing
        factors = normalize_learning_factors()
        return {
            **{key: identity[key] for key in (
                "pattern_key", "symbol_id", "side", "trigger_window", "support_tool", "horizon"
            )},
            "factors": dict(factors),
            "best_factors": dict(factors),
            "previous_factors": dict(factors),
            "version": 0,
            "status": "BASE",
            "active_variable": None,
            "active_reason": None,
            "source_signal_id": None,
            "trial_tp": 0,
            "trial_sl": 0,
            "total_tp": 0,
            "total_sl": 0,
            "consecutive_tp": 0,
            "consecutive_sl": 0,
            "failed_trials": 0,
            "stats": {},
        }

    @staticmethod
    def _signal_ratios(signal: dict[str, Any], raw: dict[str, Any]) -> dict[str, float]:
        tp_pct = max(float(signal.get("tp_pct") or raw.get("tp_pct") or 0.0), 1e-9)
        mfe = max(0.0, float(signal.get("mfe_pct") or 0.0))
        started = int(signal.get("opened_at") or signal.get("created_at") or time.time())
        closed = int(signal.get("closed_at") or time.time())
        horizon_seconds = max(60, int(signal.get("expected_minutes") or 5) * 60)
        return {
            "mfe_ratio": mfe / tp_pct,
            "elapsed_ratio": max(0.0, closed - started) / horizon_seconds,
            "move_ratio": float(raw.get("move_ratio") or 0.0),
            "range_ratio": float(raw.get("range_ratio") or 0.0),
            "volume_ratio": float(raw.get("volume_ratio") or 0.0),
            "directionality": float(raw.get("directionality") or 0.0),
            "acceleration": float(raw.get("acceleration") or 0.0),
        }

    def _diagnose(
        self, signal: dict[str, Any], final_review: dict[str, Any] | None = None
    ) -> tuple[str, list[tuple[str, int]]]:
        identity = self._identity(signal)
        raw = identity["raw"]
        ratios = self._signal_ratios(signal, raw)
        support = identity["support_tool"]
        if final_review:
            tp_pct = max(float(signal.get("tp_pct") or 0.0), 1e-9)
            sl_pct = max(float(signal.get("sl_pct") or 0.0), 1e-9)
            post_favorable_ratio = float(final_review.get("max_favorable_pct") or 0.0) / tp_pct
            post_adverse_ratio = float(final_review.get("max_adverse_pct") or 0.0) / sl_pct
            if int(final_review.get("hit_original_tp") or 0):
                return "NOISE_STOP_THEN_TP", [("sl_factor", +1), ("tp_factor", -1)]
            if post_favorable_ratio >= config.LEARNING_NEAR_TP_RATIO:
                return "POST_STOP_NEAR_TP", [("tp_factor", -1), ("sl_factor", +1)]
            if post_adverse_ratio >= 1.40 and post_favorable_ratio <= config.LEARNING_LOW_MFE_RATIO:
                variable = "range_factor" if support == "RANGE" else "volume_factor"
                return f"WRONG_{support}_ENTRY_CONTINUED", [(variable, +1), ("price_factor", -1)]
        if ratios["mfe_ratio"] >= config.LEARNING_NEAR_TP_RATIO:
            return "TP_TOO_FAR", [("tp_factor", -1), ("sl_factor", +1)]
        if (
            ratios["move_ratio"] >= config.LEARNING_LATE_ENTRY_MOVE_RATIO
            and ratios["acceleration"] <= 0.03
            and ratios["elapsed_ratio"] <= config.LEARNING_FAST_STOP_RATIO
        ):
            return "ENTRY_TOO_LATE", [("price_factor", -1), ("directionality_factor", +1)]
        if ratios["elapsed_ratio"] <= config.LEARNING_FAST_STOP_RATIO and ratios["mfe_ratio"] <= config.LEARNING_LOW_MFE_RATIO:
            if support == "RANGE" and ratios["range_ratio"] <= 1.35:
                return "WEAK_RANGE_CONFIRMATION", [("range_factor", +1), ("price_factor", -1)]
            if support == "VOLUME" and ratios["volume_ratio"] <= 1.35:
                return "WEAK_VOLUME_CONFIRMATION", [("volume_factor", +1), ("price_factor", -1)]
            if ratios["directionality"] <= config.TRIGGER_MIN_DIRECTIONALITY * 1.20:
                return "WEAK_DIRECTIONALITY", [("directionality_factor", +1), ("price_factor", -1)]
            return "PRICE_TRIGGER_TOO_SOFT", [("price_factor", +1), ("directionality_factor", +1)]
        if ratios["mfe_ratio"] >= 0.35:
            return "PARTIAL_MOVE_THEN_REVERSAL", [("tp_factor", -1), ("sl_factor", +1)]
        variable = "range_factor" if support == "RANGE" else "volume_factor"
        return f"{support}_PATTERN_UNRELIABLE", [(variable, +1), ("price_factor", +1)]

    def _apply_change(
        self,
        pattern: dict[str, Any],
        signal_id: int,
        diagnosis: str,
        candidates: list[tuple[str, int]],
        *,
        details: dict[str, Any] | None = None,
        stronger: bool = False,
    ) -> dict[str, Any]:
        if str(pattern.get("status")) == "TRIAL":
            return pattern
        base = dict(pattern.get("best_factors") or pattern.get("factors") or DEFAULT_LEARNING_FACTORS)
        step = config.LEARNING_SECOND_STEP if stronger else config.LEARNING_FIRST_STEP
        changes: list[tuple[str, float, float]] = []
        new_factors = dict(base)
        seen: set[str] = set()
        for variable, direction in candidates:
            if variable in seen or variable not in config.LEARNING_FACTOR_BOUNDS:
                continue
            seen.add(variable)
            old_value = float(new_factors[variable])
            new_value = self._clamp(variable, old_value + int(direction) * step)
            if abs(new_value - old_value) < 1e-12:
                continue
            new_factors[variable] = new_value
            changes.append((variable, old_value, new_value))
            if len(changes) >= config.LEARNING_MAX_SIMULTANEOUS_CHANGES:
                break
        if not changes:
            return pattern

        baseline = self.storage.learning_outcome_stats(pattern["pattern_key"], limit=100)
        version = int(pattern.get("version") or 0) + 1
        pattern.update({
            "previous_factors": dict(base),
            "factors": new_factors,
            "version": version,
            "status": "TRIAL",
            "active_variable": ",".join(change[0] for change in changes),
            "active_reason": diagnosis,
            "source_signal_id": int(signal_id),
            "trial_tp": 0,
            "trial_sl": 0,
            "stats": {
                **dict(pattern.get("stats") or {}),
                "baseline": baseline,
                "trial_started_at": int(time.time()),
                "trial_changes": [
                    {"variable": variable, "old": old, "new": new}
                    for variable, old, new in changes
                ],
            },
        })
        self.storage.save_learning_pattern(pattern)
        self._invalidate_symbol(pattern["symbol_id"])
        for variable, old_value, new_value in changes:
            self.storage.add_learning_change(
                pattern["pattern_key"], signal_id, version, variable, old_value, new_value,
                diagnosis, "TRIAL", details,
            )
        logger.info(
            "[LEARNING_CHANGE] pattern=%s version=%d cause=%s changes=%s",
            pattern["pattern_key"], version, diagnosis,
            ",".join(f"{v}:{o:.3f}->{n:.3f}" for v, o, n in changes),
        )
        return pattern

    def _evaluate_trial(self, pattern: dict[str, Any]) -> dict[str, Any]:
        version = int(pattern.get("version") or 0)
        trial = self.storage.learning_outcome_stats(pattern["pattern_key"], version=version, limit=100)
        baseline = dict((pattern.get("stats") or {}).get("baseline") or {})
        samples = int(trial.get("samples") or 0)
        pattern["trial_tp"] = int(trial.get("tp") or 0)
        pattern["trial_sl"] = int(trial.get("sl") or 0)
        if samples < config.LEARNING_TRIAL_MIN_SAMPLES:
            self.storage.save_learning_pattern(pattern)
            return pattern

        baseline_samples = int(baseline.get("samples") or 0)
        baseline_win = float(baseline.get("win_rate") or 0.0)
        minimum_win = config.LEARNING_ACCEPT_MIN_WIN_RATE
        if baseline_samples >= config.LEARNING_TRIAL_MIN_SAMPLES:
            minimum_win = max(
                minimum_win,
                baseline_win - config.LEARNING_ACCEPT_ALLOWED_WINRATE_DROP,
            )
        acceptable = (
            int(trial.get("tp") or 0) >= config.LEARNING_TRIAL_MIN_WINS
            and float(trial.get("win_rate") or 0.0) >= minimum_win
            and float(trial.get("expectancy") or 0.0) > config.LEARNING_ACCEPT_MIN_EXPECTANCY_USDT
        )
        final_enough = samples >= config.LEARNING_TRIAL_MAX_SAMPLES
        early_failure = (
            samples >= config.LEARNING_TRIAL_MIN_SAMPLES
            and int(trial.get("sl") or 0) >= 4
            and float(trial.get("win_rate") or 0.0) < 0.40
        )
        if acceptable:
            pattern["best_factors"] = dict(pattern["factors"])
            pattern["status"] = "TRUSTED"
            pattern["stats"] = {
                **dict(pattern.get("stats") or {}),
                "last_trial": trial,
                "accepted_at": int(time.time()),
            }
            self.storage.resolve_learning_changes(pattern["pattern_key"], version, "ACCEPTED")
            logger.info("[LEARNING_ACCEPT] pattern=%s version=%d stats=%s", pattern["pattern_key"], version, trial)
        elif final_enough or early_failure:
            pattern["factors"] = dict(pattern.get("previous_factors") or pattern.get("best_factors"))
            pattern["failed_trials"] = int(pattern.get("failed_trials") or 0) + 1
            pattern["status"] = "TRUSTED" if baseline_samples else "BASE"
            pattern["stats"] = {
                **dict(pattern.get("stats") or {}),
                "last_failed_trial": trial,
                "rolled_back_at": int(time.time()),
            }
            self.storage.resolve_learning_changes(pattern["pattern_key"], version, "ROLLED_BACK")
            logger.info("[LEARNING_ROLLBACK] pattern=%s version=%d stats=%s", pattern["pattern_key"], version, trial)
        self.storage.save_learning_pattern(pattern)
        self._invalidate_symbol(pattern["symbol_id"])
        return pattern

    def _record_result(self, signal: dict[str, Any], result: str) -> tuple[dict[str, Any], dict[str, Any]]:
        identity = self._identity(signal)
        pattern = self._base_pattern(identity)
        self.storage.add_learning_outcome(
            int(signal["id"]), pattern["pattern_key"], int(pattern.get("version") or 0),
            result, float(signal.get("net_pnl") or 0.0), valid=result in ("TP", "SL"),
        )
        if result == "TP":
            pattern["total_tp"] = int(pattern.get("total_tp") or 0) + 1
            pattern["consecutive_tp"] = int(pattern.get("consecutive_tp") or 0) + 1
            pattern["consecutive_sl"] = 0
        elif result == "SL":
            pattern["total_sl"] = int(pattern.get("total_sl") or 0) + 1
            pattern["consecutive_sl"] = int(pattern.get("consecutive_sl") or 0) + 1
            pattern["consecutive_tp"] = 0
        self.storage.save_learning_pattern(pattern)
        return identity, pattern

    def on_tp(self, signal: dict[str, Any]) -> None:
        if not config.LEARNING_ENABLED:
            return
        _, pattern = self._record_result(signal, "TP")
        if str(pattern.get("status")) == "TRIAL":
            self._evaluate_trial(pattern)
        else:
            overall = self.storage.learning_outcome_stats(pattern["pattern_key"], limit=100)
            if (
                str(pattern.get("status")) == "QUARANTINED"
                and int(overall.get("samples") or 0) >= config.LEARNING_RECOVERY_MIN_SAMPLES
                and float(overall.get("win_rate") or 0.0) >= config.LEARNING_RECOVERY_MIN_WIN_RATE
            ):
                pattern["status"] = "TRUSTED"
                self.storage.save_learning_pattern(pattern)
                self._invalidate_symbol(pattern["symbol_id"])

    def on_sl(self, signal: dict[str, Any]) -> None:
        if not config.LEARNING_ENABLED:
            return
        identity, pattern = self._record_result(signal, "SL")
        if str(pattern.get("status")) == "TRIAL":
            self._evaluate_trial(pattern)
        else:
            diagnosis, candidates = self._diagnose(signal)
            details = {**self._signal_ratios(signal, identity["raw"]), "support_tool": identity["support_tool"]}
            self._apply_change(
                pattern, int(signal["id"]), diagnosis, candidates,
                details=details,
                stronger=int(pattern.get("consecutive_sl") or 0) >= 2,
            )

        overall = self.storage.learning_outcome_stats(pattern["pattern_key"], limit=100)
        if (
            str(pattern.get("status")) != "TRIAL"
            and int(overall.get("samples") or 0) >= config.LEARNING_QUARANTINE_MIN_SAMPLES
            and float(overall.get("win_rate") or 0.0) <= config.LEARNING_QUARANTINE_MAX_WIN_RATE
        ):
            pattern["status"] = "QUARANTINED"
            pattern["stats"] = {**dict(pattern.get("stats") or {}), "quarantine_stats": overall}
            self.storage.save_learning_pattern(pattern)
            self._invalidate_symbol(pattern["symbol_id"])

        started = int(signal.get("opened_at") or signal.get("created_at") or time.time())
        sl_at = int(signal.get("closed_at") or time.time())
        natural_expiry = started + int(signal.get("expected_minutes") or 5) * 60
        expires = max(sl_at + config.LEARNING_REVIEW_MIN_SECONDS, natural_expiry)
        expires = min(expires, sl_at + config.LEARNING_REVIEW_MAX_SECONDS)
        self.storage.start_learning_review({
            "signal_id": int(signal["id"]),
            "pattern_key": identity["pattern_key"],
            "symbol_id": identity["symbol_id"],
            "side": identity["side"],
            "entry": float(signal.get("entry_real") or signal["entry"]),
            "tp": float(signal["tp"]),
            "sl": float(signal["sl"]),
            "tp_pct": float(signal["tp_pct"]),
            "sl_pct": float(signal["sl_pct"]),
            "started_at": started,
            "sl_at": sl_at,
            "expires_at": expires,
            "max_favorable_pct": float(signal.get("mfe_pct") or 0.0),
            "max_adverse_pct": float(signal.get("mae_pct") or 0.0),
        })

    def result_note(self, signal: dict[str, Any]) -> str:
        if not config.LEARNING_ENABLED:
            return "یادگیری خاموش"
        identity = self._identity(signal)
        pattern = self.storage.get_learning_pattern(identity["pattern_key"])
        if not pattern:
            return "حافظه پایه"
        variables = str(pattern.get("active_variable") or "-").replace("_factor", "")
        reason = str(pattern.get("active_reason") or "-")
        return (
            f"v{int(pattern.get('version') or 0)} | {pattern.get('status') or 'BASE'} | "
            f"علت={reason} | متغیرها={variables} | "
            f"آزمایش={int(pattern.get('trial_tp') or 0)}TP/{int(pattern.get('trial_sl') or 0)}SL"
        )

    @staticmethod
    def _review_excursion(review: dict[str, Any], price: float) -> tuple[float, float, bool]:
        entry = float(review["entry"])
        if entry <= 0:
            return 0.0, 0.0, False
        if str(review["side"]) == "LONG":
            favorable = max(0.0, (price - entry) / entry * 100.0)
            adverse = max(0.0, (entry - price) / entry * 100.0)
            hit_tp = price >= float(review["tp"])
        else:
            favorable = max(0.0, (entry - price) / entry * 100.0)
            adverse = max(0.0, (price - entry) / entry * 100.0)
            hit_tp = price <= float(review["tp"])
        return favorable, adverse, hit_tp

    def process_reviews(self, symbol_id: str, price: float, reviews: list[dict[str, Any]] | None = None) -> None:
        if not config.LEARNING_ENABLED:
            return
        now = int(time.time())
        for review in reviews if reviews is not None else self.storage.get_open_learning_reviews():
            if str(review["symbol_id"]) != str(symbol_id):
                continue
            favorable, adverse, hit_tp = self._review_excursion(review, price)
            max_favorable = max(float(review.get("max_favorable_pct") or 0.0), favorable)
            max_adverse = max(float(review.get("max_adverse_pct") or 0.0), adverse)
            should_finalize = hit_tp or now >= int(review["expires_at"])
            self.storage.update_learning_review(
                int(review["signal_id"]),
                max_favorable_pct=max_favorable,
                max_adverse_pct=max_adverse,
                hit_original_tp=int(hit_tp or int(review.get("hit_original_tp") or 0)),
                finalized=int(should_finalize),
            )
            if not should_finalize:
                continue
            signal = self.storage.get_signal(int(review["signal_id"]))
            if not signal:
                continue
            final_review = dict(review)
            final_review.update({
                "max_favorable_pct": max_favorable,
                "max_adverse_pct": max_adverse,
                "hit_original_tp": int(hit_tp or int(review.get("hit_original_tp") or 0)),
            })
            diagnosis, candidates = self._diagnose(signal, final_review)
            self.storage.update_learning_review(int(review["signal_id"]), diagnosis=diagnosis, finalized=1)
            pattern = self.storage.get_learning_pattern(str(review["pattern_key"]))
            if (
                pattern
                and int(pattern.get("source_signal_id") or 0) == int(review["signal_id"])
                and str(pattern.get("status")) == "TRIAL"
                and str(pattern.get("active_reason") or "") != diagnosis
            ):
                failed_version = int(pattern.get("version") or 0)
                self.storage.resolve_learning_changes(pattern["pattern_key"], failed_version, "REVISED")
                pattern["factors"] = dict(pattern.get("previous_factors") or pattern.get("best_factors"))
                pattern["status"] = "TRUSTED" if int(pattern.get("total_tp") or 0) else "BASE"
                self.storage.save_learning_pattern(pattern)
                self._apply_change(
                    pattern, int(review["signal_id"]), diagnosis, candidates,
                    details={
                        "post_stop_review": True,
                        "max_favorable_pct": max_favorable,
                        "max_adverse_pct": max_adverse,
                        "hit_original_tp": bool(final_review["hit_original_tp"]),
                    },
                    stronger=False,
                )
            logger.info(
                "[LEARNING_REVIEW] pattern=%s signal=%s cause=%s hit_tp=%s mfe=%.4f mae=%.4f",
                review["pattern_key"], review["signal_id"], diagnosis,
                bool(final_review["hit_original_tp"]), max_favorable, max_adverse,
            )

