def calculate_risk(funding, long_ratio, oi_change, liquidations):
    score = 0
    reasons = []
    direction = None  # "LONG" или "SHORT"

    # ----- LONG RISK (рынок перекуплен) -----
    if funding > 0.02:
        score += 2
        direction = "LONG"
        reasons.append("Funding rate экстремально положительный")

    if long_ratio > 0.7:
        score += 2
        direction = "LONG"
        reasons.append("Сильный перекос в лонги")

    if oi_change < 0:
        score += 1
        reasons.append("OI падает — движение без поддержки")

    # ----- SHORT RISK (рынок перепродан) -----
    if funding < -0.02:
        score += 2
        direction = "SHORT"
        reasons.append("Funding rate экстремально отрицательный")

    if long_ratio < 0.3:
        score += 2
        direction = "SHORT"
        reasons.append("Сильный перекос в шорты")

    if oi_change > 0:
        score += 1
        reasons.append("OI растёт против движения")

    # ----- Ликвидации -----
    if liquidations > 50_000_000:
        score += 2
        reasons.append("Аномальные ликвидации")

    return score, direction, reasons
