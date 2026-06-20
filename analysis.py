# -*- coding: utf-8 -*-
"""AI Classic Direct Analysis Engine - complete integrated version. Soft Movement Hunter harmonized."""
import math
import time
import os
import json
from typing import Dict, List, Optional, Tuple, Any
import ccxt
import pandas as pd
import ta
try:
    from config import MIN_DIRECT_SCORE, MIN_ADX_FOR_TREND, MIN_MANUAL_CONFIRMATIONS, AUTO_DIRECT_SCORE_MIN
except Exception:
    MIN_DIRECT_SCORE = 82; MIN_ADX_FOR_TREND = 20; MIN_MANUAL_CONFIRMATIONS = 3; AUTO_DIRECT_SCORE_MIN = 82
try:
    from coin_learning import build_signal_snapshot, get_smart_tp_suggestion, should_require_extra_strength
except Exception:
    build_signal_snapshot = None; get_smart_tp_suggestion = None; should_require_extra_strength = None
try:
    from coin_risk import get_direction_risk_state
except Exception:
    get_direction_risk_state = None
try:
    from coin_rotation import get_coin_rotation_score
except Exception:
    get_coin_rotation_score = None
try:
    from ai_memory import update_ai_summary
except Exception:
    update_ai_summary = None
try:
    from data_store import load_json as _ds_load_json, save_json as _ds_save_json
except Exception:
    _ds_load_json = None; _ds_save_json = None

# Optional soft trend-analysis layer.
# It must never become a hard gate by itself; it only adds small balanced
# points so simple trendline/breakout logic cannot confuse the AI.
try:
    from trend_analysis import detect_trendline, detect_breakout
except Exception:
    detect_trendline = None
    detect_breakout = None

exchange = ccxt.okx({'enableRateLimit': True, 'timeout': 20000, 'options': {'defaultType': 'swap'}})
_SOFT_MARKET_CONTEXT_CACHE = {'ts': 0, 'data': None}
SOFT_MARKET_CONTEXT_TTL_SECONDS = 120
# Keep simple structure/breakout tools soft. Previous standalone weights such
# as +12/+15 are too strong for 5M/15M scalping and can override AI context.
SOFT_TRENDLINE_MAX_SCORE = 6
SOFT_BREAKOUT_MAX_SCORE = 8
SOFT_FAKE_BREAKOUT_MAX_SCORE = 5
AUTO_DIRECT_SCORE_MIN = max(int(MIN_DIRECT_SCORE), int(AUTO_DIRECT_SCORE_MIN))
ADX_HARD_MIN = max(float(MIN_ADX_FOR_TREND), 20.0)
LONG_DIRECT_SCORE_BONUS_REQUIREMENT = 0
LONG_MIN_1H_STRICT = False
LONG_BLOCK_IF_AGAINST_VWAP = False
MIN_SL_ATR_MULTIPLIER = 1.30
TP1_FALLBACK_ATR = 0.62
TP2_FALLBACK_ATR = 1.12
MAX_REASONABLE_SL_ATR = 2.40
MIN_TP1_ATR = 0.40
LEVEL_BUFFER_ATR = 0.14
SL_BUFFER_ATR = 0.25
TF_LEVEL_WEIGHTS = {'5M': 1.00, '15M': 3.00, '30M': 0.70}
LEVEL_LOOKBACK = 160
SWING_WINDOW = 3

# ---------------------------------------------------------------------------
# 15M scalp + 5M timing quality controls
# ---------------------------------------------------------------------------
# Keep the main threshold intact, but let a truly fresh 5M trigger reach it
# earlier when higher timeframes already agree. Late entries and bad R/R are
# blocked before becoming ACTIVE so they can be learned as rejected/ghost
# candidates by scanner/bot layers instead of becoming real trades.
EARLY_5M_BONUS_MAX = 12
MULTI_TF_ALIGNMENT_BONUS = 6
LATE_ENTRY_ATR_LIMIT = float(os.getenv("LATE_ENTRY_ATR_LIMIT", "1.55") or "1.55")
LATE_ENTRY_STRONG_ALIGNMENT_ATR_LIMIT = float(os.getenv("LATE_ENTRY_STRONG_ALIGNMENT_ATR_LIMIT", "1.85") or "1.85")
MIN_REAL_RISK_REWARD = float(os.getenv("MIN_REAL_RISK_REWARD", "0.80") or "0.80")

# Use the live/current 5M candle for timing and entry price.
# Higher timeframes still use closed candles to avoid repainting the main trend.
USE_CURRENT_5M_FOR_ENTRY = str(os.getenv("USE_CURRENT_5M_FOR_ENTRY", "1")).lower() not in {"0", "false", "no", "off"}
LIVE_ENTRY_PRICE_SOURCE = str(os.getenv("LIVE_ENTRY_PRICE_SOURCE", "5m_close")).lower().strip()
PUMP_DUMP_LOOKBACK_5M = int(os.getenv("PUMP_DUMP_LOOKBACK_5M", "8") or "8")
PUMP_DUMP_MOVE_ATR_LIMIT = float(os.getenv("PUMP_DUMP_MOVE_ATR_LIMIT", "3.20") or "3.20")
PUMP_DUMP_RETRACE_ATR_MIN = float(os.getenv("PUMP_DUMP_RETRACE_ATR_MIN", "0.45") or "0.45")



def _is_recent_macd_cross(df: pd.DataFrame, direction: str, lookback: int = 3) -> bool:
    """Return True when 5M MACD crossed recently for entry timing."""
    try:
        recent = df.tail(max(lookback + 1, 3))
        if len(recent) < 3:
            return False
        for i in range(1, len(recent)):
            prev = recent.iloc[i - 1]
            cur = recent.iloc[i]
            if direction == "LONG" and prev["macd"] <= prev["macd_signal"] and cur["macd"] > cur["macd_signal"]:
                return True
            if direction == "SHORT" and prev["macd"] >= prev["macd_signal"] and cur["macd"] < cur["macd_signal"]:
                return True
    except Exception:
        pass
    return False


def _early_5m_trigger_score(direction: str, trends: Dict[str, str], df_5m: pd.DataFrame, df_15m: pd.DataFrame, df_1h: pd.DataFrame) -> Dict[str, Any]:
    """Score fresh 5M entry timing without lowering the global threshold."""
    try:
        l5 = df_5m.iloc[-1]
        p5 = df_5m.iloc[-2]
        l15 = df_15m.iloc[-1]
        l1 = df_1h.iloc[-1]
    except Exception:
        return {"score": 0, "active": False, "reason": "NO_5M_DATA"}

    direction = str(direction or "").upper().strip()
    score = 0
    reasons = []

    if direction == "LONG":
        aligned_15_1 = trends.get("15M") == "bullish" and trends.get("1H") == "bullish"
        aligned_4 = trends.get("4H") == "bullish"
        aligned_5 = trends.get("5M") == "bullish"
        macd_fresh = _is_recent_macd_cross(df_5m, "LONG") or (l5["macd"] > l5["macd_signal"] and l5["macd_hist"] > p5["macd_hist"])
        rsi_accel = safe_float(l5["rsi"]) >= 50 and safe_float(l5["rsi"]) > safe_float(p5["rsi"])
        ema_reclaim = l5["close"] > l5["ema20"] and p5["close"] <= p5["ema20"]
        vwap_reclaim = l5["close"] > l5["vwap"] and p5["close"] <= p5["vwap"]
        buy3, sell3 = buy_sell_power(df_5m, 3)
        power_ok = buy3 >= 58
        higher_momentum = l15["macd_hist"] >= df_15m.iloc[-2]["macd_hist"] or l1["close"] >= l1["ema20"]

        if aligned_15_1:
            score += 3; reasons.append("1H/15M هم‌جهت")
        if aligned_4:
            score += 2; reasons.append("4H هم‌جهت")
        if aligned_5:
            score += 2; reasons.append("5M هم‌جهت")
        if macd_fresh:
            score += 3; reasons.append("5M MACD تازه/شتاب‌دار")
        if rsi_accel:
            score += 2; reasons.append("5M RSI شتاب‌دار")
        if ema_reclaim or vwap_reclaim:
            score += 2; reasons.append("5M برگشت EMA/VWAP")
        if power_ok:
            score += 2; reasons.append("قدرت خرید 5M")
        if higher_momentum:
            score += 1

    elif direction == "SHORT":
        aligned_15_1 = trends.get("15M") == "bearish" and trends.get("1H") == "bearish"
        aligned_4 = trends.get("4H") == "bearish"
        aligned_5 = trends.get("5M") == "bearish"
        macd_fresh = _is_recent_macd_cross(df_5m, "SHORT") or (l5["macd"] < l5["macd_signal"] and l5["macd_hist"] < p5["macd_hist"])
        rsi_accel = safe_float(l5["rsi"]) <= 50 and safe_float(l5["rsi"]) < safe_float(p5["rsi"])
        ema_reclaim = l5["close"] < l5["ema20"] and p5["close"] >= p5["ema20"]
        vwap_reclaim = l5["close"] < l5["vwap"] and p5["close"] >= p5["vwap"]
        buy3, sell3 = buy_sell_power(df_5m, 3)
        power_ok = sell3 >= 58
        higher_momentum = l15["macd_hist"] <= df_15m.iloc[-2]["macd_hist"] or l1["close"] <= l1["ema20"]

        if aligned_15_1:
            score += 3; reasons.append("1H/15M هم‌جهت")
        if aligned_4:
            score += 2; reasons.append("4H هم‌جهت")
        if aligned_5:
            score += 2; reasons.append("5M هم‌جهت")
        if macd_fresh:
            score += 3; reasons.append("5M MACD تازه/شتاب‌دار")
        if rsi_accel:
            score += 2; reasons.append("5M RSI شتاب‌دار")
        if ema_reclaim or vwap_reclaim:
            score += 2; reasons.append("5M برگشت EMA/VWAP")
        if power_ok:
            score += 2; reasons.append("قدرت فروش 5M")
        if higher_momentum:
            score += 1
    else:
        return {"score": 0, "active": False, "reason": "NO_DIRECTION"}

    final = min(EARLY_5M_BONUS_MAX, max(0, int(score)))
    return {
        "score": final,
        "active": final >= 7,
        "reason": " | ".join(reasons[:6]) if reasons else "NO_EARLY_TRIGGER",
    }


def _multi_tf_alignment_bonus(direction: str, trends: Dict[str, str]) -> Dict[str, Any]:
    """Small bonus only when the major stack agrees with the selected direction."""
    direction = str(direction or "").upper().strip()
    wanted = "bullish" if direction == "LONG" else "bearish" if direction == "SHORT" else ""
    if not wanted:
        return {"score": 0, "active": False}
    aligned = [tf for tf in ("4H", "1H", "15M", "5M") if trends.get(tf) == wanted]
    if len(aligned) >= 4:
        return {"score": MULTI_TF_ALIGNMENT_BONUS, "active": True, "aligned": aligned}
    if len(aligned) == 3 and "1H" in aligned and "15M" in aligned and "5M" in aligned:
        return {"score": 4, "active": True, "aligned": aligned}
    return {"score": 0, "active": False, "aligned": aligned}


def _late_entry_pack(direction: str, df_15m: pd.DataFrame, df_5m: pd.DataFrame, trends: Dict[str, str], early_pack: Dict[str, Any]) -> Dict[str, Any]:
    """Detect chasing after the move has already stretched too far."""
    try:
        dist15 = float(distance_from_ema20_atr(df_15m))
    except Exception:
        dist15 = 0.0
    try:
        dist5 = float(distance_from_ema20_atr(df_5m))
    except Exception:
        dist5 = 0.0

    direction = str(direction or "").upper().strip()
    wanted = "bullish" if direction == "LONG" else "bearish" if direction == "SHORT" else ""
    strong_alignment = bool(wanted and all(trends.get(tf) == wanted for tf in ("1H", "15M", "5M")))
    limit = LATE_ENTRY_STRONG_ALIGNMENT_ATR_LIMIT if strong_alignment and early_pack.get("active") else LATE_ENTRY_ATR_LIMIT

    late = max(dist15, dist5) > limit
    return {
        "late": bool(late),
        "distance_15m_ema20_atr": round(dist15, 3),
        "distance_5m_ema20_atr": round(dist5, 3),
        "limit": round(limit, 3),
        "reason": f"Late Entry: فاصله از EMA20 زیاد است ({round(max(dist15, dist5), 2)} ATR > {round(limit, 2)})" if late else "",
    }


def _pump_dump_chase_pack(direction: str, df_5m: pd.DataFrame, atr_ref: float) -> Dict[str, Any]:
    """Block signals that arrive after most of a fast pump/dump is already done."""
    try:
        recent = df_5m.tail(max(4, PUMP_DUMP_LOOKBACK_5M))
        if len(recent) < 4:
            return {"late": False, "reason": ""}
        atr = max(safe_float(atr_ref), safe_float(df_5m.iloc[-1].get("atr"), 0.0), safe_float(df_5m.iloc[-1].get("close"), 0.0) * 0.0015, 1e-12)
        price = safe_float(df_5m.iloc[-1]["close"])
        hi = safe_float(recent["high"].max())
        lo = safe_float(recent["low"].min())
        move_atr = abs(hi - lo) / atr
        direction = str(direction or "").upper().strip()
        if direction == "SHORT":
            # After a large dump, shorting close to the recent low is chasing.
            from_high = (hi - price) / atr
            retrace_from_low = (price - lo) / atr
            late = move_atr >= PUMP_DUMP_MOVE_ATR_LIMIT and from_high >= (PUMP_DUMP_MOVE_ATR_LIMIT * 0.70) and retrace_from_low < PUMP_DUMP_RETRACE_ATR_MIN
            reason = f"رد شورت: بعد از دامپ بزرگ و نزدیک کف ({round(move_atr, 2)} ATR)" if late else ""
        elif direction == "LONG":
            # After a large pump, longing close to the recent high is chasing.
            from_low = (price - lo) / atr
            retrace_from_high = (hi - price) / atr
            late = move_atr >= PUMP_DUMP_MOVE_ATR_LIMIT and from_low >= (PUMP_DUMP_MOVE_ATR_LIMIT * 0.70) and retrace_from_high < PUMP_DUMP_RETRACE_ATR_MIN
            reason = f"رد لانگ: بعد از پامپ بزرگ و نزدیک سقف ({round(move_atr, 2)} ATR)" if late else ""
        else:
            late = False; reason = ""
        return {"late": bool(late), "move_atr": round(move_atr, 3), "reason": reason}
    except Exception:
        return {"late": False, "reason": ""}


