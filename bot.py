import asyncio
import os
import time
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from oi_binance import BinanceOIPoller

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
from logger import log_event, now_ts_ms

from datetime import datetime, timedelta, timezone


def send_to_db(event, payload):
    """
    Backward-compatible shim.
    Some runtime environments may still call send_to_db(...)
    from legacy code paths; route it through the centralized logger.
    """
    log_event(event, payload)

LOG_FILE_PATH = "bot_events.jsonl"
LOG_SEND_HOUR_UTC_PLUS_2 = 13
LOG_TIMEZONE = timezone(timedelta(hours=2))

oi_poller = BinanceOIPoller(SYMBOLS, period="5m", window=12)

# --- ACTIVITY REGIME CONFIG ---
ACTIVITY_WINDOW_HOURS = 4
ACTIVITY_CALM_MAX = 2
ACTIVITY_FRAGILE_MAX = 5
last_activity_regime = None
last_activity_transition = None

ALERT_WINDOW_HOURS = 4  # ← можешь менять
alert_history = defaultdict(deque)
recorded_alert_ids = {}
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
last_oi_snapshot = {}

ws_task = None
ws_running = False
MESSAGE_QUEUE_MAXSIZE = 2000
message_queue = asyncio.Queue(maxsize=MESSAGE_QUEUE_MAXSIZE)

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


# ---------------- ALERT HELPERS ----------------

def record_alert_if_first(meta):
    if not meta:
        return

    event_id = meta.get("event_id")
    symbol = meta.get("symbol")
    ts_ms = meta.get("ts_unix_ms")

    if not event_id or not symbol or not ts_ms:
        return

    if event_id in recorded_alert_ids:
        return

    recorded_alert_ids[event_id] = ts_ms
    alert_history[symbol].append(ts_ms)

    cutoff = ts_ms - ALERT_WINDOW_HOURS * 3600 * 1000
    while alert_history[symbol] and alert_history[symbol][0] < cutoff:
        alert_history[symbol].popleft()

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
    oi_vals = oi_poller.oi_window.get(symbol, [])
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

    now_ms = now_ts_ms()
    cutoff = now_ms - ACTIVITY_WINDOW_HOURS * 3600 * 1000
    
    alerts_count = sum(
        1 for q in alert_history.values() for ts in q if ts >= cutoff
    )

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


# ---------------- GLOBAL RISK LOOP ----------------

