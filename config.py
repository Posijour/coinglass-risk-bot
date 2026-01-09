import os

BOT_TOKEN = os.getenv("BOT_TOKEN")
COINGLASS_API_KEY = os.getenv("COINGLASS_API_KEY")

SYMBOLS = [
    "BTCUSDT",
    "ETHUSDT",
]
INTERVAL_SECONDS = 300  # 5 минут
RISK_ALERT_LEVEL = 6