def _live_entry_price(symbol: str, direction: str, df_5m: pd.DataFrame, fallback_price: float) -> float:
    """Prefer current 5M/latest ticker for order entry so signals are not one closed 15M candle late."""
    fallback_price = safe_float(fallback_price)
    if not USE_CURRENT_5M_FOR_ENTRY:
        return fallback_price
    try:
        if LIVE_ENTRY_PRICE_SOURCE in {"ticker", "last"}:
            t = exchange.fetch_ticker(to_okx_symbol(symbol))
            px = safe_float(t.get("last") or t.get("close") or t.get("bid") or t.get("ask"), 0.0)
            if px > 0:
                return px
    except Exception:
        pass
    try:
        px = safe_float(df_5m.iloc[-1]["close"], 0.0)
        return px if px > 0 else fallback_price
    except Exception:
        return fallback_price


def to_okx_symbol(symbol: str) -> str:
    coin = str(symbol).upper().replace('USDT','')
    return f'{coin}/USDT:USDT'

def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None: return default
        v = float(value)
        return default if math.isnan(v) or math.isinf(v) else v
    except Exception: return default

def safe_round(value: Any, digits: int = 8):
    try: return None if value is None else round(float(value), digits)
    except Exception: return None

def cap_score(value: Any) -> int:
    try: return max(0, min(100, int(round(float(value)))))
    except Exception: return 0

def get_klines(symbol: str, interval: str = '15m', limit: int = 260, include_current: bool = False) -> pd.DataFrame:
    data = exchange.fetch_ohlcv(to_okx_symbol(symbol), timeframe=interval, limit=limit)
    if not data or len(data) < 220: raise Exception(f'داده کافی برای {symbol} در تایم {interval} دریافت نشد')
    df = pd.DataFrame(data, columns=['time','open','high','low','close','volume'])
    for c in ['open','high','low','close','volume']: df[c] = pd.to_numeric(df[c], errors='coerce')
    df = df.dropna()
    if not include_current: df = df.iloc[:-1]
    if len(df) < 210: raise Exception(f'داده کندل کافی برای {symbol} در تایم {interval} کامل نیست')
    return df

def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df['ema20'] = ta.trend.ema_indicator(df['close'], window=20)
    df['ema50'] = ta.trend.ema_indicator(df['close'], window=50)
    df['ema200'] = ta.trend.ema_indicator(df['close'], window=200)
    df['rsi'] = ta.momentum.rsi(df['close'], window=14)
    macd = ta.trend.MACD(df['close']); df['macd'] = macd.macd(); df['macd_signal'] = macd.macd_signal(); df['macd_hist'] = macd.macd_diff()
    df['atr'] = ta.volatility.average_true_range(df['high'], df['low'], df['close'], window=14)
    adx = ta.trend.ADXIndicator(df['high'], df['low'], df['close'], window=14); df['adx'] = adx.adx()
    typical = (df['high'] + df['low'] + df['close']) / 3
    volume_sum = df['volume'].cumsum().replace(0, pd.NA)
    df['vwap'] = (typical * df['volume']).cumsum() / volume_sum
    df['volume_ma20'] = df['volume'].rolling(20).mean(); df['volume_ratio'] = df['volume'] / df['volume_ma20'].replace(0, pd.NA)
    df = df.dropna()
    if len(df) < 60: raise Exception('اندیکاتورها کامل محاسبه نشدند')
    return df

def ema_direction(df):
    last=df.iloc[-1]
    if last['ema50'] > last['ema200']: return 'bullish'
    if last['ema50'] < last['ema200']: return 'bearish'
    return 'range'

def trend_direction(df):
    last=df.iloc[-1]; close=safe_float(last['close'])
    if last['ema50'] > last['ema200']: return 'bullish' if close > last['ema50'] else 'weak_bullish'
    if last['ema50'] < last['ema200']: return 'bearish' if close < last['ema50'] else 'weak_bearish'
    return 'range'

def vwap_status(df):
    last=df.iloc[-1]
    if last['close'] > last['vwap']: return 'above_vwap'
    if last['close'] < last['vwap']: return 'below_vwap'
    return 'near_vwap'

def distance_from_ema20_atr(df):
    last=df.iloc[-1]; price=safe_float(last['close']); atr=max(safe_float(last['atr']), price*0.0015)
    return abs(price-safe_float(last['ema20']))/atr

def volume_quality(df):
    ratio=safe_float(df.iloc[-1].get('volume_ratio',1.0),1.0)
    if ratio>=1.35: return 'high_volume', ratio
    if ratio>=0.90: return 'normal_volume', ratio
    if ratio<=0.65: return 'weak_volume', ratio
    return 'neutral_volume', ratio

def buy_sell_power(df, candles=20):
    r=df.tail(candles); green=r[r['close']>r['open']]['volume'].sum(); red=r[r['close']<r['open']]['volume'].sum(); total=green+red
    if total<=0: return 50.0, 50.0
    return round(green/total*100,1), round(red/total*100,1)

def get_coin_risk(symbol, direction):
    default={'sl_count':0,'tp_count':0,'strictness_level':0,'risk_score':0}
    if not get_direction_risk_state: return default
    try:
        r=get_direction_risk_state(symbol,direction); default.update(r if isinstance(r,dict) else {})
    except Exception: pass
    return default

def get_rotation_context(symbol):
    default={'rotation_score':50,'priority_score':50,'risk_score':0,'status':'NORMAL'}
    if not get_coin_rotation_score: return default
    try:
        r=get_coin_rotation_score(symbol); default.update(r if isinstance(r,dict) else {})
    except Exception: pass
    return default

def ai_extra_strength_required(symbol, direction, snapshot):
    default={'required':False,'extra_score':0,'extra_confirmations':0,'reason':None}
    if not should_require_extra_strength: return default
    try:
        r=should_require_extra_strength(symbol,direction,snapshot)
        if isinstance(r,dict): default.update(r)
    except Exception: pass
    return default


def _soft_trend_analysis_pack(df_15m: pd.DataFrame) -> Dict[str, Any]:
    """Optional trendline/breakout pack with deliberately soft weights.

    trend_analysis.py returns simple labels. This helper makes them useful
    without letting them dominate Market Mode, BTC, Trap, Similarity, Risk, or
    the final AI ranking.
    """
    out = {
        'enabled': bool(callable(detect_trendline) and callable(detect_breakout)),
        'trendline': 'unavailable',
        'breakout': 'unavailable',
        'long_score': 0,
        'short_score': 0,
        'soft_layer': True,
    }
    if not out['enabled']:
        return out
    try:
        trendline = detect_trendline(df_15m)
        breakout = detect_breakout(df_15m)
        out['trendline'] = trendline
        out['breakout'] = breakout

        if trendline == 'uptrend':
            out['long_score'] += SOFT_TRENDLINE_MAX_SCORE
        elif trendline == 'downtrend':
            out['short_score'] += SOFT_TRENDLINE_MAX_SCORE

        if breakout == 'bullish_breakout':
            out['long_score'] += SOFT_BREAKOUT_MAX_SCORE
        elif breakout == 'bearish_breakout':
            out['short_score'] += SOFT_BREAKOUT_MAX_SCORE
        elif breakout == 'fake_bullish_breakout':
            out['short_score'] += SOFT_FAKE_BREAKOUT_MAX_SCORE
        elif breakout == 'fake_bearish_breakout':
            out['long_score'] += SOFT_FAKE_BREAKOUT_MAX_SCORE

        out['long_score'] = int(max(0, min(12, out['long_score'])))
        out['short_score'] = int(max(0, min(12, out['short_score'])))
    except Exception as e:
        out['enabled'] = False
        out['error'] = str(e)[:120]
    return out


# ---------------------------------------------------------------------------
# Soft AI feature packs for prediction/learning
# ---------------------------------------------------------------------------
def _slope(series, periods: int = 3) -> float:
    try:
        s = pd.Series(series).dropna().tail(max(periods + 1, 2))
        if len(s) < 2:
            return 0.0
        return safe_float(s.iloc[-1]) - safe_float(s.iloc[0])
    except Exception:
        return 0.0


def _pct_distance(a, b) -> float:
    a = safe_float(a); b = safe_float(b)
    return ((a - b) / max(abs(b), 1e-12)) * 100.0


def _candle_quality_pack(df: pd.DataFrame, lookback: int = 6) -> Dict[str, Any]:
    try:
        r = df.tail(max(lookback, 3)).copy()
        last = r.iloc[-1]
        rng = max(safe_float(last['high']) - safe_float(last['low']), 1e-12)
        body = abs(safe_float(last['close']) - safe_float(last['open']))
        upper_wick = safe_float(last['high']) - max(safe_float(last['close']), safe_float(last['open']))
        lower_wick = min(safe_float(last['close']), safe_float(last['open'])) - safe_float(last['low'])
        close_pos = (safe_float(last['close']) - safe_float(last['low'])) / rng
        body_ratio = body / rng
        same_dir = 0
        bull = safe_float(last['close']) >= safe_float(last['open'])
        for _, row in r.iloc[::-1].iterrows():
            rbull = safe_float(row['close']) >= safe_float(row['open'])
            if rbull == bull:
                same_dir += 1
            else:
                break
        return {
            'body_ratio': round(body_ratio, 4),
            'upper_wick_ratio': round(upper_wick / rng, 4),
            'lower_wick_ratio': round(lower_wick / rng, 4),
            'close_position': round(close_pos, 4),
            'same_direction_candles': int(same_dir),
            'candle_bias': 'bullish' if bull else 'bearish',
            'strong_close_up': bool(close_pos >= 0.72 and body_ratio >= 0.45),
            'strong_close_down': bool(close_pos <= 0.28 and body_ratio >= 0.45),
            'upper_rejection': bool(upper_wick / rng >= 0.45),
            'lower_rejection': bool(lower_wick / rng >= 0.45),
        }
    except Exception:
        return {}


def _compression_expansion_pack(df: pd.DataFrame, lookback: int = 24) -> Dict[str, Any]:
    try:
        recent = df.tail(max(lookback, 12))
        last = recent.iloc[-1]
        atr_now = safe_float(last.get('atr'), 0.0)
        atr_avg = safe_float(recent['atr'].tail(lookback).mean(), atr_now)
        rng_now = safe_float(last['high']) - safe_float(last['low'])
        rng_avg = safe_float((recent['high'] - recent['low']).tail(lookback).mean(), rng_now)
        vol_ratio = safe_float(last.get('volume_ratio'), 1.0)
        compression = atr_now <= atr_avg * 0.82 or rng_now <= rng_avg * 0.75
        expansion = rng_now >= rng_avg * 1.25 and vol_ratio >= 1.15
        return {
            'atr_now': safe_round(atr_now, 8), 'atr_avg': safe_round(atr_avg, 8),
            'range_to_avg': round(rng_now / max(rng_avg, 1e-12), 3),
            'volume_ratio': round(vol_ratio, 3),
            'compression': bool(compression), 'expansion': bool(expansion),
            'compression_to_expansion': bool(compression and expansion),
        }
    except Exception:
        return {}


def _relative_strength_pack(symbol: str, df_15m: pd.DataFrame, market_context: Dict[str, Any]) -> Dict[str, Any]:
    try:
        r = df_15m.tail(5)
        coin_move = _pct_distance(r.iloc[-1]['close'], r.iloc[0]['close'])
        btc_bias = market_context.get('btc_bias', 'neutral')
        market_regime = market_context.get('market_regime', 'neutral')
        status = 'neutral'
        btc_legacy = _soft_bias_for_legacy(btc_bias)
        if coin_move > 0.45 and btc_legacy != 'bullish': status = 'relative_strength'
        if coin_move < -0.45 and btc_legacy != 'bearish': status = 'relative_weakness'
        return {'coin_15m_5bar_move_pct': round(coin_move, 4), 'relative_status': status, 'btc_bias': btc_bias, 'market_regime': market_regime}
    except Exception:
        return {}


def _liquidity_trap_pack(direction: str, price: float, atr: float, df_15m: pd.DataFrame, level_pack: Dict[str, Any]) -> Dict[str, Any]:
    try:
        supports = level_pack.get('supports', []) or []
        resistances = level_pack.get('resistances', []) or []
        candle = _candle_quality_pack(df_15m, 6)
        fake_risk = _recent_fake_break_risk(df_15m, direction, price, atr, supports, resistances)
        nearest_above = min([safe_float(x.get('price')) for x in resistances if safe_float(x.get('price')) > price], default=None)
        nearest_below = max([safe_float(x.get('price')) for x in supports if safe_float(x.get('price')) < price], default=None)
        dist_above_atr = None if nearest_above is None else (nearest_above - price) / max(atr, 1e-12)
        dist_below_atr = None if nearest_below is None else (price - nearest_below) / max(atr, 1e-12)
        long_trap = direction == 'LONG' and fake_risk >= 0.34 and candle.get('upper_rejection')
        short_trap = direction == 'SHORT' and fake_risk >= 0.34 and candle.get('lower_rejection')
        return {
            'fake_break_risk': round(fake_risk, 3),
            'nearest_liquidity_above': safe_round(nearest_above),
            'nearest_liquidity_below': safe_round(nearest_below),
            'distance_above_atr': None if dist_above_atr is None else round(dist_above_atr, 3),
            'distance_below_atr': None if dist_below_atr is None else round(dist_below_atr, 3),
            'trap_risk': 'HIGH' if (long_trap or short_trap) else 'MEDIUM' if fake_risk >= 0.34 else 'LOW',
            'long_trap_risk': bool(long_trap), 'short_trap_risk': bool(short_trap),
        }
    except Exception:
        return {}


def _early_momentum_pack(direction: str, df_15m: pd.DataFrame, df_5m: pd.DataFrame) -> Dict[str, Any]:
    try:
        l15 = df_15m.iloc[-1]; p15 = df_15m.iloc[-2]
        rsi_slope = _slope(df_15m['rsi'], 3)
        adx_slope = _slope(df_15m['adx'], 3)
        hist_slope = _slope(df_15m['macd_hist'], 3)
        hist_accel = (safe_float(l15['macd_hist']) - safe_float(p15['macd_hist'])) - (safe_float(p15['macd_hist']) - safe_float(df_15m.iloc[-3]['macd_hist']))
        vol_ratio = safe_float(l15.get('volume_ratio'), 1.0)
        score = 50
        if direction == 'LONG':
            if rsi_slope > 0: score += 9
            if hist_slope > 0: score += 11
            if hist_accel > 0: score += 7
            if safe_float(l15['close']) > safe_float(l15['vwap']): score += 6
        elif direction == 'SHORT':
            if rsi_slope < 0: score += 9
            if hist_slope < 0: score += 11
            if hist_accel < 0: score += 7
            if safe_float(l15['close']) < safe_float(l15['vwap']): score += 6
        if adx_slope > 0: score += 8
        if vol_ratio >= 1.2: score += 6
        comp = _compression_expansion_pack(df_15m)
        if comp.get('expansion'): score += 5
        return {
            'early_momentum_score': cap_score(score),
            'rsi_slope_15m': round(rsi_slope, 4),
            'adx_slope_15m': round(adx_slope, 4),
            'macd_hist_slope_15m': round(hist_slope, 8),
            'macd_hist_accel_15m': round(hist_accel, 8),
            'volume_ratio_15m': round(vol_ratio, 3),
            'compression': comp,
        }
    except Exception:
        return {'early_momentum_score': 50}


