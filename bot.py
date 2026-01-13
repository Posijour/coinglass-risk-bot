import asyncio
from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils import executor
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading

from config import *
from binance import *
from risk import calculate_risk

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

active_chats = set()
last_oi = {}
last_funding = {}
cache = {}


async def risk_loop(chat_id: int):
    await asyncio.sleep(5)  # небольшая пауза после /start

    while chat_id in active_chats:
        for symbol in SYMBOLS:
            try:
                funding = get_funding_rate(symbol)
                long_ratio = get_long_short_ratio(symbol)
                oi = get_open_interest(symbol)
                liquidations = get_liquidations(symbol)

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

                # обновляем cache каждый цикл
                cache[symbol] = (score, direction, reasons)

                if funding_spike:
                    await bot.send_message(chat_id, f"📈 {symbol} FUNDING SPIKE")

                if oi_spike:
                    await bot.send_message(chat_id, f"💥 {symbol} OI SPIKE")

                if score >= HARD_ALERT_LEVEL and direction:
                    prefix = "🚨 HARD RISK ALERT"
                elif score >= EARLY_ALERT_LEVEL and direction:
                    prefix = "⚠️ EARLY WARNING"
                else:
                    continue

                text = (
                    f"{prefix} {symbol} ({direction})\n\n"
                    f"Risk score: {score}\n\n"
                    + "\n".join(f"- {r}" for r in reasons)
                )
                await bot.send_message(chat_id, text)

            except BinanceError:
                pass  # не спамим ошибками бинанса
            except Exception as e:
                print("ERROR:", e)

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

    if message.chat.id not in active_chats:
        active_chats.add(message.chat.id)

        # 🔹 первичное заполнение cache, чтобы кнопка работала сразу
        for symbol in SYMBOLS:
            try:
                funding = get_funding_rate(symbol)
                long_ratio = get_long_short_ratio(symbol)
                oi = get_open_interest(symbol)
                liquidations = get_liquidations(symbol)

                score, direction, reasons, *_ = calculate_risk(
                    funding,
                    None,
                    long_ratio,
                    0,
                    oi,
                    liquidations
                )

                cache[symbol] = (score, direction, reasons)
            except Exception:
                pass

        asyncio.create_task(risk_loop(message.chat.id))


@dp.callback_query_handler(lambda c: c.data == "risk")
async def current_risk(call: types.CallbackQuery):
    if not cache:
        await call.message.answer("Пока нет данных, рынок ещё не проверялся")
        return

    lines = []
    for symbol, (score, direction, _) in cache.items():
        dir_text = direction or "NEUTRAL"
        lines.append(f"{symbol}: {score} ({dir_text})")

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

