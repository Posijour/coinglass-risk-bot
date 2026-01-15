import asyncio
import json
import time
import websockets

SYMBOLS = ["BTCUSDT", "ETHUSDT", "XRPUSDT", "BNBUSDT"]

funding = {}
open_interest = {}
long_short_ratio = {}
liquidations = {}
last_update = {}


def touch(symbol):
    last_update[symbol] = int(time.time())


async def binance_ws():
    streams = []

    for s in SYMBOLS:
        streams += [
            f"{s}@markPrice@1s",
            f"{s}@aggTrade",
            f"{s}@forceOrder"
        ]

    url = f"wss://fstream.binance.com/stream?streams={'/'.join(streams)}"
    print("[WS] connecting")

    while True:
        try:
            async with websockets.connect(url, ping_interval=20) as ws:
                async for raw in ws:
                    msg = json.loads(raw)
                    if "data" in msg:
                        print(f"[WS] tick {msg.get('stream')}", flush=True)
                    data = msg.get("data", {})
                    stream = msg.get("stream", "")

                    symbol = data.get("s", "").upper()
                    if not symbol:
                        continue

                    if "markPrice" in stream:
                        funding[symbol] = float(data["r"])
                        touch(symbol)

                    elif "forceOrder" in stream:
                        qty = float(data["o"]["q"])
                        liquidations[symbol] = liquidations.get(symbol, 0) + qty
                        touch(symbol)

                    elif "aggTrade" in stream:
                        ls = long_short_ratio.get(symbol, {"long": 0, "short": 0})
                        if data["m"]:
                            ls["short"] += 1
                        else:
                            ls["long"] += 1
                        long_short_ratio[symbol] = ls
                        touch(symbol)

        except Exception as e:
            print("[WS ERROR]", e)
            await asyncio.sleep(5)


def start_ws():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(binance_ws())



