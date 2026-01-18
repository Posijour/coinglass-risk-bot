import time
import ws_binance as ws


# =========================
# QUALITY
# =========================

def stream_quality(symbol):
    """
    Диагностика здоровья потоков данных.
    НЕ влияет на сигналы.
    """

    now = time.time()
    checks = {}

    # WebSocket жив
    checks["ws"] = (
        symbol in ws.last_update
        and now - ws.last_update[symbol] < 180
    )

    # Funding
    checks["funding"] = ws.funding.get(symbol) is not None

    # Open Interest
    checks["oi"] = len(ws.oi_window.get(symbol, [])) >= 2

    # Trades (long/short ratio)
    ls = ws.long_short_ratio.get(symbol)
    checks["trades"] = bool(ls and (ls["long"] + ls["short"]) > 0)

    # Liquidations
    checks["liq"] = ws.liquidations.get(symbol, 0) > 0

    # Price
    checks["price"] = symbol in getattr(ws, "mark_price", {})

    score = sum(checks.values())
    max_score = len(checks)

    if score >= 5:
        level = "GOOD"
    elif score >= 3:
        level = "MEDIUM"
    else:
        level = "LOW"

    return {
        "score": score,
        "max": max_score,
        "level": level,
        "checks": checks
    }


# =========================
# STATE
# =========================

def detect_state(score, oi_spike, funding_spike, liquidations):
    """
    Определяет режим рынка.
    НЕ влияет на risk score.
    """

    if score < 3:
        return "CALM"

    if score < 6:
        if liquidations > 0:
            return "UNWIND"
        return "BUILDUP"

    if score >= 6 and oi_spike:
        if funding_spike and liquidations > 0:
            return "CHAOTIC"
        return "OVERHEATED"

    return "BUILDUP"


# =========================
# CONFIDENCE
# =========================

def calculate_confidence(
    score,
    direction,
    oi_spike,
    funding_spike,
    liquidations,
    price=None,
    liq_sides=None
):
    """
    Уверенность сигнала = согласованность факторов.
    НЕ равно score.
    """

    confidence = 0

    if score >= 4:
        confidence += 1

    if direction:
        confidence += 1

    if oi_spike:
        confidence += 1

    if funding_spike:
        confidence += 1

    if liquidations > 0:
        confidence += 1

    return min(confidence, 5)


def confidence_level(confidence):
    if confidence <= 2:
        return "LOW"
    if confidence == 3:
        return "MEDIUM"
    if confidence == 4:
        return "HIGH"
    return "VERY HIGH"
