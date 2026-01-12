import asyncio
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from aiogram import Bot, Dispatcher, types
from aiogram.utils import executor

from config import (
    BOT_TOKEN,
    SYMBOLS,
    INTERVAL_SECONDS,
    EARLY_ALERT_LEVEL,
    HARD_ALERT_LEVEL,
)

from binance import (
    get_funding_rate,
    get_long_short_ratio,
    get_open_interest,
    get_liquidations,
)

from risk import calculate_risk


bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

active_chats = set()
last_oi = {}
last_funding = {}


async def risk_loop(chat_id: int):
    while chat_id in active_chats:
        for symbol in SYMBOLS:
            errors = {}

            funding = 0.0
            long_ratio = 0.0
            oi = 0.0
            liquidations = 0.0

            # --- Funding ---
            try:
                funding = get_funding_rate(symbol)
            except Exception as e:
                errors["funding_rate"] = str(e)

            # --- Long / Short ---
            try:
                long_ratio = get_long_short_ratio(symbol)
            except Exception as e:
                errors["long_short_ratio"] = str(e)

            # --- Open Interest ---
            try:
                oi = get_open_interest(symbol)
            except Exception as e:
                errors["open_interest"] = str(e)

            # --- Liquidations ---
            try:
                liquidations = get_liquidations(symbol)
            except Exception as e:
                errors["liquidations"] = str(e)

            if errors:
                msg = f"{symbol}: данные частично недоступны\n"
                msg += "\n".join(f"- {k}: {v}" for k, v in errors.items())
                await bot.send_message(chat_id, msg)
                continue

            prev_oi = last_oi.get(symbol, oi)
            oi_change = oi - prev_oi
            last_oi[symbol] = oi

            prev_funding = last_funding.get(symbol)
            last_funding[symbol] = funding

            score, direction, reasons = calculate_risk(
                funding=funding,
                prev_funding=prev_funding,
                long_ratio=long_ratio,
                oi_change=oi_change,
                oi=oi,
                liquidations=liquidations,
            )

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


# -----------------------------
# Ping server (для Render)
# -----------------------------
class PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()


def run_ping_server():
    HTTPServer(("0.0.0.0", 8080), PingHandler).serve_forever()


threading.Thread(target=run_ping_server, daemon=True).start()


# -----------------------------
# MAIN
# -----------------------------
if __name__ == "__main__":
    executor.start_polling(dp, skip_updates=True)
