import time
import requests

BINANCE_OI_URL = "https://fapi.binance.com/futures/data/openInterestHist"

class BinanceOIPoller:
    def __init__(self, symbols, period="5m", window=12):
        """
        period: 5m
        window: number of points to keep (12 * 5m = 1h)
        """
        self.symbols = symbols
        self.period = period
        self.window = window
        self.oi_window = {s: [] for s in symbols}

    def fetch_symbol(self, symbol):
        params = {
            "symbol": symbol,
            "period": self.period,
            "limit": 2,
        }
        r = requests.get(BINANCE_OI_URL, params=params, timeout=10)
        r.raise_for_status()
        return r.json()

    def update(self):
        now = int(time.time())
        for symbol in self.symbols:
            try:
                data = self.fetch_symbol(symbol)
                if not data:
                    continue

                last = data[-1]
                oi = float(last["sumOpenInterest"])

                self.oi_window[symbol].append((now, oi))
                self.oi_window[symbol] = self.oi_window[symbol][-self.window:]

            except Exception as e:
                print(f"OI ERROR {symbol}: {e}")