async def global_risk_loop():
    await asyncio.sleep(10)

    while True:
        global last_regime_ts, current_market_regime

        now_ms = now_ts_ms()
    
        if now_ms - last_regime_ts >= MARKET_REGIME_INTERVAL * 1000:
            state = build_market_state()
            regime = detect_market_regime(state)
        
        
            # логируем ВСЕГДА
            log_event("market_regime", {
                "ts_unix_ms": now_ms,
                "regime": regime,
                **state,
            })
        
            current_market_regime = regime
            last_regime_ts = now_ms

        global last_activity_ts

        if now_ms - last_activity_ts >= ACTIVITY_REGIME_INTERVAL * 1000:
            activity = detect_activity_regime_live()
            global last_activity_regime, last_activity_transition

            if last_activity_regime is None:
                last_activity_regime = activity["regime"]
            else:
                if last_activity_regime != activity["regime"]:
                    last_activity_transition = {
                        "from": last_activity_regime,
                        "to": activity["regime"],
                        "ts_unix_ms": now_ms,
                    }
        
                    log_event("activity_transition", {
                        "ts_unix_ms": now_ms,
                        "from": last_activity_regime,
                        "to": activity["regime"],
                        "alerts": activity["alerts"],
                        "window_h": activity["window_h"],
                    })
        
                    last_activity_regime = activity["regime"]

        
            log_event("activity_regime", {
                "ts_unix_ms": now_ms,
                "regime": activity["regime"],
                "alerts": activity["alerts"],
                "window_h": activity["window_h"],
            })
        
            last_activity_ts = now_ms


        for symbol in SYMBOLS:
            try:
                now_ms = now_ts_ms()

                f = ws.funding.get(symbol)
                pf = last_funding.get(symbol)

                if f is not None:
                    prev_funding[symbol] = pf
                    last_funding[symbol] = f
                    last_funding_ts[symbol] = now_ms

                oi_vals = oi_poller.oi_window.get(symbol, [])
                oi_for_risk = oi_vals

                if len(oi_vals) == 1:
                    prev_oi_snapshot = last_oi_snapshot.get(symbol)
                    if prev_oi_snapshot and prev_oi_snapshot > 0:
                        oi_for_risk = [(now_ms - INTERVAL_SECONDS * 1000, prev_oi_snapshot), oi_vals[0]]

                if oi_vals:
                    last_oi_snapshot[symbol] = oi_vals[-1][1]

                liq = ws.liquidations.get(symbol, 0)

                ls = ws.long_short_ratio.get(symbol, {"long": 0, "short": 0})
                total = ls["long"] + ls["short"]
                current_ratio = ls["long"] / total if total else 0.5

                pressure_ratio = current_ratio

                price = getattr(ws, "mark_price", {}).get(symbol)
                liq_sides = getattr(ws, "liq_sides", {}).get(symbol, {})

                result = risk.calculate_risk(
                    f,
                    pf,
                    pressure_ratio,
                    oi_for_risk,
                    liq,
                    LIQ_THRESHOLDS[symbol],
                    price,
                    liq_sides
                )
                score, direction, reasons, funding_spike, oi_spike, risk_driver = result
                cache[symbol] = (score, direction, reasons, risk_driver)

                oi_change_pct = 0.0
                if len(oi_for_risk) >= 2 and oi_for_risk[0][1] > 0:
                    oi_change_pct = abs(oi_for_risk[-1][1] - oi_for_risk[0][1]) / oi_for_risk[0][1]

                if score == 0:
                    risk_eval_payload = {
                        "ts_unix_ms": now_ms,
                        "symbol": symbol,
                        "funding": f,
                        "price": price,
                    }
                else:
                    risk_eval_payload = {
                        "ts_unix_ms": now_ms,
                        "symbol": symbol,
                        "risk": score,
                        "direction": direction,
                        "risk_driver": risk_driver,
                        "funding": f,
                        "funding_spike": funding_spike,
                        "oi_spike": oi_spike,
                        "liq": liq,
                        "price": price,
                    }

                log_event("risk_eval", risk_eval_payload)

                global LAST_RISK_EVAL_TS
                LAST_RISK_EVAL_TS = now_ms

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

                hard_candidate = bool(
                    score >= HARD_ALERT_LEVEL
                    and direction
                    and confidence >= 3
                )
                buildup_candidate = score >= EARLY_ALERT_LEVEL
                
                now_ts = int(time.time())
                
                # ---------- HARD ALERT ----------
                if score >= HARD_ALERT_LEVEL and direction and confidence >= 3:
                    text = (
                        f"🚨 HARD RISK ALERT {symbol}\n\n"
                        f"Risk: {score}\n"
                        f"Direction: {direction}\n"
                        f"Confidence: {conf_level}"
                    )
                
                    alert_meta = {
                        "ts_unix_ms": now_ms,
                        "symbol": symbol,
                        "risk": score,
                        "direction": direction,
                        "confidence": confidence,
                        "type": "HARD",
                        "event_id": f"{symbol}:{now_ms}:HARD",
                        "chat_id": "broadcast",
                        "risk_driver": risk_driver,
                        "price": price,
                    }
                    
                    for chat in active_chats.copy():
                        await enqueue_message(chat, text, meta=alert_meta)

                
                # ---------- BUILDUP ALERT ----------
                elif score >= EARLY_ALERT_LEVEL:
                    cutoff = now_ms - ALERT_WINDOW_HOURS * 3600 * 1000
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
                
                    alert_meta = {
                        "ts_unix_ms": now_ms,
                        "symbol": symbol,
                        "risk": score,
                        "direction": direction,
                        "confidence": confidence,
                        "type": "BUILDUP",
                        "event_id": f"{symbol}:{now_ms}:BUILDUP",
                        "chat_id": "broadcast",
                        "price": price,
                    }
                    for chat in active_chats.copy():
                        await enqueue_message(chat, text, meta=alert_meta)
                    
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
        "ts_unix_ms": now_ts_ms(),
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
    display_driver = (
        risk_driver
        if risk_driver and score > 0
        else "NONE"
    )
    snap = build_market_snapshot(symbol)
    disp = display_symbol(symbol)
    f = ws.funding.get(symbol)

    if len(parts) >= 3 and parts[2].lower() == "full":
        activity = detect_activity_regime_live()
        now_ms = now_ts_ms()
        cutoff = now_ms - ALERT_WINDOW_HOURS * 3600 * 1000
    
        history = alert_history.get(symbol, [])
        alerts_last = sum(1 for ts in history if ts >= cutoff)
    
        text = (
            f"{disp}\n\n"
            f"Risk: {score}/10 ({direction or 'NEUTRAL'})\n"
            f"Risk driver: {display_driver}\n"
            f"Confidence: {meta.confidence_level(meta.calculate_confidence(score, direction, False, False, 0, None, {}))}\n\n"
    
            f"Market context:\n"
            f"• Funding: {percent_funding(f)}\n"
             f"• Pressure: {build_market_snapshot(symbol).splitlines()[2].replace('Pressure: ', '')}\n"
            f"• Alerts (last {activity['window_h']}h): {activity['alerts']}\n"
            f"• Activity regime: {activity['regime']}\n"
        )
        
        if last_activity_transition:
            delta_h = int((now_ts_ms() - last_activity_transition["ts_unix_ms"]) / 1000 / 3600)
            text += (
                f"• Last activity transition: "
                f"{last_activity_transition['from']} → "
                f"{last_activity_transition['to']} "
                f"({delta_h}h ago)\n"
            )
        
        text += (
            f"\nTicker activity:\n"
            f"• Buildups (last {ALERT_WINDOW_HOURS}h): {alerts_last}\n\n"

    
            )
        
        if score > 0:
            text += (
                f"Interpretation:\n"
                f"Crowded positioning detected.\n"
                f"Asymmetric risk is building.\n\n"
            )

        text += "This is a market risk log, not a forecast."
    
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
        f"• Alerts: {activity['alerts']}\n"
        f"• Activity regime: {activity['regime']}\n"
    )
    
    if last_activity_transition:
        delta_h = int((now_ts_ms() - last_activity_transition["ts_unix_ms"]) / 1000 / 3600)
    
        text += (
            f"• Last transition: "
            f"{last_activity_transition['from']} → "
            f"{last_activity_transition['to']} "
            f"({delta_h}h ago)\n"
        )
    
    text += "\n"

    # ---- Interpretation ----
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

        delta_sec = (now_ts_ms() - LAST_RISK_EVAL_TS) / 1000

        if delta_sec > 330:  # 5 минут без risk_eval
            log_event("system_warning", {
                "type": "RISK_LOOP_STALL",
                "last_risk_eval_sec_ago": int(delta_sec),
            })
            for chat in active_chats:
                await enqueue_message(
                    chat,
                    "⚠️ System warning: risk loop stalled. Data may be outdated."
                )


