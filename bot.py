import asyncio
import time
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils import executor

from config import *
from risk import calculate_risk
from ws_binance import (
    funding,
    long_short_ratio,
    liquidations,
    last_update,
    oi_window,
    binance_ws
)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

active_chats = set()
last_funding = {}
cache = {}
last_spikes = {"funding": {}, "oi": {}}
prev_scores = {}

ws_task = None
ws_running = False


# ---------------- WS WATCHDOG ----------------

async def ws_watchdog():
    global ws_task, ws_running

    while True:
        await asyncio.sleep(60)

        if not last_update:
            continue

        freshest = max(last_update.values())
        if time.time() - freshest > 180:
            if ws_task and not ws_task.done():
                ws_task.cancel()
                try:
                    await ws_task
                except asyncio.CancelledError:
                    pass

            ws_running = False
            ws_task = asyncio.create_task(start_ws_safe())


async def start_ws_safe():
    global ws_running
    if ws_running:
        return
    ws_running = True
    try:
        await binance_ws()
    finally:
        ws_running = False


# ---------------- GLOBAL RISK LOOP ----------------

async def global_risk_loop():
    await asyncio.sleep(10)

    while True:
        for symbol in SYMBOLS:
            try:
                f = funding.get(symbol)
                if f is None:
                    continue

                ls = long_short_ratio.get(symbol, {"long": 0, "short": 0})
                total = ls["long"] + ls["short"]
                long_ratio = ls["long"] / total if total else 0.5

                prev_f = last_funding.get(symbol)
                last_funding[symbol] = f

                score, direction, reasons, funding_spike, oi_spike = calculate_risk(
                    f,
                    prev_f,
                    long_ratio,
                    oi_window[symbol],
                    liquidations.get(symbol, 0),
                    LIQ_THRESHOLDS[symbol]
                )

                cache[symbol] = (score, direction, reasons)
                now = time.time()

                for chat_id in list(active_chats):
                    if funding_spike and now - last_spikes["funding"].get(symbol, 0) > 900:
                        last_spikes["funding"][symbol] = now
                        await bot.send_message(chat_id, f"📈 {symbol} FUNDING SPIKE")

                    if oi_spike and now - last_spikes["oi"].get(symbol, 0) > 900:
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

def ensure_chat(chat_id: int):
    active_chats.add(chat_id)


@dp.message_handler(commands=["start"])
async def start(message: types.Message):
    ensure_chat(message.chat.id)

    kb = InlineKeyboardMarkup().add(
        InlineKeyboardButton("📊 Текущий риск", callback_data="risk")
    )

    await message.reply(
        "Я слежу за Binance Futures.\n"
        "Пишу только когда реально опасно.\n\n"
        "Тишина = рынок обычный.",
        reply_markup=kb
    )


@dp.message_handler(commands=["market"])
async def market_cmd(message: types.Message):
    ensure_chat(message.chat.id)

    if not cache:
        await message.reply("⏳ Данные ещё собираются")
        return

    lines = ["📊 Market overview\n"]
    for symbol, (score, direction, _) in cache.items():
        ts = last_update.get(symbol)
        t = time.strftime("%H:%M:%S", time.localtime(ts)) if ts else "—"
        lines.append(f"{symbol:<8} {score}/10 {direction or 'NEUTRAL':<7} ⏱ {t}")

    await message.reply("\n".join(lines))


@dp.message_handler(commands=["status"])
async def status_cmd(message: types.Message):
    ensure_chat(message.chat.id)

    if not last_update:
        await message.reply("⚠️ Нет данных от Binance WS")
        return

    last_ts = max(last_update.values())
    lag = int(time.time() - last_ts)

    text = (
        "🧠 System status\n\n"
        f"WS: {'OK' if lag < 180 else 'STALE'}\n"
        f"Active symbols: {len(last_update)}\n"
        f"Last update lag: {lag}s"
    )

    await message.reply(text)


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

    score, direction, _ = cache[symbol]

    prev = prev_scores.get(symbol, score)
    trend = "rising" if score > prev else "falling" if score < prev else "flat"
    prev_scores[symbol] = score

    f_now = funding.get(symbol, 0.0)
    f_prev = last_funding.get(symbol, f_now)

    oi_vals = [v for _, v in oi_window.get(symbol, [])]
    oi_change = (
        (oi_vals[-1] - oi_vals[0]) / oi_vals[0] * 100
        if len(oi_vals) > 1 and oi_vals[0] > 0 else 0.0
    )

    ls = long_short_ratio.get(symbol, {"long": 0, "short": 0})
    total = ls["long"] + ls["short"]
    crowd = int(ls["long"] / total * 100) if total else 50

    liq = liquidations.get(symbol, 0) / 1_000_000

    text = (
        f"{symbol}\n"
        f"Risk: {score}/10 ({direction or 'NEUTRAL'} BIAS)\n"
        f"Trend: {trend}\n"
        f"Funding: {f_prev:+.4f} → {f_now:+.4f}\n"
        f"OI: {oi_change:+.1f}% / {WINDOW_SECONDS // 60}m\n"
        f"Crowd: {crowd}% long\n"
        f"Liq: {liq:.1f}M ({WINDOW_SECONDS // 60}m)"
    )

    await message.reply(text)


async def send_current_risk(chat_id):
    if not cache:
        await bot.send_message(chat_id, "⏳ Данные ещё собираются")
        return

    lines = []
    for symbol, (score, direction, _) in cache.items():
        ts = last_update.get(symbol)
        t = time.strftime("%H:%M:%S", time.localtime(ts)) if ts else "—"
        lines.append(f"{symbol}: {score} ({direction or 'NEUTRAL'}) ⏱ {t}")

    await bot.send_message(chat_id, "\n".join(lines))


@dp.callback_query_handler(lambda c: c.data == "risk")
async def current_risk(call: types.CallbackQuery):
    ensure_chat(call.message.chat.id)
    await send_current_risk(call.message.chat.id)


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