def _state_awareness_pack(direction: str, df_15m: pd.DataFrame, df_5m: pd.DataFrame) -> Dict[str, Any]:
    try:
        dist = distance_from_ema20_atr(df_15m)
        mom = _early_momentum_pack(direction, df_15m, df_5m)
        candle = _candle_quality_pack(df_15m)
        score = safe_float(mom.get('early_momentum_score'), 50)
        state = 'START'
        if dist >= 1.55 and score < 68:
            state = 'LATE_OR_EXHAUSTION'
        elif dist >= 1.25:
            state = 'MID_MOVE'
        elif score >= 70:
            state = 'EARLY_MOMENTUM'
        reversal_risk = 25
        if state == 'LATE_OR_EXHAUSTION': reversal_risk += 35
        if direction == 'LONG' and candle.get('upper_rejection'): reversal_risk += 15
        if direction == 'SHORT' and candle.get('lower_rejection'): reversal_risk += 15
        return {'move_state': state, 'distance_ema20_atr_15m': round(dist, 3), 'reversal_risk_score': cap_score(reversal_risk), 'momentum': mom, 'candle': candle}
    except Exception:
        return {'move_state': 'UNKNOWN', 'reversal_risk_score': 50}


def _prediction_layer_pack(symbol: str, direction: str, df_15m: pd.DataFrame, df_5m: pd.DataFrame, market_context: Dict[str, Any], level_pack: Dict[str, Any]) -> Dict[str, Any]:
    price = safe_float(df_15m.iloc[-1]['close'])
    atr = max(safe_float(df_15m.iloc[-1]['atr']), price * 0.0015)
    momentum = _early_momentum_pack(direction, df_15m, df_5m)
    state = _state_awareness_pack(direction, df_15m, df_5m)
    candle = _candle_quality_pack(df_15m)
    liquidity = _liquidity_trap_pack(direction, price, atr, df_15m, level_pack)
    rel = _relative_strength_pack(symbol, df_15m, market_context)
    prediction = safe_float(momentum.get('early_momentum_score'), 50)
    if state.get('move_state') == 'LATE_OR_EXHAUSTION': prediction -= 14
    if liquidity.get('trap_risk') == 'HIGH': prediction -= 14
    elif liquidity.get('trap_risk') == 'MEDIUM': prediction -= 5
    if rel.get('relative_status') in ('relative_strength', 'relative_weakness'): prediction += 5
    if direction == 'LONG' and candle.get('strong_close_up'): prediction += 5
    if direction == 'SHORT' and candle.get('strong_close_down'): prediction += 5
    reversal = safe_float(state.get('reversal_risk_score'), 50)
    if liquidity.get('trap_risk') == 'HIGH': reversal += 15
    expected_move_atr = max(0.35, min(1.65, (prediction - 45) / 35.0))
    return {
        'prediction_score': cap_score(prediction),
        'expected_move_atr': round(expected_move_atr, 3),
        'reversal_risk_score': cap_score(reversal),
        'state': state,
        'candle_behavior': candle,
        'liquidity_trap': liquidity,
        'relative_strength': rel,
        'soft_layer': True,
    }

def build_local_snapshot(symbol, direction, df_4h, df_1h, df_30m, df_15m, df_5m, score_pack, market_context):
    l15=df_15m.iloc[-1]; l5=df_5m.iloc[-1]
    buy2,sell2=buy_sell_power(df_5m,2); buy3,sell3=buy_sell_power(df_5m,3); buy20,sell20=buy_sell_power(df_5m,20)
    price=safe_float(l15['close']); atr=max(safe_float(l15['atr']), price*0.0015)
    levels=get_strong_levels(df_5m,df_15m,df_30m,price,atr)
    prediction_pack=_prediction_layer_pack(symbol, direction, df_15m, df_5m, market_context, levels)
    snap={
        'symbol':symbol,'direction':direction,'timeframe_core':'15M','entry_timing_tf':'5M',
        'price':price,'entry':price,
        'rsi':safe_float(l15['rsi']),'rsi_5m':safe_float(l5['rsi']),
        'rsi_slope_15m':prediction_pack.get('state',{}).get('momentum',{}).get('rsi_slope_15m'),
        'macd':safe_float(l15['macd']),'macd_signal':safe_float(l15['macd_signal']),'macd_hist':safe_float(l15['macd_hist']),
        'macd_hist_slope_15m':prediction_pack.get('state',{}).get('momentum',{}).get('macd_hist_slope_15m'),
        'macd_hist_accel_15m':prediction_pack.get('state',{}).get('momentum',{}).get('macd_hist_accel_15m'),
        'adx':safe_float(l15['adx']),'adx_slope_15m':prediction_pack.get('state',{}).get('momentum',{}).get('adx_slope_15m'),
        'atr':atr,'ema20':safe_float(l15['ema20']),'ema50':safe_float(l15['ema50']),'ema200':safe_float(l15['ema200']),
        'ema_structure_15m':'bullish_stack' if l15['ema20']>l15['ema50']>l15['ema200'] else 'bearish_stack' if l15['ema20']<l15['ema50']<l15['ema200'] else 'mixed',
        'vwap':safe_float(l15['vwap']),'vwap_status':vwap_status(df_15m),'vwap_distance_pct':round(_pct_distance(l15['close'], l15['vwap']),4),
        'power2_buy':buy2,'power2_sell':sell2,'power3_buy':buy3,'power3_sell':sell3,'buy_power':buy20,'sell_power':sell20,
        'volume_ratio_15m':safe_float(l15.get('volume_ratio'),1.0),
        'candle_behavior':prediction_pack.get('candle_behavior',{}),
        'state_awareness':prediction_pack.get('state',{}),
        'prediction_layer':prediction_pack,
        'liquidity_trap':prediction_pack.get('liquidity_trap',{}),
        'relative_strength':prediction_pack.get('relative_strength',{}),
        'sr_levels':{'nearest_support':levels.get('nearest_support'),'nearest_resistance':levels.get('nearest_resistance')},
        'trend_analysis':score_pack.get('trend_analysis',{}),
        'early_5m_trigger':score_pack.get('early_5m_trigger',{}),'multi_tf_alignment':score_pack.get('multi_tf_alignment',{}),
        'late_entry':score_pack.get('late_entry',{}),'pump_dump_chase':score_pack.get('pump_dump_chase',{}),
        'trends':score_pack.get('trends',{}),'long_score':score_pack.get('long_score',0),'short_score':score_pack.get('short_score',0),
        'market_regime':market_context.get('market_regime','NEUTRAL'),'btc_bias':market_context.get('btc_bias','NEUTRAL'),'btc_lead':market_context.get('btc_lead',{}),'market_regime_legacy':market_context.get('market_regime_legacy','neutral'),'btc_bias_legacy':market_context.get('btc_bias_legacy','neutral'),
        'learning_note':'candidate snapshot: classic is soft; AI/ghost/meta learning should evaluate this condition per coin+direction',
    }
    if build_signal_snapshot:
        try:
            extra=build_signal_snapshot(symbol, direction, snap, market_context)
            if isinstance(extra,dict): snap.update(extra)
        except Exception: pass
    return snap

def simple_classic_score(symbol, df_4h, df_1h, df_30m, df_15m, df_5m, market_context=None):
    market_context=market_context or {}; long_score=0.0; short_score=0.0; long_reasons=[]; short_reasons=[]; cl=0; cs=0
    trends={'4H':ema_direction(df_4h),'1H':ema_direction(df_1h),'30M':ema_direction(df_30m),'15M':ema_direction(df_15m),'5M':ema_direction(df_5m)}
    l1=df_1h.iloc[-1]; l30=df_30m.iloc[-1]; l15=df_15m.iloc[-1]; p15=df_15m.iloc[-2]; l5=df_5m.iloc[-1]; p5=df_5m.iloc[-2]
    weights={'4H':7,'1H':18,'30M':12,'15M':25,'5M':8}
    for tf,tr in trends.items():
        if tr=='bullish': long_score+=weights[tf]; cl += 1 if tf in ['1H','30M','15M'] else 0; long_reasons.append(f'{tf}: روند صعودی')
        elif tr=='bearish': short_score+=weights[tf]; cs += 1 if tf in ['1H','30M','15M'] else 0; short_reasons.append(f'{tf}: روند نزولی')
    if l15['ema20']>l15['ema50']>l15['ema200']: long_score+=10; cl+=1; long_reasons.append('15M EMA stack صعودی')
    elif l15['ema20']<l15['ema50']<l15['ema200']: short_score+=10; cs+=1; short_reasons.append('15M EMA stack نزولی')
    if l1['ema20']>l1['ema50']>l1['ema200']: long_score+=7; cl+=1
    elif l1['ema20']<l1['ema50']<l1['ema200']: short_score+=6
    if l15['close']>l15['ema20']: long_score+=10; cl+=1
    else: short_score+=10; cs+=1
    if l5['close']>l5['ema20']: long_score+=6
    else: short_score+=6
    rsi15=safe_float(l15['rsi']); rsi30=safe_float(l30['rsi']); rsi5=safe_float(l5['rsi'])
    if 52<=rsi15<=66: long_score+=12; cl+=1
    elif 32<=rsi15<=50: short_score+=12; cs+=1
    elif rsi15>72: long_score-=6
    elif rsi15<28: short_score-=5
    if rsi15>safe_float(p15['rsi']): long_score+=4
    elif rsi15<safe_float(p15['rsi']): short_score+=4
    if rsi30>=50: long_score+=4
    else: short_score+=4
    if rsi5>=50: long_score+=3
    else: short_score+=3
    if rsi5>safe_float(p5['rsi']): long_score+=2
    elif rsi5<safe_float(p5['rsi']): short_score+=2
    if l15['macd']>l15['macd_signal']: long_score+=15; cl+=1
    else: short_score+=15; cs+=1
    if l15['macd_hist']>0: long_score+=5
    else: short_score+=5
    if l15['macd_hist']>p15['macd_hist']: long_score+=5
    else: short_score+=5
    if l30['macd']>l30['macd_signal']: long_score+=7
    else: short_score+=7
    if l5['macd']>l5['macd_signal']: long_score+=6
    else: short_score+=6
    adx=safe_float(l15['adx'])
    if adx>=35: long_score+=8; short_score+=8
    elif adx>=25: long_score+=5; short_score+=5
    elif adx>=ADX_HARD_MIN: long_score+=1; short_score+=1
    else: long_score=min(long_score,69); short_score=min(short_score,69); long_reasons.append('رد: ADX پایین'); short_reasons.append('رد: ADX پایین')
    if l15['close']>l15['vwap']: long_score+=4; short_score-=4
    else: short_score+=4; long_score-=4
    mb_raw=market_context.get('market_regime','neutral')
    mb=_soft_bias_for_legacy(mb_raw)
    btc_lead_effect=safe_float((market_context.get('btc_lead') or {}).get('effect'), 0.0)
    if mb=='bullish': long_score+=3; short_score-=3
    elif mb=='bearish': short_score+=3; long_score-=3
    if btc_lead_effect > 0:
        long_score += min(4, btc_lead_effect); short_score -= min(4, btc_lead_effect)
    elif btc_lead_effect < 0:
        short_score += min(4, abs(btc_lead_effect)); long_score -= min(4, abs(btc_lead_effect))
    buy2,sell2=buy_sell_power(df_5m,2); buy3,sell3=buy_sell_power(df_5m,3); buy20,sell20=buy_sell_power(df_5m,20)
    if buy3>=62: long_score+=3
    if sell3>=62: short_score+=3

    # Optional trendline/breakout layer stays soft. It helps direction detection
    # but never decides the signal alone.
    trend_pack = _soft_trend_analysis_pack(df_15m)
    if safe_float(trend_pack.get('long_score'), 0) > 0:
        long_score += safe_float(trend_pack.get('long_score'))
        long_reasons.append(f"Trend soft لانگ +{trend_pack.get('long_score')} ({trend_pack.get('trendline')}/{trend_pack.get('breakout')})")
    if safe_float(trend_pack.get('short_score'), 0) > 0:
        short_score += safe_float(trend_pack.get('short_score'))
        short_reasons.append(f"Trend soft شورت +{trend_pack.get('short_score')} ({trend_pack.get('trendline')}/{trend_pack.get('breakout')})")

    # Fast-entry upgrades:
    # - Do not lower threshold.
    # - Reward only fresh 5M triggers when higher timeframes agree.
    # - Penalize clearly late/chasing entries.
    long_alignment = _multi_tf_alignment_bonus("LONG", trends)
    short_alignment = _multi_tf_alignment_bonus("SHORT", trends)
    long_early = _early_5m_trigger_score("LONG", trends, df_5m, df_15m, df_1h)
    short_early = _early_5m_trigger_score("SHORT", trends, df_5m, df_15m, df_1h)
    long_late = _late_entry_pack("LONG", df_15m, df_5m, trends, long_early)
    short_late = _late_entry_pack("SHORT", df_15m, df_5m, trends, short_early)
    long_chase = _pump_dump_chase_pack("LONG", df_5m, safe_float(l15.get("atr")))
    short_chase = _pump_dump_chase_pack("SHORT", df_5m, safe_float(l15.get("atr")))

    if long_alignment.get("score"):
        long_score += safe_float(long_alignment.get("score"))
        long_reasons.append("Multi-TF هم‌جهت لانگ")
    if short_alignment.get("score"):
        short_score += safe_float(short_alignment.get("score"))
        short_reasons.append("Multi-TF هم‌جهت شورت")

    if long_early.get("score"):
        long_score += safe_float(long_early.get("score"))
        if long_early.get("active"):
            cl += 1
        long_reasons.append(f"5M Early Trigger لانگ +{long_early.get('score')}")
    if short_early.get("score"):
        short_score += safe_float(short_early.get("score"))
        if short_early.get("active"):
            cs += 1
        short_reasons.append(f"5M Early Trigger شورت +{short_early.get('score')}")

    if long_late.get("late"):
        long_score -= 10
        long_reasons.append(long_late.get("reason") or "رد لانگ: ورود دیرهنگام")
    if short_late.get("late"):
        short_score -= 10
        short_reasons.append(short_late.get("reason") or "رد شورت: ورود دیرهنگام")

    if long_chase.get("late"):
        long_score -= 18
        long_reasons.append(long_chase.get("reason") or "رد لانگ: دنبال‌کردن پامپ")
    if short_chase.get("late"):
        short_score -= 18
        short_reasons.append(short_chase.get("reason") or "رد شورت: دنبال‌کردن دامپ")
    # Balanced auto-signal rules: normal classic gates + soft escape for very strong technical signals.
    # The bot should stay technical and medium-soft; AI/risk modules can tighten later.
    long_direction_ok=(trends['15M']=='bullish') or (trends['1H']=='bullish' and trends['5M']=='bullish') or (trends['30M']=='bullish' and safe_float(l15['rsi'])>=50)
    short_direction_ok=(trends['15M']=='bearish') or (trends['1H']=='bearish' and trends['5M']=='bearish') or (trends['30M']=='bearish' and safe_float(l15['rsi'])<=50)
    long_macd_ok=(l15['macd']>l15['macd_signal']) or (l5['macd']>l5['macd_signal'] and l15['macd_hist']>=safe_float(p15['macd_hist']))
    short_macd_ok=(l15['macd']<=l15['macd_signal']) or (l5['macd']<=l5['macd_signal'] and l15['macd_hist']<=safe_float(p15['macd_hist']))

    # Soft direction rescue: do not kill a very strong signal only because one timeframe gate is mixed.
    # Still requires strong score, trend strength, momentum, RSI side, and power/volume pressure.
    if (not long_direction_ok) and long_score>=95 and adx>=28 and long_macd_ok and safe_float(l15['rsi'])>=50 and (buy3>=58 or buy20>=60) and (trends['5M']=='bullish' or trends['4H']=='bullish') and l15['close']>=l15['vwap']:
        long_direction_ok=True
    if (not short_direction_ok) and short_score>=95 and adx>=28 and short_macd_ok and safe_float(l15['rsi'])<=50 and (sell3>=58 or sell20>=60) and (trends['5M']=='bearish' or trends['4H']=='bearish') and l15['close']<=l15['vwap']:
        short_direction_ok=True

    long_1h_ok=(trends['1H']=='bullish' and l1['close']>l1['ema20'] and l1['macd']>=l1['macd_signal']) if LONG_MIN_1H_STRICT else True
    long_vwap_ok=(l15['close']>=l15['vwap']) if LONG_BLOCK_IF_AGAINST_VWAP else True
    if not long_direction_ok: long_reasons.append('رد لانگ: 1H و 15M صعودی نیستند')
    if not long_macd_ok: long_reasons.append('رد لانگ: MACD کافی نیست')
    if not long_1h_ok: long_reasons.append('رد لانگ: تایید 1H کافی نیست')
    if not long_vwap_ok: long_reasons.append('رد لانگ: خلاف VWAP')
    if not short_direction_ok: short_reasons.append('رد شورت: جهت کافی نیست')
    if not short_macd_ok: short_reasons.append('رد شورت: MACD کافی نیست')
    return {'long_score':cap_score(long_score),'short_score':cap_score(short_score),'long_reasons':long_reasons,'short_reasons':short_reasons,'confirmations_long':cl,'confirmations_short':cs,'trends':trends,'distance_ema20_atr':round(distance_from_ema20_atr(df_15m),2),'volume_status':volume_quality(df_15m)[0],'volume_ratio':round(volume_quality(df_15m)[1],2),'power2_buy':buy2,'power2_sell':sell2,'power3_buy':buy3,'power3_sell':sell3,'buy_power':buy20,'sell_power':sell20,'trend_analysis':trend_pack,'early_5m_trigger':{'LONG':long_early,'SHORT':short_early},'multi_tf_alignment':{'LONG':long_alignment,'SHORT':short_alignment},'late_entry':{'LONG':long_late,'SHORT':short_late},'pump_dump_chase':{'LONG':long_chase,'SHORT':short_chase},'long_valid':adx>=ADX_HARD_MIN and long_direction_ok and long_macd_ok and long_1h_ok and long_vwap_ok and not long_late.get('late') and not long_chase.get('late'),'short_valid':adx>=ADX_HARD_MIN and short_direction_ok and short_macd_ok and not short_late.get('late') and not short_chase.get('late'),'adx_15':adx,'market_regime':mb}

