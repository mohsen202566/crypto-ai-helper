"""ساخت پروفایل رفتاری غلتان هفت‌روزه برای هر ارز."""
from __future__ import annotations

from dataclasses import dataclass
from math import sqrt
from statistics import median
from typing import Any, Iterable
import logging
import time

import config
from okx_client import OKXClient
from storage import Storage

logger = logging.getLogger("adaptive_bot.profiles")


@dataclass(frozen=True)
class SymbolSpec:
    base: str
    okx: str
    toobit: str

    @property
    def id(self) -> str:
        return self.base.upper()


def quantile(values: Iterable[float], q: float, default: float = 0.0) -> float:
    clean = sorted(float(value) for value in values if value is not None)
    if not clean:
        return default
    if len(clean) == 1:
        return clean[0]
    q = max(0.0, min(1.0, float(q)))
    pos = (len(clean) - 1) * q
    low = int(pos)
    high = min(low + 1, len(clean) - 1)
    frac = pos - low
    return clean[low] * (1.0 - frac) + clean[high] * frac


def _pct(a: float, b: float) -> float:
    return abs(b - a) / a * 100.0 if a > 0 else 0.0


def _quote_volume(candle: dict[str, Any]) -> float:
    quote = float(candle.get("vol_quote") or 0.0)
    if quote > 0:
        return quote
    return float(candle.get("volume") or 0.0) * float(candle.get("close") or 0.0)


def _wilson_lower(wins: int, total: int, z: float = 1.645) -> float:
    """One-sided conservative win-rate estimate (roughly 90% confidence)."""
    if total <= 0:
        return 0.0
    p = wins / total
    z2 = z * z
    denominator = 1.0 + z2 / total
    centre = p + z2 / (2.0 * total)
    margin = z * ((p * (1.0 - p) / total + z2 / (4.0 * total * total)) ** 0.5)
    return max(0.0, (centre - margin) / denominator)


def _first_hit(
    entry: float,
    side: str,
    future: list[dict[str, Any]],
    tp_pct: float,
    sl_pct: float,
) -> tuple[str, float]:
    """Return TP_FIRST/SL_FIRST/AMBIGUOUS/NO_HIT and horizon-close PnL percent."""
    if entry <= 0 or not future:
        return "NO_HIT", 0.0
    if side == "LONG":
        tp_price = entry * (1.0 + tp_pct / 100.0)
        sl_price = entry * (1.0 - sl_pct / 100.0)
    else:
        tp_price = entry * (1.0 - tp_pct / 100.0)
        sl_price = entry * (1.0 + sl_pct / 100.0)
    for candle in future:
        high = float(candle["high"])
        low = float(candle["low"])
        tp_hit = high >= tp_price if side == "LONG" else low <= tp_price
        sl_hit = low <= sl_price if side == "LONG" else high >= sl_price
        if tp_hit and sl_hit:
            return "AMBIGUOUS", 0.0
        if tp_hit:
            return "TP_FIRST", tp_pct
        if sl_hit:
            return "SL_FIRST", -sl_pct
    close = float(future[-1]["close"])
    close_pct = ((close - entry) / entry * 100.0) if side == "LONG" else ((entry - close) / entry * 100.0)
    return "NO_HIT", max(-sl_pct, min(tp_pct, close_pct))


