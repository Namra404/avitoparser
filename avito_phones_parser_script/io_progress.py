import os
import json
import time
import random
from pathlib import Path
from .settings import OUT_JSON, PENDING_JSON

def atomic_write_json(path: Path, data):
    """
    Надёжная запись на Windows:
    - уникальный tmp-файл;
    - до 10 ретраев os.replace при PermissionError;
    - при постоянной блокировке — безопасный fallback прямой записью.
    """
    tmp = path.with_suffix(path.suffix + f".tmp_{int(time.time()*1000)}_{random.randint(1000,9999)}")
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    tmp.write_text(payload, encoding="utf-8")
    attempts = 10
    delay = 0.1
    for _ in range(attempts):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(delay)
            delay = min(delay * 1.7, 1.0)
        except Exception:
            time.sleep(delay)
            delay = min(delay * 1.7, 1.0)
    # Fallback
    try:
        path.write_text(payload, encoding="utf-8")
    except Exception as e:
        print(f"❗ Критическая ошибка записи прогресса: {e}")

def load_progress(path: Path = OUT_JSON) -> dict[str, str]:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"⚠️ Не удалось прочитать существующий прогресс: {e}")
    return {}

def load_pending(path: Path = PENDING_JSON) -> list[str]:
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return [u for u in data if isinstance(u, str)]
        except Exception:
            pass
    return []

def save_pending(path: Path = PENDING_JSON, urls: list[str] | None = None):
    urls = urls or []
    unique = sorted(set(urls))
    atomic_write_json(path, unique)
