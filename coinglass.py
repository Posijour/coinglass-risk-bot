import requests
from config import COINGLASS_API_KEY

BASE_URL = "https://open-api-v4.coinglass.com/api"

HEADERS = {
    "accept": "application/json",
    "CG-API-KEY": COINGLASS_API_KEY
}

def _get(endpoint: str, params: dict = None, max_retries: int = 3):
    url = f"{BASE_URL}{endpoint}"
    for attempt in range(1, max_retries + 1):
        try:
            r = requests.get(url, headers=HEADERS, params=params, timeout=10)
            r.raise_for_status()
            data = r.json()
            if "data" not in data:
                raise ValueError(f"{endpoint} api returned no data")
            return data["data"]
        except Exception as e:
            if attempt == max_retries:
                raise
            else:
                continue

def get_funding_rate(symbol: str, exchange: str = "Binance", interval: str = "5m"):
    """
    Получаем последний funding rate для символа.
    Для Hobbyist тарифа доступно через OHLC-history, берём последний элемент.
    """
    endpoint = "/futures/funding-rate/history"
    params = {
        "symbol": symbol,
        "exchange": exchange,
        "interval": interval,
        "limit": 1
    }
    data = _get(endpoint, params)
    if not data:
        raise ValueError("No funding rate data returned")
    return float(data[-1]["fundingRate"])

def get_long_short_ratio(symbol: str, exchange: str = "Binance", interval: str = "5m"):
    """
    Получаем глобальное соотношение лонгов/шортов.
    """
    endpoint = "/futures/global-long-short-account-ratio/history"
    params = {
        "symbol": symbol,
        "exchange": exchange,
        "interval": interval,
        "limit": 1
    }
    data = _get(endpoint, params)
    if not data:
        raise ValueError("No long/short ratio data returned")
    return float(data[-1]["longRatio"])

def get_open_interest(symbol: str, exchange: str = "Binance", interval: str = "5m"):
    """
    Получаем open interest.
    """
    endpoint = "/futures/open-interest/history"
    params = {
        "symbol": symbol,
        "exchange": exchange,
        "interval": interval,
        "limit": 1
    }
    data = _get(endpoint, params)
    if not data:
        raise ValueError("No open interest data returned")
    return float(data[-1]["openInterest"])

def get_liquidations(symbol: str, exchange: str = "Binance", interval: str = "5m"):
    """
    Получаем ликвидации (long + short).
    """
    endpoint = "/futures/liquidation/aggregated-history"
    params = {
        "symbol": symbol,
        "exchange": exchange,
        "interval": interval,
        "limit": 1
    }
    data = _get(endpoint, params)
    if not data:
        raise ValueError("No liquidation data returned")
    latest = data[-1]
    return float(latest.get("longLiquidation", 0)) + float(latest.get("shortLiquidation", 0))

