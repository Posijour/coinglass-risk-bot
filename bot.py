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


# ---------------- HELPERS ----------------

def qualitative_funding(f):
    if f is None:
        return "unknown"
    if abs(f) < 0.0002:
        return "neutral"
    if f > 0:
        return "positive"
    return "negative"


def build_market_snapshot(symbol):
    f = ws.funding.get(symbol)
    funding_txt = qualitative_funding(f)

    oi_vals = ws.oi_window.get(symbol, [])
    if len(oi_vals) >= 2 and oi_vals[0][1] > 0:
        oi_txt = f"{(oi_vals[-1][1] - oi_vals[0][1]) / oi_vals[0][1] * 100:+.1f}%"
    else:
        oi_txt = "no change"

    ls = ws.long_short_ratio.get(symbol, {"long": 0, "short": 0})
    total = ls["long"] + ls["short"]
    pressure = f"{int(ls['long'] / total * 100)}%" if total else "—"

    liq = ws.liquidations.get(symbol, 0)
    liq_txt = f"{liq / 1_000_000:.1f}M" if liq > 0 else "none detected"

    prev = prev_scores.get(symbol)
    trend = "flat"
    if prev is not None:
        score = cache[symbol][0]
        trend = "rising" if score > prev else "falling" if score < prev else "flat"
    prev_scores[symbol] = cache[symbol][0]

    return (
        f"Trend: {trend}\n"
        f"Funding: {funding_txt}\n"
        f"OI: {oi_txt} ({WINDOW_SECONDS // 60}m)\n"
        f"Pressure: {pressure} buy\n"
        f"Liq: {liq_txt}"
    )


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

                conf_level = meta.confidence_level(confidence)

                for chat_id in active_chats:

                    # HARD
                    if score >= HARD_ALERT_LEVEL and direction and confidence >= 3:
                        await bot.send_message(
                            chat_id,
                            f"🚨 HARD RISK ALERT {symbol}\n\n"
                            f"Risk: {score}\n"
                            f"Direction: {direction}\n"
                            f"Confidence: {conf_level}"
                        )
                        continue

                    # BUILDUP
                    if score >= EARLY_ALERT_LEVEL:
                        text = (
                            f"⚠️ RISK BUILDUP {symbol}\n\n"
                            f"Risk: {score}\n"
                            f"Direction: {direction}"
                        )

                        if conf_level in ("MEDIUM", "HIGH"):
                            text += f"\nConfidence: {conf_level}"
                            if reasons:
                                text += f"\nReason: {reasons[0]}"

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

    if len(parts) == 1:
        await send_current_risk(message.chat.id)
        return

    symbol = parts[1].upper()
    if symbol not in cache:
        await message.reply("❌ Неизвестный символ")
        return

    score, direction, reasons = cache[symbol]
    snapshot = build_market_snapshot(symbol)

    # DEBUG
    if len(parts) >= 3 and parts[2].lower() == "debug":
        f = ws.funding.get(symbol)
        quality = meta.stream_quality(symbol)
        confidence = meta.calculate_confidence(
            score, direction, False, False,
            ws.liquidations.get(symbol, 0),
            None, {}
        )
        conf_level = meta.confidence_level(confidence)

        await message.reply(
            f"DEBUG {symbol}\n\n"
            f"score: {score}\n"
            f"direction: {direction}\n"
            f"funding_raw: {f}\n"
            f"confidence: {confidence} ({conf_level})\n"
            f"quality: {quality['level']}"
        )
        return

    # FULL
    if len(parts) >= 3 and parts[2].lower() == "full":
        liq = ws.liquidations.get(symbol, 0)
        state = meta.detect_state(score, False, False, liq)
        quality = meta.stream_quality(symbol)
        f = ws.funding.get(symbol)

        confidence = meta.calculate_confidence(
            score, direction, False, False, liq, None, {}
        )
        conf_level = meta.confidence_level(confidence)

        divs = divergence.detect_divergence(
            symbol=symbol,
            state=state,
            pressure_ratio=ws.long_short_ratio.get(symbol, {}).get("long", 0),
            oi_window=ws.oi_window.get(symbol, []),
            price_trend="FLAT",
            liquidations=liq
        )

        text = (
            f"{symbol}\n"
            f"Risk: {score}/10 ({direction or 'NEUTRAL'})\n"
            f"State: {state}\n"
            f"Confidence: {conf_level} ({confidence}/5)\n"
            f"Quality: {quality['level']}\n"
            f"Funding raw: {f}\n\n"
            + snapshot +
            "\n\n" + "\n".join(f"- {r}" for r in reasons)
        )

        if divs:
            text += "\n\nDivergences:\n" + "\n".join(f"- {d}" for d in divs)

        await message.reply(text)
        return

    # SIMPLE
    await message.reply(
        f"{symbol}\n"
        f"Risk: {score}/10 ({direction or 'NEUTRAL'})\n"
        + snapshot
    )


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
