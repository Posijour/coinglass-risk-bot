# logger.py
import json
import os
import time
from threading import Lock

import requests

_LOG_TO_FILE = os.getenv("LOG_TO_FILE", "1").lower() in ("1", "true", "yes")
_LOG_TO_STDOUT = os.getenv("LOG_TO_STDOUT", "1").lower() in ("1", "true", "yes")
_LOG_FILE = os.getenv("LOG_FILE_PATH", "bot_events.jsonl")
_SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
_SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
_SUPABASE_LOGS_TABLE = os.getenv("SUPABASE_LOGS_TABLE", "logs")
_LOG_TO_SUPABASE = bool(_SUPABASE_URL and _SUPABASE_KEY)

_lock = Lock()

def now_ts_ms():
    return int(time.time() * 1000)

def log_event(event_type: str, payload: dict):
    """
    Универсальный логгер событий бота.
    - По умолчанию пишет в файл (bot_events.jsonl) и stdout (JSONL)
    - Путь/поведение можно переопределить env-переменными
    """

    record = {
        "ts": now_ts_ms(),
        "type": event_type,
        "data": payload,   # 👈 важный момент
    }

    line = json.dumps(record, ensure_ascii=False)

    with _lock:
        if _LOG_TO_FILE:
            with open(_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        if _LOG_TO_STDOUT:
            print(line, flush=True)
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
                print(
                    f"SUPABASE ERROR {resp.status_code}: {resp.text}",
                    flush=True,
                )
        except Exception as exc:
            print(f"SUPABASE EXCEPTION: {exc}", flush=True)
