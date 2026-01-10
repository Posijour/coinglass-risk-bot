import time
import requests
from config import COINGLASS_API_KEY

BASE_URL = "https://open-api.coinglass.com/public/v2"

HEADERS = {
    "accept": "application/json",
    "coinglassSecret": COINGLASS_API_KEY
}


def _get(endpoint: str, params: dict, retries=3, delay=2):
    """
    Универсальная функция запроса к Coinglass API с повторными попытками
    при 500 ошибках и паузой delay секунд.
    """
    for attempt in range(retries):
        try:
            r = requests.get(
                f"{BASE_URL}/{endpoint}",
                headers=HEADERS,
                params=params,
                timeout=10
            )
            r.raise_for_status()
            data = r.json().get("data")
            if data is None:
                # API вернул пустой объект
                return []
            return data
        except requests.HTTPError as e:
            # если ошибка сервера (5xx), повторяем
            if r.status_code >= 500:
                time.sleep(delay)
                continue
            else:
                raise
        except Exception:
            if attempt < retries - 1:
                time.sleep(delay)
            else:
                raise
    raise Exception(f"Failed to fetch {endpoint} after {retries} attempts")


def get_funding_rate(symbol: str) -> float:
    base = symbol.replace("USDT", "")

    data = _get(
        "funding_rate",
        {
            "symbol": base,
            "exchange": "Binance"
        }
    )
    return sum(float(x["fundingRate"]) for x in data) / len(data)


def get_long_short_ratio(symbol: str) -> float:
    base = symbol.replace("USDT", "")

    data = _get(
        "global_long_short_account_ratio",
        {
            "symbol": base,
            "exchange": "Binance"
        }
    )

    latest = data[-1]
    return float(latest["longRatio"])


def get_open_interest(symbol: str) -> float:
    data = _get("open_interest", {"symbol": symbol})
    if not data:
        return 0
    return sum(float(x.get("openInterest", 0)) for x in data)


def get_liquidations(symbol: str) -> float:
    data = _get("liquidation_chart", {"symbol": symbol, "interval": "5m"})
    if not data:
        return 0
    latest = data[-1]
    return float(latest.get("longLiquidation", 0)) + float(latest.get("shortLiquidation", 0))

