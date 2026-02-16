# logger.py
import os
import time

import requests

_SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
_SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
_SUPABASE_LOGS_TABLE = os.getenv("SUPABASE_LOGS_TABLE", "logs")
_LOG_TO_SUPABASE = bool(_SUPABASE_URL and _SUPABASE_KEY)

def now_ts_ms():
    return int(time.time() * 1000)

def log_event(event_type: str, payload: dict):
    """Универсальный логгер событий бота (только Supabase)."""

    record = {
        "ts": now_ts_ms(),
        "type": event_type,
        "data": payload,   # 👈 важный момент
    }

    if _LOG_TO_SUPABASE:
        try:
            resp = requests.post(
                f"{_SUPABASE_URL}/rest/v1/{_SUPABASE_LOGS_TABLE}",
                headers={
                    "apikey": _SUPABASE_KEY,
                    "Authorization": f"Bearer {_SUPABASE_KEY}",
                    "Content-Type": "application/json",
                    "Prefer": "return=minimal",
                },
                json={
                    "ts": record["ts"],
                    "event": event_type,
                    "symbol": payload.get("symbol"),
                    "data": payload,
                },
                timeout=5,
            )
            if resp.status_code >= 300:
                return
        except Exception:
            return
