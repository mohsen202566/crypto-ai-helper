import requests


def get_fear_greed():
    try:
        url = "https://api.alternative.me/fng/"
        r = requests.get(url, timeout=10)
        data = r.json()["data"][0]

        return {
            "value": int(data["value"]),
            "text": data["value_classification"]
        }
    except Exception:
        return {
            "value": None,
            "text": "نامشخص"
        }


def get_btc_dominance():
    try:
        url = "https://api.coingecko.com/api/v3/global"
        r = requests.get(url, timeout=10)
        data = r.json()["data"]

        dominance = data["market_cap_percentage"]["btc"]

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

    except Exception:
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
