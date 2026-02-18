import asyncio
import threading
import time
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, HTTPServer

from oi_binance import BinanceOIPoller

import divergence
import meta
import risk
import ws_binance as ws
from config import *
from logger import log_event, now_ts_ms


oi_poller = BinanceOIPoller(SYMBOLS, period="5m", window=12)

ACTIVITY_WINDOW_HOURS = 4
ACTIVITY_CALM_MAX = 2
ACTIVITY_FRAGILE_MAX = 5
last_activity_regime = None

ALERT_WINDOW_HOURS = 4
alert_history = defaultdict(deque)
recorded_alert_ids = {}
LAST_RISK_EVAL_TS = 0

MARKET_REGIME_INTERVAL = 900
last_regime_ts = 0
current_market_regime = "UNKNOWN"
ACTIVITY_REGIME_INTERVAL = 900
last_activity_ts = 0
STRESS_CONFIRM_TICKS = 3
STRESS_EXIT_TICKS = 2
stress_confirm_counter = 0
stress_exit_counter = 0
CROWD_CONFIRM_TICKS = 2
crowd_confirm_counter = 0

last_funding = {}
prev_funding = {}
last_oi_snapshot = {}
price_history = {s: deque(maxlen=3) for s in SYMBOLS}

ws_task = None
ws_running = False



def record_alert_if_first(alert_meta):
    if not alert_meta:
        return

    event_id = alert_meta.get("event_id")
    symbol = alert_meta.get("symbol")
    ts_ms = alert_meta.get("ts_unix_ms")

    if not event_id or not symbol or not ts_ms or event_id in recorded_alert_ids:
        return

    recorded_alert_ids[event_id] = ts_ms
    alert_history[symbol].append(ts_ms)

    cutoff = ts_ms - ALERT_WINDOW_HOURS * 3600 * 1000
    while alert_history[symbol] and alert_history[symbol][0] < cutoff:
        alert_history[symbol].popleft()


def emit_alert(text, alert_meta):
    record_alert_if_first(alert_meta)
    log_event("alert_sent", {"text": text, **alert_meta})


def detect_activity_regime_live():
    now_ms = now_ts_ms()
    cutoff = now_ms - ACTIVITY_WINDOW_HOURS * 3600 * 1000

    alerts_count = sum(1 for q in alert_history.values() for ts in q if ts >= cutoff)

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


def build_market_state():
    risks = []
    directions = []

    raw_buildups = 0
    alert_buildups = 0

    now_ms = now_ts_ms()
    cutoff = now_ms - ALERT_WINDOW_HOURS * 3600 * 1000

    for _, data in cache.items():
        score, direction = data[0], data[1]

        if score is not None:
            risks.append(score)
        if direction:
            directions.append(direction)
        if score is not None and score >= EARLY_ALERT_LEVEL:
            raw_buildups += 1

    for q in alert_history.values():
        alert_buildups += sum(1 for ts in q if ts >= cutoff)

    avg_risk = sum(risks) / len(risks) if risks else 0

    return {
        "avg_risk": round(avg_risk, 2),
        "risk_buildups": raw_buildups,
        "risk_alerts": alert_buildups,
        "long_bias": directions.count("LONG"),
        "short_bias": directions.count("SHORT"),
        "symbols": len(cache),
    }


def detect_market_regime(state):
    avg_risk = state["avg_risk"]
    buildups = state["risk_buildups"]
    alerts = state["risk_alerts"]

    if avg_risk < 1 and buildups == 0:
        return "CALM"
    if avg_risk >= 2 and buildups == 0 and alerts == 0:
        return "LATENT_STRESS"
    if buildups >= 3 and avg_risk < 2:
        return "CROWD_IMBALANCE"
    if avg_risk >= 2 and buildups >= 3:
        return "STRESS"
    return "NEUTRAL"


async def start_ws_safe():
    global ws_running
    if ws_running:
        return
    ws_running = True
    try:
        await ws.binance_ws()
    finally:
        ws_running = False