def _build_outcome_set(
    candles: list[dict[str, Any]],
    indices: list[int],
    side: str,
    horizon: int,
    move_threshold: float,
) -> dict[str, Any]:
    paths: list[tuple[float, list[dict[str, Any]], float]] = []
    mae_to_mfe_values: list[float] = []
    mfe_values: list[float] = []
    for index in indices:
        if index + horizon >= len(candles):
            continue
        entry = float(candles[index]["close"])
        future = candles[index + 1 : index + horizon + 1]
        if entry <= 0 or not future:
            continue
        if side == "LONG":
            favorable = [(float(row["high"]) - entry) / entry * 100.0 for row in future]
            adverse = [(entry - float(row["low"])) / entry * 100.0 for row in future]
        else:
            favorable = [(entry - float(row["low"])) / entry * 100.0 for row in future]
            adverse = [(float(row["high"]) - entry) / entry * 100.0 for row in future]
        best_zero_index = favorable.index(max(favorable))
        mfe = max(0.0, max(favorable))
        mae_to_mfe = max(0.0, max(adverse[: best_zero_index + 1]))
        paths.append((entry, future, float(future[-1]["close"])))
        mfe_values.append(mfe)
        mae_to_mfe_values.append(mae_to_mfe)

    if not paths:
        return {"samples": 0, "candidates": []}

    noise_floor = max(config.MIN_STOP_PCT, move_threshold * 0.55)
    sl_values: set[float] = set()
    for q in config.OUTCOME_SL_QUANTILES:
        value = max(noise_floor, quantile(mae_to_mfe_values, float(q)) * config.STOP_BEHAVIOR_BUFFER)
        if value <= config.MAX_STOP_PCT:
            sl_values.add(round(value, 6))
    if not sl_values:
        sl_values.add(round(min(config.MAX_STOP_PCT, noise_floor), 6))

    hard_mfe = max(quantile(mfe_values, 0.70), quantile(mfe_values, 0.60))
    candidates: list[dict[str, Any]] = []
    for sl_pct in sorted(sl_values):
        for rr in config.OUTCOME_RR_MULTIPLIERS:
            tp_pct = round(sl_pct * float(rr), 6)
            if hard_mfe > 0 and tp_pct > hard_mfe * 1.08:
                continue
            counts = {"TP_FIRST": 0, "SL_FIRST": 0, "NO_HIT": 0, "AMBIGUOUS": 0}
            no_hit_close_sum = 0.0
            for entry, future, _ in paths:
                result, close_pct = _first_hit(entry, side, future, tp_pct, sl_pct)
                counts[result] += 1
                if result == "NO_HIT":
                    no_hit_close_sum += close_pct
            valid = counts["TP_FIRST"] + counts["SL_FIRST"]
            resolved = valid + counts["NO_HIT"]
            total = len(paths)
            win_rate = counts["TP_FIRST"] / valid if valid else 0.0
            conservative = _wilson_lower(counts["TP_FIRST"], valid)
            expectancy_pct = (
                counts["TP_FIRST"] * tp_pct
                - counts["SL_FIRST"] * sl_pct
                + no_hit_close_sum
            ) / max(1, resolved)
            no_hit_rate = counts["NO_HIT"] / total
            ambiguous_rate = counts["AMBIGUOUS"] / total
            expectancy_r = expectancy_pct / max(sl_pct, 1e-9)
            score = (
                conservative
                + 0.10 * max(-1.0, min(1.5, expectancy_r))
                - config.OUTCOME_NO_HIT_PENALTY * no_hit_rate
                - config.OUTCOME_AMBIGUOUS_PENALTY * ambiguous_rate
            )
            candidates.append({
                "tp_pct": tp_pct,
                "sl_pct": sl_pct,
                "rr": tp_pct / sl_pct,
                "samples": total,
                "valid_samples": valid,
                "tp_first": counts["TP_FIRST"],
                "sl_first": counts["SL_FIRST"],
                "no_hit": counts["NO_HIT"],
                "ambiguous": counts["AMBIGUOUS"],
                "win_rate": win_rate,
                "conservative_win_rate": conservative,
                "expectancy_pct": expectancy_pct,
                "no_hit_rate": no_hit_rate,
                "ambiguous_rate": ambiguous_rate,
                "quality_score": score,
            })
    candidates.sort(
        key=lambda row: (
            float(row["quality_score"]),
            int(row["valid_samples"]),
            float(row["expectancy_pct"]),
        ),
        reverse=True,
    )
    return {
        "samples": len(paths),
        "mfe_q60": quantile(mfe_values, 0.60),
        "mae_to_mfe_q55": quantile(mae_to_mfe_values, 0.55),
        "candidates": candidates[: config.OUTCOME_MAX_CANDIDATES_PER_HORIZON],
    }


