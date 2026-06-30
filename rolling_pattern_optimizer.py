"""بهینه‌ساز روزانه بازه‌های ورود برای همه ارزهای WATCHLIST.

این ماژول روزی یک بار، جدا از حلقه سریع سیگنال‌دهی، ۳۰ روز گذشته 5m را از OKX می‌گیرد،
حرکت‌های خوب واقعی را پیدا می‌کند، اندیکاتورهای نقطه شروع/اوایل حرکت را جمع‌بندی می‌کند
و فقط بازه نهایی هر ارز و هر جهت را در data/symbol_profiles.json ذخیره می‌کند.
"""
from __future__ import annotations

import json
import math
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import requests

import config
from utils import logger, safe_float


MS_5M = 5 * 60 * 1000


class RollingPatternOptimizer:
    def __init__(self, output_path: Path | None = None) -> None:
        self.output_path = output_path or (config.DATA_DIR / "symbol_profiles.json")
        self.tmp_path = self.output_path.with_suffix(".new.json")
        self.session = requests.Session()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._last_start_ts = 0.0

    @staticmethod
    def _utc_now() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _today_utc() -> str:
        return datetime.now(timezone.utc).date().isoformat()

    def _load_existing(self) -> dict[str, Any]:
        try:
            with self.output_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def should_run(self, *, force: bool = False) -> bool:
        if not config.ROLLING_OPTIMIZER_ENABLED:
            return False
        if force:
            return True
        existing = self._load_existing()
        if not existing:
            return True
        if str(existing.get("generated_date_utc") or "") != self._today_utc():
            now = self._utc_now()
            run_minutes = config.ROLLING_OPTIMIZER_RUN_HOUR * 60 + config.ROLLING_OPTIMIZER_RUN_MINUTE
            now_minutes = now.hour * 60 + now.minute
            return now_minutes >= run_minutes
        return False

    def start_if_needed(self, valid_symbols: dict[str, dict[str, Any]] | None = None, *, force: bool = False) -> bool:
        if not self.should_run(force=force):
            return False
        with self._lock:
            if self._thread and self._thread.is_alive():
                return False
            # جلوگیری از تلاش پشت سر هم در صورت خطای OKX
            if not force and time.time() - self._last_start_ts < 10 * 60:
                return False
            self._last_start_ts = time.time()
            self._thread = threading.Thread(
                target=self._run_thread,
                args=(valid_symbols or {},),
                name="rolling-pattern-optimizer",
                daemon=True,
            )
            self._thread.start()
            return True

    def _run_thread(self, valid_symbols: dict[str, dict[str, Any]]) -> None:
        try:
            logger.info("شروع بهینه‌سازی روزانه بازه‌ها؛ ترید زنده متوقف نمی‌شود")
            data = self.generate_profiles(valid_symbols)
            self._atomic_write(data)
            logger.info("بهینه‌سازی روزانه بازه‌ها تمام شد: %s", self.output_path)
        except Exception as exc:
            logger.warning("بهینه‌سازی روزانه بازه‌ها ناموفق بود؛ ربات با بازه قبلی/عمومی ادامه می‌دهد: %s", exc)

    def _atomic_write(self, data: dict[str, Any]) -> None:
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        with self.tmp_path.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        self.tmp_path.replace(self.output_path)

    def _okx_get(self, path: str, params: dict[str, Any], retries: int = 4) -> dict[str, Any]:
        url = config.OKX_BASE_URL.rstrip("/") + path
        last_err: Exception | None = None
        for i in range(retries):
            try:
                r = self.session.get(url, params=params, timeout=max(config.REQUEST_TIMEOUT, 20), headers={"User-Agent": "rolling-pattern-optimizer/1.0"})
                r.raise_for_status()
                payload = r.json()
                if str(payload.get("code")) != "0":
                    raise RuntimeError(payload.get("msg") or payload)
                return payload
            except Exception as exc:
                last_err = exc
                time.sleep(1.1 * (i + 1))
        raise RuntimeError(f"OKX request failed: {last_err}")

    @staticmethod
    def _parse_okx_rows(rows: list[Any]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, list) or len(row) < 6:
                continue
            try:
                out.append(
                    {
                        "open_time": int(float(row[0])),
                        "open": safe_float(row[1]),
                        "high": safe_float(row[2]),
                        "low": safe_float(row[3]),
                        "close": safe_float(row[4]),
                        "volume": safe_float(row[5]),
                        "quote_volume": safe_float(row[7] if len(row) > 7 else 0),
                        "confirm": str(row[8]) if len(row) > 8 else "1",
                    }
                )
            except Exception:
                continue
        return out

    def fetch_history(self, okx_symbol: str, days: int) -> pd.DataFrame:
        target_start = int(time.time() * 1000) - days * 24 * 60 * 60 * 1000
        need = int(days * 24 * 60 / 5) + config.ROLLING_OPTIMIZER_WARMUP_CANDLES
        collected: dict[int, dict[str, Any]] = {}
        cursor: int | None = None
        cursor_mode: str | None = None
        loops = 0
        max_loops = int(math.ceil(need / max(1, config.ROLLING_OPTIMIZER_PAGE_LIMIT))) + 20

        while len(collected) < need and loops < max_loops:
            loops += 1
            params: dict[str, Any] = {"instId": okx_symbol, "bar": config.TIMEFRAME, "limit": str(config.ROLLING_OPTIMIZER_PAGE_LIMIT)}
            if cursor is not None:
                params[cursor_mode or "after"] = str(cursor)
            payload = self._okx_get("/api/v5/market/history-candles", params)
            rows = self._parse_okx_rows(payload.get("data", []))

            # بعضی وقت‌ها جهت cursor در OKX گیج‌کننده می‌شود؛ اگر after عقب‌تر نرفت، before را امتحان کن.
            if cursor is not None and rows and cursor_mode is None:
                if not any(int(r["open_time"]) < cursor for r in rows):
                    params2 = {"instId": okx_symbol, "bar": config.TIMEFRAME, "limit": str(config.ROLLING_OPTIMIZER_PAGE_LIMIT), "before": str(cursor)}
                    payload2 = self._okx_get("/api/v5/market/history-candles", params2)
                    rows2 = self._parse_okx_rows(payload2.get("data", []))
                    if any(int(r["open_time"]) < cursor for r in rows2):
                        rows = rows2
                        cursor_mode = "before"
                    else:
                        cursor_mode = "after"
                else:
                    cursor_mode = "after"

            if not rows:
                break
            new_count = 0
            for r in rows:
                ts = int(r["open_time"])
                if ts not in collected:
                    collected[ts] = r
                    new_count += 1
            oldest = min(int(r["open_time"]) for r in rows)
            cursor = oldest
            if new_count == 0:
                break
            if oldest <= target_start and len(collected) >= need - config.ROLLING_OPTIMIZER_WARMUP_CANDLES:
                break
            time.sleep(config.ROLLING_OPTIMIZER_REQUEST_SLEEP_SECONDS)

        if not collected:
            raise RuntimeError("کندلی دریافت نشد")
        df = pd.DataFrame(collected.values()).sort_values("open_time").reset_index(drop=True)
        df = df[df["confirm"].astype(str).eq("1")].copy()
        # کمی قبل‌تر از ۳۰ روز نگه می‌داریم تا اندیکاتور ابتدای بازه گرم باشد؛ بعد از محاسبه فیلتر می‌شود.
        if len(df) < 500:
            raise RuntimeError(f"کندل کافی نیست: {len(df)}")
        return df

    @staticmethod
    def _rsi(close: pd.Series, period: int) -> pd.Series:
        delta = close.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
        avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        return (100 - (100 / (1 + rs))).fillna(50)

    @staticmethod
    def _atr(df: pd.DataFrame, period: int) -> pd.Series:
        high, low, close = df["high"], df["low"], df["close"]
        prev_close = close.shift(1)
        tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
        return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()

    @staticmethod
    def _adx(df: pd.DataFrame, period: int) -> pd.Series:
        high, low, close = df["high"], df["low"], df["close"]
        up_move = high.diff()
        down_move = -low.diff()
        plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
        minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
        prev_close = close.shift(1)
        tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
        atr_s = tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
        plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1 / period, adjust=False, min_periods=period).mean() / atr_s
        minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1 / period, adjust=False, min_periods=period).mean() / atr_s
        dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
        return dx.ewm(alpha=1 / period, adjust=False, min_periods=period).mean().fillna(0)

    def add_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        for col in ["open", "high", "low", "close", "volume", "quote_volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce").astype(float)
        df["ema_fast"] = df["close"].ewm(span=config.EMA_FAST, adjust=False).mean()
        df["ema_slow"] = df["close"].ewm(span=config.EMA_SLOW, adjust=False).mean()
        df["ema_trend"] = df["close"].ewm(span=config.EMA_TREND, adjust=False).mean()

        typical = (df["high"] + df["low"] + df["close"]) / 3
        pv = typical * df["volume"]
        # در ربات زنده VWAP روی آخرین CANDLE_LIMIT کندل محاسبه می‌شود؛ این rolling همان رفتار را شبیه‌سازی می‌کند.
        vol_sum = df["volume"].rolling(config.CANDLE_LIMIT, min_periods=20).sum().replace(0, np.nan)
        df["vwap"] = pv.rolling(config.CANDLE_LIMIT, min_periods=20).sum() / vol_sum
        df["vwap"] = df["vwap"].ffill().bfill()

        df["volume_ma"] = df["volume"].rolling(config.VOLUME_MA_PERIOD, min_periods=config.VOLUME_MA_PERIOD).mean()
        df["volume_multiplier"] = df["volume"] / df["volume_ma"].replace(0, np.nan)
        df["rsi"] = self._rsi(df["close"], config.RSI_PERIOD)
        df["atr"] = self._atr(df, config.ATR_PERIOD)
        df["atr_percent"] = df["atr"] / df["close"].replace(0, np.nan) * 100
        df["adx"] = self._adx(df, config.ADX_PERIOD)

        mid = df["close"].rolling(config.BOLLINGER_PERIOD, min_periods=config.BOLLINGER_PERIOD).mean()
        std = df["close"].rolling(config.BOLLINGER_PERIOD, min_periods=config.BOLLINGER_PERIOD).std(ddof=0)
        df["bb_mid"] = mid
        df["bb_upper"] = mid + config.BOLLINGER_STD * std
        df["bb_lower"] = mid - config.BOLLINGER_STD * std
        width = (df["bb_upper"] - df["bb_lower"]).replace(0, np.nan)
        df["bb_position"] = ((df["close"] - df["bb_lower"]) / width).clip(-2, 3)
        df["vwap_distance_percent"] = ((df["close"] - df["vwap"]).abs() / df["close"].replace(0, np.nan) * 100)
        df["ema_gap_percent"] = ((df["ema_fast"] - df["ema_slow"]) / df["close"].replace(0, np.nan) * 100)
        df["past_6_low"] = df["low"].rolling(6, min_periods=2).min()
        df["past_6_high"] = df["high"].rolling(6, min_periods=2).max()
        return df.dropna().reset_index(drop=True)

    @staticmethod
    def _num(x: Any, nd: int = 4) -> float:
        try:
            v = float(x)
            if math.isnan(v) or math.isinf(v):
                return 0.0
            return round(v, nd)
        except Exception:
            return 0.0

    def _broad_start_ok(self, row: pd.Series, side: str) -> bool:
        close = float(row["close"])
        if close <= 0:
            return False
        vwap_distance = float(row["vwap_distance_percent"])
        volume = float(row["volume_multiplier"])
        bb_pos = float(row["bb_position"])
        atr_pct = float(row["atr_percent"])
        if atr_pct < config.ATR_MIN_PERCENT or atr_pct > config.ATR_MAX_PERCENT:
            return False
        if volume < config.ROLLING_OPTIMIZER_MIN_VOLUME_MULTIPLIER:
            return False
        if vwap_distance > config.ROLLING_OPTIMIZER_MAX_START_VWAP_DISTANCE_PERCENT:
            return False
        if side == "LONG":
            past_move = (close - float(row["past_6_low"])) / close * 100
            return (
                close > float(row["vwap"])
                and float(row["ema_fast"]) > float(row["ema_slow"])
                and float(row["rsi"]) >= config.ROLLING_OPTIMIZER_LONG_RSI_FLOOR
                and bb_pos < config.ROLLING_OPTIMIZER_LONG_BB_MAX
                and past_move <= config.ROLLING_OPTIMIZER_MAX_ALREADY_MOVED_PERCENT
            )
        past_move = (float(row["past_6_high"]) - close) / close * 100
        return (
            close < float(row["vwap"])
            and float(row["ema_fast"]) < float(row["ema_slow"])
            and float(row["rsi"]) <= config.ROLLING_OPTIMIZER_SHORT_RSI_CEIL
            and bb_pos > config.ROLLING_OPTIMIZER_SHORT_BB_MIN
            and past_move <= config.ROLLING_OPTIMIZER_MAX_ALREADY_MOVED_PERCENT
        )

    def _target_hit(self, df: pd.DataFrame, i: int, side: str) -> tuple[bool, int, float]:
        entry = float(df.iloc[i]["close"])
        target_pct = config.ROLLING_OPTIMIZER_TARGET_MOVE_PERCENT
        adverse_pct = config.ROLLING_OPTIMIZER_ADVERSE_PERCENT
        max_hold = config.ROLLING_OPTIMIZER_MAX_HOLD_CANDLES
        best_move = 0.0
        for j in range(i + 1, min(len(df), i + max_hold + 1)):
            row = df.iloc[j]
            if side == "LONG":
                target_price = entry * (1 + target_pct / 100)
                adverse_price = entry * (1 - adverse_pct / 100)
                hit_adverse = float(row["low"]) <= adverse_price
                hit_target = float(row["high"]) >= target_price
                best_move = max(best_move, (float(row["high"]) - entry) / entry * 100)
            else:
                target_price = entry * (1 - target_pct / 100)
                adverse_price = entry * (1 + adverse_pct / 100)
                hit_adverse = float(row["high"]) >= adverse_price
                hit_target = float(row["low"]) <= target_price
                best_move = max(best_move, (entry - float(row["low"])) / entry * 100)
            # محافظه‌کارانه: اگر در یک کندل هر دو دیده شد، اول شکست حساب می‌شود.
            if hit_adverse:
                return False, j, best_move
            if hit_target:
                return True, j, best_move
        return False, min(len(df) - 1, i + max_hold), best_move

    def _movement_end(self, df: pd.DataFrame, start: int, hit: int, side: str) -> int:
        end_limit = min(len(df) - 1, start + config.ROLLING_OPTIMIZER_MAX_END_CANDLES)
        reversal = config.ROLLING_OPTIMIZER_REVERSAL_PERCENT / 100
        if side == "LONG":
            peak = float(df.iloc[hit]["high"])
            stale = 0
            for j in range(hit + 1, end_limit + 1):
                high = float(df.iloc[j]["high"])
                close = float(df.iloc[j]["close"])
                if high > peak:
                    peak = high
                    stale = 0
                else:
                    stale += 1
                if close <= peak * (1 - reversal) or stale >= config.ROLLING_OPTIMIZER_RANGE_CANDLES_AFTER_MOVE:
                    return j
        else:
            trough = float(df.iloc[hit]["low"])
            stale = 0
            for j in range(hit + 1, end_limit + 1):
                low = float(df.iloc[j]["low"])
                close = float(df.iloc[j]["close"])
                if low < trough:
                    trough = low
                    stale = 0
                else:
                    stale += 1
                if close >= trough * (1 + reversal) or stale >= config.ROLLING_OPTIMIZER_RANGE_CANDLES_AFTER_MOVE:
                    return j
        return end_limit

    def extract_good_moves(self, df: pd.DataFrame, side: str) -> pd.DataFrame:
        rows: list[dict[str, Any]] = []
        i = max(config.ROLLING_OPTIMIZER_WARMUP_CANDLES, config.CANDLE_LIMIT)
        n = len(df)
        while i < n - config.ROLLING_OPTIMIZER_MAX_HOLD_CANDLES - 1:
            row = df.iloc[i]
            if not self._broad_start_ok(row, side):
                i += 1
                continue
            ok, hit_idx, best_move = self._target_hit(df, i, side)
            if not ok:
                i += 1
                continue
            end_idx = self._movement_end(df, i, hit_idx, side)
            item = {
                "start_index": int(i),
                "hit_index": int(hit_idx),
                "end_index": int(end_idx),
                "start_time": int(row["open_time"]),
                "end_time": int(df.iloc[end_idx]["open_time"]),
                "best_move_percent": self._num(best_move, 4),
                "rsi": self._num(row["rsi"], 4),
                "adx": self._num(row["adx"], 4),
                "volume_multiplier": self._num(row["volume_multiplier"], 4),
                "vwap_distance_percent": self._num(row["vwap_distance_percent"], 4),
                "atr_percent": self._num(row["atr_percent"], 4),
                "bb_position": self._num(row["bb_position"], 4),
                "ema_gap_percent": self._num(row["ema_gap_percent"], 4),
            }
            rows.append(item)
            # پرش تا پایان حرکت برای اینکه یک حرکت واقعی چندین نمونه تکراری نسازد.
            i = max(i + 1, end_idx)
        return pd.DataFrame(rows)

    def _build_side_profile(self, events: pd.DataFrame, side: str) -> dict[str, Any]:
        features = ["rsi", "adx", "volume_multiplier", "vwap_distance_percent", "atr_percent", "bb_position", "ema_gap_percent"]
        total = int(len(events))
        base = {
            "enabled": False,
            "using_default": True,
            "reason": "samples_not_enough",
            "side": side,
            "samples": total,
            "good_moves": total,
            "target_move_percent": config.ROLLING_OPTIMIZER_TARGET_MOVE_PERCENT,
            "ranges": {},
        }
        if total < config.ROLLING_OPTIMIZER_MIN_GOOD_MOVES:
            return base

        best: dict[str, Any] | None = None
        for qlo, qhi in [
            (config.ROLLING_OPTIMIZER_QUANTILE_LOW, config.ROLLING_OPTIMIZER_QUANTILE_HIGH),
            (0.15, 0.85),
            (0.10, 0.90),
        ]:
            ranges: dict[str, dict[str, float]] = {}
            mask = pd.Series(True, index=events.index)
            for f in features:
                vals = pd.to_numeric(events[f], errors="coerce").dropna()
                if vals.empty:
                    continue
                lo = float(vals.quantile(qlo))
                hi = float(vals.quantile(qhi))
                if lo > hi:
                    lo, hi = hi, lo
                ranges[f] = {"min": self._num(lo, 4), "max": self._num(hi, 4)}
                mask &= pd.to_numeric(events[f], errors="coerce").between(lo, hi, inclusive="both")
            combined = events[mask]
            profile = {
                "enabled": True,
                "using_default": False,
                "reason": "ok",
                "side": side,
                "samples": total,
                "good_moves": total,
                "combined_samples": int(len(combined)),
                "target_move_percent": config.ROLLING_OPTIMIZER_TARGET_MOVE_PERCENT,
                "quantile_mode": f"q{int(qlo * 100)}-q{int(qhi * 100)}",
                "ranges": ranges,
                "avg_best_move_percent": self._num(events["best_move_percent"].mean(), 4),
                "median_best_move_percent": self._num(events["best_move_percent"].median(), 4),
            }
            best = profile
            if int(len(combined)) >= config.ROLLING_OPTIMIZER_MIN_COMBINED_SAMPLES:
                return profile
        return best or base

    def _okx_symbol_for(self, internal: str, valid_symbols: dict[str, dict[str, Any]]) -> str:
        mapped = valid_symbols.get(internal) or {}
        return str(mapped.get("okx_symbol") or config.SYMBOL_MAP[internal]["okx"])

    def generate_profiles(self, valid_symbols: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
        valid_symbols = valid_symbols or {}
        generated = self._utc_now()
        target_start = int(time.time() * 1000) - config.ROLLING_OPTIMIZER_DAYS * 24 * 60 * 60 * 1000
        symbols_out: dict[str, Any] = {}

        active_symbols = list(valid_symbols.keys()) if valid_symbols else list(config.WATCHLIST)
        for internal in active_symbols:
            okx_symbol = self._okx_symbol_for(internal, valid_symbols)
            try:
                logger.info("بهینه‌سازی بازه %s شروع شد", internal)
                raw = self.fetch_history(okx_symbol, config.ROLLING_OPTIMIZER_DAYS)
                df = self.add_indicators(raw)
                df = df[df["open_time"] >= target_start].reset_index(drop=True)
                if len(df) < 500:
                    raise RuntimeError(f"کندل قابل تحلیل کافی نیست: {len(df)}")
                long_events = self.extract_good_moves(df, "LONG")
                short_events = self.extract_good_moves(df, "SHORT")
                symbols_out[internal] = {
                    "okx_symbol": okx_symbol,
                    "LONG": self._build_side_profile(long_events, "LONG"),
                    "SHORT": self._build_side_profile(short_events, "SHORT"),
                }
                logger.info(
                    "بازه %s ساخته شد | LONG=%s SHORT=%s",
                    internal,
                    len(long_events),
                    len(short_events),
                )
            except Exception as exc:
                logger.warning("بهینه‌سازی بازه %s ناموفق بود: %s", internal, exc)
                symbols_out[internal] = {
                    "okx_symbol": okx_symbol,
                    "LONG": {"enabled": False, "using_default": True, "reason": str(exc), "samples": 0, "ranges": {}},
                    "SHORT": {"enabled": False, "using_default": True, "reason": str(exc), "samples": 0, "ranges": {}},
                }

        return {
            "version": "v15",
            "generated_utc": generated.isoformat(),
            "generated_date_utc": generated.date().isoformat(),
            "timeframe": config.TIMEFRAME,
            "days": config.ROLLING_OPTIMIZER_DAYS,
            "target_move_percent": config.ROLLING_OPTIMIZER_TARGET_MOVE_PERCENT,
            "adverse_percent": config.ROLLING_OPTIMIZER_ADVERSE_PERCENT,
            "min_good_moves": config.ROLLING_OPTIMIZER_MIN_GOOD_MOVES,
            "method": "start_of_real_good_moves_clean_quantile_ranges",
            "symbols": symbols_out,
        }


if __name__ == "__main__":
    opt = RollingPatternOptimizer()
    try:
        from storage import JSONStorage

        valid = JSONStorage().get_validated_symbols() or {}
    except Exception:
        valid = {}
    result = opt.generate_profiles(valid)
    opt._atomic_write(result)
    print(f"OK: {opt.output_path}")
