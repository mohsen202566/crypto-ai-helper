# -*- coding: utf-8 -*-
import ccxt
import pandas as pd
import ta

from market_sentiment import get_market_sentiment
from trend_analysis import detect_trendline, detect_breakout, trendline_score, breakout_score
from market_structure import detect_market_structure, structure_score


exchange = ccxt.okx({
    "enableRateLimit": True,
    "timeout": 20000,
    "options": {"defaultType": "swap"}
})

_MARKETS_CACHE = None


def get_okx_markets():
    global _MARKETS_CACHE
    if _MARKETS_CACHE is not None:
        return _MARKETS_CACHE

    try:
        _MARKETS_CACHE = exchange.load_markets()
    except Exception:
        _MARKETS_CACHE = {}
    return _MARKETS_CACHE


def symbol_supported(symbol):
    markets = get_okx_markets()
    if not markets:
        return True
    return to_okx_symbol(symbol) in markets


class AnalysisDataError(Exception):
    pass



def to_okx_symbol(symbol):
    coin = symbol.replace("USDT", "")
    return f"{coin}/USDT:USDT"


def safe_round(value, digits=8):
    if value is None:
        return None
    try:
        return round(float(value), digits)
    except Exception:
        return None


def cap_score(value):
    return max(0, min(int(value), 100))


def get_klines(symbol, interval="15m", limit=320):
    if not symbol_supported(symbol):
        raise AnalysisDataError(f"نماد {symbol} در OKX Swap قابل معامله نیست")

    try:
        ohlcv = exchange.fetch_ohlcv(to_okx_symbol(symbol), timeframe=interval, limit=limit)
    except Exception as e:
        raise AnalysisDataError(f"دریافت داده {symbol} در تایم {interval} ناموفق بود: {e}")

    # برای EMA200 حداقل حدود 220 کندل لازم است؛ اگر کمتر باشد تحلیل قابل اعتماد نیست.
    if not ohlcv or len(ohlcv) < 220:
        raise AnalysisDataError(f"داده کافی برای {symbol} در تایم {interval} دریافت نشد")

    df = pd.DataFrame(ohlcv, columns=["time", "open", "high", "low", "close", "volume"])
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna().iloc[:-1]

    if len(df) < 210:
        raise AnalysisDataError(f"داده کافی بعد از پاکسازی برای {symbol} در تایم {interval} وجود ندارد")

    return df


def add_indicators(df):
    df = df.copy()
    df["ema20"] = ta.trend.ema_indicator(df["close"], window=20)
    df["ema50"] = ta.trend.ema_indicator(df["close"], window=50)
    df["ema200"] = ta.trend.ema_indicator(df["close"], window=200)
    df["rsi"] = ta.momentum.rsi(df["close"], window=14)

    macd = ta.trend.MACD(df["close"])
    df["macd"] = macd.macd()
    df["macd_signal"] = macd.macd_signal()
    df["macd_hist"] = macd.macd_diff()

    df["atr"] = ta.volatility.average_true_range(df["high"], df["low"], df["close"], window=14)

    adx = ta.trend.ADXIndicator(df["high"], df["low"], df["close"], window=14)
    df["adx"] = adx.adx()

    df["volume_ma20"] = df["volume"].rolling(20).mean()
    df["atr_ma50"] = df["atr"].rolling(50).mean()

    typical_price = (df["high"] + df["low"] + df["close"]) / 3
    df["vwap"] = (typical_price * df["volume"]).cumsum() / df["volume"].cumsum()

    df = df.dropna()
    if len(df) < 80:
        raise AnalysisDataError("اندیکاتورها کامل محاسبه نشدند")
    return df


def get_funding_rate(symbol):
    try:
        data = exchange.fetch_funding_rate(to_okx_symbol(symbol))
        rate = data.get("fundingRate")
        return None if rate is None else round(float(rate) * 100, 5)
    except Exception:
        return None


def get_open_interest(symbol):
    try:
        data = exchange.fetch_open_interest(to_okx_symbol(symbol))
        value = data.get("openInterestAmount") or data.get("openInterestValue")
        return None if value is None else float(value)
    except Exception:
        return None


def get_spread_percent(symbol):
    try:
        orderbook = exchange.fetch_order_book(to_okx_symbol(symbol), limit=5)
        if not orderbook.get("bids") or not orderbook.get("asks"):
            return None
        bid = orderbook["bids"][0][0]
        ask = orderbook["asks"][0][0]
        mid = (bid + ask) / 2
        return round(((ask - bid) / mid) * 100, 4)
    except Exception:
        return None


def trend_direction(df):
    last = df.iloc[-1]
    if last["close"] > last["ema20"] > last["ema50"] > last["ema200"]:
        return "bullish"
    if last["close"] < last["ema20"] < last["ema50"] < last["ema200"]:
        return "bearish"
    if last["close"] > last["ema200"]:
        return "weak_bullish"
    if last["close"] < last["ema200"]:
        return "weak_bearish"
    return "range"


def buy_sell_power(df):
    recent = df.tail(20)
    green = recent[recent["close"] > recent["open"]]["volume"].sum()
    red = recent[recent["close"] < recent["open"]]["volume"].sum()
    total = green + red
    if total == 0:
        return 50, 50
    return round((green / total) * 100, 1), round((red / total) * 100, 1)


def support_resistance(df):
    recent = df.tail(80)
    return recent["low"].min(), recent["high"].max()


