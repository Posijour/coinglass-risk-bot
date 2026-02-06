from config import FUNDING_EXTREME_THRESHOLD, FUNDING_SPIKE_THRESHOLD, OI_SPIKE_THRESHOLD

def calculate_risk(
    funding,
    prev_funding,
    long_ratio,
    oi_window,
    liquidations,
    liq_threshold,
    price=None,
    liq_sides=None
):
    score = 0
    reasons = []
    direction_votes = {"LONG": 0, "SHORT": 0}

    # FUNDING
    if funding is not None:
        if funding > FUNDING_EXTREME_THRESHOLD:
            score += 3
            direction_votes["LONG"] += 1
            reasons.append("Funding экстремально положительный")

        if funding < -FUNDING_EXTREME_THRESHOLD:
            score += 3
            direction_votes["SHORT"] += 1
            reasons.append("Funding экстремально отрицательный")

    funding_spike = (
        funding is not None
        and prev_funding is not None
        and abs(funding - prev_funding) > FUNDING_SPIKE_THRESHOLD
    )

    # LONG / SHORT
    if long_ratio > 0.85:
        score += 3
        direction_votes["LONG"] += 2
        reasons.append("Экстремальный перекос в лонги")
    elif long_ratio > 0.7:
        score += 2
        direction_votes["LONG"] += 1
        reasons.append("Перекос в лонги")

    if long_ratio < 0.15:
        score += 3
        direction_votes["SHORT"] += 2
        reasons.append("Экстремальный перекос в шорты")
    elif long_ratio < 0.3:
        score += 2
        direction_votes["SHORT"] += 1
        reasons.append("Перекос в шорты")

    # OI TREND + SPIKE
    oi_spike = False
    if len(oi_window) >= 2:
        oi_start = oi_window[0][1]
        oi_end = oi_window[-1][1]

        if oi_end > oi_start:
            score += 3
            reasons.append("OI растёт")
        elif oi_end < oi_start:
            score += 3
            reasons.append("OI падает")

        if oi_start > 0:
            if abs(oi_end - oi_start) / oi_start > OI_SPIKE_THRESHOLD:
                oi_spike = True
                if price is not None:
                    reasons.append("OI spike при движении цены")

    # LIQUIDATIONS
    if liquidations > liq_threshold:
        score += 3
        reasons.append("Крупные ликвидации")

        if liq_sides:
            if liq_sides.get("long", 0) > liq_sides.get("short", 0):
                reasons.append("Преобладают ликвидации лонгов")
            else:
                reasons.append("Преобладают ликвидации шортов")

    direction = None
    if direction_votes["LONG"] != direction_votes["SHORT"]:
        direction = max(direction_votes, key=direction_votes.get)
    elif long_ratio >= 0.7:
        direction = "LONG"
    elif long_ratio <= 0.3:
        direction = "SHORT"

    risk_driver = detect_risk_driver(
        funding=funding,
        funding_spike=funding_spike,
        long_ratio=long_ratio,
        oi_spike=oi_spike,
        liquidations=liquidations,
        liq_threshold=liq_threshold
    )
    
    return score, direction, reasons, funding_spike, oi_spike, risk_driver

def detect_risk_driver(
    funding,
    funding_spike,
    long_ratio,
    oi_spike,
    liquidations,
    liq_threshold
):
    drivers = []

    # CROWD (long/short imbalance)
    if long_ratio >= 0.7 or long_ratio <= 0.3:
        drivers.append("CROWD")

    # LIQUIDATIONS
    if liquidations > liq_threshold:
        drivers.append("LIQUIDATION")

    # FUNDING
    if funding_spike:
        drivers.append("FUNDING")

    # OPEN INTEREST
    if oi_spike:
        drivers.append("OI")

    if not drivers:
        return "UNKNOWN"

    if len(drivers) == 1:
        return drivers[0]

    return "MIXED"



