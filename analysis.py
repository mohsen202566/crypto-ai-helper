import os
import ccxt
import pandas as pd
import ta

from market_sentiment import get_market_sentiment
from trend_analysis import detect_trendline, detect_breakout, trendline_score, breakout_score
from market_structure import detect_market_structure, structure_score


exchange = ccxt.okx({
    "enableRateLimit": True,
    "options": {"defaultType": "swap"}
})


def to_okx_symbol(symbol):
    coin = symbol.replace("USDT", "")
    return f"{coin}/USDT:USDT"


def get_klines(symbol, interval="15m", limit=300):
    ohlcv = exchange.fetch_ohlcv(to_okx_symbol(symbol), timeframe=interval, limit=limit)

    if not ohlcv or len(ohlcv) < 240:
        raise Exception("داده کافی از OKX دریافت نشد")

    df = pd.DataFrame(ohlcv, columns=["time", "open", "high", "low", "close", "volume"])

    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna()

    # حذف کندل باز
    df = df.iloc[:-1]

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

    df["atr"] = ta.volatility.average_true_range(
        df["high"], df["low"], df["close"], window=14
    )

    adx = ta.trend.ADXIndicator(
        high=df["high"],
        low=df["low"],
        close=df["close"],
        window=14
    )
    df["adx"] = adx.adx()

    df["volume_ma20"] = df["volume"].rolling(20).mean()
    df["atr_ma50"] = df["atr"].rolling(50).mean()

    df = df.dropna()

    if len(df) < 80:
        raise Exception("اندیکاتورها کامل محاسبه نشدند")

    return df


def get_funding_rate(symbol):
    try:
        data = exchange.fetch_funding_rate(to_okx_symbol(symbol))
        rate = data.get("fundingRate")
        if rate is None:
            return None
        return round(float(rate) * 100, 5)
    except Exception:
        return None


def get_open_interest(symbol):
    try:
        data = exchange.fetch_open_interest(to_okx_symbol(symbol))
        value = data.get("openInterestAmount") or data.get("openInterestValue")
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def get_spread_percent(symbol):
    try:
        orderbook = exchange.fetch_order_book(to_okx_symbol(symbol), limit=5)
        bid = orderbook["bids"][0][0] if orderbook["bids"] else None
        ask = orderbook["asks"][0][0] if orderbook["asks"] else None

        if not bid or not ask:
            return None

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

    green_volume = recent[recent["close"] > recent["open"]]["volume"].sum()
    red_volume = recent[recent["close"] < recent["open"]]["volume"].sum()
    total = green_volume + red_volume

    if total == 0:
        return 50, 50

    buy_power = round((green_volume / total) * 100, 1)
    sell_power = round((red_volume / total) * 100, 1)

    return buy_power, sell_power


def support_resistance(df):
    recent = df.tail(80)
    support = recent["low"].min()
    resistance = recent["high"].max()
    return support, resistance


def is_near_resistance(price, resistance, atr):
    return (resistance - price) <= atr * 0.9


def is_near_support(price, support, atr):
    return (price - support) <= atr * 0.9


def is_middle_of_range(price, support, resistance):
    if resistance <= support:
        return False

    position = (price - support) / (resistance - support)

    return 0.38 <= position <= 0.62


def candle_pattern(df):
    last = df.iloc[-1]
    prev = df.iloc[-2]

    body = abs(last["close"] - last["open"])
    candle_range = last["high"] - last["low"]

    if candle_range == 0:
        return "weak"

    upper_wick = last["high"] - max(last["close"], last["open"])
    lower_wick = min(last["close"], last["open"]) - last["low"]

    if last["close"] > last["open"] and prev["close"] < prev["open"]:
        if last["close"] > prev["open"] and last["open"] < prev["close"]:
            return "bullish_engulfing"

    if last["close"] < last["open"] and prev["close"] > prev["open"]:
        if last["close"] < prev["open"] and last["open"] > prev["close"]:
            return "bearish_engulfing"

    if lower_wick > body * 2.2 and upper_wick < body * 1.2:
        return "bullish_pinbar"

    if upper_wick > body * 2.2 and lower_wick < body * 1.2:
        return "bearish_pinbar"

    if body / candle_range >= 0.6:
        if last["close"] > last["open"]:
            return "bullish_strong"
        return "bearish_strong"

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

    if last["volume_ma20"] == 0:
        return False

    return last["volume"] > last["volume_ma20"] * 1.5