def is_middle_of_range(price, support, resistance):
    if resistance <= support:
        return False
    pos = (price - support) / (resistance - support)
    return 0.38 <= pos <= 0.62


def candle_pattern(df):
    last = df.iloc[-1]
    prev = df.iloc[-2]
    body = abs(last["close"] - last["open"])
    rng = last["high"] - last["low"]
    if rng == 0:
        return "weak"
    upper = last["high"] - max(last["close"], last["open"])
    lower = min(last["close"], last["open"]) - last["low"]

    if last["close"] > last["open"] and prev["close"] < prev["open"]:
        if last["close"] > prev["open"] and last["open"] < prev["close"]:
            return "bullish_engulfing"
    if last["close"] < last["open"] and prev["close"] > prev["open"]:
        if last["close"] < prev["open"] and last["open"] > prev["close"]:
            return "bearish_engulfing"
    if lower > body * 2.2 and upper < body * 1.2:
        return "bullish_pinbar"
    if upper > body * 2.2 and lower < body * 1.2:
        return "bearish_pinbar"
    if body / rng >= 0.6:
        return "bullish_strong" if last["close"] > last["open"] else "bearish_strong"
    return "weak"


def multi_candle_confirmation(df):
    recent = df.tail(3)
    bullish = len(recent[recent["close"] > recent["open"]])
    bearish = len(recent[recent["close"] < recent["open"]])
    if bullish >= 2 and recent.iloc[-1]["close"] > recent.iloc[-2]["close"]:
        return "bullish"
    if bearish >= 2 and recent.iloc[-1]["close"] < recent.iloc[-2]["close"]:
        return "bearish"
    return "neutral"


def volume_spike(df):
    last = df.iloc[-1]
    return last["volume_ma20"] != 0 and last["volume"] > last["volume_ma20"] * 1.5


def minimum_volatility_ok(df):
    last = df.iloc[-1]
    if last["close"] == 0:
        return False
    return (last["atr"] / last["close"]) * 100 >= 0.08


def market_is_choppy(df_15m, df_5m):
    last_15 = df_15m.iloc[-1]
    last_5 = df_5m.iloc[-1]
    ema_gap_15 = abs(last_15["ema20"] - last_15["ema50"]) / last_15["close"] * 100
    ema_gap_5 = abs(last_5["ema20"] - last_5["ema50"]) / last_5["close"] * 100
    return (ema_gap_15 < 0.08 and ema_gap_5 < 0.08) or (last_15["adx"] < 18 and last_5["adx"] < 18)


def detect_liquidity_grab(df):
    recent = df.tail(20)
    last = df.iloc[-1]
    prev_high = recent.iloc[:-1]["high"].max()
    prev_low = recent.iloc[:-1]["low"].min()
    if last["high"] > prev_high and last["close"] < prev_high:
        return "bearish_liquidity_grab"
    if last["low"] < prev_low and last["close"] > prev_low:
        return "bullish_liquidity_grab"
    return "none"


def detect_stop_hunt(df):
    recent = df.tail(30)
    last = df.iloc[-1]
    prev_high = recent.iloc[:-1]["high"].max()
    prev_low = recent.iloc[:-1]["low"].min()
    rng = last["high"] - last["low"]
    if rng == 0:
        return "none"
    upper = last["high"] - max(last["open"], last["close"])
    lower = min(last["open"], last["close"]) - last["low"]
    if last["high"] > prev_high and upper / rng > 0.45:
        return "bearish_stop_hunt"
    if last["low"] < prev_low and lower / rng > 0.45:
        return "bullish_stop_hunt"
    return "none"


def detect_fvg(df):
    if len(df) < 5:
        return "none"
    c1 = df.iloc[-3]
    c3 = df.iloc[-1]
    if c1["high"] < c3["low"]:
        return "bullish_fvg"
    if c1["low"] > c3["high"]:
        return "bearish_fvg"
    return "none"


def detect_order_block(df):
    recent = df.tail(16)
    for i in range(len(recent) - 3, 2, -1):
        candle = recent.iloc[i]
        nxt = recent.iloc[i + 1]
        if candle["close"] < candle["open"] and nxt["close"] > nxt["open"] and nxt["close"] > candle["high"]:
            return "bullish_order_block"
        if candle["close"] > candle["open"] and nxt["close"] < nxt["open"] and nxt["close"] < candle["low"]:
            return "bearish_order_block"
    return "none"


def detect_rsi_divergence(df):
    recent = df.tail(35)
    lows = recent.nsmallest(2, "low").sort_index()
    highs = recent.nlargest(2, "high").sort_index()
    if len(lows) == 2 and lows.iloc[1]["low"] < lows.iloc[0]["low"] and lows.iloc[1]["rsi"] > lows.iloc[0]["rsi"]:
        return "bullish_rsi_divergence"
    if len(highs) == 2 and highs.iloc[1]["high"] > highs.iloc[0]["high"] and highs.iloc[1]["rsi"] < highs.iloc[0]["rsi"]:
        return "bearish_rsi_divergence"
    return "none"


def detect_macd_divergence(df):
    recent = df.tail(35)
    lows = recent.nsmallest(2, "low").sort_index()
    highs = recent.nlargest(2, "high").sort_index()
    if len(lows) == 2 and lows.iloc[1]["low"] < lows.iloc[0]["low"] and lows.iloc[1]["macd_hist"] > lows.iloc[0]["macd_hist"]:
        return "bullish_macd_divergence"
    if len(highs) == 2 and highs.iloc[1]["high"] > highs.iloc[0]["high"] and highs.iloc[1]["macd_hist"] < highs.iloc[0]["macd_hist"]:
        return "bearish_macd_divergence"
    return "none"