def find_swing_levels(df, timeframe, lookback=LEVEL_LOOKBACK, window=SWING_WINDOW):
    recent=df.tail(lookback).copy(); levels=[]; w=TF_LEVEL_WEIGHTS.get(timeframe,1.0)
    if len(recent)<window*2+10: return levels
    for i in range(window, len(recent)-window):
        row=recent.iloc[i]; left=recent.iloc[i-window:i]; right=recent.iloc[i+1:i+1+window]; rec=1.0+(i/max(len(recent),1))*0.8
        if row['low']<=left['low'].min() and row['low']<=right['low'].min(): levels.append({'price':safe_float(row['low']),'kind':'support','timeframe':timeframe,'strength':w*rec})
        if row['high']>=left['high'].max() and row['high']>=right['high'].max(): levels.append({'price':safe_float(row['high']),'kind':'resistance','timeframe':timeframe,'strength':w*rec})
    return levels

def cluster_levels(raw, price, atr):
    if not raw: return []
    raw=sorted(raw,key=lambda x:x['price']); md=max(atr*0.25, price*0.001); clusters=[]
    for lv in raw:
        if not clusters or abs(lv['price']-clusters[-1][-1]['price'])>md: clusters.append([lv])
        else: clusters[-1].append(lv)
    out=[]
    for g in clusters:
        strength=sum(safe_float(x.get('strength')) for x in g); wp=sum(safe_float(x['price'])*safe_float(x['strength']) for x in g)/max(strength,1e-9); tfs=sorted(set(x['timeframe'] for x in g)); sup=sum(1 for x in g if x['kind']=='support'); kind='support' if sup>=len(g)-sup else 'resistance'
        out.append({'price':wp,'kind':kind,'strength':round(strength+min(len(g),5)*0.9+len(tfs)*0.8,2),'touches':len(g),'timeframes':tfs})
    return out

def get_strong_levels(df_5m, df_15m, df_30m, price, atr):
    raw=find_swing_levels(df_5m,'5M')+find_swing_levels(df_15m,'15M')+find_swing_levels(df_30m,'30M')
    clustered=cluster_levels(raw,price,atr); supports=[x for x in clustered if x['price']<price]; resistances=[x for x in clustered if x['price']>price]
    supports=sorted(supports,key=lambda x:(x['strength'],-abs(price-x['price'])),reverse=True); resistances=sorted(resistances,key=lambda x:(x['strength'],-abs(x['price']-price)),reverse=True)
    return {'supports':supports,'resistances':resistances,'nearest_support':max([x['price'] for x in supports], default=price-atr*MIN_SL_ATR_MULTIPLIER),'nearest_resistance':min([x['price'] for x in resistances], default=price+atr*TP1_FALLBACK_ATR)}

def coin_volatility_factor(df_15m, price):
    try:
        atr_pct=safe_float(df_15m.iloc[-1]['atr'])/max(price,1e-12); avg=((df_15m.tail(96)['high']-df_15m.tail(96)['low'])/df_15m.tail(96)['close'].replace(0,pd.NA)).mean(); raw=max(float(atr_pct),float(avg))
    except Exception: raw=0.004
    if raw>=0.012: return 1.25
    if raw>=0.008: return 1.15
    if raw<=0.003: return 0.95
    return 1.0

def select_level_for_sl(direction, price, atr, levels, base_distance):
    valid=[lv for lv in levels if base_distance*0.45 <= abs(price-safe_float(lv['price'])) <= atr*MAX_REASONABLE_SL_ATR]
    if not valid: return None
    valid.sort(key=lambda x:x['strength'], reverse=True); return safe_float(valid[0]['price'])

def select_level_for_tp(direction, price, atr, levels, fallback_mult, buffer):
    candidates=[]; min_d=atr*MIN_TP1_ATR; max_d=atr*2.20
    for lv in levels:
        lp=safe_float(lv['price']); target=lp-buffer if direction=='LONG' else lp+buffer; d=abs(target-price)
        if direction=='LONG' and target<=price: continue
        if direction=='SHORT' and target>=price: continue
        if min_d<=d<=max_d: candidates.append((lv['strength'],-d,target))
    if candidates: candidates.sort(reverse=True); return safe_float(candidates[0][2])
    return price+atr*fallback_mult if direction=='LONG' else price-atr*fallback_mult

def get_ai_tp_memory(symbol, direction, price, atr, snapshot):
    if not get_smart_tp_suggestion: return {}
    try: return get_smart_tp_suggestion(symbol, direction, snapshot) or {}
    except Exception: return {}

def merge_tp_with_ai_memory(direction, price, atr, sr_tp1, sr_tp2, ai_tp):
    tp1, tp2=sr_tp1, sr_tp2; mind=max(atr*MIN_TP1_ATR, price*0.0015)
    a1=ai_tp.get('tp1') if isinstance(ai_tp,dict) else None; a2=ai_tp.get('tp2') if isinstance(ai_tp,dict) else None
    if a1 is not None:
        a1=safe_float(a1)
        if direction=='LONG' and price+mind<=a1<=price+atr*2.5: tp1=min(tp1,a1)
        if direction=='SHORT' and price-atr*2.5<=a1<=price-mind: tp1=max(tp1,a1)
    if a2 is not None:
        a2=safe_float(a2)
        if direction=='LONG' and price+mind*1.35<=a2<=price+atr*4: tp2=min(tp2,a2)
        if direction=='SHORT' and price-atr*4<=a2<=price-mind*1.35: tp2=max(tp2,a2)
    if direction=='LONG' and tp2<=tp1: tp2=tp1+atr*0.45
    if direction=='SHORT' and tp2>=tp1: tp2=tp1-atr*0.45
    return tp1,tp2



# ---------------------------------------------------------------------------
# AI TP/SL v2 memory helpers
# ---------------------------------------------------------------------------
# Entry-time TP/SL is now built from 6 coordinated layers:
# 1) SR memory/quality, 2) coin personality, 3) direction memory,
# 4) TP reach memory, 5) dynamic TP metadata, 6) SL survival memory.
# Result learning hooks are exposed here; managers/learning modules can call
# register_tp_sl_v2_result without breaking old code.
TP_SL_AI_FILE = "tp_sl_ai_memory.json"


def _ai_mem_load(default=None):
    default = default if isinstance(default, dict) else {"version": 1, "coin_direction": {}, "sr": {}, "events": []}
    if _ds_load_json:
        try:
            data = _ds_load_json(TP_SL_AI_FILE, default)
            return data if isinstance(data, dict) else default
        except Exception:
            pass
    try:
        paths = [TP_SL_AI_FILE, os.path.join("data", TP_SL_AI_FILE)]
        for path in paths:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return data if isinstance(data, dict) else default
    except Exception:
        pass
    return default


