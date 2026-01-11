import asyncio
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from aiogram import Bot, Dispatcher, types
from aiogram.utils import executor

from config import (
    BOT_TOKEN,
    SYMBOLS,
    INTERVAL_SECONDS,
    RISK_ALERT_LEVEL
)

from coinglass import (
    get_funding_rate,
    get_long_short_ratio,
    get_open_interest,
    get_liquidations,
    CoinGlassError
)

from risk import calculate_risk


# -----------------------------
# BOT INIT
# -----------------------------
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

active_chats = set()
last_oi = {}


# -----------------------------
# RISK LOOP
# -----------------------------
async def risk_loop(chat_id: int):
    await bot.send_message(chat_id, "🔍 Мониторинг запущен")

    while chat_id in active_chats:
        for symbol in SYMBOLS:
            errors = {}
            funding = None
            long_ratio = None
            oi = None
            liquidations = None

            try:
                try:
                    funding = get_funding_rate(symbol)
                except Exception as e:
                    errors["funding_rate"] = str(e)

                try:
                    long_ratio = get_long_short_ratio(symbol)
                except Exception as e:
                    errors["long_short_ratio"] = str(e)

                try:
                    oi = get_open_interest(symbol)
                except Exception as e:
                    errors["open_interest"] = str(e)

                try:
                    liquidations = get_liquidations(symbol)
                except Exception as e:
                    errors["liquidations"] = str(e)

                if len(errors) == 4:
                    await bot.send_message(
                        chat_id,
                        f"{symbol}: все данные недоступны\n"
                        + "\n".join(f"- {k}: {v}" for k, v in errors.items())
                    )
                    continue

                prev_oi = last_oi.get(symbol, oi or 0)
                oi_change = (oi - prev_oi) if oi is not None else 0
                if oi is not None:
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

            except Exception as e:
                await bot.send_message(chat_id, f"{symbol}: критическая ошибка ({e})")

        await asyncio.sleep(INTERVAL_SECONDS)


# -----------------------------
# COMMANDS
# -----------------------------
@dp.message_handler(commands=["start"])
async def start_handler(message: types.Message):
    chat_id = message.chat.id

    await message.reply(
        "Я слежу за рынком и предупреждаю,\n"
        "когда риск для лонгов или шортов становится высоким.\n\n"
        "Если я молчу — рынок обычный."
    )

    if chat_id not in active_chats:
        active_chats.add(chat_id)
        asyncio.create_task(risk_loop(chat_id))


@dp.message_handler(commands=["stop"])
async def stop_handler(message: types.Message):
    chat_id = message.chat.id
    active_chats.discard(chat_id)
    await message.reply("⏹ Мониторинг остановлен")


# -----------------------------
# PING SERVER (Render)
# -----------------------------
class PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()


def run_ping_server():
    server = HTTPServer(("0.0.0.0", 8080), PingHandler)
    print("Ping server running on port 8080")
    server.serve_forever()


threading.Thread(target=run_ping_server, daemon=True).start()


# -----------------------------
# MAIN
# -----------------------------
if __name__ == "__main__":
    executor.start_polling(dp, skip_updates=True)