def build_behavior_profile(symbol: SymbolSpec, candles: list[dict[str, Any]]) -> dict[str, Any]:
    if len(candles) < config.PROFILE_MIN_CANDLES:
        raise ValueError(f"نمونه پروفایل کم است: {len(candles)}")
    candles = sorted(candles, key=lambda row: int(row["ts"]))
    bodies: list[float] = []
    ranges: list[float] = []
    volumes: list[float] = []
    directions: list[float] = []
    signed_bodies: list[float] = []

    for candle in candles:
        open_price = float(candle["open"])
        high = float(candle["high"])
        low = float(candle["low"])
        close = float(candle["close"])
        body = _pct(open_price, close)
        range_pct = (high - low) / open_price * 100.0 if open_price > 0 else 0.0
        directionality = body / range_pct if range_pct > 0 else 0.0
        bodies.append(body)
        ranges.append(range_pct)
        volumes.append(_quote_volume(candle))
        directions.append(directionality)
        signed_bodies.append((close - open_price) / open_price * 100.0 if open_price > 0 else 0.0)

    move_q = quantile(bodies, config.TRIGGER_MOVE_QUANTILE)
    range_q = quantile(ranges, config.TRIGGER_SUPPORT_QUANTILE)
    volume_q = quantile(volumes, config.TRIGGER_SUPPORT_QUANTILE)
    noise_q75 = quantile(bodies, 0.75)

    windows: dict[str, dict[str, float]] = {}
    for window in config.TRIGGER_WINDOWS_SECONDS:
        time_scale = sqrt(window / 60.0)
        volume_scale = window / 60.0
        windows[str(window)] = {
            "move_threshold_pct": max(config.MIN_WINDOW_MOVE_PCT[window], move_q * time_scale),
            "range_threshold_pct": max(config.MIN_WINDOW_RANGE_PCT[window], range_q * time_scale),
            "volume_threshold_quote": max(0.0, volume_q * volume_scale),
        }

    event_indices: dict[str, list[int]] = {"LONG": [], "SHORT": []}
    event_groups: dict[str, dict[str, dict[str, list[int]]]] = {
        side: {
            str(window): {"RANGE": [], "VOLUME": []}
            for window in config.TRIGGER_WINDOWS_SECONDS
        }
        for side in ("LONG", "SHORT")
    }
    stop = len(candles) - max(config.HORIZONS_MINUTES)
    for index in range(max(0, stop)):
        if signed_bodies[index] == 0 or directions[index] < config.PROFILE_EVENT_MIN_DIRECTIONALITY:
            continue
        side = "LONG" if signed_bodies[index] > 0 else "SHORT"
        matches: list[tuple[int, str, float]] = []
        for window in sorted(config.TRIGGER_WINDOWS_SECONDS):
            threshold = windows[str(window)]
            move_ratio = bodies[index] / max(float(threshold["move_threshold_pct"]), 1e-9)
            range_ratio = ranges[index] / max(float(threshold["range_threshold_pct"]), 1e-9)
            volume_ratio = volumes[index] / max(float(threshold["volume_threshold_quote"]), 1e-9)
            if move_ratio < 1.0:
                continue
            if range_ratio < 1.0 and volume_ratio < 1.0:
                continue
            support = "VOLUME" if volume_ratio > range_ratio else "RANGE"
            matches.append((window, support, max(range_ratio, volume_ratio)))
        if not matches:
            continue
        window, support, _ = min(matches, key=lambda row: row[0])
        event_indices[side].append(index)
        event_groups[side][str(window)][support].append(index)

    horizons: dict[str, dict[str, dict[str, float | int]]] = {"LONG": {}, "SHORT": {}}
    for side in ("LONG", "SHORT"):
        indices = event_indices[side]
        for horizon in config.HORIZONS_MINUTES:
            mfe_values: list[float] = []
            mae_to_mfe_values: list[float] = []
            time_values: list[float] = []
            for index in indices:
                if index + horizon >= len(candles):
                    continue
                entry = float(candles[index]["close"])
                future = candles[index + 1 : index + horizon + 1]
                if entry <= 0 or not future:
                    continue
                if side == "LONG":
                    favorable = [(float(row["high"]) - entry) / entry * 100.0 for row in future]
                    adverse = [(entry - float(row["low"])) / entry * 100.0 for row in future]
                else:
                    favorable = [(entry - float(row["low"])) / entry * 100.0 for row in future]
                    adverse = [(float(row["high"]) - entry) / entry * 100.0 for row in future]
                mfe = max(0.0, max(favorable))
                best_zero_index = favorable.index(max(favorable))
                mae_to_mfe = max(0.0, max(adverse[: best_zero_index + 1]))
                mfe_values.append(mfe)
                mae_to_mfe_values.append(mae_to_mfe)
                time_values.append(float(best_zero_index + 1))
            horizons[side][str(horizon)] = {
                "samples": len(mfe_values),
                "mfe_q40": quantile(mfe_values, 0.40),
                "mfe_q45": quantile(mfe_values, 0.45),
                "mfe_q50": quantile(mfe_values, 0.50),
                "mfe_q60": quantile(mfe_values, 0.60),
                "mfe_q70": quantile(mfe_values, 0.70),
                "mae_to_mfe_q40": quantile(mae_to_mfe_values, 0.40),
                "mae_to_mfe_q50": quantile(mae_to_mfe_values, 0.50),
                "mae_to_mfe_q55": quantile(mae_to_mfe_values, 0.55),
                "mae_to_mfe_q60": quantile(mae_to_mfe_values, 0.60),
                "mae_q70": quantile(mae_to_mfe_values, 0.70),
                "mae_q75": quantile(mae_to_mfe_values, 0.75),
                "time_to_mfe_median": median(time_values) if time_values else float(horizon),
            }

    outcomes: dict[str, dict[str, dict[str, dict[str, Any]]]] = {"LONG": {}, "SHORT": {}}
    for side in ("LONG", "SHORT"):
        outcomes[side]["ANY"] = {"ANY": {}}
        for horizon in config.HORIZONS_MINUTES:
            outcomes[side]["ANY"]["ANY"][str(horizon)] = _build_outcome_set(
                candles,
                event_indices[side],
                side,
                horizon,
                min(float(windows[str(w)]["move_threshold_pct"]) for w in config.TRIGGER_WINDOWS_SECONDS),
            )
        for window in config.TRIGGER_WINDOWS_SECONDS:
            window_key = str(window)
            outcomes[side][window_key] = {"RANGE": {}, "VOLUME": {}, "ANY": {}}
            combined = event_groups[side][window_key]["RANGE"] + event_groups[side][window_key]["VOLUME"]
            for horizon in config.HORIZONS_MINUTES:
                for support in ("RANGE", "VOLUME"):
                    outcomes[side][window_key][support][str(horizon)] = _build_outcome_set(
                        candles,
                        event_groups[side][window_key][support],
                        side,
                        horizon,
                        float(windows[window_key]["move_threshold_pct"]),
                    )
                outcomes[side][window_key]["ANY"][str(horizon)] = _build_outcome_set(
                    candles,
                    combined,
                    side,
                    horizon,
                    float(windows[window_key]["move_threshold_pct"]),
                )

    return {
        "version": config.PROFILE_VERSION,
        "symbol_id": symbol.id,
        "okx_symbol": symbol.okx,
        "toobit_symbol": symbol.toobit,
        "created_at": int(time.time()),
        "candle_count": len(candles),
        "first_ts": int(candles[0]["ts"]),
        "last_ts": int(candles[-1]["ts"]),
        "base": {
            "move_q72_pct": move_q,
            "range_q60_pct": range_q,
            "volume_q60_quote": volume_q,
            "noise_q75_pct": noise_q75,
            "directionality_median": quantile(directions, 0.50),
        },
        "windows": windows,
        "events": {
            "long": len(event_indices["LONG"]),
            "short": len(event_indices["SHORT"]),
            "groups": {
                side: {
                    window: {support: len(values) for support, values in supports.items()}
                    for window, supports in event_groups[side].items()
                }
                for side in ("LONG", "SHORT")
            },
        },
        "horizons": horizons,
        "outcomes": outcomes,
    }


