import asyncio
import time
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from aiogram import Bot, Dispatcher, types
from aiogram.utils import executor
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from aiogram.utils.exceptions import BotBlocked

import ws_binance as ws
import risk
import meta
import divergence
from config import *

from collections import defaultdict, deque
from logger import log_event

ALERT_WINDOW_HOURS = 3  # ← можешь менять
alert_history = defaultdict(deque)

# ---------------- MARKET REGIME ----------------

MARKET_REGIME_INTERVAL = 900  # 15 минут
last_regime_ts = 0
current_market_regime = "UNKNOWN"

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
main_kb.add(KeyboardButton("📋 Команды"))


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
    return f"{f * 100:.5f}%"


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
    score = cache.get(symbol, (None,))[0]

    trend = "flat"
    if prev is not None and score is not None:
        trend = (
            "rising" if score > prev
            else "falling" if score < prev
            else "flat"
        )

    prev_scores[symbol] = score

    return (
        f"Trend: {trend}\n"
        f"OI: {oi_txt} ({WINDOW_SECONDS // 60}m)\n"
        f"Pressure: {pressure} buy\n"
        f"Liq: {liq_txt}"
    )

def build_market_state():
    risks = []
    directions = []
    buildups = 0
    funding_vals = []

    for symbol, data in cache.items():
        score, direction, _ = data
        if score is not None:
            risks.append(score)
        if direction:
            directions.append(direction)

        f = ws.funding.get(symbol)
        if f is not None:
            funding_vals.append(f)

    for q in alert_history.values():
        buildups += len(q)

    avg_risk = sum(risks) / len(risks) if risks else 0
    avg_funding = sum(funding_vals) / len(funding_vals) if funding_vals else 0

    long_bias = directions.count("LONG")
    short_bias = directions.count("SHORT")

    bias = "neutral"
    if long_bias > short_bias + 1:
        bias = "LONG-heavy"
    elif short_bias > long_bias + 1:
        bias = "SHORT-heavy"

    return {
        "avg_risk": round(avg_risk, 2),
        "buildup_count": buildups,
        "bias": bias,
        "avg_funding": round(avg_funding, 6),
        "symbols": len(cache),
    }


def detect_market_regime(state):
    if state["avg_risk"] < 1 and state["buildup_count"] < 5:
        return "CALM"

    if state["buildup_count"] >= 5 and state["avg_risk"] < 2:
        return "CROWD_IMBALANCE"

    if state["avg_risk"] >= 2:
        return "STRESS"

    return "UNDEFINED"



# ---------------- GLOBAL RISK LOOP ----------------

async def global_risk_loop():
    await asyncio.sleep(10)

    while True:
        global last_regime_ts, current_market_regime

        now_ts = int(time.time())
    
        if now_ts - last_regime_ts >= MARKET_REGIME_INTERVAL:
            state = build_market_state()
            regime = detect_market_regime(state)
        
            # логируем ВСЕГДА
            log_event("market_regime", {
                "ts": now_ts,
                "regime": regime,
                **state,
            })
        
            current_market_regime = regime
            last_regime_ts = now_ts


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

                log_event("risk_eval", {
                    "symbol": symbol,
                    "risk": score,
                    "direction": direction,
                    "funding": f,
                    "oi_spike": oi_spike,
                    "funding_spike": funding_spike,
                    "liq": liq,
                })

                
                # -------- RISK ALERTS --------
                quality = meta.stream_quality(symbol)
                if quality["level"] == "LOW":
                    continue
                
                confidence = meta.calculate_confidence(
                    score,
                    direction,
                    oi_spike,
                    funding_spike,
                    liq,
                    price,
                    liq_sides
                )
                
                if funding_spike:
                    confidence += 1
                if oi_spike:
                    confidence += 1
                confidence = min(confidence, 5)
                
                conf_level = meta.confidence_level(confidence)
                
                now_ts = int(time.time())
                
                # ---------- HARD ALERT ----------
                if score >= HARD_ALERT_LEVEL and direction and confidence >= 3:
                    text = (
                        f"🚨 HARD RISK ALERT {symbol}\n\n"
                        f"Risk: {score}\n"
                        f"Direction: {direction}\n"
                        f"Confidence: {conf_level}"
                    )
                
                    for chat in active_chats.copy():
                        try:
                            await bot.send_message(chat, text)
                        except BotBlocked:
                            active_chats.discard(chat)

                    log_event("alert_sent", {
                        "ts": now_ts,
                        "symbol": symbol,
                        "risk": score,
                        "direction": direction,
                        "confidence": confidence,
                        "type": "HARD",
                        "chat_id": "broadcast",
                    })
                
                # ---------- BUILDUP ALERT ----------
                elif score >= EARLY_ALERT_LEVEL:
                    alert_history[symbol].append(now_ts)
                
                    cutoff = now_ts - ALERT_WINDOW_HOURS * 3600
                    while alert_history[symbol] and alert_history[symbol][0] < cutoff:
                        alert_history[symbol].popleft()
                
                    symbol_alerts_count = len(alert_history[symbol])
                
                    text = (
                        f"⚠️ RISK BUILDUP {symbol}\n\n"
                        f"Risk: {score}\n"
                        f"Direction: {direction}\n"
                        f"Alerts last {ALERT_WINDOW_HOURS}h: {symbol_alerts_count}"
                    )
                
                    if conf_level in ("MEDIUM", "HIGH") and reasons:
                        text += f"\nConfidence: {conf_level}\nReason: {reasons[0]}"
                
                    for chat in active_chats.copy():
                        try:
                            await bot.send_message(chat, text)
                        except BotBlocked:
                            active_chats.discard(chat)

                
                    log_event("alert_sent", {
                        "ts": now_ts,
                        "symbol": symbol,
                        "risk": score,
                        "direction": direction,
                        "confidence": confidence,
                        "type": "BUILDUP",
                        "chat_id": "broadcast",
                    })
                    
            except Exception as e:
                print("RISK LOOP ERROR:", e, flush=True)

        await asyncio.sleep(INTERVAL_SECONDS)

