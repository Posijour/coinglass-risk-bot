import requests
from config import COINGLASS_API_KEY

BASE_URL = "https://open-api.coinglass.com/public/v2"

HEADERS = {
    "accept": "application/json",
    "coinglassSecret": COINGLASS_API_KEY
}


def _get(endpoint: str, params: dict):
    r = requests.get(
        f"{BASE_URL}/{endpoint}",
        headers=HEADERS,
        params=params,
        timeout=10
    )
    r.raise_for_status()
    return r.json()["data"]


def get_funding_rate(symbol: str) -> float:
    data = _get("funding_rate", {"symbol": symbol})
    return sum(x["fundingRate"] for x in data) / len(data)


def get_long_short_ratio(symbol: str) -> float:
    data = _get("global_long_short_account_ratio", {"symbol": symbol})
    latest = data[-1]
    return float(latest["longRatio"])


def get_open_interest(symbol: str) -> float:
    data = _get("open_interest", {"symbol": symbol})
    return sum(float(x["openInterest"]) for x in data)


def get_liquidations(symbol: str) -> float:
    data = _get(
        "liquidation_chart",
        {"symbol": symbol, "interval": "5m"}
    )
    latest = data[-1]
    return float(latest["longLiquidation"]) + float(latest["shortLiquidation"])
