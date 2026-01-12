import os

BOT_TOKEN = os.getenv("BOT_TOKEN")

SYMBOLS = [
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
]

INTERVAL_SECONDS = 300
# ---- Risk alert levels ----
EARLY_ALERT_LEVEL = 4   # раннее предупреждение
HARD_ALERT_LEVEL = 6    # жесткий риск

# ---- Spike thresholds ----
FUNDING_SPIKE_THRESHOLD = 0.015   # резкое изменение funding
OI_SPIKE_PERCENT = 0.05            # +5% OI за интервал