def _ai_mem_save(data):
    if not isinstance(data, dict):
        return False
    data["version"] = 1
    if _ds_save_json:
        try:
            _ds_save_json(TP_SL_AI_FILE, data)
            return True
        except Exception:
            pass
    try:
        os.makedirs("data", exist_ok=True)
        with open(os.path.join("data", TP_SL_AI_FILE), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def _cd_key(symbol, direction):
    sym = str(symbol or "UNKNOWN").upper().strip()
    direct = str(direction or "UNKNOWN").upper().strip()
    return f"{sym}:{direct}"


def _memory_bucket(symbol, direction):
    data = _ai_mem_load()
    key = _cd_key(symbol, direction)
    bucket = data.setdefault("coin_direction", {}).setdefault(key, {
        "symbol": str(symbol or "UNKNOWN").upper(),
        "direction": str(direction or "UNKNOWN").upper(),
        "tp1_hits": 0, "tp2_hits": 0, "sl_hits": 0,
        "avg_tp1_atr": None, "avg_tp2_atr": None, "avg_sl_wick_atr": None,
        "avg_max_favorable_atr": None, "avg_max_adverse_atr": None,
        "fake_breakouts": 0, "breakouts": 0, "bounces": 0,
        "personality": "UNKNOWN", "confidence": 0, "updated_at": 0,
    })
    return data, bucket


def _weighted_update(old, value, weight=0.25):
    try:
        value = float(value)
        if old is None:
            return value
        return float(old) * (1.0 - weight) + value * weight
    except Exception:
        return old


def register_tp_sl_v2_result(symbol, direction, result, entry=None, stop_loss=None, tp1=None, tp2=None, snapshot=None, max_favorable=None, max_adverse=None):
    """Optional learning hook for TP/SL v2.

    Safe to call from real_trade_manager, ghost_signals, or coin_learning.
    It preserves existing files and only writes a compact per coin+direction
    memory used by build_trade_levels on future entries.
    """
    data, b = _memory_bucket(symbol, direction)
    res = str(result or "").upper().strip()
    atr = max(safe_float((snapshot or {}).get("atr"), 0.0), safe_float(entry, 0.0) * 0.0015, 1e-12)
    price = safe_float(entry, 0.0)
    if res in {"TP", "TP1", "TAKE_PROFIT", "TAKEPROFIT"}:
        b["tp1_hits"] = int(b.get("tp1_hits", 0)) + 1
    elif res == "TP2":
        b["tp2_hits"] = int(b.get("tp2_hits", 0)) + 1
    elif res in {"SL", "STOP", "STOP_LOSS", "STOPLOSS"}:
        b["sl_hits"] = int(b.get("sl_hits", 0)) + 1
    if price and tp1:
        b["avg_tp1_atr"] = _weighted_update(b.get("avg_tp1_atr"), abs(safe_float(tp1) - price) / atr)
    if price and tp2:
        b["avg_tp2_atr"] = _weighted_update(b.get("avg_tp2_atr"), abs(safe_float(tp2) - price) / atr)
    if price and stop_loss:
        b["avg_sl_wick_atr"] = _weighted_update(b.get("avg_sl_wick_atr"), abs(price - safe_float(stop_loss)) / atr)
    if max_favorable is not None:
        b["avg_max_favorable_atr"] = _weighted_update(b.get("avg_max_favorable_atr"), abs(safe_float(max_favorable)) / atr)
    if max_adverse is not None:
        b["avg_max_adverse_atr"] = _weighted_update(b.get("avg_max_adverse_atr"), abs(safe_float(max_adverse)) / atr)
    total = int(b.get("tp1_hits", 0)) + int(b.get("tp2_hits", 0)) + int(b.get("sl_hits", 0))
    wins = int(b.get("tp1_hits", 0)) + int(b.get("tp2_hits", 0))
    wr = wins / max(total, 1)
    if total >= 8 and wr >= 0.62:
        b["personality"] = "CLEAN_RUNNER"
    elif total >= 8 and wr <= 0.42:
        b["personality"] = "CHOPPY_RISKY"
    elif safe_float(b.get("avg_sl_wick_atr"), 0.0) >= 1.6:
        b["personality"] = "WICKY"
    else:
        b["personality"] = "NORMAL" if total >= 3 else "UNKNOWN"
    b["confidence"] = min(100, total * 8)
    b["updated_at"] = int(time.time())
    data.setdefault("events", []).append({"ts": int(time.time()), "symbol": str(symbol).upper(), "direction": str(direction).upper(), "result": res})
    data["events"] = data.get("events", [])[-500:]
    _ai_mem_save(data)
    return dict(b)


def _sr_quality_pack(levels, price, atr, direction):
    supports = levels.get("supports", []) or []
    resistances = levels.get("resistances", []) or []
    target_levels = resistances if direction == "LONG" else supports
    stop_levels = supports if direction == "LONG" else resistances
    best_target = target_levels[0] if target_levels else {}
    best_stop = stop_levels[0] if stop_levels else {}
    target_strength = safe_float(best_target.get("strength"), 0.0)
    stop_strength = safe_float(best_stop.get("strength"), 0.0)
    target_tfs = len(best_target.get("timeframes", []) or []) if isinstance(best_target, dict) else 0
    stop_tfs = len(best_stop.get("timeframes", []) or []) if isinstance(best_stop, dict) else 0
    return {
        "target_strength": round(target_strength, 3),
        "stop_strength": round(stop_strength, 3),
        "target_timeframes": target_tfs,
        "stop_timeframes": stop_tfs,
        "target_level": best_target,
        "stop_level": best_stop,
    }


def _recent_fake_break_risk(df_15m, direction, price, atr, supports, resistances):
    try:
        recent = df_15m.tail(18)
        if direction == "LONG":
            near = min([safe_float(x.get("price")) for x in resistances if safe_float(x.get("price")) > price], default=None)
            if near is None:
                return 0.0
            fake = ((recent["high"] > near) & (recent["close"] < near)).sum()
        else:
            near = max([safe_float(x.get("price")) for x in supports if safe_float(x.get("price")) < price], default=None)
            if near is None:
                return 0.0
            fake = ((recent["low"] < near) & (recent["close"] > near)).sum()
        return min(1.0, float(fake) / 3.0)
    except Exception:
        return 0.0


def _tp_sl_v2_profile(symbol, direction, price, atr, snapshot, df_15m, levels):
    data, b = _memory_bucket(symbol, direction)
    confidence = safe_float(b.get("confidence"), 0.0)
    personality = str(b.get("personality") or "UNKNOWN").upper()
    supports = levels.get("supports", []) or []
    resistances = levels.get("resistances", []) or []
    srq = _sr_quality_pack(levels, price, atr, direction)
    fake_risk = _recent_fake_break_risk(df_15m, direction, price, atr, supports, resistances)

    tp1_memory = safe_float(b.get("avg_tp1_atr"), 0.0)
    tp2_memory = safe_float(b.get("avg_tp2_atr"), 0.0)
    sl_memory = safe_float(b.get("avg_sl_wick_atr"), 0.0)
    tp1_mult = tp1_memory if confidence >= 20 and tp1_memory > 0 else TP1_FALLBACK_ATR
    tp2_mult = tp2_memory if confidence >= 20 and tp2_memory > 0 else TP2_FALLBACK_ATR
    sl_mult = max(MIN_SL_ATR_MULTIPLIER, sl_memory if confidence >= 20 and sl_memory > 0 else MIN_SL_ATR_MULTIPLIER)

    # Coin personality layer.
    if personality == "CLEAN_RUNNER":
        tp2_mult *= 1.05
        tp1_mult *= 1.02
    elif personality == "WICKY":
        sl_mult *= 1.12
        tp1_mult *= 0.96
    elif personality == "CHOPPY_RISKY":
        tp1_mult *= 0.92
        tp2_mult *= 0.88
        sl_mult *= 1.05

    # Fake breakout + SR quality layer.
    if fake_risk >= 0.67:
        tp1_mult *= 0.88
        tp2_mult *= 0.82
        sl_mult *= 1.08
    elif fake_risk >= 0.34:
        tp1_mult *= 0.94
        tp2_mult *= 0.92

    if srq["target_strength"] >= 8 and srq["target_timeframes"] >= 2:
        tp1_mult *= 0.96  # strong nearby target/resistance: take a little earlier
    if srq["stop_strength"] >= 8 and srq["stop_timeframes"] >= 2:
        sl_mult *= 1.03  # hide SL beyond stronger invalidation zone

    # Keep safe bounds so this layer cannot create unrealistic orders.
    tp1_mult = max(MIN_TP1_ATR, min(1.35, tp1_mult))
    tp2_mult = max(tp1_mult + 0.28, min(2.05, tp2_mult))
    sl_mult = max(MIN_SL_ATR_MULTIPLIER * 0.95, min(MAX_REASONABLE_SL_ATR, sl_mult))
    return {
        "confidence": int(confidence), "personality": personality,
        "tp1_mult": round(tp1_mult, 4), "tp2_mult": round(tp2_mult, 4), "sl_mult": round(sl_mult, 4),
        "fake_break_risk": round(fake_risk, 3), "sr_quality": srq,
        "memory": {k: b.get(k) for k in ["tp1_hits", "tp2_hits", "sl_hits", "avg_tp1_atr", "avg_tp2_atr", "avg_sl_wick_atr"]},
        "dynamic_tp": {"enabled": True, "extend_tp2_if_trend_strong": True, "tighten_tp2_if_momentum_fades": True},
    }


def _apply_tp_sl_v2(direction, price, atr, sl, tp1, tp2, profile):
    tp1_mem = price + atr * profile["tp1_mult"] if direction == "LONG" else price - atr * profile["tp1_mult"]
    tp2_mem = price + atr * profile["tp2_mult"] if direction == "LONG" else price - atr * profile["tp2_mult"]
    sl_mem = price - atr * profile["sl_mult"] if direction == "LONG" else price + atr * profile["sl_mult"]

    # Blend SR levels with memory rather than replacing them. SR still controls
    # price structure; memory nudges TP/SL to what this coin+direction usually reaches.
    w = min(0.25, max(0.08, profile.get("confidence", 0) / 260.0))
    tp1_new = safe_float(tp1) * (1 - w) + tp1_mem * w
    tp2_new = safe_float(tp2) * (1 - w) + tp2_mem * w
    sl_new = safe_float(sl) * (1 - min(0.45, w)) + sl_mem * min(0.45, w)

    mind = max(atr * MIN_TP1_ATR, price * 0.0015)
    if direction == "LONG":
        tp1_new = max(tp1_new, price + mind)
        tp2_new = max(tp2_new, tp1_new + atr * 0.35)
        sl_new = min(sl_new, price - atr * MIN_SL_ATR_MULTIPLIER * 0.95)
        if abs(price - sl_new) > atr * MAX_REASONABLE_SL_ATR:
            sl_new = price - atr * MAX_REASONABLE_SL_ATR
    else:
        tp1_new = min(tp1_new, price - mind)
        tp2_new = min(tp2_new, tp1_new - atr * 0.35)
        sl_new = max(sl_new, price + atr * MIN_SL_ATR_MULTIPLIER * 0.95)
        if abs(price - sl_new) > atr * MAX_REASONABLE_SL_ATR:
            sl_new = price + atr * MAX_REASONABLE_SL_ATR
    return sl_new, tp1_new, tp2_new

def build_trade_levels(direction, price, atr, df_5m, df_15m, df_30m, snapshot=None, symbol=None):
    price=safe_float(price); atr=max(safe_float(atr), price*0.0015); vf=coin_volatility_factor(df_15m,price); min_sl=atr*MIN_SL_ATR_MULTIPLIER*vf; buf_tp=max(atr*LEVEL_BUFFER_ATR*vf, price*0.0007); buf_sl=max(atr*SL_BUFFER_ATR*vf, price*0.001)
    levels=get_strong_levels(df_5m,df_15m,df_30m,price,atr); supports=levels['supports']; res=levels['resistances']; ai_tp=get_ai_tp_memory(symbol,direction,price,atr,snapshot) if symbol and snapshot else {}
    tp_sl_v2=_tp_sl_v2_profile(symbol or (snapshot or {}).get('symbol'),direction,price,atr,snapshot or {},df_15m,levels)
    # Layer 6: SL survival memory gently adjusts the classic SR/ATR base distance.
    learned_min_sl=atr*safe_float(tp_sl_v2.get('sl_mult'),MIN_SL_ATR_MULTIPLIER)*vf
    min_sl=max(min_sl, learned_min_sl)
    if direction=='LONG':
        classic_sl=price-min_sl; sp=select_level_for_sl(direction,price,atr,supports,min_sl); sl=min(sp-buf_sl, classic_sl) if sp else classic_sl
        if abs(price-sl)>atr*MAX_REASONABLE_SL_ATR*vf: sl=classic_sl
        sr_tp1=select_level_for_tp(direction,price,atr,res,TP1_FALLBACK_ATR*vf,buf_tp); sr_tp2=select_level_for_tp(direction,price,atr,[x for x in res if x['price']>sr_tp1],TP2_FALLBACK_ATR*vf,buf_tp)
        if sr_tp2<=sr_tp1: sr_tp2=price+atr*TP2_FALLBACK_ATR*vf
    else:
        classic_sl=price+min_sl; rp=select_level_for_sl(direction,price,atr,res,min_sl); sl=max(rp+buf_sl, classic_sl) if rp else classic_sl
        if abs(price-sl)>atr*MAX_REASONABLE_SL_ATR*vf: sl=classic_sl
        sr_tp1=select_level_for_tp(direction,price,atr,supports,TP1_FALLBACK_ATR*vf,buf_tp); sr_tp2=select_level_for_tp(direction,price,atr,[x for x in supports if x['price']<sr_tp1],TP2_FALLBACK_ATR*vf,buf_tp)
        if sr_tp2>=sr_tp1: sr_tp2=price-atr*TP2_FALLBACK_ATR*vf
    tp1,tp2=merge_tp_with_ai_memory(direction,price,atr,sr_tp1,sr_tp2,ai_tp)
    sl,tp1,tp2=_apply_tp_sl_v2(direction,price,atr,sl,tp1,tp2,tp_sl_v2)
    risk=abs(price-sl); reward=abs(tp1-price); rr=round(reward/risk,2) if risk>0 else 0
    return safe_round(sl), safe_round(tp1), safe_round(tp2), rr, {
        'volatility_factor':round(vf,3),'ai_tp_used':bool(ai_tp),'ai_tp':ai_tp,
        'nearest_support':levels.get('nearest_support'),'nearest_resistance':levels.get('nearest_resistance'),
        'tp_sl_v2':tp_sl_v2,
        'tp_sl_layers':['15M_SR_PRIMARY','5M_ENTRY_STRUCTURE','30M_CONTEXT','COIN_PERSONALITY','TP_REACH_MEMORY','LIQUIDITY_AWARE_SL','DYNAMIC_TP_META']
    }

# ---------------------------------------------------------------------------
# BTC Lead Indicator / Market Regime
# ---------------------------------------------------------------------------
def _trend_to_bias_score(trend: str) -> int:
    t = str(trend or '').lower().strip()
    if t == 'bullish':
        return 2
    if t == 'weak_bullish':
        return 1
    if t == 'bearish':
        return -2
    if t == 'weak_bearish':
        return -1
    return 0


def _btc_power_pack(df: pd.DataFrame) -> Dict[str, Any]:
    try:
        buy2, sell2 = buy_sell_power(df, 2)
        buy3, sell3 = buy_sell_power(df, 3)
        buy6, sell6 = buy_sell_power(df, 6)
        return {'buy2': buy2, 'sell2': sell2, 'buy3': buy3, 'sell3': sell3, 'buy6': buy6, 'sell6': sell6}
    except Exception:
        return {'buy2': 50.0, 'sell2': 50.0, 'buy3': 50.0, 'sell3': 50.0, 'buy6': 50.0, 'sell6': 50.0}


def _btc_tf_score(df: pd.DataFrame, tf_name: str) -> Dict[str, Any]:
    try:
        last = df.iloc[-1]
        prev = df.iloc[-2]
        score = 0
        reasons = []

        close = safe_float(last.get('close'))
        ema20 = safe_float(last.get('ema20'))
        vwap = safe_float(last.get('vwap'))
        rsi = safe_float(last.get('rsi'))
        prev_rsi = safe_float(prev.get('rsi'))
        macd = safe_float(last.get('macd'))
        macd_signal = safe_float(last.get('macd_signal'))
        hist = safe_float(last.get('macd_hist'))
        prev_hist = safe_float(prev.get('macd_hist'))
        hist_slope = hist - prev_hist
        power = _btc_power_pack(df)

        if close > ema20:
            score += 2; reasons.append(f'{tf_name}: بالای EMA20')
        elif close < ema20:
            score -= 2; reasons.append(f'{tf_name}: زیر EMA20')

        if close > vwap:
            score += 2; reasons.append(f'{tf_name}: بالای VWAP')
        elif close < vwap:
            score -= 2; reasons.append(f'{tf_name}: زیر VWAP')

        if macd > macd_signal:
            score += 2
        elif macd < macd_signal:
            score -= 2

        if hist_slope > 0:
            score += 2; reasons.append(f'{tf_name}: هیستوگرام رو به بالا')
        elif hist_slope < 0:
            score -= 2; reasons.append(f'{tf_name}: هیستوگرام رو به پایین')

        if rsi >= 52 and rsi > prev_rsi:
            score += 2; reasons.append(f'{tf_name}: RSI صعودی')
        elif rsi <= 48 and rsi < prev_rsi:
            score -= 2; reasons.append(f'{tf_name}: RSI نزولی')

        if power.get('buy3', 50) >= 58 or power.get('buy2', 50) >= 62:
            score += 2; reasons.append(f'{tf_name}: قدرت خرید کوتاه‌مدت')
        if power.get('sell3', 50) >= 58 or power.get('sell2', 50) >= 62:
            score -= 2; reasons.append(f'{tf_name}: قدرت فروش کوتاه‌مدت')

        return {
            'tf': tf_name, 'score': int(score), 'reasons': reasons[:6],
            'close': safe_round(close), 'ema20': safe_round(ema20), 'vwap': safe_round(vwap),
            'rsi': safe_round(rsi, 2), 'rsi_slope': safe_round(rsi - prev_rsi, 4),
            'macd_hist': safe_round(hist, 8), 'macd_hist_slope': safe_round(hist_slope, 8),
            'power': power, 'trend': trend_direction(df),
        }
    except Exception as e:
        return {'tf': tf_name, 'score': 0, 'reasons': [f'{tf_name}: خطا در BTC Lead {str(e)[:80]}'], 'trend': 'range'}


def _classify_btc_bias(score: float) -> str:
    if score >= 10:
        return 'STRONG_BULLISH'
    if score >= 4:
        return 'BULLISH'
    if score <= -10:
        return 'STRONG_BEARISH'
    if score <= -4:
        return 'BEARISH'
    return 'NEUTRAL'


def _classify_market_regime(score: float, t4: str, t1: str, t15: str) -> str:
    if score >= 9 and t4 in {'bullish', 'weak_bullish'} and t1 in {'bullish', 'weak_bullish'}:
        return 'STRONG_BULLISH'
    if score >= 3:
        return 'BULLISH'
    if score <= -9 and t4 in {'bearish', 'weak_bearish'} and t1 in {'bearish', 'weak_bearish'}:
        return 'STRONG_BEARISH'
    if score <= -3:
        return 'BEARISH'
    return 'NEUTRAL'


def _soft_bias_for_legacy(value: str) -> str:
    v = str(value or '').upper()
    if 'BULLISH' in v:
        return 'bullish'
    if 'BEARISH' in v:
        return 'bearish'
    return 'neutral'


def _persist_market_context_to_ai_summary(data: Dict[str, Any]) -> None:
    if not callable(update_ai_summary):
        return
    try:
        update_ai_summary(
            market_regime=data.get('market_regime'),
            btc_bias=data.get('btc_bias'),
            btc_lead=data.get('btc_lead'),
            market_context=data,
            last_market_update=int(time.time()),
        )
    except TypeError:
        try:
            update_ai_summary({'market_regime': data.get('market_regime'), 'btc_bias': data.get('btc_bias'), 'btc_lead': data.get('btc_lead'), 'market_context': data})
        except Exception:
            pass
    except Exception:
        pass


def get_soft_market_context():
    now = time.time() if 'time' in globals() else 0
    try:
        cached = _SOFT_MARKET_CONTEXT_CACHE.get('data')
        cached_ts = float(_SOFT_MARKET_CONTEXT_CACHE.get('ts') or 0)
        if cached and now and (now - cached_ts) <= SOFT_MARKET_CONTEXT_TTL_SECONDS:
            return dict(cached)
    except Exception:
        pass
    try:
        b4 = add_indicators(get_klines('BTCUSDT', '4h'))
        b1 = add_indicators(get_klines('BTCUSDT', '1h'))
        b15 = add_indicators(get_klines('BTCUSDT', '15m'))
        b5 = add_indicators(get_klines('BTCUSDT', '5m', include_current=USE_CURRENT_5M_FOR_ENTRY))

        t4 = trend_direction(b4)
        t1 = trend_direction(b1)
        t15 = trend_direction(b15)
        t5 = trend_direction(b5)

        s15 = _btc_tf_score(b15, '15M')
        s5 = _btc_tf_score(b5, '5M')

        # 15M is the lead scalp context; 5M is the fast turn/entry context.
        btc_score = (s15.get('score', 0) * 1.45) + (s5.get('score', 0) * 1.25)
        btc_score += _trend_to_bias_score(t4) * 1.2
        btc_score += _trend_to_bias_score(t1) * 1.6

        btc_bias = _classify_btc_bias(btc_score)
        regime_score = btc_score + _trend_to_bias_score(t4) * 2.0 + _trend_to_bias_score(t1) * 2.0 + _trend_to_bias_score(t15)
        market_regime = _classify_market_regime(regime_score, t4, t1, t15)

        data = {
            'market_regime': market_regime,
            'btc_bias': btc_bias,
            'market_regime_legacy': _soft_bias_for_legacy(market_regime),
            'btc_bias_legacy': _soft_bias_for_legacy(btc_bias),
            'btc_4h': t4, 'btc_1h': t1, 'btc_15m': t15, 'btc_5m': t5,
            'btc_lead': {
                'enabled': True,
                'bias': btc_bias,
                'score': round(btc_score, 3),
                'market_regime': market_regime,
                'regime_score': round(regime_score, 3),
                'effect': 4 if btc_bias == 'STRONG_BULLISH' else 2 if btc_bias == 'BULLISH' else -4 if btc_bias == 'STRONG_BEARISH' else -2 if btc_bias == 'BEARISH' else 0,
                'tf_15m': s15,
                'tf_5m': s5,
                'trends': {'4H': t4, '1H': t1, '15M': t15, '5M': t5},
                'updated_at': int(time.time()),
            },
        }
        try:
            _SOFT_MARKET_CONTEXT_CACHE['data'] = dict(data)
            _SOFT_MARKET_CONTEXT_CACHE['ts'] = now or 0
        except Exception:
            pass
        _persist_market_context_to_ai_summary(data)
        return data
    except Exception as e:
        data = {
            'market_regime': 'NEUTRAL',
            'btc_bias': 'NEUTRAL',
            'market_regime_legacy': 'neutral',
            'btc_bias_legacy': 'neutral',
            'btc_lead': {'enabled': False, 'bias': 'NEUTRAL', 'score': 0, 'error': str(e)[:160]},
        }
        _persist_market_context_to_ai_summary(data)
        return data


# ---------------------------------------------------------------------------
# AI Movement Hunter / Movement Prediction layer
# ---------------------------------------------------------------------------
# Classic indicators are sensors only.  They provide raw features, but they do
# not score, approve, reject, or issue signals.  The final decision is produced
# here by evaluating movement freshness, phase, trap/liquidity, current market
# context, and adaptive AI learning/risk layers.
AI_MOVEMENT_REAL_MIN_SCORE = int(os.getenv("AI_MOVEMENT_REAL_MIN_SCORE", "68") or "68")
AI_MOVEMENT_SETUP_MIN_SCORE = int(os.getenv("AI_MOVEMENT_SETUP_MIN_SCORE", "55") or "55")
AI_MOVEMENT_EARLY_MAX_ATR = float(os.getenv("AI_MOVEMENT_EARLY_MAX_ATR", "1.85") or "1.85")
AI_MOVEMENT_EXHAUSTION_ATR = float(os.getenv("AI_MOVEMENT_EXHAUSTION_ATR", "3.05") or "3.05")
AI_MOVEMENT_RANGE_AFTER_MOVE_ATR = float(os.getenv("AI_MOVEMENT_RANGE_AFTER_MOVE_ATR", "2.65") or "2.65")


def _crossed(prev_value: Any, current_value: Any, level: float, direction: str) -> bool:
    try:
        p = safe_float(prev_value)
        c = safe_float(current_value)
        if str(direction).upper() == "UP":
            return p <= level < c
        return p >= level > c
    except Exception:
        return False


def _recent_range_context(df: pd.DataFrame, atr_ref: float, lookback: int = 12) -> Dict[str, Any]:
    try:
        recent = df.tail(max(lookback, 6))
        last4 = df.tail(4)
        price = safe_float(df.iloc[-1]["close"])
        atr = max(safe_float(atr_ref), safe_float(df.iloc[-1].get("atr"), 0.0), price * 0.0015, 1e-12)
        hi = safe_float(recent["high"].max())
        lo = safe_float(recent["low"].min())
        last4_hi = safe_float(last4["high"].max())
        last4_lo = safe_float(last4["low"].min())
        move_atr = (hi - lo) / atr
        last4_range_atr = (last4_hi - last4_lo) / atr
        pos = (price - lo) / max(hi - lo, 1e-12)
        return {
            "hi": safe_round(hi),
            "lo": safe_round(lo),
            "move_atr": round(move_atr, 3),
            "last4_range_atr": round(last4_range_atr, 3),
            "position_in_range": round(pos, 3),
            "near_high": bool(pos >= 0.76),
            "near_low": bool(pos <= 0.24),
            "tight_recent_range": bool(last4_range_atr <= 0.90),
        }
    except Exception:
        return {"move_atr": 0.0, "last4_range_atr": 0.0, "position_in_range": 0.5}


def _raw_tf_features(df: pd.DataFrame, tf_name: str) -> Dict[str, Any]:
    try:
        last = df.iloc[-1]
        prev = df.iloc[-2]
        buy2, sell2 = buy_sell_power(df, 2)
        buy3, sell3 = buy_sell_power(df, 3)
        buy6, sell6 = buy_sell_power(df, 6)
        candle = _candle_quality_pack(df, 6)
        comp = _compression_expansion_pack(df, 24)
        return {
            "tf": tf_name,
            "close": safe_round(last.get("close")),
            "rsi": safe_round(last.get("rsi"), 3),
            "rsi_prev": safe_round(prev.get("rsi"), 3),
            "rsi_slope": safe_round(safe_float(last.get("rsi")) - safe_float(prev.get("rsi")), 4),
            "rsi_cross_30_up": _crossed(prev.get("rsi"), last.get("rsi"), 30, "UP"),
            "rsi_cross_50_up": _crossed(prev.get("rsi"), last.get("rsi"), 50, "UP"),
            "rsi_cross_70_down": _crossed(prev.get("rsi"), last.get("rsi"), 70, "DOWN"),
            "rsi_cross_50_down": _crossed(prev.get("rsi"), last.get("rsi"), 50, "DOWN"),
            "macd": safe_round(last.get("macd"), 8),
            "macd_signal": safe_round(last.get("macd_signal"), 8),
            "macd_hist": safe_round(last.get("macd_hist"), 8),
            "macd_hist_prev": safe_round(prev.get("macd_hist"), 8),
            "macd_hist_slope": safe_round(safe_float(last.get("macd_hist")) - safe_float(prev.get("macd_hist")), 8),
            "hist_cross_zero_up": safe_float(prev.get("macd_hist")) <= 0 < safe_float(last.get("macd_hist")),
            "hist_cross_zero_down": safe_float(prev.get("macd_hist")) >= 0 > safe_float(last.get("macd_hist")),
            "ema20": safe_round(last.get("ema20")),
            "ema50": safe_round(last.get("ema50")),
            "ema200": safe_round(last.get("ema200")),
            "ema20_distance_atr": safe_round(distance_from_ema20_atr(df), 4),
            "vwap": safe_round(last.get("vwap")),
            "above_vwap": bool(safe_float(last.get("close")) > safe_float(last.get("vwap"))),
            "below_vwap": bool(safe_float(last.get("close")) < safe_float(last.get("vwap"))),
            "vwap_reclaim": bool(safe_float(last.get("close")) > safe_float(last.get("vwap")) and safe_float(prev.get("close")) <= safe_float(prev.get("vwap"))),
            "vwap_loss": bool(safe_float(last.get("close")) < safe_float(last.get("vwap")) and safe_float(prev.get("close")) >= safe_float(prev.get("vwap"))),
            "adx": safe_round(last.get("adx"), 3),
            "adx_slope": safe_round(_slope(df["adx"], 3), 4),
            "atr": safe_round(last.get("atr")),
            "volume_ratio": safe_round(last.get("volume_ratio"), 3),
            "buy2": buy2, "sell2": sell2, "buy3": buy3, "sell3": sell3, "buy6": buy6, "sell6": sell6,
            "candle": candle,
            "compression": comp,
            "trend_hint": trend_direction(df),
            "ema_hint": ema_direction(df),
        }
    except Exception as e:
        return {"tf": tf_name, "error": str(e)[:120]}


def build_technical_sensor_snapshot(symbol: str, df_4h: pd.DataFrame, df_1h: pd.DataFrame, df_30m: pd.DataFrame, df_15m: pd.DataFrame, df_5m: pd.DataFrame, market_context: Dict[str, Any]) -> Dict[str, Any]:
    """Return raw technical sensor data.  No signal, no score, no gate."""
    price = safe_float(df_5m.iloc[-1].get("close") if USE_CURRENT_5M_FOR_ENTRY else df_15m.iloc[-1].get("close"))
    atr = max(safe_float(df_15m.iloc[-1].get("atr")), price * 0.0015, 1e-12)
    return {
        "sensor_mode": True,
        "classic_signal_disabled": True,
        "classic_score_disabled": True,
        "symbol": str(symbol).upper(),
        "price": safe_round(price),
        "atr": safe_round(atr),
        "timeframes": {
            "5M": _raw_tf_features(df_5m, "5M"),
            "15M": _raw_tf_features(df_15m, "15M"),
            "30M": _raw_tf_features(df_30m, "30M"),
            "1H": _raw_tf_features(df_1h, "1H"),
            "4H": _raw_tf_features(df_4h, "4H"),
        },
        "range_5m": _recent_range_context(df_5m, atr, 12),
        "range_15m": _recent_range_context(df_15m, atr, 12),
        "market_context": market_context or {},
        "learning_note": "technical indicators are raw AI sensors only; no classic scoring or signal issuance",
    }


def _direction_sensor_evidence(direction: str, sensors: Dict[str, Any], market_context: Dict[str, Any]) -> Dict[str, Any]:
    direction = str(direction or "").upper().strip()
    t5 = sensors.get("timeframes", {}).get("5M", {})
    t15 = sensors.get("timeframes", {}).get("15M", {})
    t1 = sensors.get("timeframes", {}).get("1H", {})
    r5 = sensors.get("range_5m", {})
    r15 = sensors.get("range_15m", {})
    score = 50.0
    evidence = []
    warnings = []

    if direction == "LONG":
        if t5.get("rsi_cross_30_up"):
            score += 12; evidence.append("5M RSI برگشت از 30")
        if t5.get("rsi_cross_50_up") or t15.get("rsi_cross_50_up"):
            score += 7; evidence.append("RSI عبور از 50")
        if safe_float(t5.get("rsi_slope")) > 0 and safe_float(t15.get("rsi_slope")) >= -0.2:
            score += 6; evidence.append("RSI slope مثبت")
        if t5.get("hist_cross_zero_up") or t15.get("hist_cross_zero_up"):
            score += 9; evidence.append("MACD hist عبور مثبت")
        if safe_float(t5.get("macd_hist_slope")) > 0 and safe_float(t15.get("macd_hist_slope")) >= 0:
            score += 9; evidence.append("MACD histogram acceleration مثبت")
        if t5.get("vwap_reclaim") or t15.get("vwap_reclaim"):
            score += 8; evidence.append("VWAP reclaim")
        elif t5.get("above_vwap") and t15.get("above_vwap"):
            score += 5; evidence.append("بالای VWAP")
        if safe_float(t5.get("buy2")) >= 62 or safe_float(t5.get("buy3")) >= 58:
            score += 9; evidence.append("Power Shift خرید")
        if (t5.get("candle") or {}).get("strong_close_up"):
            score += 7; evidence.append("کندل بسته‌شدن قوی لانگ")
        if (t5.get("compression") or {}).get("compression") or (t15.get("compression") or {}).get("compression"):
            score += 5; evidence.append("فشردگی قبل از حرکت")
        if (t5.get("compression") or {}).get("expansion") or (t15.get("compression") or {}).get("expansion"):
            score += 8; evidence.append("شروع expansion/volume")
        if safe_float(t1.get("macd_hist_slope")) >= 0:
            score += 2
        if (t5.get("candle") or {}).get("upper_rejection") and r5.get("near_high"):
            score -= 9; warnings.append("ریجکت سقف/احتمال تله لانگ")
    else:
        if t5.get("rsi_cross_70_down"):
            score += 12; evidence.append("5M RSI برگشت از 70")
        if t5.get("rsi_cross_50_down") or t15.get("rsi_cross_50_down"):
            score += 7; evidence.append("RSI شکست 50 به پایین")
        if safe_float(t5.get("rsi_slope")) < 0 and safe_float(t15.get("rsi_slope")) <= 0.2:
            score += 6; evidence.append("RSI slope منفی")
        if t5.get("hist_cross_zero_down") or t15.get("hist_cross_zero_down"):
            score += 9; evidence.append("MACD hist عبور منفی")
        if safe_float(t5.get("macd_hist_slope")) < 0 and safe_float(t15.get("macd_hist_slope")) <= 0:
            score += 9; evidence.append("MACD histogram acceleration منفی")
        if t5.get("vwap_loss") or t15.get("vwap_loss"):
            score += 8; evidence.append("VWAP loss")
        elif t5.get("below_vwap") and t15.get("below_vwap"):
            score += 5; evidence.append("زیر VWAP")
        if safe_float(t5.get("sell2")) >= 62 or safe_float(t5.get("sell3")) >= 58:
            score += 9; evidence.append("Power Shift فروش")
        if (t5.get("candle") or {}).get("strong_close_down"):
            score += 7; evidence.append("کندل بسته‌شدن قوی شورت")
        if (t5.get("compression") or {}).get("compression") or (t15.get("compression") or {}).get("compression"):
            score += 5; evidence.append("فشردگی قبل از حرکت")
        if (t5.get("compression") or {}).get("expansion") or (t15.get("compression") or {}).get("expansion"):
            score += 8; evidence.append("شروع expansion/volume")
        if safe_float(t1.get("macd_hist_slope")) <= 0:
            score += 2
        if (t5.get("candle") or {}).get("lower_rejection") and r5.get("near_low"):
            score -= 9; warnings.append("ریجکت کف/احتمال تله شورت")

    btc_bias = str(market_context.get("btc_bias", "NEUTRAL")).upper()
    if direction == "LONG" and "BULLISH" in btc_bias:
        score += 3; evidence.append("BTC/Market همسو لانگ")
    if direction == "SHORT" and "BEARISH" in btc_bias:
        score += 3; evidence.append("BTC/Market همسو شورت")
    if direction == "LONG" and "BEARISH" in btc_bias:
        score -= 3; warnings.append("BTC خلاف لانگ")
    if direction == "SHORT" and "BULLISH" in btc_bias:
        score -= 3; warnings.append("BTC خلاف شورت")

    return {"score": cap_score(score), "evidence": evidence[:12], "warnings": warnings[:8]}


def _movement_phase_for_direction(direction: str, sensors: Dict[str, Any], evidence_score: float) -> Dict[str, Any]:
    direction = str(direction or "").upper().strip()
    t5 = sensors.get("timeframes", {}).get("5M", {})
    t15 = sensors.get("timeframes", {}).get("15M", {})
    r5 = sensors.get("range_5m", {})
    r15 = sensors.get("range_15m", {})
    move_atr = max(safe_float(r5.get("move_atr")), safe_float(r15.get("move_atr")))
    last_range = safe_float(r5.get("last4_range_atr"))
    dist5 = safe_float(t5.get("ema20_distance_atr"))
    dist15 = safe_float(t15.get("ema20_distance_atr"))
    dist = max(dist5, dist15)
    pos = safe_float(r5.get("position_in_range"), 0.5)

    range_after_move = (
        move_atr >= AI_MOVEMENT_RANGE_AFTER_MOVE_ATR
        and last_range <= 0.95
        and ((direction == "SHORT" and pos <= 0.38) or (direction == "LONG" and pos >= 0.62))
    )
    exhausted = (
        move_atr >= AI_MOVEMENT_EXHAUSTION_ATR
        and ((direction == "SHORT" and pos <= 0.30) or (direction == "LONG" and pos >= 0.70))
        and dist >= 1.35
    )
    if exhausted:
        phase = "EXHAUSTION"
    elif range_after_move and evidence_score < 72:
        phase = "RANGE_AFTER_MOVE"
    elif move_atr <= 1.15 and evidence_score >= 66:
        phase = "START"
    elif move_atr <= AI_MOVEMENT_EARLY_MAX_ATR and evidence_score >= 66:
        phase = "EARLY"
    elif evidence_score >= AI_MOVEMENT_SETUP_MIN_SCORE and move_atr <= 1.05:
        phase = "SETUP"
    else:
        phase = "MID" if move_atr > AI_MOVEMENT_EARLY_MAX_ATR or dist >= 1.25 else "WATCH"

    real_allowed = phase in {"START", "EARLY"}
    setup_only = phase in {"SETUP", "WATCH", "MID"}
    return {
        "phase": phase,
        "real_allowed": bool(real_allowed),
        "setup_only": bool(setup_only),
        "move_atr": round(move_atr, 3),
        "last4_range_atr": round(last_range, 3),
        "distance_ema20_atr": round(dist, 3),
        "position_in_range": round(pos, 3),
        "range_after_move": bool(range_after_move),
        "exhausted": bool(exhausted),
    }


def _trap_liquidity_from_sensors(direction: str, sensors: Dict[str, Any]) -> Dict[str, Any]:
    direction = str(direction or "").upper().strip()
    t5 = sensors.get("timeframes", {}).get("5M", {})
    r5 = sensors.get("range_5m", {})
    candle = t5.get("candle") or {}
    trap_score = 18
    reasons = []
    if direction == "LONG":
        if candle.get("upper_rejection") and r5.get("near_high"):
            trap_score += 42; reasons.append("upper wick near high")
        if safe_float(t5.get("buy2")) < 45 and safe_float(t5.get("sell2")) > 55:
            trap_score += 15; reasons.append("buy power weak")
        if t5.get("below_vwap"):
            trap_score += 8; reasons.append("below VWAP")
    else:
        if candle.get("lower_rejection") and r5.get("near_low"):
            trap_score += 42; reasons.append("lower wick near low")
        if safe_float(t5.get("sell2")) < 45 and safe_float(t5.get("buy2")) > 55:
            trap_score += 15; reasons.append("sell power weak")
        if t5.get("above_vwap"):
            trap_score += 8; reasons.append("above VWAP")
    level = "HIGH" if trap_score >= 60 else "MEDIUM" if trap_score >= 38 else "LOW"
    return {"trap_score": cap_score(trap_score), "trap_risk": level, "reasons": reasons[:6]}


def ai_movement_hunter_decision(symbol: str, sensors: Dict[str, Any], market_context: Dict[str, Any]) -> Dict[str, Any]:
    """AI-first movement hunter.

    This function is the only direction/score/decision source in analysis.py.
    Classic functions may still be called for legacy telemetry, but their
    scores are not used to approve or reject a trade.
    """
    long_ev = _direction_sensor_evidence("LONG", sensors, market_context)
    short_ev = _direction_sensor_evidence("SHORT", sensors, market_context)
    candidates = {}
    for direction, ev in (("LONG", long_ev), ("SHORT", short_ev)):
        phase = _movement_phase_for_direction(direction, sensors, ev.get("score", 50))
        trap = _trap_liquidity_from_sensors(direction, sensors)
        score = safe_float(ev.get("score"), 50)
        if phase.get("phase") in {"START", "EARLY"}:
            score += 7
        elif phase.get("phase") == "SETUP":
            score += 2
        elif phase.get("phase") == "MID":
            # MID is still valid for 5M/15M scalping when evidence is strong.
            score -= 4
        elif phase.get("phase") == "RANGE_AFTER_MOVE":
            # Do not kill all range-after-move candidates; keep them as cautious setups.
            score -= 8
        elif phase.get("phase") == "EXHAUSTION":
            score -= 28
        if trap.get("trap_risk") == "HIGH":
            score -= 16
        elif trap.get("trap_risk") == "MEDIUM":
            score -= 5
        candidates[direction] = {
            "direction": direction,
            "score": cap_score(score),
            "raw_evidence_score": ev.get("score"),
            "evidence": ev.get("evidence", []),
            "warnings": ev.get("warnings", []),
            "phase": phase,
            "trap": trap,
        }

    selected = candidates["LONG"] if candidates["LONG"]["score"] >= candidates["SHORT"]["score"] else candidates["SHORT"]
    score = int(selected.get("score", 0))
    phase_name = selected.get("phase", {}).get("phase", "UNKNOWN")
    trap_risk = selected.get("trap", {}).get("trap_risk", "LOW")
    evidence_count = len(selected.get("evidence") or [])

    if (
        score >= AI_MOVEMENT_REAL_MIN_SCORE
        and selected.get("phase", {}).get("real_allowed")
        and trap_risk != "HIGH"
        and evidence_count >= 2
    ):
        decision = "REAL"
    elif (
        score >= AI_MOVEMENT_REAL_MIN_SCORE + 4
        and phase_name in {"SETUP", "WATCH"}
        and trap_risk in {"LOW", "MEDIUM"}
        and evidence_count >= 2
    ):
        decision = "REAL"
    elif (
        score >= AI_MOVEMENT_REAL_MIN_SCORE + 2
        and phase_name == "MID"
        and trap_risk == "LOW"
        and evidence_count >= 3
    ):
        decision = "REAL"
    elif (
        score >= AI_MOVEMENT_REAL_MIN_SCORE + 6
        and phase_name == "RANGE_AFTER_MOVE"
        and trap_risk == "LOW"
        and evidence_count >= 4
    ):
        # Rare case: strong evidence after a range pause can still be tradable,
        # but only with LOW trap and many confirmations.
        decision = "REAL"
    elif score >= AI_MOVEMENT_SETUP_MIN_SCORE and phase_name != "EXHAUSTION" and trap_risk != "HIGH":
        decision = "GHOST"
    else:
        decision = "REJECT"

    return {
        "architecture": "AI_MOVEMENT_HUNTER",
        "classic_signal_disabled": True,
        "classic_score_disabled": True,
        "symbol": str(symbol).upper(),
        "decision": decision,
        "direction": selected.get("direction", "NONE"),
        "ai_score": cap_score(score),
        "confidence": "HIGH" if score >= 88 else "MEDIUM" if score >= 76 else "LOW",
        "evidence_count": evidence_count,
        "required_evidence": 2,
        "move_phase": phase_name,
        "move_freshness": "HIGH" if phase_name == "START" else "MEDIUM" if phase_name == "EARLY" else "LOW",
        "trap_risk": trap_risk,
        "selected": selected,
        "candidates": candidates,
        "reasons": (selected.get("evidence") or []) + (selected.get("warnings") or []) + (selected.get("trap", {}).get("reasons") or []),
    }

def analyze_symbol(symbol: str) -> Dict:
    symbol = str(symbol).upper().strip()
    try:
        # Data/indicator layer: still unchanged so every existing command, paper
        # trade, real trade, TP/SL, risk, rotation, ghost, and statistics module
        # can keep using the same output shape.
        df_4h = add_indicators(get_klines(symbol, '4h'))
        df_1h = add_indicators(get_klines(symbol, '1h'))
        df_30m = add_indicators(get_klines(symbol, '30m'))
        df_15m = add_indicators(get_klines(symbol, '15m'))
        df_5m = add_indicators(get_klines(symbol, '5m', include_current=USE_CURRENT_5M_FOR_ENTRY))

        market_context = get_soft_market_context()

        # Legacy telemetry only.  simple_classic_score is no longer allowed to
        # issue/approve/reject a signal.  It is kept so old UI fields and other
        # files that expect trends/reasons/power values do not break.
        sp = simple_classic_score(symbol, df_4h, df_1h, df_30m, df_15m, df_5m, market_context)

        sensors = build_technical_sensor_snapshot(symbol, df_4h, df_1h, df_30m, df_15m, df_5m, market_context)
        movement_decision = ai_movement_hunter_decision(symbol, sensors, market_context)

        direction = str(movement_decision.get('direction') or 'NONE').upper()
        ai_score = int(movement_decision.get('ai_score') or 0)
        confirmations = int(movement_decision.get('evidence_count') or 0)
        reasons = list(movement_decision.get('reasons') or [])
        if not reasons:
            reasons = ['AI Movement Hunter: نشانه کافی برای حرکت تازه پیدا نشد']

        closed_15m_price = safe_float(df_15m.iloc[-1]['close'])
        price = _live_entry_price(symbol, direction if direction in {'LONG', 'SHORT'} else 'LONG', df_5m, closed_15m_price)
        atr = max(safe_float(df_15m.iloc[-1]['atr']), safe_float(price) * 0.0015)

        # Build selected-direction snapshot for all learning/trade modules.
        # This snapshot now clearly marks classic output as sensor-only.
        snapshot = build_local_snapshot(
            symbol,
            direction if direction in {'LONG', 'SHORT'} else 'NONE',
            df_4h, df_1h, df_30m, df_15m, df_5m, sp, market_context
        )
        snapshot['technical_sensors'] = sensors
        snapshot['ai_movement_hunter'] = movement_decision
        snapshot['classic_signal_disabled'] = True
        snapshot['classic_score_disabled'] = True
        snapshot['live_entry_price'] = safe_float(price)
        snapshot['closed_15m_price'] = safe_float(closed_15m_price)
        snapshot['entry'] = safe_float(price)
        snapshot['price'] = safe_float(price)

        risk_state = get_coin_risk(symbol, direction)
        strict = int(risk_state.get('strictness_level', 0) or 0)
        sl_count = int(risk_state.get('sl_count', 0) or 0)
        risk_score = safe_float(risk_state.get('risk_score', 0), 0)

        rotation = get_rotation_context(symbol)
        rs = safe_float(rotation.get('rotation_score', 50), 50)

        # AI score is the only score used for final permission.
        final_score = ai_score
        base_min_score = int(AI_MOVEMENT_REAL_MIN_SCORE)
        base_conf = 2
        req_conf = base_conf
        ai_penalty = 0
        ai_min_score_add = 0
        ai_block = False

        # Risk/rotation/learning are AI layers and remain active, but they
        # adjust the AI Movement score instead of any classic score.
        if strict:
            ai_penalty += min(9, max(2, strict * 2))
            ai_min_score_add += min(7, max(1, strict * 1.5))
            req_conf += min(2, max(1, strict))
            reasons.append(f'AI Risk: سختگیری سطح {strict} | SL={sl_count}')
        elif sl_count >= 2:
            fallback_level = min(3, sl_count - 1)
            ai_penalty += min(9, fallback_level * 3)
            ai_min_score_add += min(6, fallback_level * 2)
            req_conf += min(2, fallback_level)
            reasons.append(f'AI Risk fallback: SLهای تکراری {sl_count}')

        if rs >= 80:
            final_score += 2
            reasons.append('AI Rotation: اولویت مثبت')
        elif rs <= 20:
            ai_penalty += 4
            ai_min_score_add += 2
            req_conf += 1
            reasons.append('AI Rotation: کوین کم‌اولویت/پرریسک')
        elif rs <= 35:
            ai_penalty += 2
            ai_min_score_add += 1
            reasons.append('AI Rotation: اولویت ضعیف')

        extra = ai_extra_strength_required(symbol, direction, snapshot)
        ai_min_score_add += int(extra.get('extra_score', 0) or 0)
        req_conf += int(extra.get('extra_confirmations', 0) or 0)
        if extra.get('required'):
            reasons.append(extra.get('reason') or 'AI Learning: تایید بیشتر لازم است')

        move_phase = str(movement_decision.get('move_phase', 'UNKNOWN')).upper()
        trap_risk = str(movement_decision.get('trap_risk', 'LOW')).upper()
        move_freshness = str(movement_decision.get('move_freshness', 'LOW')).upper()

        # Fresh movement is the main gate. Direction alone is not enough.
        if move_phase == 'EXHAUSTION':
            ai_block = True
            reasons.append(f'AI State Block: حرکت کاملاً تمام/فرسوده است ({move_phase})')
        elif move_phase == 'RANGE_AFTER_MOVE':
            # Range-after-move is no longer a hard reject. For scalping it can be
            # a valid continuation/restart setup when evidence is strong.
            ai_penalty += 5
            ai_min_score_add += 1
            reasons.append('AI State: بعد از حرکت وارد رنج شده؛ فقط با شواهد قوی مجاز است')
        elif move_phase == 'MID':
            # Softer Movement Hunter mode: MID is not automatically dead.
            # It is still penalized, but strong fresh momentum can pass as REAL.
            ai_penalty += 4
            ai_min_score_add += 1
            reasons.append('AI State: حرکت در میانه راه است، فقط با قدرت بالاتر مجاز است')
        elif move_phase == 'SETUP':
            # SETUP should be able to activate when score/evidence are strong.
            ai_penalty += 1
            reasons.append('AI Setup: آماده حرکت است؛ با تایید قوی می‌تواند فعال شود')

        if trap_risk == 'HIGH':
            ai_block = True
            reasons.append('AI Trap Block: ریسک تله/لیکوییدیتی بالا')
        elif trap_risk == 'MEDIUM':
            ai_penalty += 2
            reasons.append('AI Trap: ریسک متوسط')

        if risk_score >= 92 and final_score < 95:
            ai_block = True
            reasons.append('AI Block: ریسک کوین/جهت خیلی بالا و امتیاز کافی نیست')

        final_score -= ai_penalty
        min_score = min(94, base_min_score + ai_min_score_add)
        effective_req_conf = min(6, max(1, req_conf))

        level_pack = get_strong_levels(df_5m, df_15m, df_30m, price, atr)
        support = level_pack.get('nearest_support')
        resistance = level_pack.get('nearest_resistance')

        ai_decision_kind = str(movement_decision.get('decision', 'REJECT')).upper()
        selected_pack = movement_decision.get('selected', {}) if isinstance(movement_decision, dict) else {}
        selected_evidence_count = len(selected_pack.get('evidence') or []) if isinstance(selected_pack, dict) else 0

        # Normal REAL path stays safest.
        real_confirmed = (
            direction in {'LONG', 'SHORT'}
            and not ai_block
            and ai_decision_kind in {'REAL', 'ACTIVE', 'ENTRY'}
            and final_score >= min_score
            and confirmations >= effective_req_conf
        )

        # Soft activation path: some strong SETUP/GHOST/WATCH candidates should
        # become ACTIVE instead of staying Ghost forever. This still blocks HIGH
        # trap and exhausted/range-after-move states, and requires enough score,
        # evidence, and at least minimal confirmation.
        soft_setup_confirmed = (
            direction in {'LONG', 'SHORT'}
            and not ai_block
            and ai_decision_kind in {'GHOST', 'SETUP', 'WATCH'}
            and move_phase in {'START', 'EARLY', 'SETUP', 'WATCH'}
            and trap_risk in {'LOW', 'MEDIUM'}
            and final_score >= max(int(AI_MOVEMENT_REAL_MIN_SCORE) - 2, min_score - 4)
            and confirmations >= max(1, effective_req_conf - 1)
            and selected_evidence_count >= 2
        )

        # Very strong MID moves can pass only when trap is LOW and evidence is strong.
        soft_mid_confirmed = (
            direction in {'LONG', 'SHORT'}
            and not ai_block
            and ai_decision_kind in {'GHOST', 'SETUP', 'WATCH', 'REAL'}
            and move_phase == 'MID'
            and trap_risk == 'LOW'
            and final_score >= int(AI_MOVEMENT_REAL_MIN_SCORE) + 2
            and confirmations >= max(1, effective_req_conf - 1)
            and selected_evidence_count >= 3
        )

        # Controlled continuation after range: not dry, but still protected.
        soft_range_confirmed = (
            direction in {'LONG', 'SHORT'}
            and not ai_block
            and ai_decision_kind in {'GHOST', 'SETUP', 'WATCH', 'REAL'}
            and move_phase == 'RANGE_AFTER_MOVE'
            and trap_risk == 'LOW'
            and final_score >= int(AI_MOVEMENT_REAL_MIN_SCORE) + 6
            and confirmations >= max(2, effective_req_conf)
            and selected_evidence_count >= 4
        )

        entry_confirmed = bool(real_confirmed or soft_setup_confirmed or soft_mid_confirmed or soft_range_confirmed)
        effective_ai_decision_kind = 'REAL' if entry_confirmed else ai_decision_kind
        soft_activation = bool((soft_setup_confirmed or soft_mid_confirmed or soft_range_confirmed) and not real_confirmed)
        if soft_activation:
            reasons.append('AI Soft Activation: ستاپ قوی به ورود فعال تبدیل شد')

        # For backward compatibility, long_score/short_score are now AI movement
        # candidate scores, not classic scores.
        candidates = movement_decision.get('candidates', {}) if isinstance(movement_decision, dict) else {}
        long_score = int((candidates.get('LONG') or {}).get('score', 0) or 0)
        short_score = int((candidates.get('SHORT') or {}).get('score', 0) or 0)

        prediction_pack = snapshot.get('prediction_layer', {}) if isinstance(snapshot, dict) else {}
        common = {
            'symbol': symbol,
            'score': cap_score(final_score),
            'long_score': long_score,
            'short_score': short_score,
            'classic_long_score': sp.get('long_score', 0),
            'classic_short_score': sp.get('short_score', 0),
            'price': safe_round(price),
            'closed_15m_price': safe_round(closed_15m_price),
            'entry_price_source': '5M_CURRENT' if USE_CURRENT_5M_FOR_ENTRY else '15M_CLOSED',
            'atr': safe_round(atr),
            'market_regime': market_context.get('market_regime', 'NEUTRAL'),
            'btc_bias': market_context.get('btc_bias', 'NEUTRAL'),
            'btc_lead': market_context.get('btc_lead', {}),
            'market_regime_legacy': market_context.get('market_regime_legacy', 'neutral'),
            'btc_bias_legacy': market_context.get('btc_bias_legacy', 'neutral'),
            'confirmations': confirmations,
            'required_confirmations': effective_req_conf,
            'rsi': safe_round(df_15m.iloc[-1]['rsi'], 2),
            'macd': safe_round(df_15m.iloc[-1]['macd'], 6),
            'macd_signal': safe_round(df_15m.iloc[-1]['macd_signal'], 6),
            'macd_hist': safe_round(df_15m.iloc[-1]['macd_hist'], 6),
            'adx': safe_round(df_15m.iloc[-1]['adx'], 2),
            'vwap_status': vwap_status(df_15m),
            'support': safe_round(support),
            'resistance': safe_round(resistance),
            'trends': sp.get('trends', {}),
            'distance_ema20_atr': max(
                safe_float((sensors.get('timeframes', {}).get('5M') or {}).get('ema20_distance_atr')),
                safe_float((sensors.get('timeframes', {}).get('15M') or {}).get('ema20_distance_atr')),
            ),
            'volume_status': sp.get('volume_status'),
            'volume_ratio': sp.get('volume_ratio'),
            'buy_power': sp.get('buy_power'),
            'sell_power': sp.get('sell_power'),
            'power2_buy': sp.get('power2_buy'),
            'power2_sell': sp.get('power2_sell'),
            'power3_buy': sp.get('power3_buy'),
            'power3_sell': sp.get('power3_sell'),
            'snapshot': snapshot,
            'technical_sensors': sensors,
            'coin_risk': risk_state,
            'rotation': rotation,
            'ai_decision': {
                'architecture': 'AI_MOVEMENT_HUNTER',
                'classic_signal_disabled': True,
                'classic_score_disabled': True,
                'base_min_score': base_min_score,
                'min_score': min_score,
                'ai_penalty': ai_penalty,
                'ai_min_score_add': ai_min_score_add,
                'ai_block': ai_block,
                'decision': effective_ai_decision_kind,
                'raw_decision': ai_decision_kind,
                'soft_activation': soft_activation,
                'strictness_level': strict,
                'sl_count': sl_count,
                'risk_score': risk_score,
                'rotation_score': rs,
                'ai_movement_score': cap_score(final_score),
                'move_phase': move_phase,
                'move_freshness': move_freshness,
                'trap_risk': trap_risk,
                'selected': movement_decision.get('selected', {}),
            },
            'ai_movement_hunter': movement_decision,
            'prediction_layer': prediction_pack,
            'reasons': reasons[:24],
            'signal_timeframe': 'AI Movement Hunter 5M/15M',
        }

        # Build TP/SL before the final REAL gate too.
        # If AI says GHOST/SETUP, scanner can store it as a Ghost signal for learning.
        ghost_sl = ghost_tp1 = ghost_tp2 = ghost_rr = None
        ghost_tp_meta = {}
        if direction in {'LONG', 'SHORT'}:
            try:
                ghost_sl, ghost_tp1, ghost_tp2, ghost_rr, ghost_tp_meta = build_trade_levels(
                    direction, price, atr, df_5m, df_15m, df_30m, snapshot, symbol
                )
            except Exception as _level_err:
                reasons.append(f'TP/SL build error: {str(_level_err)[:120]}')

        if not entry_confirmed:
            entry_mode = 'AI_GHOST_SETUP' if ai_decision_kind in {'GHOST', 'SETUP', 'WATCH'} else 'AI_NO_ENTRY'
            can_ghost = (
                direction in {'LONG', 'SHORT'}
                and not ai_block
                and ai_decision_kind in {'GHOST', 'SETUP', 'WATCH'}
                and final_score >= int(AI_MOVEMENT_SETUP_MIN_SCORE)
                and ghost_sl is not None
                and ghost_tp1 is not None
                and move_phase != 'EXHAUSTION'
                and trap_risk != 'HIGH'
            )
            return {
                **common,
                'direction': direction if can_ghost else 'NO TRADE',
                'candidate_direction': direction if direction in {'LONG', 'SHORT'} else None,
                'status': 'SETUP' if can_ghost else 'NO_TRADE',
                'entry_confirmed': False,
                'entry_mode': entry_mode,
                'entry': safe_round(price) if can_ghost else None,
                'stop_loss': ghost_sl if can_ghost else None,
                'tp1': ghost_tp1 if can_ghost else None,
                'tp2': ghost_tp2 if can_ghost else None,
                'risk_reward': ghost_rr if can_ghost else 0,
                'risk_level': 'GHOST' if can_ghost else 'UNKNOWN',
                'freshness': move_freshness,
                'tp_meta': ghost_tp_meta if can_ghost else {},
                'validity': 'برای یادگیری - سفارش واقعی نیست' if can_ghost else 'سیگنال معتبر نیست',
                'valid_gate': False,
                'min_score': min_score,
            }

        sl, tp1, tp2, rr, tp_meta = build_trade_levels(direction, price, atr, df_5m, df_15m, df_30m, snapshot, symbol)
        if safe_float(rr, 0.0) < MIN_REAL_RISK_REWARD:
            rr_reason = f"رد R/R: نسبت سود به ضرر {rr} کمتر از حداقل {MIN_REAL_RISK_REWARD}"
            reasons.append(rr_reason)
            common["reasons"] = reasons[:24]
            return {
                **common,
                'direction': 'NO TRADE',
                'candidate_direction': direction,
                'status': 'NO_TRADE',
                'entry_confirmed': False,
                'entry_mode': 'RR_FILTER',
                'entry': None,
                'stop_loss': sl,
                'tp1': tp1,
                'tp2': tp2,
                'risk_reward': rr,
                'risk_level': 'UNKNOWN',
                'freshness': move_freshness,
                'tp_meta': tp_meta,
                'validity': 'سیگنال معتبر نیست',
                'valid_gate': False,
                'min_score': min_score,
            }

        risk_level = 'LOW' if final_score >= 92 and move_freshness == 'HIGH' else 'MEDIUM' if final_score >= 86 else 'HIGH'
        freshness = move_freshness
        if update_ai_summary:
            try:
                update_ai_summary(total_signals=1, market_regime=market_context.get('market_regime'), btc_bias=market_context.get('btc_bias'), btc_lead=market_context.get('btc_lead'), market_context=market_context)
            except TypeError:
                try:
                    update_ai_summary(total_signals=1)
                except Exception:
                    pass
            except Exception:
                pass

        return {
            **common,
            'direction': direction,
            'status': 'ACTIVE',
            'entry_confirmed': True,
            'entry_mode': 'AI_SOFT_MOVEMENT_HUNTER' if soft_activation else 'AI_MOVEMENT_HUNTER',
            'entry': safe_round(price),
            'stop_loss': sl,
            'tp1': tp1,
            'tp2': tp2,
            'risk_reward': rr,
            'risk_level': risk_level,
            'freshness': freshness,
            'tp_meta': tp_meta,
            'validity': '15 تا 45 دقیقه',
        }
    except Exception as e:
        return {
            'symbol': symbol,
            'direction': 'NO TRADE',
            'status': 'NO_TRADE',
            'entry_confirmed': False,
            'entry_mode': 'ERROR',
            'score': 0,
            'long_score': 0,
            'short_score': 0,
            'price': None,
            'entry': None,
            'stop_loss': None,
            'tp1': None,
            'tp2': None,
            'atr': None,
            'risk_reward': 0,
            'risk_level': 'UNKNOWN',
            'market_regime': 'unknown',
            'btc_bias': 'unknown',
            'freshness': 'LOW',
            'confirmations': 0,
            'required_confirmations': 0,
            'rsi': None,
            'macd': None,
            'macd_signal': None,
            'macd_hist': None,
            'adx': None,
            'vwap_status': None,
            'support': None,
            'resistance': None,
            'trends': {},
            'snapshot': {},
            'coin_risk': {},
            'rotation': {},
            'tp_meta': {},
            'ai_decision': {'architecture': 'AI_MOVEMENT_HUNTER', 'classic_signal_disabled': True, 'classic_score_disabled': True},
            'reasons': [f'Analysis Error: {str(e)[:200]}'],
            'signal_timeframe': 'AI Movement Hunter 5M/15M',
            'validity': 'سیگنال معتبر نیست',
        }