def map_regime_for_divergence(regime):
    if regime == "CALM":
        return "CALM"
    if regime in ("STRESS", "CROWD_IMBALANCE"):
        return "OVERHEATED"
    if regime in ("LATENT_STRESS", "NEUTRAL"):
        return "BUILDUP"
    return "BUILDUP"


def detect_price_trend(prices):
    if len(prices) < 2:
        return "FLAT"

    start = prices[0]
    end = prices[-1]
    if start <= 0:
        return "FLAT"

    delta = (end - start) / start
    if delta > 0.0005:
        return "UP"
    if delta < -0.0005:
        return "DOWN"
    return "FLAT"


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


cache = {}


async def global_risk_loop():
    await asyncio.sleep(10)

    while True:
        global last_regime_ts, current_market_regime

        now_ms = now_ts_ms()

        if now_ms - last_regime_ts >= MARKET_REGIME_INTERVAL * 1000:
            global stress_confirm_counter, stress_exit_counter, crowd_confirm_counter

            state = build_market_state()
            candidate = detect_market_regime(state)

            if candidate == "STRESS":
                stress_confirm_counter += 1
            else:
                stress_confirm_counter = 0

            if current_market_regime == "STRESS" and candidate != "STRESS":
                stress_exit_counter += 1
            else:
                stress_exit_counter = 0

            if candidate == "CROWD_IMBALANCE":
                crowd_confirm_counter += 1
            else:
                crowd_confirm_counter = 0

            if candidate == "STRESS":
                regime = "STRESS" if stress_confirm_counter >= STRESS_CONFIRM_TICKS else "LATENT_STRESS"
            elif current_market_regime == "STRESS":
                regime = candidate if stress_exit_counter >= STRESS_EXIT_TICKS else "STRESS"
            elif candidate == "CROWD_IMBALANCE":
                regime = "CROWD_IMBALANCE" if crowd_confirm_counter >= CROWD_CONFIRM_TICKS else "CALM"
            else:
                regime = candidate

            log_event(
                "market_regime",
                {
                    "regime": regime,
                    "candidate": candidate,
                    "stress_enter_ticks": stress_confirm_counter,
                    "stress_exit_ticks": stress_exit_counter,
                    "crowd_ticks": crowd_confirm_counter,
                    **state,
                },
            )

            current_market_regime = regime
            last_regime_ts = now_ms

        global last_activity_ts, last_activity_regime

        if now_ms - last_activity_ts >= ACTIVITY_REGIME_INTERVAL * 1000:
            activity = detect_activity_regime_live()

            if last_activity_regime is None:
                last_activity_regime = activity["regime"]
            elif last_activity_regime != activity["regime"]:
                log_event(
                    "activity_transition",
                    {
                        "from": last_activity_regime,
                        "to": activity["regime"],
                        "alerts": activity["alerts"],
                        "window_h": activity["window_h"],
                    },
                )
                last_activity_regime = activity["regime"]

            log_event(
                "activity_regime",
                {
                    "regime": activity["regime"],
                    "alerts": activity["alerts"],
                    "window_h": activity["window_h"],
                },
            )

            last_activity_ts = now_ms

        for symbol in SYMBOLS:
            try:
                now_ms = now_ts_ms()

                f = ws.funding.get(symbol)
                pf = last_funding.get(symbol)

                if f is not None:
                    prev_funding[symbol] = pf
                    last_funding[symbol] = f

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
                pressure_ratio = ls["long"] / total if total else 0.5

                price = getattr(ws, "mark_price", {}).get(symbol)
                liq_sides = getattr(ws, "liq_sides", {}).get(symbol, {})

                if price is not None:
                    price_history[symbol].append(price)

                result = risk.calculate_risk(
                    f,
                    pf,
                    pressure_ratio,
                    oi_for_risk,
                    liq,
                    LIQ_THRESHOLDS[symbol],
                    price,
                    liq_sides,
                )
                score, direction, reasons, funding_spike, oi_spike, risk_driver = result
                cache[symbol] = (score, direction, reasons, risk_driver)

                risk_eval_payload = {
                    "symbol": symbol,
                    "risk": score,
                    "funding": f,
                    "price": price,
                }
                if score != 0:
                    risk_eval_payload.update(
                        {
                            "direction": direction,
                            "risk_driver": risk_driver,
                            "funding_spike": funding_spike,
                            "oi_spike": oi_spike,
                            "liq": liq,
                        }
                    )
                log_event("risk_eval", risk_eval_payload)

                global LAST_RISK_EVAL_TS
                LAST_RISK_EVAL_TS = now_ms

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
                    liq_sides,
                )
                if funding_spike:
                    confidence += 1
                if oi_spike:
                    confidence += 1
                confidence = min(confidence, 5)
                conf_level = meta.confidence_level(confidence)

                if score >= HARD_ALERT_LEVEL and direction and confidence >= 3:
                    text = (
                        f"🚨 HARD RISK ALERT {symbol}\n\n"
                        f"Risk: {score}\n"
                        f"Direction: {direction}\n"
                        f"Confidence: {conf_level}"
                    )
                    emit_alert(
                        text,
                        {
                            "symbol": symbol,
                            "risk": score,
                            "direction": direction,
                            "confidence": confidence,
                            "type": "HARD",
                            "event_id": f"{symbol}:{now_ms}:HARD",
                            "ts_unix_ms": now_ms,
                            "risk_driver": risk_driver,
                            "price": price,
                        },
                    )
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

                    emit_alert(
                        text,
                        {
                            "symbol": symbol,
                            "risk": score,
                            "direction": direction,
                            "confidence": confidence,
                            "type": "BUILDUP",
                            "event_id": f"{symbol}:{now_ms}:BUILDUP",
                            "ts_unix_ms": now_ms,
                            "price": price,
                        },
                    )

                divergence_state = map_regime_for_divergence(current_market_regime)
                price_trend = detect_price_trend(price_history[symbol])
                divergences = divergence.detect_divergence(
                    symbol=symbol,
                    state=divergence_state,
                    pressure_ratio=pressure_ratio,
                    oi_window=oi_for_risk,
                    price_trend=price_trend,
                    liquidations=liq,
                )

                for idx, div_text in enumerate(divergences):
                    emit_alert(
                        f"🧭 DIVERGENCE {symbol}\n\n{div_text}",
                        {
                            "symbol": symbol,
                            "type": "DIVERGENCE",
                            "event_id": f"{symbol}:{now_ms}:DIV:{idx}",
                            "ts_unix_ms": now_ms,
                            "market_regime": current_market_regime,
                            "divergence_state": divergence_state,
                            "price_trend": price_trend,
                            "pressure_ratio": round(pressure_ratio, 4),
                            "risk": score,
                            "price": price,
                        },
                    )

            except Exception as e:
                log_event("risk_loop_error", {"symbol": symbol, "error": str(e)})

        await asyncio.sleep(INTERVAL_SECONDS)


async def risk_loop_watchdog():
    while True:
        await asyncio.sleep(120)
        if LAST_RISK_EVAL_TS == 0:
            continue

        delta_sec = (now_ts_ms() - LAST_RISK_EVAL_TS) / 1000
        if delta_sec > 330:
            log_event(
                "system_warning",
                {
                    "type": "RISK_LOOP_STALL",
                    "last_risk_eval_sec_ago": int(delta_sec),
                },
            )


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
            log_event("oi_poll_error", {"ts_unix_ms": now_ts_ms(), "error": str(e)})
        await asyncio.sleep(60)


async def main():
    global ws_task
    ws_task = asyncio.create_task(start_ws_safe())
    asyncio.create_task(ws_watchdog())
    asyncio.create_task(global_risk_loop())
    asyncio.create_task(risk_loop_watchdog())
    asyncio.create_task(oi_loop())

    await asyncio.Event().wait()


if __name__ == "__main__":
    threading.Thread(target=start_http, daemon=True).start()
    asyncio.run(main())
