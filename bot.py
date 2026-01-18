import asyncio
import time
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from aiogram import Bot, Dispatcher, types
from aiogram.utils import executor

import ws_binance as ws
import risk
import meta
import divergence
from config import *

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

active_chats = set()
cache = {}
prev_scores = {}

last_funding = {}
prev_funding = {}
last_funding_ts = {}

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

                oi_vals = ws.oi_window.get(symbol, [])
                liq = ws.liquidations.get(symbol, 0)

                ls = ws.long_short_ratio.get(symbol, {"long": 0, "short": 0})
                total = ls["long"] + ls["short"]
                pressure_ratio = ls["long"] / total if total else 0.5

                price = getattr(ws, "mark_price", {}).get(symbol)
                liq_sides = getattr(ws, "liq_sides", {}).get(symbol, {})

                score, direction, reasons, funding_spike, oi_spike = risk.calculate_risk(
                    f if funding_valid else None,
                    pf,
                    pressure_ratio,
                    oi_vals,
                    liq,
                    LIQ_THRESHOLDS[symbol],
                    price,
                    liq_sides
                )

                cache[symbol] = (score, direction, reasons)

                quality = meta.stream_quality(symbol)
                if quality["level"] == "LOW":
                    continue

                confidence = meta.calculate_confidence(
                    score,
                    direction,
                    oi_spike,
                    funding_spike,
                    liq,
                    price,
                    liq_sides
                )

                if funding_spike:
                    confidence += 1
                if oi_spike:
                    confidence += 1
                confidence = min(confidence, 5)

                for chat_id in active_chats:
                    if score >= HARD_ALERT_LEVEL and direction and confidence >= 3:
                        prefix = "🚨 HARD RISK ALERT"
                    elif score >= EARLY_ALERT_LEVEL:
                        prefix = "⚠️ RISK BUILDUP"
                    else:
                        continue

                    await bot.send_message(
                        chat_id,
                        f"{prefix} {symbol}\n"
                        f"Risk: {score}\n"
                        f"Direction: {direction}\n"
                        f"Confidence: {meta.confidence_level(confidence)}"
                    )

            except Exception as e:
                print("RISK LOOP ERROR:", e, flush=True)

        await asyncio.sleep(INTERVAL_SECONDS)


# ---------------- COMMANDS ----------------

def ensure_chat(chat_id):
    active_chats.add(chat_id)


@dp.message_handler(commands=["risk"])
async def risk_cmd(message: types.Message):
    ensure_chat(message.chat.id)
    parts = message.text.strip().split()

    if len(parts) == 1:
        await send_current_risk(message.chat.id)
        return

    symbol = parts[1].upper()
    if symbol not in cache:
        await message.reply("❌ Неизвестный символ")
        return

    score, direction, reasons = cache[symbol]

    oi_vals = ws.oi_window.get(symbol, [])
    liq = ws.liquidations.get(symbol, 0)

    ls = ws.long_short_ratio.get(symbol, {"long": 0, "short": 0})
    total = ls["long"] + ls["short"]
    pressure_ratio = ls["long"] / total if total else 0.5

    state = meta.detect_state(score, False, False, liq)
    quality = meta.stream_quality(symbol)

    # -------- DEBUG --------
    if len(parts) >= 3 and parts[2].lower() == "debug":
        text = (
            f"DEBUG {symbol}\n\n"
            f"score: {score}\n"
            f"direction: {direction}\n"
            f"pressure_ratio: {pressure_ratio:.2f}\n"
            f"oi_points: {len(oi_vals)}\n"
            f"liquidations: {liq}\n"
            f"state: {state}\n"
            f"quality: {quality['level']}\n"
        )
        await message.reply(text)
        return

    # -------- FULL --------
    if len(parts) >= 3 and parts[2].lower() == "full":
        price_trend = "FLAT"
        if len(oi_vals) >= 2:
            price_trend = "UP" if oi_vals[-1][1] > oi_vals[0][1] else "DOWN"

        divs = divergence.detect_divergence(
            symbol=symbol,
            state=state,
            pressure_ratio=pressure_ratio,
            oi_window=oi_vals,
            price_trend=price_trend,
            liquidations=liq
        )

        text = (
            f"{symbol}\n"
            f"Risk: {score}/10 ({direction or 'NEUTRAL'})\n"
            f"State: {state}\n"
            f"Quality: {quality['level']}\n\n"
            + "\n".join(f"- {r}" for r in reasons)
        )

        if divs:
            text += "\n\nDivergences:\n" + "\n".join(f"- {d}" for d in divs)

        await message.reply(text)
        return

    await message.reply(f"{symbol}: {score} ({direction or 'NEUTRAL'})")


async def send_current_risk(chat_id):
    lines = [f"{s}: {v[0]} ({v[1] or 'NEUTRAL'})" for s, v in cache.items()]
    await bot.send_message(chat_id, "\n".join(lines))


# ---------------- HEALTH ----------------

class PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")


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
