
import asyncio
import time
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from aiogram import Bot, Dispatcher, types
from aiogram.utils import executor
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton

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

diag_cooldowns = {
    "oi": {},
    "liq": {}
}

ws_task = None
ws_running = False


# ---------------- KEYBOARD ----------------

main_kb = ReplyKeyboardMarkup(resize_keyboard=True)
main_kb.add(KeyboardButton("/commands"))


# ---------------- SYMBOL HELPERS ----------------

def normalize_symbol(user_input: str) -> str:
    s = user_input.upper()
    if not s.endswith("USDT"):
        s += "USDT"
    return s


def display_symbol(symbol: str) -> str:
    return symbol.replace("USDT", "")


# ---------------- FUNDING HELPERS ----------------

def qualitative_funding(f):
    if f is None:
        return "unknown"
    if abs(f) < 0.0002:
        return "neutral"
    return "positive" if f > 0 else "negative"


def percent_funding(f):
    if f is None:
        return "unknown"
    return f"{f * 100:.4f}%"


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


# ---------------- SNAPSHOT ----------------

def build_market_snapshot(symbol):
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

                if f is not None:
                    prev_funding[symbol] = pf
                    last_funding[symbol] = f
                    last_funding_ts[symbol] = now

                oi_vals = ws.oi_window.get(symbol, [])
                liq = ws.liquidations.get(symbol, 0)

                ls = ws.long_short_ratio.get(symbol, {"long": 0, "short": 0})
                total = ls["long"] + ls["short"]
                pressure_ratio = ls["long"] / total if total else 0.5

                price = getattr(ws, "mark_price", {}).get(symbol)
                liq_sides = getattr(ws, "liq_sides", {}).get(symbol, {})

                score, direction, reasons, funding_spike, oi_spike = risk.calculate_risk(
                    f,
                    pf,
                    pressure_ratio,
                    oi_vals,
                    liq,
                    LIQ_THRESHOLDS[symbol],
                    price,
                    liq_sides
                )

                cache[symbol] = (score, direction, reasons)

                # -------- DIAGNOSTIC PINGS --------

                if len(oi_vals) >= 2 and oi_vals[0][1] > 0:
                    oi_change = abs(oi_vals[-1][1] - oi_vals[0][1]) / oi_vals[0][1]
                    last = diag_cooldowns["oi"].get(symbol, 0)
                    if oi_change >= 0.015 and now - last > 1200:
                        diag_cooldowns["oi"][symbol] = now
                        for chat in active_chats:
                            await bot.send_message(chat, f"👀 OI activity detected: {symbol}")

                last = diag_cooldowns["liq"].get(symbol, 0)
                if liq >= LIQ_THRESHOLDS.get(symbol, 0) * 0.7 and now - last > 1800:
                    diag_cooldowns["liq"][symbol] = now
                    for chat in active_chats:
                        await bot.send_message(chat, f"👀 Liquidations activity: {symbol}")

                # -------- RISK ALERTS --------

                quality = meta.stream_quality(symbol)
                if quality["level"] == "LOW":
                    continue

                confidence = meta.calculate_confidence(
                    score, direction, oi_spike, funding_spike, liq, price, liq_sides
                )

                if funding_spike:
                    confidence += 1
                if oi_spike:
                    confidence += 1
                confidence = min(confidence, 5)

                conf_level = meta.confidence_level(confidence)

                for chat in active_chats:
                    if score >= HARD_ALERT_LEVEL and direction and confidence >= 3:
                        await bot.send_message(
                            chat,
                            f"🚨 HARD RISK ALERT {symbol}\n\n"
                            f"Risk: {score}\nDirection: {direction}\nConfidence: {conf_level}"
                        )
                        continue

                    if score >= EARLY_ALERT_LEVEL:
                        text = f"⚠️ RISK BUILDUP {symbol}\n\nRisk: {score}\nDirection: {direction}"
                        if conf_level in ("MEDIUM", "HIGH") and reasons:
                            text += f"\nConfidence: {conf_level}\nReason: {reasons[0]}"
                        await bot.send_message(chat, text)

            except Exception as e:
                print("RISK LOOP ERROR:", e, flush=True)

        await asyncio.sleep(INTERVAL_SECONDS)


# ---------------- COMMANDS ----------------

def ensure_chat(chat_id):
    active_chats.add(chat_id)


@dp.message_handler(commands=["start"])
async def start_cmd(message: types.Message):
    ensure_chat(message.chat.id)
    await message.reply(
        "Привет. Я бот оценки рыночного риска.\n\n"
        "Нажми /commands, чтобы увидеть доступные команды.",
        reply_markup=main_kb
    )


@dp.message_handler(commands=["commands"])
async def commands_cmd(message: types.Message):
    await message.reply(
        "📋 Команды:\n\n"
        "/risk\n"
        "/risk BTC\n"
        "/risk BTC full\n"
        "/risk BTC debug"
    )


@dp.message_handler(commands=["help"])
async def help_cmd(message: types.Message):
    await message.reply(
        "ℹ️ О боте\n\n"
        "Этот бот отслеживает РЫНОЧНЫЙ РИСК, а не даёт торговые сигналы.\n"
        "Он пишет только тогда, когда рынок становится уязвимым.\n\n"
        "📊 Метрики:\n"
        "Risk (0–10) — уровень рыночного напряжения\n"
        "Direction — направление риска (LONG / SHORT)\n"
        "Confidence — надёжность оценки (LOW / MEDIUM / HIGH)\n"
        "State — CALM / BUILDUP / UNWIND\n"
        "Pressure — соотношение объёмов покупателей и продавцов\n"
        "Liquidations — принудительные закрытия позиций\n\n"
        "📌 Важно:\n"
        "Если бот молчит — это нормально.\n"
        "Тишина означает отсутствие структурного риска."
    )


@dp.message_handler(commands=["risk"])
async def risk_cmd(message: types.Message):
    ensure_chat(message.chat.id)
    parts = message.text.strip().split()

    if len(parts) == 1:
        await send_current_risk(message.chat.id)
        return

    symbol = normalize_symbol(parts[1])
    if symbol not in cache:
        await message.reply("❌ Неизвестный символ")
        return

    score, direction, reasons = cache[symbol]
    snap = build_market_snapshot(symbol)
    disp = display_symbol(symbol)
    f = ws.funding.get(symbol)

    if len(parts) >= 3 and parts[2].lower() == "debug":
        await message.reply(f"DEBUG {disp}\nfunding_raw: {f}\nrisk: {score}")
        return

    if len(parts) >= 3 and parts[2].lower() == "full":
        text = (
            f"{disp}\nRisk: {score}/10 ({direction or 'NEUTRAL'})\n"
            f"Funding: {percent_funding(f)}\n\n{snap}"
        )
        if reasons:
            text += "\n\nReasons:\n" + "\n".join(f"- {r}" for r in reasons)
        await message.reply(text)
        return

    await message.reply(
        f"{disp}\nRisk: {score}/10 ({direction or 'NEUTRAL'})\n"
        f"Funding: {qualitative_funding(f)}\n\n{snap}"
    )


async def send_current_risk(chat_id):
    lines = [f"{display_symbol(s)}: {v[0]} ({v[1] or 'NEUTRAL'})" for s, v in cache.items()]
    await bot.send_message(chat_id, "\n".join(lines))


# ---------------- HEALTH ----------------

class PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def do_HEAD(self):
        self.send_response(200)
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