# ---------------- OUTBOX ----------------

async def enqueue_message(chat_id, text, meta=None):
    payload = {"chat_id": chat_id, "text": text, "meta": meta}
    try:
        message_queue.put_nowait(payload)
    except asyncio.QueueFull:
        log_event("queue_drop", {
            "chat_id": chat_id,
            "reason": "message_queue_full",
        })


async def message_worker():
    while True:
        payload = await message_queue.get()
        chat_id = payload["chat_id"]
        text = payload["text"]
        meta = payload.get("meta")

        sent = False
        for attempt in range(1, SEND_RETRY_LIMIT + 1):
            try:
                await bot.send_message(chat_id, text)
                sent = True
                if meta:
                    record_alert_if_first(meta)
                    log_event("alert_sent", meta)
                break
            except BotBlocked:
                active_chats.discard(chat_id)
                break
            except RetryAfter as exc:
                await asyncio.sleep(exc.timeout)
            except (NetworkError, TelegramAPIError):
                backoff = min(2 ** attempt, 30)
                await asyncio.sleep(backoff)
        if meta and not sent:
            log_event("alert_fail", meta)
        await asyncio.sleep(SEND_DELAY_SECONDS)
        message_queue.task_done()


async def send_and_rotate_logs():
    if not os.path.isfile(LOG_FILE_PATH):
        return

    if os.path.getsize(LOG_FILE_PATH) == 0:
        return  # нечего отправлять

    for chat in active_chats.copy():
        try:
            await bot.send_document(
                chat_id=chat,
                document=open(LOG_FILE_PATH, "rb"),
                caption="📎 Daily risk logs (last 24h)"
            )
        except Exception as e:
            print("LOG SEND ERROR:", e)
            
    log_event("daily_log_sent", {
        "ts_unix_ms": now_ts_ms(),
        "size_bytes": os.path.getsize(LOG_FILE_PATH),
    })

    # ---- ROTATE (clear file) ----
    try:
        with open(LOG_FILE_PATH, "w", encoding="utf-8") as f:
            pass  # truncate
        print("Log file rotated")
    except Exception as e:
        print("LOG ROTATE ERROR:", e)


async def daily_log_scheduler():
    while True:
        now = datetime.now(LOG_TIMEZONE)

        target = now.replace(
            hour=LOG_SEND_HOUR_UTC_PLUS_2,
            minute=0,
            second=0,
            microsecond=0,
        )

        if now >= target:
            target += timedelta(days=1)

        sleep_seconds = (target - now).total_seconds()
        await asyncio.sleep(sleep_seconds)

        await send_and_rotate_logs()

        # защита от двойного запуска
        await asyncio.sleep(60)


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

async def oi_loop():
    while True:
        try:
            oi_poller.update()
        except Exception as e:
            log_event("oi_poll_error", {
                "ts_unix_ms": now_ts_ms(),
                "error": str(e),
            })
        await asyncio.sleep(60)


# ---------------- STARTUP ----------------
async def on_startup(dp):
    global ws_task
    await bot.delete_webhook(drop_pending_updates=True)
    ws_task = asyncio.create_task(start_ws_safe())
    asyncio.create_task(ws_watchdog())
    asyncio.create_task(global_risk_loop())
    asyncio.create_task(risk_loop_watchdog())
    asyncio.create_task(message_worker())
    asyncio.create_task(daily_log_scheduler())
    asyncio.create_task(oi_loop())
    
if __name__ == "__main__":
    threading.Thread(target=start_http, daemon=True).start()
    executor.start_polling(dp, skip_updates=True, on_startup=on_startup)





