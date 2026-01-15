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
    start_ws,
    binance_ws
)

print("[BOOT] bot starting")
print("=== BOT FILE LOADED ===", flush=True)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

active_chats = set()
last_oi = {}
last_funding = {}
cache = {}


async def risk_loop(chat_id: int):
    await asyncio.sleep(5)

    while chat_id in active_chats:
        for symbol in SYMBOLS:
            try:
                f = funding.get(symbol)
                oi = open_interest.get(symbol)
                ls = long_short_ratio.get(symbol, {"long": 0, "short": 0})
                liq = liquidations.get(symbol, 0)

                if f is None or oi is None or ls is None:
                    continue

                print(f"[RISK] {symbol} f={f} ls={ls}", flush=True)

                long_ratio = ls["long"] / max(ls["long"] + ls["short"], 1)

                prev_oi = last_oi.get(symbol, oi)
                oi_change = oi - prev_oi
                last_oi[symbol] = oi

                prev_funding = last_funding.get(symbol)
                last_funding[symbol] = f

                score, direction, reasons, *_ = calculate_risk(
                    f,
                    prev_funding,
                    long_ratio,
                    oi_change,
                    oi,
                    liq
                )
                
                print(
                    f"[RISK] {symbol} f={f} long={ls['long']} short={ls['short']}",
                    flush=True
                )

                cache[symbol] = (score, direction, reasons)
                print(f"[CACHE] updated {symbol}")

            except Exception as e:
                import traceback
                traceback.print_exc()

        await asyncio.sleep(INTERVAL_SECONDS)

@dp.message_handler(commands=["start"])
async def start(message: types.Message):
    kb = InlineKeyboardMarkup().add(
        InlineKeyboardButton("📊 Текущий риск", callback_data="risk")
    )

    await message.reply(
        "Я слежу за Binance Futures.\nПишу только когда реально опасно.",
        reply_markup=kb
    )

    for s in SYMBOLS:
        cache[s] = (0, None, ["Идёт прогрев данных"])
        
    if message.chat.id not in active_chats:
        active_chats.add(message.chat.id)
        asyncio.create_task(risk_loop(message.chat.id))


@dp.callback_query_handler(lambda c: c.data == "risk")
async def current_risk(call: types.CallbackQuery):
    if not cache:
        await call.message.answer("⏳ Данные ещё собираются")
        return

    lines = []
    for symbol, (score, direction, _) in cache.items():
        ts = last_update.get(symbol)
        t = time.strftime("%H:%M:%S", time.localtime(ts)) if ts else "—"
        lines.append(f"{symbol}: {score} ({direction or 'NEUTRAL'}) ⏱ {t}")

    await call.message.answer("\n".join(lines))

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

async def on_startup(dp):
    print("[BOOT] on_startup", flush=True)
    await bot.delete_webhook(drop_pending_updates=True)
    asyncio.create_task(binance_ws())

if __name__ == "__main__":
    threading.Thread(target=start_http, daemon=True).start()
    
    executor.start_polling(
    dp,
    skip_updates=True,
    on_startup=on_startup
    )