def atr_compression(df):
    last = df.iloc[-1]

    if last["atr_ma50"] == 0:
        return False

    return last["atr"] < last["atr_ma50"] * 0.65


def minimum_volatility_ok(df):
    last = df.iloc[-1]

    if last["close"] == 0:
        return False

    atr_percent = (last["atr"] / last["close"]) * 100

    return atr_percent >= 0.08


def market_is_choppy(df_15m, df_5m):
    last_15 = df_15m.iloc[-1]
    last_5 = df_5m.iloc[-1]

    ema_gap_15 = abs(last_15["ema20"] - last_15["ema50"]) / last_15["close"] * 100
    ema_gap_5 = abs(last_5["ema20"] - last_5["ema50"]) / last_5["close"] * 100

    if ema_gap_15 < 0.08 and ema_gap_5 < 0.08:
        return True

    if last_15["adx"] < 18 and last_5["adx"] < 18:
        return True

    if atr_compression(df_15m) and atr_compression(df_5m):
        return True

    return False


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

    candle_range = last["high"] - last["low"]
    if candle_range == 0:
        return "none"

    upper_wick = last["high"] - max(last["open"], last["close"])
    lower_wick = min(last["open"], last["close"]) - last["low"]

    if last["high"] > prev_high and upper_wick / candle_range > 0.45:
        return "bearish_stop_hunt"

    if last["low"] < prev_low and lower_wick / candle_range > 0.45:
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
        next_candle = recent.iloc[i + 1]

        if candle["close"] < candle["open"] and next_candle["close"] > next_candle["open"]:
            if next_candle["close"] > candle["high"]:
                return "bullish_order_block"

        if candle["close"] > candle["open"] and next_candle["close"] < next_candle["open"]:
            if next_candle["close"] < candle["low"]:
                return "bearish_order_block"

    return "none"


def detect_rsi_divergence(df):
    recent = df.tail(35)

    lows = recent.nsmallest(2, "low").sort_index()
    highs = recent.nlargest(2, "high").sort_index()

    if len(lows) == 2:
        first = lows.iloc[0]
        second = lows.iloc[1]

        if second["low"] < first["low"] and second["rsi"] > first["rsi"]:
            return "bullish_rsi_divergence"

    if len(highs) == 2:
        first = highs.iloc[0]
        second = highs.iloc[1]

        if second["high"] > first["high"] and second["rsi"] < first["rsi"]:
            return "bearish_rsi_divergence"

    return "none"


def detect_macd_divergence(df):
    recent = df.tail(35)

    lows = recent.nsmallest(2, "low").sort_index()
    highs = recent.nlargest(2, "high").sort_index()

    if len(lows) == 2:
        first = lows.iloc[0]
        second = lows.iloc[1]

        if second["low"] < first["low"] and second["macd_hist"] > first["macd_hist"]:
            return "bullish_macd_divergence"

    if len(highs) == 2:
        first = highs.iloc[0]
        second = highs.iloc[1]

        if second["high"] > first["high"] and second["macd_hist"] < first["macd_hist"]:
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