def detect_fake_breakout(df):
    recent = df.tail(25)
    last = df.iloc[-1]
    prev_high = recent.iloc[:-1]["high"].max()
    prev_low = recent.iloc[:-1]["low"].min()
    if last["high"] > prev_high and last["close"] < prev_high:
        return "fake_bullish_breakout"
    if last["low"] < prev_low and last["close"] > prev_low:
        return "fake_bearish_breakout"
    return "none"


def detect_trend_exhaustion(df):
    last = df.iloc[-1]
    prev = df.iloc[-2]
    if last["rsi"] > 74 and last["macd_hist"] < prev["macd_hist"]:
        return "bullish_exhaustion"
    if last["rsi"] < 26 and last["macd_hist"] > prev["macd_hist"]:
        return "bearish_exhaustion"
    return "none"


def calculate_vwap_status(df):
    last = df.iloc[-1]
    if last["close"] > last["vwap"]:
        return "above_vwap"
    if last["close"] < last["vwap"]:
        return "below_vwap"
    return "near_vwap"


def calculate_volume_profile(df):
    recent = df.tail(120).copy()
    try:
        recent["price_bin"] = pd.cut(recent["close"], bins=24)
        grouped = recent.groupby("price_bin", observed=False)["volume"].sum()
        if grouped.empty:
            return None, "unknown"
        poc_bin = grouped.idxmax()
        poc_price = (poc_bin.left + poc_bin.right) / 2
        last_price = float(recent.iloc[-1]["close"])
        if last_price > poc_price:
            status = "above_poc"
        elif last_price < poc_price:
            status = "below_poc"
        else:
            status = "near_poc"
        return float(poc_price), status
    except Exception:
        return None, "unknown"


def btc_filter(symbol):
    if symbol == "BTCUSDT":
        return "neutral", 0, 0, [], []
    try:
        btc_15m = add_indicators(get_klines("BTCUSDT", "15m"))
        btc_5m = add_indicators(get_klines("BTCUSDT", "5m"))
        t15 = trend_direction(btc_15m)
        t5 = trend_direction(btc_5m)
        if t15 in ["bullish", "weak_bullish"] and t5 in ["bullish", "weak_bullish"]:
            return "ok", 7, 0, ["BTC در تایم ورود صعودی است"], []
        if t15 in ["bearish", "weak_bearish"] and t5 in ["bearish", "weak_bearish"]:
            return "ok", 0, 7, [], ["BTC در تایم ورود نزولی است"]
        return "ok", 0, 0, ["BTC جهت واضحی ندارد"], ["BTC جهت واضحی ندارد"]
    except Exception:
        return "unknown", 0, 0, [], []


def score_macro_trend(df_1d, df_4h, df_1h, df_30m):
    trends = {
        "1D": trend_direction(df_1d),
        "4H": trend_direction(df_4h),
        "1H": trend_direction(df_1h),
        "30M": trend_direction(df_30m),
    }
    weights = {"1D": 8, "4H": 12, "1H": 14, "30M": 14}
    long_score = short_score = 0
    rl, rs = [], []
    for tf, tr in trends.items():
        w = weights[tf]
        if tr == "bullish":
            long_score += w; rl.append(f"{tf}: روند صعودی")
        elif tr == "weak_bullish":
            long_score += int(w * 0.5); rl.append(f"{tf}: تمایل صعودی")
        elif tr == "bearish":
            short_score += w; rs.append(f"{tf}: روند نزولی")
        elif tr == "weak_bearish":
            short_score += int(w * 0.5); rs.append(f"{tf}: تمایل نزولی")
    return long_score, short_score, rl, rs, trends


def score_entry(df_15m, df_5m):
    long_score = short_score = 0
    rl, rs = [], []
    last_15 = df_15m.iloc[-1]
    last_5 = df_5m.iloc[-1]
    buy_power, sell_power = buy_sell_power(df_5m)

    if last_15["close"] > last_15["ema20"] > last_15["ema50"]:
        long_score += 15; rl.append("15M: قیمت بالای EMA20 و EMA50")
    if last_15["close"] < last_15["ema20"] < last_15["ema50"]:
        short_score += 15; rs.append("15M: قیمت زیر EMA20 و EMA50")

    if last_5["close"] > last_5["ema20"] and last_5["macd"] > last_5["macd_signal"]:
        long_score += 15; rl.append("5M: تایید ورود لانگ با EMA و MACD")
    if last_5["close"] < last_5["ema20"] and last_5["macd"] < last_5["macd_signal"]:
        short_score += 15; rs.append("5M: تایید ورود شورت با EMA و MACD")

    if 45 <= last_5["rsi"] <= 68:
        long_score += 10; rl.append("RSI مناسب برای لانگ در 5M")
    if 32 <= last_5["rsi"] <= 55:
        short_score += 10; rs.append("RSI مناسب برای شورت در 5M")

    if buy_power >= 62:
        long_score += 10; rl.append("قدرت خرید بالا در تایم ورود")
    if sell_power >= 62:
        short_score += 10; rs.append("قدرت فروش بالا در تایم ورود")

    pattern = candle_pattern(df_5m)
    multi = multi_candle_confirmation(df_5m)
    if pattern in ["bullish_engulfing", "bullish_pinbar", "bullish_strong"]:
        long_score += 10; rl.append(f"کندل تاییدی لانگ: {pattern}")
    if pattern in ["bearish_engulfing", "bearish_pinbar", "bearish_strong"]:
        short_score += 10; rs.append(f"کندل تاییدی شورت: {pattern}")
    if multi == "bullish":
        long_score += 8; rl.append("تایید چند کندلی صعودی")
    if multi == "bearish":
        short_score += 8; rs.append("تایید چند کندلی نزولی")
    if volume_spike(df_5m):
        long_score += 6; short_score += 6; rl.append("افزایش حجم واقعی"); rs.append("افزایش حجم واقعی")
    if last_5["adx"] >= 22:
        long_score += 5; short_score += 5

    return long_score, short_score, rl, rs, buy_power, sell_power, pattern, multi


