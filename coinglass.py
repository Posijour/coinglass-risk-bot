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
        timeout=15
    )

    if r.status_code != 200:
        raise RuntimeError(
            f"{endpoint} error {r.status_code}: {r.text}"
        )

    payload = r.json()

    if payload.get("success") is not True:
        raise RuntimeError(
            f"{endpoint} failed: {payload}"
        )

    return payload["data"]


def _base_symbol(symbol: str) -> str:
    # BTCUSDT -> BTC
    return symbol.replace("USDT", "")


# ---------- FUNDING RATE ----------
def get_funding_rate(symbol: str) -> float:
    base = _base_symbol(symbol)

    data = _get(
        "funding_rate",
        {
            "symbol": base,
            "exchange": "Binance"
        }
    )

    if not data:
        raise RuntimeError("empty funding_rate data")

    return sum(float(x["fundingRate"]) for x in data) / len(data)


# ---------- LONG / SHORT RATIO ----------
def get_long_short_ratio(symbol: str) -> float:
    base = _base_symbol(symbol)

    data = _get(
        "global_long_short_account_ratio",
        {
            "symbol": base,
            "exchange": "Binance"
        }
    )

    if not data:
        raise RuntimeError("empty long_short_ratio data")

    return float(data[-1]["longRatio"])


# ---------- OPEN INTEREST ----------
def get_open_interest(symbol: str) -> float:
    base = _base_symbol(symbol)

    data = _get(
        "open_interest",
        {
            "symbol": base,
            "exchange": "Binance"
        }
    )

    if not data:
        raise RuntimeError("empty open_interest data")

    return sum(float(x["openInterest"]) for x in data)


# ---------- LIQUIDATIONS ----------
def get_liquidations(symbol: str) -> float:
    base = _base_symbol(symbol)

    data = _get(
        "liquidation_chart",
        {
            "symbol": base,
            "exchange": "Binance",
            "interval": "5m"
        }
    )

    if not data:
        raise RuntimeError("empty liquidation data")

    last = data[-1]
    return float(last["longLiquidation"]) + float(last["shortLiquidation"])

