import asyncio
from aiogram import Bot, Dispatcher, types
from aiogram.utils import executor
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading

from config import BOT_TOKEN, SYMBOLS, INTERVAL_SECONDS, RISK_ALERT_LEVEL
from binance import (
    get_funding_rate,
    get_long_short_ratio,
    get_open_interest,
    get_liquidations,
    BinanceError
)
from risk import calculate_risk

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

last_oi = {}
active_chats = set()


async def risk_loop(chat_id: int):
    while chat_id in active_chats:
        for symbol in SYMBOLS:
            errors = {}
            funding = long_ratio = oi = liquidations = 0

            for name, func in {
                "funding_rate": get_funding_rate,
                "long_short_ratio": get_long_short_ratio,
                "open_interest": get_open_interest,
                "liquidations": get_liquidations,
            }.items():
                try:
                    locals()[name] = func(symbol)
                except Exception as e:
                    errors[name] = str(e)

            if errors:
                msg = f"{symbol}: данные частично недоступны\n"
                msg += "\n".join(f"- {k}: {v}" for k, v in errors.items())
                await bot.send_message(chat_id, msg)
                continue

            prev_oi = last_oi.get(symbol, oi)
            oi_change = oi - prev_oi
            last_oi[symbol] = oi

            score, direction, reasons = calculate_risk(
                funding=funding,
                long_ratio=long_ratio,
                oi_change=oi_change,
                liquidations=liquidations
            )

            if score >= RISK_ALERT_LEVEL and direction:
                text = (
                    f"⚠️ {symbol} RISK ALERT ({direction})\n\n"
                    f"Risk score: {score}\n\n"
                    + "\n".join(f"- {r}" for r in reasons)
                )
                await bot.send_message(chat_id, text)

        await asyncio.sleep(INTERVAL_SECONDS)


@dp.message_handler(commands=["start"])
async def start_handler(message: types.Message):
    await message.reply(
        "Я слежу за Binance Futures.\n"
        "Пишу только когда реально опасно.\n\n"
        "Тишина = рынок обычный."
    )
    if message.chat.id not in active_chats:
        active_chats.add(message.chat.id)
        asyncio.create_task(risk_loop(message.chat.id))


class PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()


def run_ping():
    HTTPServer(("0.0.0.0", 8080), PingHandler).serve_forever()


threading.Thread(target=run_ping, daemon=True).start()


if __name__ == "__main__":
    executor.start_polling(dp, skip_updates=True)
