import asyncio
import time
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from aiogram import Bot, Dispatcher, types
from aiogram.utils import executor

import ws_binance as ws
import risk
from config import *

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

active_chats = set()
cache = {}
last_spikes = {"funding": {}, "oi": {}}
prev_scores = {}

last_funding = {}
prev_funding = {}
last_funding_ts = {}
last_oi_ts = {}
last_liq_ts = {}

ws_task = None
ws_running = False


# ---------------- WS SAFE START ----------------

async def start_ws_safe():
    global ws_running
    if ws_running:
        return
    ws_running = True
    try:
        await ws.binance_ws()
    finally:
        ws_running = False


# ---------------- WS WATCHDOG ----------------

async def ws_watchdog():
    global ws_task
    while True:
        await asyncio.sleep(60)

        if not ws.last_update:
            continue

        freshest = max(ws.last_update.values())
        if time.time() - freshest > 180:
            if ws_task and not ws_task.done():
                ws_task.cancel()
                try:
                    await ws_task
                except asyncio.CancelledError:
                    pass

            ws_task = asyncio.create_task(start_ws_safe())


# ---------------- GLOBAL RISK LOOP ----------------

async def global_risk_loop():
    await asyncio.sleep(10)

    while True:
        for symbol in SYMBOLS:
            try:
                now = time.time()

                # -------- FUNDING --------
                f = ws.funding.get(symbol)
                pf = last_funding.get(symbol)

                funding_valid = False
                if f is not None:
                    prev_funding[symbol] = pf
                    last_funding[symbol] = f
                    last_funding_ts[symbol] = now
                    funding_valid = True
                elif now - last_funding_ts.get(symbol, 0) < 120:
                    f = last_funding.get(symbol)
                    pf = prev_funding.get(symbol)
                    funding_valid = True

                # -------- OI --------
                oi_vals = ws.oi_window.get(symbol, [])
                oi_valid = len(oi_vals) >= 2
                if oi_valid:
                    last_oi_ts[symbol] = now

                # -------- LIQ --------
                liq = ws.liquidations.get(symbol, 0)
                liq_valid = liq > 0
                if liq_valid:
                    last_liq_ts[symbol] = now

                ls = ws.long_short_ratio.get(symbol, {"long": 0, "short": 0})
                total = ls["long"] + ls["short"]
                long_ratio = ls["long"] / total if total else 0.5

                price = getattr(ws, "mark_price", {}).get(symbol)
                liq_sides = getattr(ws, "liq_sides", {}).get(symbol, {})

                score, direction, reasons, funding_spike, oi_spike = risk.calculate_risk(
                    f if funding_valid else None,
                    pf,
                    long_ratio,
                    oi_vals if oi_valid else [],
                    liq if liq_valid else 0,
                    LIQ_THRESHOLDS[symbol],
                    price,
                    liq_sides
                )

                cache[symbol] = (score, direction, reasons)

                for chat_id in active_chats:
                    if funding_spike and funding_valid:
                        if now - last_spikes["funding"].get(symbol, 0) > 900:
                            last_spikes["funding"][symbol] = now
                            await bot.send_message(chat_id, f"📈 {symbol} FUNDING SPIKE")

                    if oi_spike and oi_valid:
                        if now - last_spikes["oi"].get(symbol, 0) > 900:
                            last_spikes["oi"][symbol] = now
                            await bot.send_message(chat_id, f"💥 {symbol} OI SPIKE")

                    if score >= HARD_ALERT_LEVEL and direction:
                        prefix = "🚨 HARD RISK ALERT"
                    elif score >= EARLY_ALERT_LEVEL:
                        prefix = "⚠️ RISK BUILDUP"
                    else:
                        continue

                    text = (
                        f"{prefix} {symbol}\n\n"
                        f"Risk score: {score}\n"
                        f"Direction: {direction}\n\n"
                        + "\n".join(f"- {r}" for r in reasons)
                    )

                    await bot.send_message(chat_id, text)

            except Exception as e:
                print("RISK LOOP ERROR:", e, flush=True)

        await asyncio.sleep(INTERVAL_SECONDS)


# ---------------- COMMANDS ----------------

def ensure_chat(chat_id):
    active_chats.add(chat_id)


@dp.message_handler(commands=["risk"])
async def risk_cmd(message: types.Message):
    ensure_chat(message.chat.id)
    await send_current_risk(message.chat.id)


async def send_current_risk(chat_id):
    lines = []
    for symbol, (score, direction, _) in cache.items():
        lines.append(f"{symbol}: {score} ({direction or 'NEUTRAL'})")
    await bot.send_message(chat_id, "\n".join(lines))


# ---------------- HEALTH ----------------

class PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/health"):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
        else:
            self.send_response(404)
            self.end_headers()


def start_http():
    HTTPServer(("0.0.0.0", 8080), PingHandler).serve_forever()


# ---------------- STARTUP ----------------

async def on_startup(dp):
    global ws_task
    await bot.delete_webhook(drop_pending_updates=True)
    ws_task = asyncio.create_task(start_ws_safe())
    asyncio.create_task(ws_watchdog())
    asyncio.create_task(global_risk_loop())


if __name__ == "__main__":
    threading.Thread(target=start_http, daemon=True).start()
    executor.start_polling(dp, skip_updates=True, on_startup=on_startup)

