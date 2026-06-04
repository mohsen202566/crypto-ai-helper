import requests


def safe_get_json(url, timeout=12):
    headers = {
        "accept": "application/json",
        "user-agent": "CryptoAIHelperBot/1.0"
    }

    r = requests.get(url, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.json()


def get_fear_greed():
    try:
        url = "https://api.alternative.me/fng/"
        data = safe_get_json(url)

        item = data["data"][0]

        return {
            "value": int(item["value"]),
            "text": item["value_classification"]
        }

    except Exception as e:
        print("FEAR GREED ERROR:", str(e))
        return {
            "value": None,
            "text": "نامشخص"
        }


def get_btc_dominance_from_coingecko():
    url = "https://api.coingecko.com/api/v3/global"
    data = safe_get_json(url)

    dominance = data.get("data", {}).get("market_cap_percentage", {}).get("btc")

    if dominance is None:
        raise Exception("BTC dominance not found in CoinGecko response")

    return float(dominance)


def get_btc_dominance_from_coinstats():
    url = "https://openapiv1.coinstats.app/coins/bitcoin"
    data = safe_get_json(url)

    dominance = data.get("marketCapDominance")

    if dominance is None:
        raise Exception("BTC dominance not found in CoinStats response")

    return float(dominance)


def get_btc_dominance():
    try:
        try:
            dominance = get_btc_dominance_from_coingecko()
        except Exception as e:
            print("COINGECKO DOMINANCE ERROR:", str(e))
            dominance = get_btc_dominance_from_coinstats()

        if dominance >= 55:
            status = "دامیننس بیتکوین بالا است"
            altseason = "ضعیف"
        elif dominance <= 45:
            status = "دامیننس بیتکوین پایین است"
            altseason = "قوی"
        else:
            status = "دامیننس بیتکوین خنثی است"
            altseason = "متوسط"

        return {
            "btc_dominance": round(dominance, 2),
            "dominance_status": status,
            "altseason_status": altseason
        }

    except Exception as e:
        print("BTC DOMINANCE ERROR:", str(e))
        return {
            "btc_dominance": None,
            "dominance_status": "نامشخص",
            "altseason_status": "نامشخص"
        }


def get_market_sentiment():
    fear = get_fear_greed()
    dominance = get_btc_dominance()

    return {
        "fear_value": fear["value"],
        "fear_text": fear["text"],
        "btc_dominance": dominance["btc_dominance"],
        "dominance_status": dominance["dominance_status"],
        "altseason_status": dominance["altseason_status"]
    }
