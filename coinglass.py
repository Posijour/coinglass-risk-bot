import requests
from config import COINGLASS_API_KEY

BASE_URL = "https://open-api-v4.coinglass.com/api"
HEADERS = {
    "CG-API-KEY": COINGLASS_API_KEY,
    "accept": "application/json"
}

TIMEOUT = 10


class CoinGlassError(Exception):
    pass


def _get(path: str, params: dict):
    url = BASE_URL + path
    r = requests.get(url, headers=HEADERS, params=params, timeout=TIMEOUT)

    if r.status_code != 200:
        raise CoinGlassError(
            f"{path} error {r.status_code}: {r.text}"
        )

    data = r.json()
    if data.get("code") != "0":
        raise CoinGlassError(f"{path} api error: {data}")

    return data["data"]


# -----------------------------
# FUNDING RATE
# -----------------------------
def get_funding_rate(symbol: str) -> float:
    """
    Возвращает последний funding rate (средний)
    """
    data = _get(
        "/futures/fundingRate/history",
        {
            "symbol": symbol,
            "interval": "4h",
            "limit": 1
        }
    )

    if not data:
        raise CoinGlassError("funding_rate empty response")

    return float(data[-1]["fundingRate"])


# -----------------------------
# LONG / SHORT RATIO
# -----------------------------
def get_long_short_ratio(symbol: str) -> float:
    """
    Возвращает глобальный long ratio (0..1)
    """
    data = _get(
        "/futures/global-long-short-account-ratio/history",
        {
            "symbol": symbol,
            "interval": "4h",
            "limit": 1
        }
    )

    if not data:
        raise CoinGlassError("long_short_ratio empty response")

    long_account = float(data[-1]["longAccount"])
    short_account = float(data[-1]["shortAccount"])

    if long_account + short_account == 0:
        return 0.0

    return long_account / (long_account + short_account)


# -----------------------------
# OPEN INTEREST
# -----------------------------
def get_open_interest(symbol: str) -> float:
    """
    Возвращает последний open interest
    """
    data = _get(
        "/futures/openInterest/ohlc-history",
        {
            "symbol": symbol,
            "interval": "4h",
            "limit": 1
        }
    )

    if not data:
        raise CoinGlassError("open_interest empty response")

    return float(data[-1]["close"])


# -----------------------------
# LIQUIDATIONS
# -----------------------------
def get_liquidations(symbol: str) -> float:
    """
    Возвращает сумму ликвидаций за последний интервал
    """
    data = _get(
        "/futures/liquidation/aggregated-history",
        {
            "symbol": symbol,
            "interval": "4h",
            "limit": 1
        }
    )

    if not data:
        raise CoinGlassError("liquidations empty response")

    last = data[-1]
    return float(last.get("longLiquidation", 0)) + float(
        last.get("shortLiquidation", 0)
    )