class ProfileManager:
    def __init__(self, okx: OKXClient, storage: Storage) -> None:
        self.okx = okx
        self.storage = storage
        self.profiles: dict[str, dict[str, Any]] = {}

    def load_or_build(self, symbols: list[SymbolSpec], force: bool = False) -> dict[str, dict[str, Any]]:
        ready: dict[str, dict[str, Any]] = {}
        logger.info("[PROFILE_START] symbols=%d days=%d force=%s version=%d", len(symbols), config.PROFILE_DAYS, force, config.PROFILE_VERSION)
        self.storage.set("profiles_requested", len(symbols))
        self.storage.set("profiles_progress", 0)
        for index, symbol in enumerate(symbols, start=1):
            self.storage.set("profiles_progress", index - 1)
            try:
                cached = self.storage.load_profile(symbol.id)
                cached_version = int((cached or {}).get("version") or 0)
                if (
                    not force
                    and cached
                    and cached_version >= config.PROFILE_VERSION
                    and self.storage.is_profile_fresh(symbol.id)
                ):
                    ready[symbol.id] = cached
                    logger.info(
                        "[PROFILE_READY] %s source=cache candles=%s progress=%d/%d",
                        symbol.id, cached.get("candle_count"), index, len(symbols),
                    )
                    continue
                candles = self.okx.get_history_candles(symbol.okx)
                profile = build_behavior_profile(symbol, candles)
                self.storage.save_profile(symbol.id, symbol.okx, symbol.toobit, profile)
                ready[symbol.id] = profile
                logger.info(
                    "[PROFILE_READY] %s source=okx candles=%d events_long=%d events_short=%d progress=%d/%d",
                    symbol.id, len(candles), profile["events"]["long"], profile["events"]["short"], index, len(symbols),
                )
            except Exception as exc:
                cached = self.storage.load_profile(symbol.id)
                if cached:
                    ready[symbol.id] = cached
                    logger.warning("[PROFILE_FALLBACK] %s reason=%s", symbol.id, exc)
                else:
                    logger.warning("[PROFILE_FAILED] %s reason=%s", symbol.id, exc)
                    self.storage.add_health_event("profile", "warning", str(exc), symbol.id)
        self.storage.set("profiles_progress", len(symbols))
        self.profiles = ready
        self.storage.set("profiles_ready", len(ready))
        self.storage.set("profiles_updated_at", int(time.time()))
        logger.info("[PROFILE_DONE] ready=%d total=%d", len(ready), len(symbols))
        return ready

    def get(self, symbol_id: str) -> dict[str, Any] | None:
        return self.profiles.get(symbol_id)
