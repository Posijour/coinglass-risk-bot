import asyncio
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from aiogram import Bot, Dispatcher, types
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from aiogram.utils import executor

from config import (
    BOT_TOKEN,
    SYMBOLS,
    INTERVAL_SECONDS,
    EARLY_ALERT_LEVEL,
    HARD_ALERT_LEVEL,
    FUNDING_SPIKE_THRESHOLD,
    OI_SPIKE_THRESHOLD,
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


# -----------------------------
# Keyboard
# -----------------------------
main_kb = ReplyKeyboardMarkup(resize_keyboard=True)
main_kb.add(KeyboardButton("📊 Текущий риск"))


# -----------------------------
# Core risk check
# -----------------------------
async def check_symbol(symbol: str, chat_id: int, manual=False):
    funding = get_funding_rate(symbol)
    long_ratio = get_long_short_ratio(symbol)
    oi = get_open_interest(symbol)
    liquidations = get_liquidations(symbol)

    prev_oi = last_oi.get(symbol, oi)
    oi_change = oi - prev_oi
    last_oi[symbol] = oi

    prev_funding = last_funding.get(symbol)
    last_funding[symbol] = funding

    # ----- SPIKES -----
    if prev_funding is not None:
        if abs(funding - prev_funding) >= FUNDING_SPIKE_THRESHOLD:
            await bot.send_message(
                chat_id,
                f"⚡ FUNDING SPIKE {symbol}\n"
                f"Было: {prev_funding:.4f}\n"
                f"Стало: {funding:.4f}"
            )

    if abs(oi_change) >= OI_SPIKE_THRESHOLD * oi:
        await bot.send_message(
            chat_id,
            f"📈 OI SPIKE {symbol}\n"
            f"Изменение OI: {oi_change:.2f}"
        )

    # ----- RISK SCORE -----
    score, direction, reasons = calculate_risk(
        funding=funding,
        prev_funding=prev_funding,
        long_ratio=long_ratio,
        oi_change=oi_change,
        oi=oi,
        liquidations=liquidations,
    )

    if manual:
        text = (
            f"📊 {symbol} текущий риск\n\n"
            f"Risk score: {score}\n"
            f"Направление: {direction or 'нейтрально'}\n\n"
            + "\n".join(f"- {r}" for r in reasons)
        )
        await bot.send_message(chat_id, text)
        return

    if score >= HARD_ALERT_LEVEL and direction:
        prefix = "🚨 HARD RISK ALERT"
    elif score >= EARLY_ALERT_LEVEL and direction:
        prefix = "⚠️ EARLY WARNING"
    else:
        return

    text = (
        f"{prefix} {symbol} ({direction})\n\n"
        f"Risk score: {score}\n\n"
        + "\n".join(f"- {r}" for r in reasons)
    )

    await bot.send_message(chat_id, text)


# -----------------------------
# Loop
# -----------------------------
async def risk_loop(chat_id: int):
    while chat_id in active_chats:
        for symbol in SYMBOLS:
            try:
                await check_symbol(symbol, chat_id)
            except Exception as e:
                await bot.send_message(chat_id, f"{symbol}: ошибка {e}")

        await asyncio.sleep(INTERVAL_SECONDS)


# -----------------------------
# Handlers
# -----------------------------
@dp.message_handler(commands=["start"])
async def start_handler(message: types.Message):
    await message.reply(
        "Я слежу за Binance Futures.\n"
        "Пишу только когда реально опасно.\n\n"
        "Тишина = рынок обычный.",
        reply_markup=main_kb
    )

    if message.chat.id not in active_chats:
        active_chats.add(message.chat.id)
        asyncio.create_task(risk_loop(message.chat.id))


@dp.message_handler(lambda m: m.text == "📊 Текущий риск")
async def manual_risk(message: types.Message):
    for symbol in SYMBOLS:
        try:
            await check_symbol(symbol, message.chat.id, manual=True)
        except Exception as e:
            await message.reply(f"{symbol}: ошибка {e}")


# -----------------------------
# Ping server
# -----------------------------
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

