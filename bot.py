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
last_spikes = {"funding": {}, "oi": {}}
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

                # -------- LIQ --------
                liq = ws.liquidations.get(symbol, 0)

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
                    liq,
                    LIQ_THRESHOLDS[symbol],
                    price,
                    liq_sides
                )

                # cache ТОЛЬКО базовый риск
                cache[symbol] = (score, direction, reasons)

                # ---------- ALERT FILTERING ----------

                quality = meta.stream_quality(symbol)
                if quality["level"] == "LOW":
                    continue  # SUPPRESSION при плохих данных

                confidence = meta.calculate_confidence(
                    score,
                    direction,
                    oi_spike,
                    funding_spike,
                    liq,
                    price,
                    liq_sides
                )

                for chat_id in active_chats:

                    if score >= HARD_ALERT_LEVEL and direction and confidence >= 3:
                        prefix = "🚨 HARD RISK ALERT"
                    elif score >= EARLY_ALERT_LEVEL:
                        prefix = "⚠️ RISK BUILDUP"
                    else:
                        continue

                    text = (
                        f"{prefix} {symbol}\n\n"
                        f"Risk score: {score}\n"
                        f"Direction: {direction}\n"
                        f"Confidence: {meta.confidence_level(confidence)}\n\n"
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

    parts = message.text.strip().split()

    # /risk
    if len(parts) == 1:
        await send_current_risk(message.chat.id)
        return

    symbol = parts[1].upper()
    if symbol not in cache:
        await message.reply("❌ Неизвестный символ")
        return

    is_full = len(parts) >= 3 and parts[2].lower() == "full"

    score, direction, reasons = cache[symbol]

    liq = ws.liquidations.get(symbol, 0)
    price = getattr(ws, "mark_price", {}).get(symbol)
    liq_sides = getattr(ws, "liq_sides", {}).get(symbol, {})
    oi_vals = ws.oi_window.get(symbol, [])
    funding = ws.funding.get(symbol)

    quality = meta.stream_quality(symbol)
    state = meta.detect_state(score, False, False, liq)
    confidence = meta.calculate_confidence(
        score,
        direction,
        False,
        False,
        liq,
        price,
        liq_sides
    )
    conf_level = meta.confidence_level(confidence)

    if is_full:
        divs = divergence.detect_divergence(
            oi_vals,
            funding,
            ws.long_short_ratio.get(symbol, {}).get("long", 0),
            [],
            liq_sides
        )

        text = (
            f"{symbol}\n"
            f"Risk: {score}/10 ({direction or 'NEUTRAL'} BIAS)\n"
            f"State: {state}\n"
            f"Confidence: {conf_level} ({confidence}/5)\n"
            f"Quality: {quality['level']} ({quality['score']}/{quality['max']})\n\n"
            + "\n".join(f"- {r}" for r in reasons)
        )

        if divs:
            text += "\n\nDivergences:\n" + "\n".join(f"- {d}" for d in divs)

        await message.reply(text)
        return

    # -------- SIMPLE RISK --------

    prev = prev_scores.get(symbol, score)
    trend = (
        "rising" if score > prev else
        "falling" if score < prev else
        "flat"
    )
    prev_scores[symbol] = score

    f_txt = f"{funding:+.4f}" if funding is not None else "—"

    oi_txt = (
        f"{(oi_vals[-1][1] - oi_vals[0][1]) / oi_vals[0][1] * 100:+.1f}%"
        if len(oi_vals) >= 2 and oi_vals[0][1] > 0 else "—"
    )

    ls = ws.long_short_ratio.get(symbol, {"long": 0, "short": 0})
    total = ls["long"] + ls["short"]
    crowd = f"{int(ls['long'] / total * 100)}%" if total else "—"

    liq_txt = f"{liq / 1_000_000:.1f}M" if liq > 0 else "—"

    text = (
        f"{symbol}\n"
        f"Risk: {score}/10 ({direction or 'NEUTRAL'} BIAS)\n"
        f"Trend: {trend}\n"
        f"Funding: {f_txt}\n"
        f"OI: {oi_txt} / {WINDOW_SECONDS // 60}m\n"
        f"Crowd: {crowd} long\n"
        f"Liq: {liq_txt} ({WINDOW_SECONDS // 60}m)"
    )

    await message.reply(text)


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
