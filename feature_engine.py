"""هشت ابزار نرم با پروفایل مستقل برای هر تایم معامله."""
from __future__ import annotations

import math
from typing import Any

import config
from models import Candle, FeatureSnapshot
from utils import clamp, ema, mean, now_ms, percentile, sma, stdev, true_ranges

FEATURE_VERSION = "features-high-tf-v2"


class FeatureError(RuntimeError):
    pass


class FeatureEngine:
    @staticmethod
    def _resample_for_analysis(candles: list[Candle], target_minutes: int) -> list[Candle]:
        if not candles:
            return []
        bucket_ms = target_minutes * 60_000
        grouped: dict[int, list[Candle]] = {}
        for candle in candles:
            grouped.setdefault(candle.ts // bucket_ms * bucket_ms, []).append(candle)
        out: list[Candle] = []
        for ts in sorted(grouped):
            rows = sorted(grouped[ts], key=lambda x: x.ts)
            out.append(Candle(
                ts=ts, open=rows[0].open, high=max(x.high for x in rows),
                low=min(x.low for x in rows), close=rows[-1].close,
                volume=sum(x.volume for x in rows), turnover=sum(x.turnover for x in rows),
                confirmed=all(x.confirmed for x in rows),
            ))
        return out

    @staticmethod
    def _rsi(closes: list[float], period: int = 14) -> list[float]:
        if len(closes) < 2:
            return [50.0] * len(closes)
        gains = [0.0]
        losses = [0.0]
        for a, b in zip(closes, closes[1:]):
            d = b - a
            gains.append(max(d, 0.0))
            losses.append(max(-d, 0.0))
        avg_gain = ema(gains, period)
        avg_loss = ema(losses, period)
        out = []
        for g, l in zip(avg_gain, avg_loss):
            if l <= 1e-12:
                out.append(100.0 if g > 0 else 50.0)
            else:
                rs = g / l
                out.append(100.0 - 100.0 / (1.0 + rs))
        return out

    @staticmethod
    def _atr(candles: list[Candle], period: int = 14) -> list[float]:
        highs = [x.high for x in candles]
        lows = [x.low for x in candles]
        closes = [x.close for x in candles]
        return ema(true_ranges(highs, lows, closes), period)

    @staticmethod
    def _adx_dmi(candles: list[Candle], period: int = 14) -> tuple[list[float], list[float], list[float]]:
        if len(candles) < 2:
            n = len(candles)
            return [0.0] * n, [50.0] * n, [50.0] * n
        plus_dm = [0.0]
        minus_dm = [0.0]
        tr = [candles[0].high - candles[0].low]
        for prev, cur in zip(candles, candles[1:]):
            up = cur.high - prev.high
            down = prev.low - cur.low
            plus_dm.append(up if up > down and up > 0 else 0.0)
            minus_dm.append(down if down > up and down > 0 else 0.0)
            tr.append(max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close)))
        atr = ema(tr, period)
        psm = ema(plus_dm, period)
        msm = ema(minus_dm, period)
        pdi: list[float] = []
        mdi: list[float] = []
        dx: list[float] = []
        for a, p, m in zip(atr, psm, msm):
            if a <= 1e-12:
                pp = mm = 0.0
            else:
                pp = 100.0 * p / a
                mm = 100.0 * m / a
            pdi.append(pp)
            mdi.append(mm)
            denom = pp + mm
            dx.append(100.0 * abs(pp - mm) / denom if denom > 1e-12 else 0.0)
        return ema(dx, period), pdi, mdi

    @staticmethod
    def _linear_slope(values: list[float], window: int = 20) -> float:
        vals = values[-window:]
        if len(vals) < 3:
            return 0.0
        n = len(vals)
        xs = list(range(n))
        xm = (n - 1) / 2
        ym = sum(vals) / n
        den = sum((x - xm) ** 2 for x in xs)
        if den <= 0 or abs(ym) <= 1e-12:
            return 0.0
        slope = sum((x - xm) * (y - ym) for x, y in zip(xs, vals)) / den
        return slope / abs(ym)

    @staticmethod
    def _structure(candles: list[Candle], lookback: int = 24) -> tuple[float, float, dict[str, Any]]:
        rows = candles[-lookback:]
        if len(rows) < 8:
            return 50.0, 50.0, {}
        half = len(rows) // 2
        old = rows[:half]
        new = rows[half:]
        old_high, old_low = max(x.high for x in old), min(x.low for x in old)
        new_high, new_low = max(x.high for x in new), min(x.low for x in new)
        last = rows[-1].close
        span = max(max(x.high for x in rows) - min(x.low for x in rows), last * 1e-6)
        higher_high = clamp((new_high - old_high) / span * 100 + 50, 0, 100)
        higher_low = clamp((new_low - old_low) / span * 100 + 50, 0, 100)
        long_score = 0.5 * higher_high + 0.5 * higher_low
        short_score = 100.0 - long_score
        return long_score, short_score, {
            "old_high": old_high, "old_low": old_low, "new_high": new_high, "new_low": new_low,
            "range_high": max(x.high for x in rows), "range_low": min(x.low for x in rows),
        }

    def _single_timeframe(self, candles: list[Candle], profile: dict[str, Any]) -> dict[str, Any]:
        if len(candles) < 60:
            raise FeatureError(f"insufficient candles: {len(candles)}")
        closes = [x.close for x in candles]
        volumes = [x.volume for x in candles]
        last = closes[-1]

        e9, e21, e50 = ema(closes, 9), ema(closes, 21), ema(closes, 50)
        slope9 = self._linear_slope(e9, 12)
        slope21 = self._linear_slope(e21, 18)
        ema_long = clamp(50 + (e9[-1] - e21[-1]) / last * 2600 + slope9 * 9000 + slope21 * 6000, 0, 100)
        ema_short = 100 - ema_long

        rsi = self._rsi(closes)
        rsi_now = rsi[-1]
        rsi_delta = rsi[-1] - rsi[-4]
        # RSI is momentum, not a hard overbought/oversold gate.
        rsi_long = clamp(50 + (rsi_now - 50) * 0.65 + rsi_delta * 1.4, 0, 100)
        rsi_short = 100 - rsi_long

        fast, slow = ema(closes, 12), ema(closes, 26)
        macd = [a - b for a, b in zip(fast, slow)]
        signal = ema(macd, 9)
        hist = [a - b for a, b in zip(macd, signal)]
        hist_scale = max(mean([abs(x) for x in hist[-50:]]), last * 1e-6)
        macd_long = clamp(50 + hist[-1] / hist_scale * 18 + (hist[-1] - hist[-4]) / hist_scale * 12, 0, 100)
        macd_short = 100 - macd_long

        adx, pdi, mdi = self._adx_dmi(candles)
        adx_now = adx[-1]
        dmi_edge = pdi[-1] - mdi[-1]
        strength_factor = clamp(adx_now / 35, 0.2, 1.4)
        adx_long = clamp(50 + dmi_edge * 1.2 * strength_factor, 0, 100)
        adx_short = 100 - adx_long

        atr = self._atr(candles)
        atr_now = atr[-1]
        natr = atr_now / last if last > 0 else 0.0
        hist_natr_p50 = float(profile.get("natr_p50") or natr or 0.005)
        hist_natr_p90 = float(profile.get("natr_p90") or max(hist_natr_p50 * 2, natr, 0.01))
        vol_state = clamp((natr - hist_natr_p50) / max(hist_natr_p90 - hist_natr_p50, 1e-6), -1, 2)
        # ATR does not choose direction; it contributes readiness and risk.
        atr_score = clamp(55 + vol_state * 18, 20, 95)

        vol_mean = mean(volumes[-21:-1], 0.0)
        rel_vol = volumes[-1] / vol_mean if vol_mean > 0 else 1.0
        price_delta = closes[-1] - closes[-4]
        volume_bias = 1 if price_delta >= 0 else -1
        rv_edge = clamp((rel_vol - 1.0) * 30, -25, 35) * volume_bias
        rv_long = clamp(50 + rv_edge, 0, 100)
        rv_short = 100 - rv_long

        structure_long, structure_short, structure_raw = self._structure(candles)
        trend_slope = self._linear_slope(closes, 24)
        returns = [(b / a - 1) for a, b in zip(closes[-30:-1], closes[-29:]) if a > 0]
        noise = stdev(returns, 0.0)
        efficiency = abs(closes[-1] - closes[-20]) / max(sum(abs(b - a) for a, b in zip(closes[-20:-1], closes[-19:])), last * 1e-8)

        return {
            "last": last,
            "ema": {"long": ema_long, "short": ema_short, "e9": e9[-1], "e21": e21[-1], "e50": e50[-1], "slope9": slope9, "slope21": slope21},
            "rsi": {"long": rsi_long, "short": rsi_short, "value": rsi_now, "delta": rsi_delta},
            "macd": {"long": macd_long, "short": macd_short, "hist": hist[-1], "hist_delta": hist[-1] - hist[-4]},
            "adx_dmi": {"long": adx_long, "short": adx_short, "adx": adx_now, "pdi": pdi[-1], "mdi": mdi[-1]},
            "atr_natr": {"long": atr_score, "short": atr_score, "atr": atr_now, "natr": natr, "state": vol_state},
            "relative_volume": {"long": rv_long, "short": rv_short, "ratio": rel_vol},
            "market_structure": {"long": structure_long, "short": structure_short, **structure_raw},
            "trend_slope": trend_slope,
            "noise": noise,
            "efficiency": efficiency,
            "recent_high": max(x.high for x in candles[-20:]),
            "recent_low": min(x.low for x in candles[-20:]),
            "volume": volumes[-1],
        }

    @staticmethod
    def _context_score(context: dict[str, Any] | None, side: str) -> float:
        if not context:
            return 50.0
        vals = []
        for key in ("BTCUSDT", "ETHUSDT"):
            item = context.get(key) or {}
            tf = item.get("15m") or item.get("5m") or {}
            score = float(tf.get("ema", {}).get("long", 50.0))
            vals.append(score if side == "LONG" else 100.0 - score)
        return mean(vals, 50.0)

    def analyze(
        self,
        canonical: str,
        source: str,
        bundle: dict[str, list[Candle]],
        profile: dict[str, Any],
        context: dict[str, Any] | None = None,
        data_quality: float = 100.0,
        trade_timeframe: str = "30m",
    ) -> FeatureSnapshot:
        if trade_timeframe not in config.TRADE_TIMEFRAMES:
            raise FeatureError(f"unsupported trade timeframe: {trade_timeframe}")
        bundle = dict(bundle)
        if trade_timeframe == "30m" and len(bundle.get("30m", [])) < 60 and len(bundle.get("15m", [])) >= 120:
            bundle["30m"] = self._resample_for_analysis(bundle["15m"], 30)
        if trade_timeframe not in bundle or len(bundle.get(trade_timeframe, [])) < 60:
            available = [tf for tf in ("1H", "30m", "4H", "1D") if len(bundle.get(tf, [])) >= 60]
            if not available:
                raise FeatureError(f"no usable trade timeframe {trade_timeframe}")
            trade_timeframe = available[0]
        bootstrap = profile.get("bootstrap") or {}
        config_data = profile.get("config") or {}
        weights = dict(config.BASE_TOOL_WEIGHTS)
        weights.update(config_data.get("tool_weights") or {})

        required_tfs = set(config.ENTRY_TIMEFRAME_OPTIONS[trade_timeframe]) | {trade_timeframe, "1H", "4H", "1D", "30m"}
        per_tf: dict[str, Any] = {}
        for tf in required_tfs:
            if tf in bundle and len(bundle[tf]) >= 60:
                per_tf[tf] = self._single_timeframe(bundle[tf], bootstrap)
        if trade_timeframe not in per_tf:
            raise FeatureError(f"no usable trade timeframe {trade_timeframe}")

        entry_candidates = [
            tf for tf in config.ENTRY_TIMEFRAME_OPTIONS[trade_timeframe]
            if tf in per_tf
        ]
        if not entry_candidates:
            raise FeatureError(f"no usable entry timeframe for {trade_timeframe}")
        learned_entry = str(
            config_data.get("entry_timeframe")
            or config.DEFAULT_ENTRY_BY_TRADE_TIMEFRAME[trade_timeframe]
        )
        best_tf = learned_entry if learned_entry in entry_candidates else entry_candidates[0]
        best_value = -1e9
        tf_scores: dict[str, float] = {}
        for tf in entry_candidates:
            raw = per_tf[tf]
            directional_edge = abs(raw["ema"]["long"] - 50) + abs(raw["market_structure"]["long"] - 50)
            readiness = raw["adx_dmi"]["adx"] + min(raw["relative_volume"]["ratio"], 3) * 10 + raw["efficiency"] * 35
            learned_bias = 8.0 if tf == learned_entry else 0.0
            default_bias = 4.0 if tf == config.DEFAULT_ENTRY_BY_TRADE_TIMEFRAME[trade_timeframe] else 0.0
            value = directional_edge * 0.45 + readiness * 0.55 + learned_bias + default_bias
            tf_scores[tf] = value
            if value > best_value:
                best_value = value
                best_tf = tf

        selected = per_tf[trade_timeframe]
        entry_selected = per_tf[best_tf]
        actual_context_long = self._context_score(context, "LONG")
        actual_context_short = self._context_score(context, "SHORT")
        if bool(config_data.get("btc_eth_weight_enabled", True)):
            context_long, context_short = actual_context_long, actual_context_short
        else:
            context_long = context_short = 50.0
        tool_map = {
            "market_structure": selected["market_structure"],
            "ema": selected["ema"],
            "rsi": selected["rsi"],
            "macd": selected["macd"],
            "adx_dmi": selected["adx_dmi"],
            "relative_volume": selected["relative_volume"],
            "atr_natr": selected["atr_natr"],
            "btc_eth_context": {"long": context_long, "short": context_short},
        }
        weight_sum = sum(max(0.0, float(weights.get(k, 0))) for k in tool_map) or 1.0
        long = sum(float(weights.get(k, 0)) * float(v["long"]) for k, v in tool_map.items()) / weight_sum
        short = sum(float(weights.get(k, 0)) * float(v["short"]) for k, v in tool_map.items()) / weight_sum
        long_scores = {k: float(v["long"]) for k, v in tool_map.items()}
        short_scores = {k: float(v["short"]) for k, v in tool_map.items()}
        long_scores["weighted"] = long
        short_scores["weighted"] = short

        strength = max(long, short)
        natr = float(selected["atr_natr"]["natr"])
        low_hold, high_hold = config.HOLD_MINUTES_BY_TIMEFRAME[trade_timeframe]
        quality_factor = clamp((strength - 50.0) / 35.0, 0.0, 1.0)
        volatility_factor = clamp(natr / max(float((bootstrap or {}).get("natr_p90") or natr or 0.01), 1e-6), 0.0, 1.5)
        hold = int(clamp(high_hold - (high_hold - low_hold) * (0.55 * quality_factor + 0.20 * volatility_factor), low_hold, high_hold))
        raw = {
            "per_tf": per_tf,
            "selected": selected,
            "entry_selected": entry_selected,
            "trade_timeframe": trade_timeframe,
            "entry_timeframe": best_tf,
            "tf_scores": tf_scores,
            "tool_weights": weights,
            "bootstrap": bootstrap,
            "actual_context_scores": {"long": actual_context_long, "short": actual_context_short},
        }
        return FeatureSnapshot(
            canonical=canonical,
            source=source,
            trade_timeframe=trade_timeframe,
            entry_timeframe=best_tf,
            ts=now_ms(),
            long_scores=long_scores,
            short_scores=short_scores,
            raw=raw,
            data_quality=data_quality,
            estimated_hold_minutes=hold,
        )

    def bootstrap_profile(self, candles: list[Candle], minimum: int | None = None) -> dict[str, Any]:
        required = config.MIN_PROFILE_CANDLES if minimum is None else max(60, int(minimum))
        if len(candles) < required:
            raise FeatureError(f"profile requires {required} candles; got {len(candles)}")
        closes = [x.close for x in candles]
        volumes = [x.volume for x in candles]
        atr = self._atr(candles)
        natrs = [a / c if c > 0 else 0.0 for a, c in zip(atr, closes)]
        returns = [(b / a - 1) for a, b in zip(closes, closes[1:]) if a > 0]
        abs_returns = [abs(x) for x in returns]
        rel_vols: list[float] = []
        smav = sma(volumes, 20)
        for v, m in zip(volumes, smav):
            rel_vols.append(v / m if m > 0 else 1.0)
        slopes = []
        for i in range(50, len(closes), 12):
            slopes.append(abs(self._linear_slope(closes[:i], 24)))
        return {
            "built_at": now_ms(),
            "candles": len(candles),
            "first_ts": candles[0].ts,
            "last_ts": candles[-1].ts,
            "natr_p25": percentile(natrs[-1500:], 0.25),
            "natr_p50": percentile(natrs[-1500:], 0.50),
            "natr_p75": percentile(natrs[-1500:], 0.75),
            "natr_p90": percentile(natrs[-1500:], 0.90),
            "abs_return_p50": percentile(abs_returns[-1500:], 0.50),
            "abs_return_p90": percentile(abs_returns[-1500:], 0.90),
            "rel_volume_p50": percentile(rel_vols[-1500:], 0.50),
            "rel_volume_p90": percentile(rel_vols[-1500:], 0.90),
            "trend_slope_p50": percentile(slopes, 0.50),
            "trend_slope_p90": percentile(slopes, 0.90),
        }

    @staticmethod
    def default_profile_config(trade_timeframe: str = "30m") -> dict[str, Any]:
        trade_timeframe = trade_timeframe if trade_timeframe in config.TRADE_TIMEFRAMES else "30m"
        return {
            "trade_timeframe": trade_timeframe,
            "tool_weights": dict(config.BASE_TOOL_WEIGHTS),
            "entry_timeframe": config.DEFAULT_ENTRY_BY_TRADE_TIMEFRAME[trade_timeframe],
            "entry_atr_offset": 0.0,
            "rr": config.RR_DEFAULT_BY_TIMEFRAME[trade_timeframe],
            "tp_atr_multiplier": {"30m": 1.55, "1H": 1.85, "4H": 2.25, "1D": 2.80}[trade_timeframe],
            "sl_atr_multiplier": {"30m": 0.95, "1H": 1.05, "4H": 1.20, "1D": 1.40}[trade_timeframe],
            "initial_min_score": config.INITIAL_MIN_SCORE,
            "medium_min_score": config.MEDIUM_MIN_SCORE,
            "real_min_score": config.REAL_MIN_SCORE,
            "behavior_min_confidence": 0.0,
            "btc_eth_weight_enabled": True,
            "behavior_bias": {
                "TREND_START": 1.0, "TREND_CONTINUATION": 1.0, "PULLBACK": 1.0,
                "COMPRESSION": 1.0, "TRUE_BREAKOUT": 1.0, "FALSE_BREAKOUT": 1.0,
                "REVERSAL": 1.0, "RANGE": 1.0, "SHOCK": 1.0, "UNKNOWN": 1.0,
            },
            "entry_type_bias": {
                "EARLY_MOVEMENT": 1.0, "PULLBACK_CONTINUATION": 1.0,
                "DIRECT_BREAKOUT": 1.0, "BREAKOUT_RETEST": 1.0,
                "LIQUIDITY_SWEEP_REVERSAL": 1.0, "RANGE_EDGE_REVERSAL": 1.0,
                "FAILED_BREAKOUT_REVERSAL": 1.0, "FLEXIBLE": 1.0,
            },
            "behavior_tp_factors": {},
            "behavior_sl_factors": {},
        }
