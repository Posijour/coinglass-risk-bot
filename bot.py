import asyncio
import time
from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils import executor
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading

from config import *
from binance import *
from risk import calculate_risk


async def call(fn, *args):
    return await asyncio.to_thread(fn, *args)


bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

active_chats = set()

last_oi = {}
last_funding = {}

cache = {}
last_update_ts = None


async def risk_loop(chat_id: int):
    global last_update_ts

    await asyncio.sleep(3)

    print(f"[LOOP] cycle done, cache size = {len(cache)}")

    while chat_id in active_chats:
        cycle_had_data = False

        for symbol in SYMBOLS:
            try:
                funding = await call(get_funding_rate, symbol)
                if funding is None:
                    print(f"[BINANCE] {symbol}: empty funding data")
                    continue

                long_ratio = await call(get_long_short_ratio, symbol)
                oi = await call(get_open_interest, symbol)
                liquidations = await call(get_liquidations, symbol)

                prev_oi = last_oi.get(symbol, oi)
                oi_change = oi - prev_oi
                last_oi[symbol] = oi

                prev_funding = last_funding.get(symbol)
                last_funding[symbol] = funding

                score, direction, reasons, funding_spike, oi_spike = calculate_risk(
                    funding,
                    prev_funding,
                    long_ratio,
                    oi_change,
                    oi,
                    liquidations
                )

                cache[symbol] = (score, direction, reasons)
                cycle_had_data = True

                if funding_spike:
                    await bot.send_message(chat_id, f"📈 {symbol} FUNDING SPIKE")

                if oi_spike:
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
                    f"Direction: {direction or 'NEUTRAL'}\n\n"
                    + "\n".join(f"- {r}" for r in reasons)
                )
                await bot.send_message(chat_id, text)

            except BinanceError as e:
                print(f"[BINANCE] {symbol}: {e}")
            except Exception as e:
                print(f"[ERROR] {symbol}: {e}")

        if cycle_had_data:
            last_update_ts = int(time.time())

        await asyncio.sleep(INTERVAL_SECONDS)


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

    for symbol in SYMBOLS:
        cache[symbol] = (0, None, ["Ожидание данных от Binance"])

    if message.chat.id not in active_chats:
        active_chats.add(message.chat.id)
        asyncio.create_task(risk_loop(message.chat.id))


@dp.callback_query_handler(lambda c: c.data == "risk")
async def current_risk(call: types.CallbackQuery):
    if not cache:
        await call.message.answer("⏳ Данных пока нет")
        return

    lines = []
    for symbol, (score, direction, _) in cache.items():
        lines.append(f"{symbol}: {score} ({direction or 'NEUTRAL'})")

    ts = time.strftime("%H:%M:%S", time.localtime(last_update_ts)) if last_update_ts else "—"
    lines.append(f"\n🕒 Обновлено: {ts}")

    await call.message.answer("\n".join(lines))


class PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()


threading.Thread(
    target=lambda: HTTPServer(("0.0.0.0", 8080), PingHandler).serve_forever(),
    daemon=True
).start()


if __name__ == "__main__":
    executor.start_polling(dp, skip_updates=True)


