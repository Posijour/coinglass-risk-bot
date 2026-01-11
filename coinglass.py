import time
import requests
from config import COINGLASS_API_KEY

BASE_URL = "https://open-api-v4.coinglass.com/api"

HEADERS = {
    "accept": "application/json",
    "CG-API-KEY": COINGLASS_API_KEY,
}

EXCHANGE = "BINANCE"
TIME_WINDOW_MINUTES = 60


class CoinGlassError(Exception):
    pass


def _time_range():
    end = int(time.time() * 1000)
    start = end - TIME_WINDOW_MINUTES * 60 * 1000
    return start, end


def _request(path: str, params: dict):
    r = requests.get(
        f"{BASE_URL}{path}",
        headers=HEADERS,
        params=params,
        timeout=15,
    )

    if r.status_code != 200:
        raise CoinGlassError(f"{path} error {r.status_code}: {r.text}")

    data = r.json().get("data")
    if not data:
        raise CoinGlassError(f"{path} api returned no data")

    return data


def get_funding_rate(symbol: str) -> float:
    start, end = _time_range()

    data = _request(
        "/futures/funding-rate/history",
        {
            "symbol": symbol,
            "exchange": EXCHANGE,
            "startTime": start,
            "endTime": end,
        },
    )

    return float(data[-1]["fundingRate"])


def get_long_short_ratio(symbol: str) -> float:
    start, end = _time_range()

    data = _request(
        "/futures/global-long-short-account-ratio/history",
        {
            "symbol": symbol,
            "startTime": start,
            "endTime": end,
        },
    )

    last = data[-1]
    long = float(last["longAccount"])
    short = float(last["shortAccount"])

    if short == 0:
        raise CoinGlassError("shortAccount is zero")

    return long / short


def get_open_interest(symbol: str) -> float:
    start, end = _time_range()

    data = _request(
        "/futures/open-interest/history",
        {
            "symbol": symbol,
            "exchange": EXCHANGE,
            "startTime": start,
            "endTime": end,
        },
    )

    return float(data[-1]["openInterest"])


def get_liquidations(symbol: str) -> float:
    start, end = _time_range()

    data = _request(
        "/futures/liquidation/aggregated-history",
        {
            "symbol": symbol,
            "exchange_list": EXCHANGE,
            "startTime": start,
            "endTime": end,
        },
    )

    last = data[-1]
    return float(last.get("longLiquidation", 0)) + float(
        last.get("shortLiquidation", 0)
    )
