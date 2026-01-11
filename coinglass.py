import requests
from config import COINGLASS_API_KEY

BASE_URL = "https://open-api-v4.coinglass.com/api"
EXCHANGE = "Binance"

HEADERS = {
    "accept": "application/json",
    "coinglassSecret": COINGLASS_API_KEY
}


class CoinGlassError(Exception):
    pass


def _get(endpoint: str, params: dict):
    url = f"{BASE_URL}{endpoint}"

    r = requests.get(
        url,
        headers=HEADERS,
        params=params,
        timeout=15
    )

    if r.status_code != 200:
        raise CoinGlassError(
            f"{endpoint} error {r.status_code}: {r.text}"
        )

    data = r.json()

    # v4 иногда возвращает code != 0 с HTTP 200
    if isinstance(data, dict) and data.get("code") not in (0, "0", None):
        raise CoinGlassError(
            f"{endpoint} api error: {data}"
        )

    return data.get("data")


# -------------------------------------------------
# FUNDING RATE (current)
# -------------------------------------------------
def get_funding_rate(symbol: str) -> float:
    data = _get(
        "/futures/funding-rate",
        {
            "symbol": symbol,
            "exchange": EXCHANGE
        }
    )

    if not data:
        raise CoinGlassError("funding_rate: empty data")

    # v4 возвращает список
    return float(data[0]["fundingRate"])


# -------------------------------------------------
# GLOBAL LONG / SHORT RATIO
# -------------------------------------------------
def get_long_short_ratio(symbol: str) -> float:
    data = _get(
        "/futures/global-long-short-account-ratio",
        {
            "symbol": symbol,
            "exchange": EXCHANGE
        }
    )

    if not data:
        raise CoinGlassError("long_short_ratio: empty data")

    return float(data[0]["longRatio"])


# -------------------------------------------------
# OPEN INTEREST (current)
# -------------------------------------------------
def get_open_interest(symbol: str) -> float:
    data = _get(
        "/futures/open-interest",
        {
            "symbol": symbol,
            "exchange": EXCHANGE
        }
    )

    if not data:
        raise CoinGlassError("open_interest: empty data")

    return float(data[0]["openInterest"])


# -------------------------------------------------
# LIQUIDATIONS (aggregated)
# -------------------------------------------------
def get_liquidations(symbol: str) -> float:
    data = _get(
        "/futures/liquidation",
        {
            "symbol": symbol,
            "exchange_list": EXCHANGE
        }
    )

    if not data:
        raise CoinGlassError("liquidations: empty data")

    latest = data[0]

    long_liq = float(latest.get("longLiquidation", 0))
    short_liq = float(latest.get("shortLiquidation", 0))

    return long_liq + short_liq
