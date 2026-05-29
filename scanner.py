import time
from analysis import analyze_symbol
from config import AUTO_SIGNAL_SCORE, AUTO_SIGNAL_COOLDOWN_MINUTES


SCAN_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
    "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT",
    "LTCUSDT", "BCHUSDT", "TRXUSDT", "SUIUSDT", "APTUSDT",
    "ARBUSDT", "OPUSDT", "UNIUSDT", "AAVEUSDT", "NEARUSDT",
    "PEPEUSDT", "SHIBUSDT", "INJUSDT", "ATOMUSDT", "TIAUSDT",
    "FILUSDT", "ICPUSDT", "HBARUSDT", "ETCUSDT", "STXUSDT"
]


last_alerts = {}


def get_best_signals(limit=5):
    results = []

    for symbol in SCAN_SYMBOLS:
        try:
            result = analyze_symbol(symbol)
            if result["direction"] != "NO TRADE":
                results.append(result)
        except Exception:
            continue

    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:limit]


def should_send_auto_signal(result):
    if result["score"] < AUTO_SIGNAL_SCORE:
        return False

    if result["direction"] == "NO TRADE":
        return False

    key = f"{result['symbol']}_{result['direction']}"
    now = time.time()

    cooldown_seconds = AUTO_SIGNAL_COOLDOWN_MINUTES * 60

    if key in last_alerts:
        if now - last_alerts[key] < cooldown_seconds:
            return False

    last_alerts[key] = now
    return True
