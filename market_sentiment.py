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
