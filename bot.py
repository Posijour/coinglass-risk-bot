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
    open_interest,
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


async def risk_loop(chat_id: int):
    await asyncio.sleep(5)

    while chat_id in active_chats:
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

            except Exception:
                pass

        await asyncio.sleep(INTERVAL_SECONDS)


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


@dp.message_handler(commands=["start"])
async def start(message: types.Message):
    kb = InlineKeyboardMarkup().add(
        InlineKeyboardButton("📊 Текущий риск", callback_data="risk")
    )

    await message.reply(
        "Я слежу за Binance Futures.\n"
        "Пишу только когда реально опасно.\n\n"
        "Тишина = рынок обычный.",
        reply_markup=kb
    )

    if message.chat.id not in active_chats:
        active_chats.add(message.chat.id)
        asyncio.create_task(risk_loop(message.chat.id))


@dp.message_handler(commands=["risk"])
async def risk_cmd(message: types.Message):
    await send_current_risk(message.chat.id)


@dp.callback_query_handler(lambda c: c.data == "risk")
async def current_risk(call: types.CallbackQuery):
    await send_current_risk(call.message.chat.id)


class PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")


def start_http():
    HTTPServer(("0.0.0.0", 8080), PingHandler).serve_forever()


async def on_startup(dp):
    await bot.delete_webhook(drop_pending_updates=True)
    asyncio.create_task(binance_ws())


if __name__ == "__main__":
    threading.Thread(target=start_http, daemon=True).start()
    executor.start_polling(dp, skip_updates=True, on_startup=on_startup)