def btc_filter(symbol):
    if symbol == "BTCUSDT":
        return "neutral", 0, 0, [], []

    try:
        btc_15m = add_indicators(get_klines("BTCUSDT", "15m"))
        btc_5m = add_indicators(get_klines("BTCUSDT", "5m"))

        btc_15_trend = trend_direction(btc_15m)
        btc_5_trend = trend_direction(btc_5m)

        long_score = 0
        short_score = 0
        reasons_long = []
        reasons_short = []

        if btc_15_trend in ["bullish", "weak_bullish"] and btc_5_trend in ["bullish", "weak_bullish"]:
            long_score += 8
            reasons_long.append("BTC در تایم ورود صعودی است")

        if btc_15_trend in ["bearish", "weak_bearish"] and btc_5_trend in ["bearish", "weak_bearish"]:
            short_score += 8
            reasons_short.append("BTC در تایم ورود نزولی است")

        return "ok", long_score, short_score, reasons_long, reasons_short

    except Exception:
        return "unknown", 0, 0, [], []


def signal_validity(score, direction):
    if direction == "NO TRADE":
        return "سیگنال معتبر نیست"

    if score >= 90:
        return "30 دقیقه تا 3 ساعت"
    elif score >= 80:
        return "15 تا 90 دقیقه"
    elif score >= 70:
        return "10 تا 45 دقیقه"

    return "اعتبار پایین"


def signal_timeframe(score, direction):
    if direction == "NO TRADE":
        return "بدون تایم‌فریم ورود"

    return "5M تا 15M"


def score_macro_trend(df_1d, df_4h, df_1h, df_30m):
    long_score = 0
    short_score = 0
    reasons_long = []
    reasons_short = []

    trends = {
        "1D": trend_direction(df_1d),
        "4H": trend_direction(df_4h),
        "1H": trend_direction(df_1h),
        "30M": trend_direction(df_30m),
    }

    weights = {
        "1D": 8,
        "4H": 12,
        "1H": 14,
        "30M": 14,
    }

    for tf, trend in trends.items():
        weight = weights[tf]

        if trend == "bullish":
            long_score += weight
            reasons_long.append(f"{tf}: روند صعودی")
        elif trend == "weak_bullish":
            long_score += int(weight * 0.5)
            reasons_long.append(f"{tf}: تمایل صعودی")
        elif trend == "bearish":
            short_score += weight
            reasons_short.append(f"{tf}: روند نزولی")
        elif trend == "weak_bearish":
            short_score += int(weight * 0.5)
            reasons_short.append(f"{tf}: تمایل نزولی")

    return long_score, short_score, reasons_long, reasons_short, trends


def score_entry(df_15m, df_5m):
    long_score = 0
    short_score = 0
    reasons_long = []
    reasons_short = []

    last_15 = df_15m.iloc[-1]
    last_5 = df_5m.iloc[-1]

    buy_power, sell_power = buy_sell_power(df_5m)

    if last_15["close"] > last_15["ema20"] > last_15["ema50"]:
        long_score += 15
        reasons_long.append("15M: قیمت بالای EMA20 و EMA50")

    if last_15["close"] < last_15["ema20"] < last_15["ema50"]:
        short_score += 15
        reasons_short.append("15M: قیمت زیر EMA20 و EMA50")

    if last_5["close"] > last_5["ema20"] and last_5["macd"] > last_5["macd_signal"]:
        long_score += 15
        reasons_long.append("5M: تایید ورود لانگ با EMA و MACD")

    if last_5["close"] < last_5["ema20"] and last_5["macd"] < last_5["macd_signal"]:
        short_score += 15
        reasons_short.append("5M: تایید ورود شورت با EMA و MACD")

    if 45 <= last_5["rsi"] <= 68:
        long_score += 10
        reasons_long.append("RSI مناسب برای لانگ در 5M")

    if 32 <= last_5["rsi"] <= 55:
        short_score += 10
        reasons_short.append("RSI مناسب برای شورت در 5M")

    if buy_power >= 62:
        long_score += 10
        reasons_long.append("قدرت خرید بالا در تایم ورود")

    if sell_power >= 62:
        short_score += 10
        reasons_short.append("قدرت فروش بالا در تایم ورود")

    pattern = candle_pattern(df_5m)
    multi = multi_candle_confirmation(df_5m)

    if pattern in ["bullish_engulfing", "bullish_pinbar", "bullish_strong"]:
        long_score += 10
        reasons_long.append(f"کندل تاییدی لانگ: {pattern}")

    if pattern in ["bearish_engulfing", "bearish_pinbar", "bearish_strong"]:
        short_score += 10
        reasons_short.append(f"کندل تاییدی شورت: {pattern}")

    if multi == "bullish":
        long_score += 8
        reasons_long.append("تایید چند کندلی صعودی")

    if multi == "bearish":
        short_score += 8
        reasons_short.append("تایید چند کندلی نزولی")

    if volume_spike(df_5m):
        long_score += 6
        short_score += 6

    if last_5["adx"] >= 22:
        long_score += 5
        short_score += 5

    return long_score, short_score, reasons_long, reasons_short, buy_power, sell_power, pattern, multi


