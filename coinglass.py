import time
import requests

from config import COINGLASS_API_KEY


BASE_URL = "https://open-api-v4.coinglass.com/api"
HEADERS = {
    "accept": "application/json",
    "CG-API-KEY": COINGLASS_API_KEY,
}

TIME_WINDOW_MINUTES = 5
DEFAULT_EXCHANGE_LIST = "BINANCE"


class CoinGlassError(Exception):
    pass


def _time_range():
    end_time = int(time.time() * 1000)
    start_time = end_time - TIME_WINDOW_MINUTES * 60 * 1000
    return start_time, end_time


def _request(path: str, params: dict):
    url = f"{BASE_URL}{path}"
    r = requests.get(url, headers=HEADERS, params=params, timeout=15)

    if r.status_code != 200:
        raise CoinGlassError(f"{path} error {r.status_code}: {r.text}")

    data = r.json()

    if not data or "data" not in data or not data["data"]:
        raise CoinGlassError(f"{path} api returned no data")

    return data["data"]


# -------------------------------------------------
# FUNDING RATE
# -------------------------------------------------
def get_funding_rate(symbol: str) -> float:
    start, end = _time_range()

    data = _request(
        "/futures/funding-rate/history",
        {
            "symbol": symbol,
            "startTime": start,
            "endTime": end,
        },
    )

    # Берём последнее значение
    return float(data[-1]["fundingRate"])


# -------------------------------------------------
# LONG / SHORT RATIO
# -------------------------------------------------
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

    item = data[-1]
    long = float(item["longAccount"])
    short = float(item["shortAccount"])

    if short == 0:
        raise CoinGlassError("shortAccount is zero")

    return long / short


# -------------------------------------------------
# OPEN INTEREST
# -------------------------------------------------
def get_open_interest(symbol: str) -> float:
    start, end = _time_range()

    data = _request(
        "/futures/open-interest/history",
        {
            "symbol": symbol,
            "startTime": start,
            "endTime": end,
        },
    )

    return float(data[-1]["openInterest"])


# -------------------------------------------------
# LIQUIDATIONS
# -------------------------------------------------
def get_liquidations(symbol: str) -> float:
    start, end = _time_range()

    data = _request(
        "/futures/liquidation/aggregated-history",
        {
            "symbol": symbol,
            "exchange_list": DEFAULT_EXCHANGE_LIST,
            "startTime": start,
            "endTime": end,
        },
    )

    last = data[-1]
    long_liq = float(last.get("longLiquidation", 0))
    short_liq = float(last.get("shortLiquidation", 0))

    return long_liq + short_liq