def score_smart_money(df_15m, df_5m):
    long_score = short_score = 0
    rl, rs = [], []
    liquidity_grab = detect_liquidity_grab(df_5m)
    stop_hunt = detect_stop_hunt(df_5m)
    fvg = detect_fvg(df_5m)
    order_block = detect_order_block(df_15m)

    if liquidity_grab == "bullish_liquidity_grab":
        long_score += 10; rl.append("Liquidity Grab صعودی")
    if liquidity_grab == "bearish_liquidity_grab":
        short_score += 10; rs.append("Liquidity Grab نزولی")
    if stop_hunt == "bullish_stop_hunt":
        long_score += 8; rl.append("Stop Hunt صعودی")
    if stop_hunt == "bearish_stop_hunt":
        short_score += 8; rs.append("Stop Hunt نزولی")

    # FVG خیلی نرم: فقط اثر کم، نه رد کامل
    if fvg == "bullish_fvg":
        long_score += 1; rl.append("FVG صعودی")
    if fvg == "bearish_fvg":
        short_score += 1; rs.append("FVG نزولی")

    if order_block == "bullish_order_block":
        long_score += 7; rl.append("Order Block صعودی")
    if order_block == "bearish_order_block":
        short_score += 7; rs.append("Order Block نزولی")

    return long_score, short_score, rl, rs, liquidity_grab, stop_hunt, fvg, order_block


def score_divergence(df_5m):
    long_score = short_score = 0
    rl, rs = [], []
    rsi_div = detect_rsi_divergence(df_5m)
    macd_div = detect_macd_divergence(df_5m)
    if rsi_div == "bullish_rsi_divergence":
        long_score += 10; rl.append("واگرایی مثبت RSI")
    if rsi_div == "bearish_rsi_divergence":
        short_score += 10; rs.append("واگرایی منفی RSI")
    if macd_div == "bullish_macd_divergence":
        long_score += 10; rl.append("واگرایی مثبت MACD")
    if macd_div == "bearish_macd_divergence":
        short_score += 10; rs.append("واگرایی منفی MACD")
    return long_score, short_score, rl, rs, rsi_div, macd_div


def score_futures_data(symbol):
    funding = get_funding_rate(symbol)
    oi = get_open_interest(symbol)
    long_score = short_score = 0
    notes = []
    if funding is not None:
        if funding > 0.05:
            short_score += 4; notes.append("Funding مثبت و نسبتاً بالا")
        elif funding < -0.05:
            long_score += 4; notes.append("Funding منفی و نسبتاً بالا")
    if oi is not None and oi > 0:
        long_score += 2; short_score += 2
    return long_score, short_score, funding, oi, notes


def score_market_sentiment(symbol):
    market = get_market_sentiment()
    long_score = short_score = 0
    rl, rs = [], []
    fear = market.get("fear_value")
    altseason = market.get("altseason_status")

    # ترس و طمع، دامیننس و آلت‌سیزن باقی می‌مانند؛ خبر حذف شده است.
    if fear is not None:
        if fear <= 25:
            long_score += 3; rl.append("Fear & Greed در ترس شدید")
        elif fear >= 80:
            short_score += 3; rs.append("Fear & Greed در طمع شدید")

    if symbol != "BTCUSDT":
        if altseason == "قوی":
            long_score += 3; rl.append("آلت‌سیزن برای آلت‌کوین‌ها مناسب است")
        elif altseason == "ضعیف":
            short_score += 3; rs.append("آلت‌سیزن ضعیف است")
    return long_score, short_score, rl, rs, market


def score_vwap_volume_profile(df_15m, df_5m):
    long_score = short_score = 0
    rl, rs = [], []
    vwap_status = calculate_vwap_status(df_5m)
    poc, vol_status = calculate_volume_profile(df_15m)
    if vwap_status == "above_vwap":
        long_score += 6; rl.append("قیمت بالای VWAP است")
    if vwap_status == "below_vwap":
        short_score += 6; rs.append("قیمت پایین VWAP است")
    if vol_status == "above_poc":
        long_score += 5; rl.append("قیمت بالای POC حجمی است")
    if vol_status == "below_poc":
        short_score += 5; rs.append("قیمت پایین POC حجمی است")
    return long_score, short_score, rl, rs, vwap_status, poc, vol_status