def score_smart_money(df_15m, df_5m):
    long_score = 0
    short_score = 0
    reasons_long = []
    reasons_short = []

    liquidity = detect_liquidity_grab(df_5m)
    stop_hunt = detect_stop_hunt(df_5m)
    fvg = detect_fvg(df_5m)
    order_block = detect_order_block(df_15m)

    if liquidity == "bullish_liquidity_grab":
        long_score += 12
        reasons_long.append("Liquidity Grab صعودی")

    if liquidity == "bearish_liquidity_grab":
        short_score += 12
        reasons_short.append("Liquidity Grab نزولی")

    if stop_hunt == "bullish_stop_hunt":
        long_score += 10
        reasons_long.append("Stop Hunt صعودی")

    if stop_hunt == "bearish_stop_hunt":
        short_score += 10
        reasons_short.append("Stop Hunt نزولی")

    if fvg == "bullish_fvg":
        long_score += 6
        reasons_long.append("FVG صعودی")

    if fvg == "bearish_fvg":
        short_score += 6
        reasons_short.append("FVG نزولی")

    if order_block == "bullish_order_block":
        long_score += 7
        reasons_long.append("Order Block صعودی")

    if order_block == "bearish_order_block":
        short_score += 7
        reasons_short.append("Order Block نزولی")

    return long_score, short_score, reasons_long, reasons_short, liquidity, stop_hunt, fvg, order_block


def score_divergence(df_5m):
    long_score = 0
    short_score = 0
    reasons_long = []
    reasons_short = []

    rsi_div = detect_rsi_divergence(df_5m)
    macd_div = detect_macd_divergence(df_5m)

    if rsi_div == "bullish_rsi_divergence":
        long_score += 10
        reasons_long.append("واگرایی مثبت RSI")

    if rsi_div == "bearish_rsi_divergence":
        short_score += 10
        reasons_short.append("واگرایی منفی RSI")

    if macd_div == "bullish_macd_divergence":
        long_score += 10
        reasons_long.append("واگرایی مثبت MACD")

    if macd_div == "bearish_macd_divergence":
        short_score += 10
        reasons_short.append("واگرایی منفی MACD")

    return long_score, short_score, reasons_long, reasons_short, rsi_div, macd_div


def score_futures_data(symbol):
    funding_rate = get_funding_rate(symbol)
    open_interest = get_open_interest(symbol)

    long_score = 0
    short_score = 0
    risk_notes = []

    if funding_rate is not None:
        if funding_rate > 0.05:
            short_score += 4
            risk_notes.append("Funding مثبت و نسبتاً بالا")
        elif funding_rate < -0.05:
            long_score += 4
            risk_notes.append("Funding منفی و نسبتاً بالا")

    if open_interest is not None and open_interest > 0:
        long_score += 2
        short_score += 2

    return long_score, short_score, funding_rate, open_interest, risk_notes


