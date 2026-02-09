# logger.py
import json
import os
import time
from threading import Lock

_LOG_TO_FILE = os.getenv("LOG_TO_FILE", "1").lower() in ("1", "true", "yes")
_LOG_TO_STDOUT = os.getenv("LOG_TO_STDOUT", "1").lower() in ("1", "true", "yes")
_LOG_FILE = os.getenv("LOG_FILE_PATH", "bot_events.jsonl")

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
        "ts": int(time.time()),
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