def apply_direction_conflict_penalties(long_score, short_score, raw_direction, buy_power, sell_power,
                                       vwap_status, trendline, structure, fvg):
    rl, rs = [], []

    # قدرت خرید/فروش فقط جریمه نرم؛ رد کامل نیست
    gap = abs(buy_power - sell_power)
    if buy_power > sell_power and gap >= 15:
        short_score -= 6; rs.append("جریمه: قدرت خرید نسبت به فروش بالاتر است")
    elif buy_power > sell_power and gap >= 8:
        short_score -= 3; rs.append("جریمه سبک: قدرت خرید کمی بالاتر است")
    if sell_power > buy_power and gap >= 15:
        long_score -= 6; rl.append("جریمه: قدرت فروش نسبت به خرید بالاتر است")
    elif sell_power > buy_power and gap >= 8:
        long_score -= 3; rl.append("جریمه سبک: قدرت فروش کمی بالاتر است")

    # VWAP، ترندلاین و ساختار جریمه/تقویت متعادل
    if vwap_status == "above_vwap":
        long_score += 3; short_score -= 3
        rl.append("تقویت: قیمت بالای VWAP است")
        rs.append("جریمه سبک: شورت بالای VWAP ریسک دارد")
    if vwap_status == "below_vwap":
        short_score += 3; long_score -= 3
        rs.append("تقویت: قیمت زیر VWAP است")
        rl.append("جریمه سبک: لانگ زیر VWAP ریسک دارد")

    if trendline == "uptrend":
        long_score += 8; short_score -= 10
        rl.append("تقویت: خط روند صعودی است")
        rs.append("جریمه: شورت خلاف خط روند صعودی است")
    elif trendline == "downtrend":
        short_score += 8; long_score -= 10
        rs.append("تقویت: خط روند نزولی است")
        rl.append("جریمه: لانگ خلاف خط روند نزولی است")

    if structure == "bullish_structure":
        long_score += 10; short_score -= 12
        rl.append("تقویت: ساختار بازار صعودی است")
        rs.append("جریمه: شورت خلاف ساختار صعودی بازار است")
    elif structure == "bearish_structure":
        short_score += 10; long_score -= 12
        rs.append("تقویت: ساختار بازار نزولی است")
        rl.append("جریمه: لانگ خلاف ساختار نزولی بازار است")

    # FVG خیلی نرم
    if fvg == "bullish_fvg":
        short_score -= 1; rs.append("جریمه خیلی سبک: FVG صعودی خلاف شورت است")
    if fvg == "bearish_fvg":
        long_score -= 1; rl.append("جریمه خیلی سبک: FVG نزولی خلاف لانگ است")

    return cap_score(long_score), cap_score(short_score), rl, rs


def calculate_trade_levels(raw_direction, price, atr, support=None, resistance=None):
    buffer = atr * 0.15
    if raw_direction == "LONG":
        sl = price - atr * 1.2
        tp1 = price + atr * 1.5
        tp2 = price + atr * 2.5
        if resistance is not None and resistance > price:
            adjusted = resistance - buffer
            if adjusted > price:
                tp1 = min(tp1, adjusted)
        return sl, tp1, tp2
    if raw_direction == "SHORT":
        sl = price + atr * 1.2
        tp1 = price - atr * 1.5
        tp2 = price - atr * 2.5
        if support is not None and support < price:
            adjusted = support + buffer
            if adjusted < price:
                tp1 = max(tp1, adjusted)
        return sl, tp1, tp2
    return None, None, None


def risk_reward(direction, price, sl, tp1):
    if direction == "NO TRADE" or sl is None or tp1 is None:
        return 0
    risk = abs(price - sl)
    reward = abs(tp1 - price)
    return 0 if risk <= 0 else round(reward / risk, 2)


def calculate_level_percent(direction, price, level):
    if direction not in ["LONG", "SHORT"] or price is None or level is None:
        return None
    price = float(price); level = float(level)
    if price == 0:
        return None
    if direction == "LONG":
        return round(((level - price) / price) * 100, 3)
    return round(((price - level) / price) * 100, 3)


def calculate_risk_level(direction, score, liquidity_risk, funding, adx, spread, rr):
    if direction == "NO TRADE":
        return "بالا"
    risk = 0
    if score < 75: risk += 2
    if adx < 20: risk += 2
    if liquidity_risk == "بالا": risk += 2
    if funding is not None and abs(funding) > 0.07: risk += 1
    if spread is not None and spread > 0.08: risk += 2
    if rr < 1: risk += 2
    if risk >= 4: return "بالا"
    if risk >= 2: return "متوسط"
    return "پایین"


def entry_grade(score, risk_level, rr, direction):
    if direction == "NO TRADE":
        return "Reject"
    if score >= 90 and risk_level == "پایین" and rr >= 1:
        return "A+"
    if score >= 82 and risk_level in ["پایین", "متوسط"] and rr >= 1:
        return "A"
    if score >= 75 and rr >= 1:
        return "B"
    return "Reject"


def win_probability(score, risk_level, rr, adx, grade):
    p = 45 + int(score * 0.25)
    p += 10 if risk_level == "پایین" else 4 if risk_level == "متوسط" else -8
    p += 6 if rr >= 1.5 else 3 if rr >= 1 else -8
    p += 5 if adx >= 25 else -5 if adx < 18 else 0
    p += 5 if grade == "A+" else 3 if grade == "A" else -15 if grade == "Reject" else 0
    return max(0, min(p, 95))


