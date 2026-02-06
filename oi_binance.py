import time
import requests
from collections import deque

BINANCE_OI_URL = "https://fapi.binance.com/futures/data/openInterestHist"

MAX_OI_AGE = 15 * 60  # 15 минут


class BinanceOIPoller:
    def __init__(self, symbols, period="5m", window=12):
        """
        period: 5m
        window: number of points to keep (12 * 5m = 1h)
        """
        self.symbols = symbols
        self.period = period
        self.window = window

        self.oi_window = {
            s: deque(maxlen=window) for s in symbols
        }

        self.last_update_ts = {}

    def fetch_oi(self, symbol):
        params = {
            "symbol": symbol,
            "period": self.period,
            "limit": 1,
        }

        r = requests.get(BINANCE_OI_URL, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()

        if not data:
            return None

        # Binance возвращает строку
        return float(data[-1]["sumOpenInterest"])

    def update(self):
        now = time.time()

        for symbol in self.symbols:
            try:
                # --- сброс протухшего окна ---
                last_ts = self.last_update_ts.get(symbol)
                if last_ts and now - last_ts > MAX_OI_AGE:
                    self.oi_window[symbol].clear()

                oi = self.fetch_oi(symbol)
                if oi is None:
                    continue

                self.oi_window[symbol].append((now, oi))
                self.last_update_ts[symbol] = now

            except Exception as e:
                print(f"OI ERROR {symbol}: {e}")
