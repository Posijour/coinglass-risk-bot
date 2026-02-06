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

        latest = data[-1]
        # Binance возвращает строку
        oi_value = float(latest["sumOpenInterest"])
        ts_ms = latest.get("timestamp")
        ts = ts_ms / 1000 if ts_ms is not None else None
        return oi_value, ts

    def update(self):
        now = time.time()

        for symbol in self.symbols:
            try:
                # --- сброс протухшего окна ---
                last_ts = self.last_update_ts.get(symbol)
                if last_ts and now - last_ts > MAX_OI_AGE:
                    self.oi_window[symbol].clear()

                result = self.fetch_oi(symbol)
                if result is None:
                    continue

                oi, ts = result
                if ts is None:
                    ts = now

                if last_ts and ts <= last_ts:
                    continue

                self.oi_window[symbol].append((ts, oi))
                self.last_update_ts[symbol] = ts

            except Exception as e:
                print(f"OI ERROR {symbol}: {e}")