def entry_filter(raw_direction, score, long_score, short_score, df_15m, df_5m, spread, order_block):
    last = df_5m.iloc[-1]
    price = float(last["close"])
    atr = float(last["atr"])
    support, resistance = support_resistance(df_15m)
    reasons = []
    liquidity_risk = "پایین"

    if raw_direction == "NO TRADE":
        return False, ["اختلاف لانگ و شورت کافی نیست"], "بالا", "none", "none"

    # فقط Order Block مخالف رد کامل است
    if raw_direction == "LONG" and order_block == "bearish_order_block":
        return False, ["اوردر بلاک نزولی خلاف سیگنال لانگ است"], "بالا", detect_fake_breakout(df_5m), detect_trend_exhaustion(df_5m)
    if raw_direction == "SHORT" and order_block == "bullish_order_block":
        return False, ["اوردر بلاک صعودی خلاف سیگنال شورت است"], "بالا", detect_fake_breakout(df_5m), detect_trend_exhaustion(df_5m)

    if market_is_choppy(df_15m, df_5m):
        reasons.append("بازار رنج، فشرده یا کم‌قدرت است")
        liquidity_risk = "بالا"
    if not minimum_volatility_ok(df_5m):
        reasons.append("نوسان برای معامله کافی نیست")
        liquidity_risk = "بالا"
    if spread is not None and spread > 0.08:
        reasons.append("اسپرد زیاد است")
        liquidity_risk = "بالا"
    if is_middle_of_range(price, support, resistance):
        reasons.append("قیمت وسط رنج است")
        liquidity_risk = "بالا"

    fake = detect_fake_breakout(df_5m)
    exhaustion = detect_trend_exhaustion(df_5m)

    if raw_direction == "LONG":
        if long_score < short_score + 18: reasons.append("اختلاف امتیاز لانگ و شورت کافی نیست")
        if (resistance - price) <= atr * 0.9: reasons.append("قیمت نزدیک مقاومت است"); liquidity_risk = "بالا"
        if last["rsi"] > 72: reasons.append("RSI برای لانگ بیش از حد بالاست")
        if last["adx"] < 18: reasons.append("قدرت روند برای لانگ کافی نیست")
        if fake == "fake_bullish_breakout": reasons.append("احتمال فیک بریک‌اوت صعودی")
        if exhaustion == "bullish_exhaustion": reasons.append("خستگی روند صعودی")

    if raw_direction == "SHORT":
        if short_score < long_score + 18: reasons.append("اختلاف امتیاز شورت و لانگ کافی نیست")
        if (price - support) <= atr * 0.9: reasons.append("قیمت نزدیک حمایت است"); liquidity_risk = "بالا"
        if last["rsi"] < 28: reasons.append("RSI برای شورت بیش از حد پایین است")
        if last["adx"] < 18: reasons.append("قدرت روند برای شورت کافی نیست")
        if fake == "fake_bearish_breakout": reasons.append("احتمال فیک بریک‌اوت نزولی")
        if exhaustion == "bearish_exhaustion": reasons.append("خستگی روند نزولی")

    return len(reasons) == 0, reasons, liquidity_risk, fake, exhaustion


def very_safe_status(direction, score, win_prob, risk_level, rr, trends, vwap_status, buy_power, sell_power,
                     adx, pattern, multi, order_block, fvg):
    reasons = []
    if direction == "NO TRADE":
        return False, ["جهت معامله مناسب نیست"]
    if score < 88: reasons.append("امتیاز برای حالت خیلی امن کافی نیست")
    if win_prob < 75: reasons.append("احتمال موفقیت برای حالت خیلی امن کافی نیست")
    if risk_level != "پایین": reasons.append("ریسک پایین نیست")
    if rr < 1.2: reasons.append("ریسک به ریوارد کافی نیست")
    if adx < 22: reasons.append("ADX کافی نیست")

    good = ["bullish", "weak_bullish"] if direction == "LONG" else ["bearish", "weak_bearish"]
    aligned = sum(1 for tf in ["4H", "1H", "30M"] if trends.get(tf) in good)
    if aligned < 2:
        reasons.append("هم‌جهتی تایم‌فریم‌ها کافی نیست")

    if direction == "LONG":
        if vwap_status == "below_vwap": reasons.append("لانگ زیر VWAP است")
        if sell_power > buy_power + 12: reasons.append("قدرت فروش برای لانگ زیاد است")
        if order_block == "bearish_order_block": reasons.append("اوردر بلاک مخالف لانگ است")
    if direction == "SHORT":
        if vwap_status == "above_vwap": reasons.append("شورت بالای VWAP است")
        if buy_power > sell_power + 12: reasons.append("قدرت خرید برای شورت زیاد است")
        if order_block == "bullish_order_block": reasons.append("اوردر بلاک مخالف شورت است")

    # FVG در Very Safe ردکننده نیست؛ فقط در امتیازدهی اثر خیلی کم دارد
    return len(reasons) == 0, reasons


def signal_validity(score, direction):
    if direction == "NO TRADE":
        return "سیگنال معتبر نیست"
    if score >= 90:
        return "30 دقیقه تا 3 ساعت"
    if score >= 80:
        return "15 تا 90 دقیقه"
    if score >= 70:
        return "10 تا 45 دقیقه"
    return "اعتبار پایین"


def signal_timeframe(score, direction):
    return "بدون تایم‌فریم ورود" if direction == "NO TRADE" else "5M تا 15M"



