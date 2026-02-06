import time
import requests

MAX_OI_AGE = 15 * 60  # 15 минут


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
        now = time.time()
    
        for symbol in self.symbols:
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
