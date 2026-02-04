import asyncio
import time
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from aiogram import Bot, Dispatcher, types
from aiogram.utils import executor
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from aiogram.utils.exceptions import BotBlocked, RetryAfter, NetworkError, TelegramAPIError

import ws_binance as ws
import risk
import meta
import divergence
from config import *

from collections import defaultdict, deque
from logger import log_event

# --- ACTIVITY REGIME CONFIG ---
ACTIVITY_WINDOW_HOURS = 4
ACTIVITY_CALM_MAX = 2
ACTIVITY_FRAGILE_MAX = 5
last_activity_regime = None

ALERT_WINDOW_HOURS = 4  # ← можешь менять
alert_history = defaultdict(deque)
LAST_RISK_EVAL_TS = 0


# ---------------- MARKET REGIME ----------------

MARKET_REGIME_INTERVAL = 900  # 15 минут
last_regime_ts = 0
current_market_regime = "UNKNOWN"
ACTIVITY_REGIME_INTERVAL = 900  # 15 минут
last_activity_ts = 0

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
message_queue = asyncio.Queue()

SEND_DELAY_SECONDS = 0.2
SEND_RETRY_LIMIT = 5


# ---------------- KEYBOARD ----------------

main_kb = ReplyKeyboardMarkup(resize_keyboard=True)
main_kb.add(KeyboardButton("📋 Commands"))


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

    for symbol, data in cache.items():
        score, direction = data[0], data[1]
        if score is not None:
            risks.append(score)
        if direction:
            directions.append(direction)

    for q in alert_history.values():
        buildups += len(q)

    avg_risk = sum(risks) / len(risks) if risks else 0

    long_bias = directions.count("LONG")
    short_bias = directions.count("SHORT")

    return {
        "avg_risk": round(avg_risk, 2),
        "buildup_count": buildups,
        "long_bias": long_bias,
        "short_bias": short_bias,
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

def detect_activity_regime_live():
    """
    Alert-based activity regime (independent from market regime)
    """

    now = time.time()
    cutoff = now - ACTIVITY_WINDOW_HOURS * 3600

    alerts_count = 0
    for q in alert_history.values():
        alerts_count += sum(1 for ts in q if ts >= cutoff)

    if alerts_count <= ACTIVITY_CALM_MAX:
        regime = "CALM"
    elif alerts_count <= ACTIVITY_FRAGILE_MAX:
        regime = "FRAGILE_CALM"
    else:
        regime = "STRESS"

    return {
        "regime": regime,
        "alerts": alerts_count,
        "window_h": ACTIVITY_WINDOW_HOURS,
    }

def detect_activity_transition(prev, current):
    if prev is None:
        return None

    if prev != current:
        return f"{prev} → {current}"

    return None


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

        global last_activity_ts

        if now_ts - last_activity_ts >= ACTIVITY_REGIME_INTERVAL:
            activity = detect_activity_regime_live()
            global last_activity_regime

            transition = detect_activity_transition(
                last_activity_regime,
                activity["regime"]
            )
            
            if transition:
                log_event("activity_transition", {
                    "ts": now_ts,
                    "from": last_activity_regime,
                    "to": activity["regime"],
                    "alerts": activity["alerts"],
                    "window_h": activity["window_h"],
                })
            
            last_activity_regime = activity["regime"]

        
            log_event("activity_regime", {
                "ts": now_ts,
                "regime": activity["regime"],
                "alerts": activity["alerts"],
                "window_h": activity["window_h"],
            })
        
            last_activity_ts = now_ts


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

                result = risk.calculate_risk(
                    f,
                    pf,
                    pressure_ratio,
                    oi_vals,
                    liq,
                    LIQ_THRESHOLDS[symbol],
                    price,
                    liq_sides
                )
                score, direction, reasons, funding_spike, oi_spike, risk_driver = result
                cache[symbol] = (score, direction, reasons, risk_driver)

                log_event("risk_eval", {
                    "symbol": symbol,
                    "risk": score,
                    "direction": direction,
                    "risk_driver": risk_driver,
                    "funding": f,
                    "oi_spike": oi_spike,
                    "funding_spike": funding_spike,
                    "liq": liq,
                })

                global LAST_RISK_EVAL_TS
                LAST_RISK_EVAL_TS = int(time.time())

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
                        await enqueue_message(chat, text)

                    log_event("alert_sent", {
                        "ts": now_ts,
                        "symbol": symbol,
                        "risk": score,
                        "direction": direction,
                        "confidence": confidence,
                        "type": "HARD",
                        "chat_id": "broadcast",
                        "risk_driver": risk_driver,
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
                        await enqueue_message(chat, text)

                    log_event("alert_sent", {
                        "ts": now_ts,
                        "symbol": symbol,
                        "risk": score,
                        "direction": direction,
                        "confidence": confidence,
                        "type": "BUILDUP",
                        "chat_id": "broadcast",
                        "risk_driver": risk_driver,
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
        "Livermore monitors crypto market risk and crowd behavior in real time.\n\n"
        "It tracks:\n"
        "• Risk buildup\n"
        "• Crowd imbalance (long/short pressure)\n"
        "• Market stress regimes\n\n"
        "Important:\n"
        "• This is NOT a trading signal bot\n"
        "• It does NOT predict price\n"
        "• It provides context, not advice\n\n"
        "If Livermore is silent — the market is calm.\n"
        "If alerts appear — something is changing.\n\n"
        "Experimental system."
    )


@dp.message_handler(commands=["philosophy"])
async def philosophy_cmd(message: types.Message):
    await message.reply(
        "Markets move because of people, not indicators.\n\n"
        "This bot does not chase price.\n"
        "It observes stress, imbalance and crowd behavior.\n\n"
        "Risk appears before direction.\n"
        "Silence is a signal."
    )


@dp.message_handler(commands=["start"])
async def start_cmd(message: types.Message):
    ensure_chat(message.chat.id)
    await message.reply(
        "Hi. I am Livermore, a crypto market risk monitoring bot.\n\n"
        "Tap “📋 Commands” to see what I can do.",
        reply_markup=main_kb
    )


@dp.message_handler(commands=["commands"])
async def commands_cmd(message: types.Message):
    await message.reply(
        "📋 Commands:\n\n"
        "/risk — market risk overview\n"
        "/risk BTC — current risk snapshot\n"
        "/risk BTC full — extended market context\n"
        "/risk BTC debug — technical data\n\n"
        "/regime — market-level risk state\n"
        "/about — what this bot is\n"
        "/help — how to read the data"
    )


@dp.message_handler(commands=["help"])
async def help_cmd(message: types.Message):
    await message.reply(
        "ℹ️ About this bot\n\n"
        "This bot tracks MARKET RISK — not price, not signals.\n"
        "It provides context about crowd behavior and systemic stress.\n\n"

        "Core concepts:\n"
        "• Risk — aggregate market stress (0–10)\n"
        "• Direction — side where the market is vulnerable (LONG / SHORT)\n"
        "• Confidence — reliability of the risk assessment\n\n"

        "Crowd & positioning:\n"
        "• Pressure — long/short participation ratio\n"
        "• Crowd imbalance — asymmetric positioning\n\n"

        "Market level:\n"
        "• Market regime — high-level market state\n"
        "  (CALM / CROWD_IMBALANCE / STRESS)\n\n"

        "Important:\n"
        "• This is NOT a trading signal bot\n"
        "• It does NOT predict price\n"
        "• It provides observations, not advice\n\n"

        "If the bot is silent — the market is calm.\n"
        "If alerts appear — something is changing.\n\n"

        "This is a market risk log, not a forecast."
    )


@dp.message_handler(lambda m: m.text and "Commands" in m.text)
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

    score, direction, reasons, risk_driver = cache[symbol]
    snap = build_market_snapshot(symbol)
    disp = display_symbol(symbol)
    f = ws.funding.get(symbol)

    if len(parts) >= 3 and parts[2].lower() == "full":
        activity = detect_activity_regime_live()
        now_ts = int(time.time())
        cutoff = now_ts - ALERT_WINDOW_HOURS * 3600
    
        history = alert_history.get(symbol, [])
        alerts_last = sum(1 for ts in history if ts >= cutoff)
    
        text = (
            f"{disp}\n\n"
            f"Risk: {score}/10 ({direction or 'NEUTRAL'})\n"
            f"Risk driver: {risk_driver}\n"
            f"Confidence: {meta.confidence_level(meta.calculate_confidence(score, direction, False, False, 0, None, {}))}\n\n"
    
            f"Market context:\n"
            f"• Funding: {percent_funding(f)}\n"
            f"• Pressure: {build_market_snapshot(symbol).splitlines()[2].replace('Pressure: ', '')}\n\n"
    
            f"Activity context:\n"
            f"• BUILDUP alerts (last {activity['window_h']}h): {activity['alerts']}\n"
            f"• Activity regime: {activity['regime']}\n\n"
            
            f"Risk activity:\n"
            f"• Buildups (last {ALERT_WINDOW_HOURS}h): {alerts_last}\n\n"

    
            f"Interpretation:\n"
            f"Crowded positioning detected.\n"
            f"Asymmetric risk is building.\n\n"
    
            f"This is a market risk log, not a forecast."
        )
    
        await message.reply(text)
        return


    await message.reply(
        f"{disp}\nRisk: {score}/10 ({direction or 'NEUTRAL'})\n"
        f"Funding: {qualitative_funding(f)}\n\n{snap}"
    )


@dp.message_handler(commands=["regime"])
async def regime_cmd(message: types.Message):
    activity = detect_activity_regime_live()
    state = build_market_state()
    regime = detect_market_regime(state)

    text = (
        f"🌍 Market Regime: {regime}\n\n"
        f"Market metrics:\n"
        f"• Average risk: {state['avg_risk']}\n"
        f"• Risk buildups (last 3h): {state['buildup_count']}\n"
        f"• Long bias: {state['long_bias']}\n"
        f"• Short bias: {state['short_bias']}\n"
        f"• Symbols tracked: {state['symbols']}\n\n"
    )

    text += (
        f"Activity (last {activity['window_h']}h):\n"
        f"• BUILDUP alerts: {activity['alerts']}\n"
        f"• Activity regime: {activity['regime']}\n\n"
    )

    if regime == "CALM":
        text += (
            "Interpretation:\n"
            "Systemic stress is low.\n"
            "Crowd positioning appears balanced.\n"
        )
    elif regime == "CROWD_IMBALANCE":
        text += (
            "Interpretation:\n"
            "Crowded positioning dominates.\n"
            "Asymmetric risk is building beneath the surface.\n"
        )
    elif regime == "STRESS":
        text += (
            "Interpretation:\n"
            "Systemic stress is elevated.\n"
            "Volatility expansion becomes more likely.\n"
        )
    else:
        text += (
            "Interpretation:\n"
            "Market conditions are mixed.\n"
            "Signals lack clear alignment.\n"
        )

    text += "\nThis is a market risk observation, not a forecast."

    await message.reply(text)


async def send_current_risk(chat_id):
    lines = [
        f"{display_symbol(s)}: {v[0]} ({v[1] or 'NEUTRAL'})"
        for s, v in cache.items()
    ]
    await enqueue_message(chat_id, "\n".join(lines))


async def risk_loop_watchdog():
    while True:
        await asyncio.sleep(120)  # каждые 2 минуты

        if LAST_RISK_EVAL_TS == 0:
            continue

        delta = time.time() - LAST_RISK_EVAL_TS

        if delta > 330:  # 5 минут без risk_eval
            log_event("system_warning", {
                "type": "RISK_LOOP_STALL",
                "last_risk_eval_sec_ago": int(delta),
            })
            for chat in active_chats:
                await enqueue_message(
                    chat,
                    "⚠️ System warning: risk loop stalled. Data may be outdated."
                )


# ---------------- OUTBOX ----------------

async def enqueue_message(chat_id, text):
    await message_queue.put({"chat_id": chat_id, "text": text})


async def message_worker():
    while True:
        payload = await message_queue.get()
        chat_id = payload["chat_id"]
        text = payload["text"]

        for attempt in range(1, SEND_RETRY_LIMIT + 1):
            try:
                await bot.send_message(chat_id, text)
                break
            except BotBlocked:
                active_chats.discard(chat_id)
                break
            except RetryAfter as exc:
                await asyncio.sleep(exc.timeout)
            except (NetworkError, TelegramAPIError):
                backoff = min(2 ** attempt, 30)
                await asyncio.sleep(backoff)
        await asyncio.sleep(SEND_DELAY_SECONDS)
        message_queue.task_done()


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
    asyncio.create_task(risk_loop_watchdog())
    asyncio.create_task(message_worker())

if __name__ == "__main__":
    threading.Thread(target=start_http, daemon=True).start()
    executor.start_polling(dp, skip_updates=True, on_startup=on_startup)