def score_market_sentiment(symbol):
    market = get_market_sentiment()

    long_score = 0
    short_score = 0
    reasons_long = []
    reasons_short = []

    fear_value = market["fear_value"]
    altseason = market["altseason_status"]

    if fear_value is not None:
        if fear_value <= 25:
            long_score += 3
            reasons_long.append("Fear & Greed در ترس شدید")
        elif fear_value >= 80:
            short_score += 3
            reasons_short.append("Fear & Greed در طمع شدید")

    if symbol != "BTCUSDT":
        if altseason == "قوی":
            long_score += 3
            reasons_long.append("آلت‌سیزن برای آلت‌کوین‌ها مناسب است")
        elif altseason == "ضعیف":
            short_score += 3
            reasons_short.append("آلت‌سیزن ضعیف است")

    return long_score, short_score, reasons_long, reasons_short, market


def calculate_trade_levels(direction, price, atr):
    if direction == "LONG":
        stop_loss = price - (atr * 1.2)
        tp1 = price + (atr * 1.2)
        tp2 = price + (atr * 2.2)
    elif direction == "SHORT":
        stop_loss = price + (atr * 1.2)
        tp1 = price - (atr * 1.2)
        tp2 = price - (atr * 2.2)
    else:
        return None, None, None

    return stop_loss, tp1, tp2


def risk_reward(direction, price, stop_loss, tp1):
    if direction == "NO TRADE" or stop_loss is None or tp1 is None:
        return 0

    risk = abs(price - stop_loss)
    reward = abs(tp1 - price)

    if risk == 0:
        return 0

    return round(reward / risk, 2)


def entry_grade(score, risk_level, rr, direction):
    if direction == "NO TRADE":
        return "Reject"

    if score >= 90 and risk_level == "پایین" and rr >= 1:
        return "A+"

    if score >= 82 and risk_level in ["پایین", "متوسط"]:
        return "A"

    if score >= 75:
        return "B"

    return "Reject"


def calculate_risk_level(score, direction, liquidity_risk, funding_rate, adx, spread_percent, rr):
    if direction == "NO TRADE":
        return "بالا"

    risk = 0

    if score < 75:
        risk += 2

    if adx < 20:
        risk += 2

    if liquidity_risk == "بالا":
        risk += 2

    if funding_rate is not None and abs(funding_rate) > 0.07:
        risk += 1

    if spread_percent is not None and spread_percent > 0.08:
        risk += 2

    if rr < 0.9:
        risk += 2

    if risk >= 4:
        return "بالا"

    if risk >= 2:
        return "متوسط"

    return "پایین"


def news_filter_active():
    return os.getenv("HIGH_IMPACT_NEWS", "0") == "1"


def entry_filter(direction, score, long_score, short_score, df_15m, df_5m, spread_percent):
    last_5 = df_5m.iloc[-1]
    price = float(last_5["close"])
    atr = float(last_5["atr"])
    support, resistance = support_resistance(df_15m)

    reasons_block = []
    liquidity_risk = "پایین"

    if news_filter_active():
        reasons_block.append("فیلتر خبر فعال است")
        liquidity_risk = "بالا"

    if market_is_choppy(df_15m, df_5m):
        reasons_block.append("بازار رنج، فشرده یا کم‌قدرت است")
        liquidity_risk = "بالا"

    if not minimum_volatility_ok(df_5m):
        reasons_block.append("نوسان برای اسکالپ کافی نیست")
        liquidity_risk = "بالا"

    if spread_percent is not None and spread_percent > 0.08:
        reasons_block.append("اسپرد برای اسکالپ زیاد است")
        liquidity_risk = "بالا"

    if is_middle_of_range(price, support, resistance):
        reasons_block.append("قیمت وسط رنج است")
        liquidity_risk = "بالا"

    fake = detect_fake_breakout(df_5m)
    exhaustion = detect_trend_exhaustion(df_5m)

    if direction == "LONG":
        if long_score < short_score + 18:
            reasons_block.append("اختلاف امتیاز لانگ و شورت کافی نیست")

        if is_near_resistance(price, resistance, atr):
            reasons_block.append("قیمت نزدیک مقاومت است")
            liquidity_risk = "بالا"

        if last_5["rsi"] > 72:
            reasons_block.append("RSI برای لانگ بیش از حد بالاست")

        if last_5["adx"] < 18:
            reasons_block.append("قدرت روند برای لانگ کافی نیست")

        if fake == "fake_bullish_breakout":
            reasons_block.append("احتمال فیک بریک‌اوت صعودی")

        if exhaustion == "bullish_exhaustion":
            reasons_block.append("خستگی روند صعودی")

    if direction == "SHORT":
        if short_score < long_score + 18:
            reasons_block.append("اختلاف امتیاز شورت و لانگ کافی نیست")

        if is_near_support(price, support, atr):
            reasons_block.append("قیمت نزدیک حمایت است")
            liquidity_risk = "بالا"

        if last_5["rsi"] < 28:
            reasons_block.append("RSI برای شورت بیش از حد پایین است")

        if last_5["adx"] < 18:
            reasons_block.append("قدرت روند برای شورت کافی نیست")

        if fake == "fake_bearish_breakout":
            reasons_block.append("احتمال فیک بریک‌اوت نزولی")

        if exhaustion == "bearish_exhaustion":
            reasons_block.append("خستگی روند نزولی")

    if score < 72:
        reasons_block.append("امتیاز سیگنال برای ورود کافی نیست")

    if reasons_block:
        return False, reasons_block, liquidity_risk, fake, exhaustion

    return True, [], liquidity_risk, fake, exhaustion


