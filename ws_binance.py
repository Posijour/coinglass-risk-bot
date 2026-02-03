import asyncio
import json
import random
import time
import websockets
from collections import deque
from config import SYMBOLS, WINDOW_SECONDS

funding = {}
mark_price = {}
long_short_ratio = {}
liquidations = {}
liq_sides = {}
last_update = {}

trades_window = {s: deque() for s in SYMBOLS}
liq_window = {s: deque() for s in SYMBOLS}
oi_window = {s: deque() for s in SYMBOLS}


def touch(symbol):
    last_update[symbol] = int(time.time())


def cleanup_window(dq):
    now = time.time()
    while dq and now - dq[0][0] > WINDOW_SECONDS:
        dq.popleft()


async def binance_ws():
    streams = []
    for s in SYMBOLS:
        s = s.lower()
        streams += [
            f"{s}@markPrice@1s",
            f"{s}@openInterest@1s",
            f"{s}@aggTrade",
            f"{s}@forceOrder"
        ]

    url = f"wss://fstream.binance.com/stream?streams={'/'.join(streams)}"
    backoff = 1
    max_backoff = 60
    while True:
        try:
            async with websockets.connect(url, ping_interval=20) as ws:
                backoff = 1
                async for raw in ws:
                    msg = json.loads(raw)
                    data = msg.get("data", {})
                    stream = msg.get("stream", "")

                    symbol = data.get("s", "").upper()
                    if symbol not in SYMBOLS:
                        continue

                    now = time.time()

                    if "markPrice" in stream:
                        funding[symbol] = float(data["r"])
                        mark_price[symbol] = float(data["p"])
                        touch(symbol)

                    elif "openInterest" in stream:
                        oi = float(data["oi"])
                        oi_window[symbol].append((now, oi))
                        cleanup_window(oi_window[symbol])
                        touch(symbol)

                    elif "aggTrade" in stream:
                        qty = float(data["q"])
                        side = "short" if data["m"] else "long"

                        trades_window[symbol].append((now, qty, side))
                        cleanup_window(trades_window[symbol])

                        long_vol = sum(q for _, q, d in trades_window[symbol] if d == "long")
                        short_vol = sum(q for _, q, d in trades_window[symbol] if d == "short")

                        long_short_ratio[symbol] = {
                            "long": long_vol,
                            "short": short_vol
                        }
                        touch(symbol)

                    elif "forceOrder" in stream:
                        qty = float(data["o"]["q"])
                        side = "long" if data["o"]["S"] == "SELL" else "short"

                        liq_window[symbol].append((now, qty, side))
                        cleanup_window(liq_window[symbol])

                        liq_sides[symbol] = {
                            "long": sum(q for _, q, s in liq_window[symbol] if s == "long"),
                            "short": sum(q for _, q, s in liq_window[symbol] if s == "short"),
                        }

                        liquidations[symbol] = (
                            liq_sides[symbol]["long"] + liq_sides[symbol]["short"]
                        )
                        touch(symbol)

        except Exception:
            jitter = random.uniform(0.3, 1.3)
            await asyncio.sleep(backoff * jitter)
            backoff = min(backoff * 2, max_backoff)
