import asyncio
import json
import time
import websockets
from collections import deque
from config import SYMBOLS, WINDOW_SECONDS

funding = {}
open_interest = {}
long_short_ratio = {}
liquidations = {}
last_update = {}

# окна по времени
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

    while True:
        try:
            async with websockets.connect(url, ping_interval=20) as ws:
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
                        touch(symbol)

                    elif "openInterest" in stream:
                        oi = float(data["oi"])
                        open_interest[symbol] = oi
                        oi_window[symbol].append((now, oi))
                        cleanup_window(oi_window[symbol])
                        touch(symbol)

                    elif "aggTrade" in stream:
                        qty = float(data["q"])
                        is_maker = data["m"]
                        trades_window[symbol].append(
                            (now, qty, "short" if is_maker else "long")
                        )
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
                        liq_window[symbol].append((now, qty))
                        cleanup_window(liq_window[symbol])
                        liquidations[symbol] = sum(q for _, q in liq_window[symbol])
                        touch(symbol)

        except Exception:
            await asyncio.sleep(5)