# ---------------- COMMANDS ----------------

def ensure_chat(chat_id):
    active_chats.add(chat_id)

@dp.message_handler(commands=["about"])
async def about_cmd(message: types.Message):
    await message.reply(
        "This bot monitors crypto market risk and crowd behavior in real time.\n\n"
        "It tracks:\n"
        "• Risk buildup\n"
        "• Crowd imbalance (long/short pressure)\n"
        "• Market stress regimes\n\n"
        "Important:\n"
        "• This is NOT a trading signal bot\n"
        "• It does NOT predict price\n"
        "• It provides context, not advice\n\n"
        "If the bot is silent — the market is calm.\n"
        "If alerts appear — something is changing.\n\n"
        "Experimental system."
    )

@dp.message_handler(commands=["philosophy"])
async def about_cmd(message: types.Message):
    await message.reply(
        "Markets move because of people, not indicators.\n\n"
        "  \n"
        "This bot does not chase price.\n"
        "It observes stress, imbalance and crowd behavior.\n\n"
        "  \n"
        "Risk appears before direction.\n"
        "Silence is a signal."
    )

@dp.message_handler(commands=["start"])
async def start_cmd(message: types.Message):
    ensure_chat(message.chat.id)
    await message.reply(
        "Привет. Я бот оценки рыночного риска.\n\n"
        "Нажми «📋 Команды», чтобы посмотреть, что я умею.",
        reply_markup=main_kb
    )


@dp.message_handler(commands=["commands"])
async def commands_cmd(message: types.Message):
    await message.reply(
        "📋 Команды:\n\n"
        "/risk — обзор риска по всем инструментам\n"
        "/risk BTC — текущий рыночный срез\n"
        "/risk BTC full — расширенный контекст\n"
        "/risk BTC debug — технические данные\n\n"
        "/regime — макро-состояние рынка\n"
        "/help — как читать данные"
    )


@dp.message_handler(commands=["help"])
async def help_cmd(message: types.Message):
    await message.reply(
        "ℹ️ О боте\n\n"
        "Бот отслеживает рыночный РИСК, а не торговые сигналы.\n"
        "Если бот молчит — рынок стабилен.\n\n"
        "Метрики:\n"
        "Risk — уровень напряжения (0–10)\n"
        "Direction — куда уязвим рынок\n"
        "Confidence — надёжность оценки\n"
        "Pressure — соотношение объёмов\n"
        "Liquidations — принудительные закрытия"
    )


@dp.message_handler(lambda m: m.text and "Команды" in m.text)
async def commands_button(message: types.Message):
    await commands_cmd(message)


@dp.message_handler(commands=["risk"])
async def risk_cmd(message: types.Message):
    ensure_chat(message.chat.id)

    log_event("cmd_risk", {
        "ts": int(time.time()),
        "chat_id": message.chat.id,
        "text": message.text,
    })

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

    if len(parts) >= 3 and parts[2].lower() == "full":
        now_ts = int(time.time())
        cutoff = now_ts - ALERT_WINDOW_HOURS * 3600
    
        history = alert_history.get(symbol, [])
        alerts_last = sum(1 for ts in history if ts >= cutoff)
    
        text = (
            f"{disp}\n"
            f"Market regime: {current_market_regime}\n"
            f"Risk: {score}/10 ({direction or 'NEUTRAL'})\n"
            f"Funding: {percent_funding(f)}\n\n"
            f"Alerts last {ALERT_WINDOW_HOURS}h: {alerts_last}\n\n"
            f"{snap}"
        )
    
        if reasons:
            text += "\n\nReasons:\n" + "\n".join(f"- {r}" for r in reasons)
    
        await message.reply(text)
        return

    await message.reply(
        f"{disp}\nRisk: {score}/10 ({direction or 'NEUTRAL'})\n"
        f"Funding: {qualitative_funding(f)}\n\n{snap}"
    )

@dp.message_handler(commands=["regime"])
async def regime_cmd(message: types.Message):
    state = build_market_state()
    regime = detect_market_regime(state)

    text = (
        f"🌍 Market Regime: {regime}\n\n"
        f"Avg risk: {state['avg_risk']}\n"
        f"Buildups (3h): {state['buildup_count']}\n"
        f"Bias: {state['bias']}\n"
        f"Avg funding: {state['avg_funding']}\n"
        f"Symbols tracked: {state['symbols']}\n\n"
    )

    if regime == "CALM":
        text += "Interpretation:\nLow systemic stress.\nCrowd positioning balanced."
    elif regime == "CROWD_IMBALANCE":
        text += "Interpretation:\nCrowded positioning detected.\nAsymmetric risk increasing."
    elif regime == "STRESS":
        text += "Interpretation:\nMarket under stress.\nVolatility expansion likely."
    else:
        text += "Interpretation:\nMarket state unclear."

    await message.reply(text)
    

async def send_current_risk(chat_id):
    lines = [
        f"{display_symbol(s)}: {v[0]} ({v[1] or 'NEUTRAL'})"
        for s, v in cache.items()
    ]
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



