import time

# Базовый cooldown в секундах по типам дивергенций
BASE_DIVERGENCE_COOLDOWN = {
    "LONG_TRAP": 1800,        # 30 мин
    "SHORT_SQUEEZE": 900,     # 15 мин
    "FAKE_MOVE": 1200,        # 20 мин
    "CAPITULATION": 1800,
}

# Классы тикеров для дивергенций
SYMBOL_CLASSES = {
    "BTCUSDT": "L1",
    "ETHUSDT": "L1",
    "SOLUSDT": "L2",
    "DOGEUSDT": "L2",
    "ADAUSDT": "L2",
    "LINKUSDT": "L2",
    "LTCUSDT": "L2",
    "BCHUSDT": "L2",
    "BNBUSDT": "L3",
    "TRXUSDT": "L3",
    "XRPUSDT": "L3",
    "XLMUSDT": "L3",
    "HBARUSDT": "L4",
    "XMRUSDT": "L4",
    "ZECUSDT": "L4",
    "HYPEUSDT": "L4",
}

# Параметры дивергенций по классам
CLASS_DIVERGENCE_PARAMS = {
    "L1": {
        "long_trap_pressure": 0.68,
        "short_squeeze_pressure": 0.74,
        "fake_move_pressure": 0.74,
        "capitulation_pressure": 0.32,
        "price_trend_delta": 0.0007,
        "cooldown_multiplier": 1.2,
    },
    "L2": {
        "long_trap_pressure": 0.66,
        "short_squeeze_pressure": 0.72,
        "fake_move_pressure": 0.72,
        "capitulation_pressure": 0.34,
        "price_trend_delta": 0.0010,
        "cooldown_multiplier": 1.0,
    },
    "L3": {
        "long_trap_pressure": 0.65,
        "short_squeeze_pressure": 0.71,
        "fake_move_pressure": 0.71,
        "capitulation_pressure": 0.35,
        "price_trend_delta": 0.0012,
        "cooldown_multiplier": 0.95,
    },
    "L4": {
        "long_trap_pressure": 0.64,
        "short_squeeze_pressure": 0.70,
        "fake_move_pressure": 0.70,
        "capitulation_pressure": 0.36,
        "price_trend_delta": 0.0015,
        "cooldown_multiplier": 0.9,
    },
}

SYMBOL_PARAM_OVERRIDES = {
    "ETHUSDT": {
        "long_trap_pressure": 0.67,
        "short_squeeze_pressure": 0.73,
        "fake_move_pressure": 0.73,
        "capitulation_pressure": 0.33,
        "cooldown_multiplier": 1.15,
    },
    "DOGEUSDT": {
        "price_trend_delta": 0.0010,
    },
    "ADAUSDT": {
        "price_trend_delta": 0.0010,
    },
    "LINKUSDT": {
        "price_trend_delta": 0.0010,
    },
    "LTCUSDT": {
        "price_trend_delta": 0.0010,
    },
    "BCHUSDT": {
        "price_trend_delta": 0.0010,
    },
    "SOLUSDT": {
        "price_trend_delta": 0.0009,
    },
    "BNBUSDT": {
        "price_trend_delta": 0.0011,
        "cooldown_multiplier": 0.95,
    },
    "TRXUSDT": {
        "price_trend_delta": 0.0011,
        "cooldown_multiplier": 0.95,
    },
    "XRPUSDT": {
        "price_trend_delta": 0.0012,
        "cooldown_multiplier": 0.95,
    },
    "XLMUSDT": {
        "price_trend_delta": 0.0012,
        "cooldown_multiplier": 0.95,
    },
    "HBARUSDT": {
        "price_trend_delta": 0.0014,
    },
    "XMRUSDT": {
        "price_trend_delta": 0.0014,
    },
    "ZECUSDT": {
        "price_trend_delta": 0.0015,
    },
    "HYPEUSDT": {
        "price_trend_delta": 0.0016,
        "cooldown_multiplier": 0.85,
    },
}

_last_seen = {}  # (symbol, type) -> ts


def get_divergence_params(symbol):
    symbol_class = SYMBOL_CLASSES.get(symbol, "L3")
    params = dict(CLASS_DIVERGENCE_PARAMS[symbol_class])
    params.update(SYMBOL_PARAM_OVERRIDES.get(symbol, {}))
    return params


def get_price_trend_delta(symbol):
    return get_divergence_params(symbol)["price_trend_delta"]


def _cooldown_ok(symbol, div_type):
    now = time.time()
    key = (symbol, div_type)
    params = get_divergence_params(symbol)
    base_ttl = BASE_DIVERGENCE_COOLDOWN.get(div_type, 900)
    ttl = int(base_ttl * params["cooldown_multiplier"])

    last = _last_seen.get(key)
    if last and now - last < ttl:
        return False

    _last_seen[key] = now
    return True


def detect_divergence(
    symbol,
    state,
    pressure_ratio,
    oi_window,
    price_trend,
    liquidations,
):
    """
    WS-only divergence detection.
    Возвращает список human-readable строк.
    """

    divergences = []

    # --- базовые вычисления ---
    oi_trend = None
    if len(oi_window) >= 2:
        start = oi_window[0][1]
        end = oi_window[-1][1]
        if end > start:
            oi_trend = "UP"
        elif end < start:
            oi_trend = "DOWN"

    pressure = pressure_ratio
    params = get_divergence_params(symbol)
    
    # ---------------- STATE-AWARE RULES ----------------

    # ❌ В CALM — ничего не показываем
    if state == "CALM":
        return []

    # 🔻 LONG TRAP
    if (
        state in ("LATENT_STRESS", "NEUTRAL", "CROWD_IMBALANCE", "STRESS")
        and pressure > params["long_trap_pressure"]
        and oi_trend == "UP"
        and price_trend in ("FLAT", "DOWN")
    ):
        if _cooldown_ok(symbol, "LONG_TRAP"):
            divergences.append(
                "LONG TRAP — активные покупки, позиции растут, но цена не идёт. "
                "Риск: покупатели могут остаться без продолжения движения."
            )

    # 🔺 SHORT SQUEEZE
    if (
        state in ("CROWD_IMBALANCE", "STRESS")
        and pressure > params["short_squeeze_pressure"]
        and oi_trend == "UP"
        and liquidations > 0
    ):
        if _cooldown_ok(symbol, "SHORT_SQUEEZE"):
            divergences.append(
                "SHORT SQUEEZE — агрессивные покупки при росте открытого интереса. "
                "Риск: шорты могут быть вынуждены закрываться выше."
            )

    # 🔻 FAKE MOVE
    if (
        state in ("LATENT_STRESS", "NEUTRAL", "CROWD_IMBALANCE", "STRESS")
        and pressure > params["fake_move_pressure"]
        and oi_trend == "DOWN"
        and price_trend in ("UP", "FLAT")
    ):
        if _cooldown_ok(symbol, "FAKE_MOVE"):
            divergences.append(
                "FAKE MOVE — сделки есть, но позиции сокращаются. "
                "Риск: движение не подтверждено интересом."
            )

    # 🧨 CAPITULATION
    if (
        state == "STRESS"
        and pressure < params["capitulation_pressure"]
        and oi_trend == "DOWN"
        and liquidations > 0
    ):
        if _cooldown_ok(symbol, "CAPITULATION"):
            divergences.append(
                "CAPITULATION — закрытие позиций под давлением ликвидаций. "
                "Риск: это выход, а не начало тренда."
            )

    return divergences
