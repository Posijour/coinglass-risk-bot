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


def _get(path: str, params: dict):
    url = f"{BASE_URL}{path}"

    r = requests.get(
        url,
        headers=HEADERS,
        params=params,
        timeout=15
    )

    if r.status_code != 200:
        raise CoinGlassError(
            f"{path} error {r.status_code}: {r.text}"
        )

    data = r.json()

    # CoinGlass любит возвращать code != 0 без HTTP ошибки
    if isinstance(data, dict) and data.get("code") not in (None, "0", 0):
        raise CoinGlassError(
            f"{path} api error: {data}"
        )

    return data.get("data")


# -------------------------
# FUNDING RATE
# -------------------------
def get_funding_rate(symbol: str) -> float:
    data = _get(
        "/api/pro/v1/futures/funding-rate",
        {
            "symbol": symbol,
            "exchange": EXCHANGE
        }
    )

    if not data:
        raise CoinGlassError("funding_rate: empty data")

    return float(data[0]["fundingRate"])


# -------------------------
# LONG / SHORT RATIO
# -------------------------
def get_long_short_ratio(symbol: str) -> float:
    data = _get(
        "/api/pro/v1/futures/global-long-short-account-ratio",
        {
            "symbol": symbol,
            "exchange": EXCHANGE
        }
    )

    if not data:
        raise CoinGlassError("long_short_ratio: empty data")

    return float(data[0]["longRatio"])


# -------------------------
# OPEN INTEREST
# -------------------------
def get_open_interest(symbol: str) -> float:
    data = _get(
        "/api/pro/v1/futures/open-interest",
        {
            "symbol": symbol,
            "exchange": EXCHANGE
        }
    )

    if not data:
        raise CoinGlassError("open_interest: empty data")

    return float(data[0]["openInterest"])


# -------------------------
# LIQUIDATIONS
# -------------------------
def get_liquidations(symbol: str) -> float:
    data = _get(
        "/api/pro/v1/futures/liquidation",
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