def make_no_trade_result(symbol, reason="داده کافی یا بازار قابل تحلیل نیست"):
    symbol = symbol.upper().strip()
    if not symbol.endswith("USDT"):
        symbol += "USDT"

    return {
        "symbol": symbol,
        "price": None,
        "direction": "NO TRADE",
        "raw_direction": "NO TRADE",
        "score": 0,
        "win_probability": 0,
        "entry_grade": "Reject",
        "risk_level": "بالا",
        "risk_reward": 0,
        "liquidity_risk": "بالا",
        "validity": "سیگنال معتبر نیست",
        "signal_timeframe": "بدون تایم‌فریم ورود",

        "long_score": 0,
        "short_score": 0,
        "buy_power": 50,
        "sell_power": 50,
        "rsi": None,
        "adx": None,
        "macd": None,
        "macd_hist": None,
        "vwap": None,
        "vwap_status": "unknown",
        "poc_price": None,
        "volume_profile_status": "unknown",
        "funding_rate": None,
        "open_interest": None,
        "spread_percent": None,
        "btc_filter": "unknown",

        "candle_pattern": "unknown",
        "multi_candle": "unknown",
        "liquidity_grab": "none",
        "stop_hunt": "none",
        "fvg": "none",
        "order_block": "none",
        "rsi_divergence": "none",
        "macd_divergence": "none",
        "fake_breakout": "none",
        "trend_exhaustion": "none",
        "support": None,
        "resistance": None,
        "trendline": "unknown",
        "market_structure": "unknown",
        "breakout": "unknown",

        "fear_value": None,
        "fear_text": "نامشخص",
        "btc_dominance": None,
        "dominance_status": "نامشخص",
        "altseason_status": "نامشخص",

        "stop_loss": None,
        "tp1": None,
        "tp2": None,
        "sl_percent": None,
        "tp1_percent": None,
        "tp2_percent": None,
        "candidate_stop_loss": None,
        "candidate_tp1": None,
        "candidate_tp2": None,
        "entry_zone_low": None,
        "entry_zone_high": None,
        "entry_trigger": "ورود پیشنهاد نمی‌شود",
        "very_safe": False,
        "very_safe_reasons": [reason],
        "trends": {},
        "reasons": [f"رد/عدم تحلیل: {reason}"],
    }



