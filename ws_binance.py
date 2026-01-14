import asyncio
import json
import time
import websockets

SYMBOLS = ["btcusdt", "ethusdt", "xrpusdt", "bnbusdt"]

# === LIVE DATA CACHE ===
funding = {}
mark_price = {}
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
            f"{s}@markPrice@1s",        # funding + price
            f"{s}@openInterest@1s",     # OI
            f"{s}@aggTrade",            # trades
            f"{s}@forceOrder"           # liquidations
        ]

    url = f"wss://fstream.binance.com/stream?streams={'/'.join(streams)}"
    print("[WS] connecting")

    while True:
        try:
            async with websockets.connect(url, ping_interval=20) as ws:
                async for raw in ws:
                    msg = json.loads(raw)
                    data = msg.get("data", {})
                    stream = msg.get("stream", "")

                    symbol = data.get("s", "").upper()
                    if not symbol:
                        continue

                    # MARK PRICE + FUNDING
                    if "markPrice" in stream:
                        funding[symbol] = float(data["r"])
                        mark_price[symbol] = float(data["p"])
                        touch(symbol)

                    # OPEN INTEREST
                    elif "openInterest" in stream:
                        open_interest[symbol] = float(data["oi"])
                        touch(symbol)

                    # LIQUIDATIONS
                    elif "forceOrder" in stream:
                        side = data["o"]["S"]
                        qty = float(data["o"]["q"])
                        liquidations[symbol] = liquidations.get(symbol, 0) + qty
                        touch(symbol)

                    # AGG TRADES → для перекоса
                    elif "aggTrade" in stream:
                        is_buyer_maker = data["m"]
                        ls = long_short_ratio.get(symbol, {"long": 0, "short": 0})

                        if is_buyer_maker:
                            ls["short"] += 1
                        else:
                            ls["long"] += 1

                        long_short_ratio[symbol] = ls
                        touch(symbol)

        except Exception as e:
            print("[WS ERROR]", e)
            await asyncio.sleep(5)