def analyze_symbol(symbol):
    df_1d = add_indicators(get_klines(symbol, "1d"))
    df_4h = add_indicators(get_klines(symbol, "4h"))
    df_1h = add_indicators(get_klines(symbol, "1h"))
    df_30m = add_indicators(get_klines(symbol, "30m"))
    df_15m = add_indicators(get_klines(symbol, "15m"))
    df_5m = add_indicators(get_klines(symbol, "5m"))

    long_score = 0
    short_score = 0
    reasons_long = []
    reasons_short = []

    l, s, rl, rs, trends = score_macro_trend(df_1d, df_4h, df_1h, df_30m)
    long_score += l
    short_score += s
    reasons_long += rl
    reasons_short += rs

    l, s, rl, rs, buy_power, sell_power, pattern, multi_candle = score_entry(df_15m, df_5m)
    long_score += l
    short_score += s
    reasons_long += rl
    reasons_short += rs

    l, s, rl, rs, liquidity_grab, stop_hunt, fvg, order_block = score_smart_money(df_15m, df_5m)
    long_score += l
    short_score += s
    reasons_long += rl
    reasons_short += rs

    l, s, rl, rs, rsi_divergence, macd_divergence = score_divergence(df_5m)
    long_score += l
    short_score += s
    reasons_long += rl
    reasons_short += rs

    btc_status, l, s, rl, rs = btc_filter(symbol)
    long_score += l
    short_score += s
    reasons_long += rl
    reasons_short += rs

    trendline = detect_trendline(df_15m)
    l, s = trendline_score(trendline)
    long_score += l
    short_score += s

    if trendline == "uptrend":
        reasons_long.append("خط روند 15M صعودی است")
    elif trendline == "downtrend":
        reasons_short.append("خط روند 15M نزولی است")

    breakout = detect_breakout(df_5m)
    l, s = breakout_score(breakout)
    long_score += l
    short_score += s

    if breakout == "bullish_breakout":
        reasons_long.append("بریک‌اوت صعودی در 5M")
    elif breakout == "bearish_breakout":
        reasons_short.append("بریک‌اوت نزولی در 5M")
    elif breakout == "fake_bullish_breakout":
        reasons_short.append("احتمال فیک بریک‌اوت صعودی")
    elif breakout == "fake_bearish_breakout":
        reasons_long.append("احتمال فیک بریک‌اوت نزولی")

    structure = detect_market_structure(df_15m)
    l, s = structure_score(structure)
    long_score += l
    short_score += s

    if structure == "bullish_structure":
        reasons_long.append("ساختار بازار 15M صعودی است")
    elif structure == "bearish_structure":
        reasons_short.append("ساختار بازار 15M نزولی است")

    l, s, rl, rs, market = score_market_sentiment(symbol)
    long_score += l
    short_score += s
    reasons_long += rl
    reasons_short += rs

    l, s, funding_rate, open_interest, risk_notes = score_futures_data(symbol)
    long_score += l
    short_score += s

    last = df_5m.iloc[-1]
    price = float(last["close"])
    atr = float(last["atr"])

    spread_percent = get_spread_percent(symbol)

    raw_direction = "NO TRADE"

    if long_score >= short_score + 18:
        raw_direction = "LONG"
        score = min(long_score, 100)
        reasons = reasons_long + risk_notes
    elif short_score >= long_score + 18:
        raw_direction = "SHORT"
        score = min(short_score, 100)
        reasons = reasons_short + risk_notes
    else:
        score = max(long_score, short_score)
        reasons = ["اختلاف لانگ و شورت کافی نیست"]

    entry_ok, block_reasons, liquidity_risk, fake_breakout, trend_exhaustion = entry_filter(
        raw_direction,
        score,
        long_score,
        short_score,
        df_15m,
        df_5m,
        spread_percent
    )

    if raw_direction == "NO TRADE" or not entry_ok:
        direction = "NO TRADE"
        reasons = reasons + block_reasons
    else:
        direction = raw_direction

    stop_loss, tp1, tp2 = calculate_trade_levels(direction, price, atr)
    rr = risk_reward(direction, price, stop_loss, tp1)

    risk_level = calculate_risk_level(
        score=score,
        direction=direction,
        liquidity_risk=liquidity_risk,
        funding_rate=funding_rate,
        adx=float(last["adx"]),
        spread_percent=spread_percent,
        rr=rr
    )

    grade = entry_grade(score, risk_level, rr, direction)

    if grade == "Reject":
        direction = "NO TRADE"

    support, resistance = support_resistance(df_15m)

    return {
        "symbol": symbol,
        "price": round(price, 8),
        "direction": direction,
        "score": min(score, 100),

        "entry_grade": grade,
        "risk_level": risk_level,
        "risk_reward": rr,
        "validity": signal_validity(score, direction),
        "signal_timeframe": signal_timeframe(score, direction),

        "rsi": round(float(last["rsi"]), 2),
        "macd": round(float(last["macd"]), 6),
        "macd_signal": round(float(last["macd_signal"]), 6),
        "macd_hist": round(float(last["macd_hist"]), 6),
        "ema20": round(float(last["ema20"]), 8),
        "ema50": round(float(last["ema50"]), 8),
        "ema200": round(float(last["ema200"]), 8),
        "atr": round(atr, 8),
        "adx": round(float(last["adx"]), 2),

        "stop_loss": None if stop_loss is None else round(stop_loss, 8),
        "tp1": None if tp1 is None else round(tp1, 8),
        "tp2": None if tp2 is None else round(tp2, 8),

        "support": round(float(support), 8),
        "resistance": round(float(resistance), 8),

        "buy_power": buy_power,
        "sell_power": sell_power,

        "trendline": trendline,
        "breakout": breakout,
        "market_structure": structure,
        "trends": trends,
        "btc_filter": btc_status,

        "candle_pattern": pattern,
        "multi_candle": multi_candle,
        "liquidity_grab": liquidity_grab,
        "stop_hunt": stop_hunt,
        "fvg": fvg,
        "order_block": order_block,
        "rsi_divergence": rsi_divergence,
        "macd_divergence": macd_divergence,
        "fake_breakout": fake_breakout,
        "trend_exhaustion": trend_exhaustion,

        "funding_rate": funding_rate,
        "open_interest": open_interest,
        "spread_percent": spread_percent,
        "liquidity_risk": liquidity_risk,

        "fear_value": market["fear_value"],
        "fear_text": market["fear_text"],
        "btc_dominance": market["btc_dominance"],
        "dominance_status": market["dominance_status"],
        "altseason_status": market["altseason_status"],

        "long_score": long_score,
        "short_score": short_score,
        "reasons": reasons[:16],
    }