def analyze_symbol(symbol):
    symbol = symbol.upper().strip()
    if not symbol.endswith("USDT"):
        symbol += "USDT"

    if not symbol_supported(symbol):
        return make_no_trade_result(symbol, f"نماد {symbol} در OKX Swap قابل معامله نیست")

    try:
        df_1d = add_indicators(get_klines(symbol, "1d"))
        df_4h = add_indicators(get_klines(symbol, "4h"))
        df_1h = add_indicators(get_klines(symbol, "1h"))
        df_30m = add_indicators(get_klines(symbol, "30m"))
        df_15m = add_indicators(get_klines(symbol, "15m"))
        df_5m = add_indicators(get_klines(symbol, "5m"))
    except AnalysisDataError as e:
        return make_no_trade_result(symbol, str(e))
    except Exception as e:
        return make_no_trade_result(symbol, f"خطای دریافت یا آماده‌سازی داده: {e}")

    price = float(df_5m.iloc[-1]["close"])
    atr = float(df_15m.iloc[-1]["atr"])
    adx = float(df_5m.iloc[-1]["adx"])
    rsi = float(df_5m.iloc[-1]["rsi"])

    long_score = short_score = 0
    reasons_long, reasons_short = [], []

    l, s, rl, rs, trends = score_macro_trend(df_1d, df_4h, df_1h, df_30m)
    long_score += l; short_score += s; reasons_long += rl; reasons_short += rs

    l, s, rl, rs, buy_power, sell_power, pattern, multi = score_entry(df_15m, df_5m)
    long_score += l; short_score += s; reasons_long += rl; reasons_short += rs

    l, s, rl, rs, liquidity_grab, stop_hunt, fvg, order_block = score_smart_money(df_15m, df_5m)
    long_score += l; short_score += s; reasons_long += rl; reasons_short += rs

    l, s, rl, rs, rsi_div, macd_div = score_divergence(df_5m)
    long_score += l; short_score += s; reasons_long += rl; reasons_short += rs

    l, s, funding, open_interest, risk_notes = score_futures_data(symbol)
    long_score += l; short_score += s; reasons_long += risk_notes; reasons_short += risk_notes

    try:
        l, s, rl, rs, market = score_market_sentiment(symbol)
    except Exception:
        l, s, rl, rs = 0, 0, [], []
        market = {
            "fear_value": None,
            "fear_text": "نامشخص",
            "btc_dominance": None,
            "dominance_status": "نامشخص",
            "altseason_status": "نامشخص",
        }
    long_score += l; short_score += s; reasons_long += rl; reasons_short += rs

    l, s, rl, rs, vwap_status, poc_price, volume_profile_status = score_vwap_volume_profile(df_15m, df_5m)
    long_score += l; short_score += s; reasons_long += rl; reasons_short += rs

    btc_status, l, s, rl, rs = btc_filter(symbol)
    long_score += l; short_score += s; reasons_long += rl; reasons_short += rs

    try:
        trendline = detect_trendline(df_30m)
    except Exception:
        trendline = "unknown"
    try:
        breakout = detect_breakout(df_15m)
    except Exception:
        breakout = "unknown"
    try:
        structure = detect_market_structure(df_30m)
    except Exception:
        structure = "unknown"
    l, s = trendline_score(trendline)
    long_score += l; short_score += s
    l, s = breakout_score(breakout)
    long_score += l; short_score += s
    l, s = structure_score(structure)
    long_score += l; short_score += s

    long_score, short_score = cap_score(long_score), cap_score(short_score)
    raw_direction = "NO TRADE"
    if long_score >= short_score + 18:
        raw_direction = "LONG"
    elif short_score >= long_score + 18:
        raw_direction = "SHORT"

    long_score, short_score, rl, rs = apply_direction_conflict_penalties(
        long_score, short_score, raw_direction, buy_power, sell_power,
        vwap_status, trendline, structure, fvg
    )
    reasons_long += rl; reasons_short += rs

    score = max(long_score, short_score) if raw_direction != "NO TRADE" else max(long_score, short_score)
    spread = get_spread_percent(symbol)
    entry_ok, block_reasons, liquidity_risk, fake_breakout, trend_exhaustion = entry_filter(
        raw_direction, score, long_score, short_score, df_15m, df_5m, spread, order_block
    )

    support, resistance = support_resistance(df_15m)
    sl_raw, tp1_raw, tp2_raw = calculate_trade_levels(raw_direction, price, atr, support, resistance)
    rr_raw = risk_reward(raw_direction, price, sl_raw, tp1_raw)

    final_direction = raw_direction if entry_ok else "NO TRADE"
    stop_loss, tp1, tp2 = (sl_raw, tp1_raw, tp2_raw) if final_direction != "NO TRADE" else (None, None, None)
    rr = rr_raw if final_direction != "NO TRADE" else 0
    risk_level = calculate_risk_level(final_direction, score, liquidity_risk, funding, adx, spread, rr)
    grade = entry_grade(score, risk_level, rr, final_direction)
    win_prob = win_probability(score, risk_level, rr, adx, grade)

    # B و Reject ارسال نهایی نمی‌شوند
    if grade in ["B", "Reject"]:
        final_direction = "NO TRADE"
        stop_loss = tp1 = tp2 = None
        rr = 0

    very_safe, very_safe_reasons = very_safe_status(
        final_direction, score, win_prob, risk_level, rr, trends, vwap_status,
        buy_power, sell_power, adx, pattern, multi, order_block, fvg
    )

    reasons = reasons_long if raw_direction == "LONG" else reasons_short if raw_direction == "SHORT" else reasons_long + reasons_short
    if block_reasons:
        reasons += [f"رد/احتیاط: {r}" for r in block_reasons]

    entry_low = entry_high = None
    if final_direction == "LONG":
        entry_low = price - atr * 0.25
        entry_high = price + atr * 0.10
    elif final_direction == "SHORT":
        entry_low = price - atr * 0.10
        entry_high = price + atr * 0.25

    return {
        "symbol": symbol,
        "price": safe_round(price, 8),
        "direction": final_direction,
        "raw_direction": raw_direction,
        "score": int(score),
        "win_probability": int(win_prob),
        "entry_grade": grade,
        "risk_level": risk_level,
        "risk_reward": rr,
        "liquidity_risk": liquidity_risk,
        "validity": signal_validity(score, final_direction),
        "signal_timeframe": signal_timeframe(score, final_direction),

        "long_score": int(long_score),
        "short_score": int(short_score),
        "buy_power": buy_power,
        "sell_power": sell_power,
        "rsi": safe_round(rsi, 2),
        "adx": safe_round(adx, 2),
        "macd": safe_round(df_5m.iloc[-1]["macd"], 8),
        "macd_hist": safe_round(df_5m.iloc[-1]["macd_hist"], 8),
        "vwap": safe_round(df_5m.iloc[-1]["vwap"], 8),
        "vwap_status": vwap_status,
        "poc_price": safe_round(poc_price, 8),
        "volume_profile_status": volume_profile_status,
        "funding_rate": funding,
        "open_interest": open_interest,
        "spread_percent": spread,
        "btc_filter": btc_status,

        "candle_pattern": pattern,
        "multi_candle": multi,
        "liquidity_grab": liquidity_grab,
        "stop_hunt": stop_hunt,
        "fvg": fvg,
        "order_block": order_block,
        "rsi_divergence": rsi_div,
        "macd_divergence": macd_div,
        "fake_breakout": fake_breakout,
        "trend_exhaustion": trend_exhaustion,
        "support": safe_round(support, 8),
        "resistance": safe_round(resistance, 8),
        "trendline": trendline,
        "market_structure": structure,
        "breakout": breakout,

        "fear_value": market.get("fear_value"),
        "fear_text": market.get("fear_text"),
        "btc_dominance": market.get("btc_dominance"),
        "dominance_status": market.get("dominance_status"),
        "altseason_status": market.get("altseason_status"),

        "stop_loss": None if stop_loss is None else safe_round(stop_loss, 8),
        "tp1": None if tp1 is None else safe_round(tp1, 8),
        "tp2": None if tp2 is None else safe_round(tp2, 8),

        "sl_percent": calculate_level_percent(final_direction, price, stop_loss),
        "tp1_percent": calculate_level_percent(final_direction, price, tp1),
        "tp2_percent": calculate_level_percent(final_direction, price, tp2),

        "candidate_stop_loss": None if sl_raw is None else safe_round(sl_raw, 8),
        "candidate_tp1": None if tp1_raw is None else safe_round(tp1_raw, 8),
        "candidate_tp2": None if tp2_raw is None else safe_round(tp2_raw, 8),

        "entry_zone_low": safe_round(entry_low, 8),
        "entry_zone_high": safe_round(entry_high, 8),
        "entry_trigger": "ورود فقط بعد از حفظ ناحیه ورود و تایید کندل هم‌جهت",
        "very_safe": very_safe,
        "very_safe_reasons": very_safe_reasons,
        "trends": trends,
        "reasons": reasons[:30],
    }
