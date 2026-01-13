import requests

BASE = "https://fapi.binance.com"

class BinanceError(Exception):
    pass


def _get(path, params=None):
    r = requests.get(BASE + path, params=params, timeout=10)
    if r.status_code != 200:
        raise BinanceError(f"{path} error {r.status_code}")
    return r.json()


def get_funding_rate(symbol: str) -> float:
    data = _get("/fapi/v1/fundingRate", {"symbol": symbol, "limit": 1})
    if not data:
        raise BinanceError("empty funding data")
    return float(data[0]["fundingRate"])


def get_long_short_ratio(symbol: str) -> float:
    data = _get(
        "/futures/data/globalLongShortAccountRatio",
        {"symbol": symbol, "period": "5m", "limit": 1}
    )
    if not data:
        raise BinanceError("empty long/short ratio")
    return float(data[0]["longAccount"])


def get_open_interest(symbol: str) -> float:
    data = _get("/fapi/v1/openInterest", {"symbol": symbol})
    return float(data["openInterest"])


def get_liquidations(symbol: str) -> float:
    data = _get(
        "/futures/data/liqHist",
        {"symbol": symbol, "limit": 1}
    )
    if not data:
        raise BinanceError("empty liquidation data")
    row = data[0]
    return float(row["longVol"]) + float(row["shortVol"])
