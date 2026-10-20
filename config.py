import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Переменная окружения {name} не задана (см. .env.example)")
    return value


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, default))


BOT_TOKEN = _required("BOT_TOKEN")
API_ID = int(_required("API_ID"))
API_HASH = _required("API_HASH")

ADMIN_IDS: set[int] = {
    int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x
}
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "").lstrip("@")

def _path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else BASE_DIR / path


# Всё, что нужно сохранять между перезапусками (база, сессии аккаунтов, эмодзи), лежит в одной
# папке. На хостинге задайте DATA_DIR=/app/data (или папку, которую он сохраняет).
DATA_DIR = _path(os.getenv("DATA_DIR", "data"))


def _in_data(name: str, default: str) -> Path:
    """Абсолютный путь — как есть; относительный («sessions», «data/invite.db») — всегда внутри
    DATA_DIR, чтобы старые переменные окружения не уводили данные из сохраняемой папки."""
    value = os.getenv(name, "").strip()
    if value and Path(value).is_absolute():
        return Path(value)
    return DATA_DIR / Path(value or default).name


DB_PATH = _in_data("DB_PATH", "invite.db")
SESSIONS_DIR = _in_data("SESSIONS_DIR", "sessions")

DAILY_INVITE_LIMIT = _int("DAILY_INVITE_LIMIT", 40)
MAX_CONCURRENT_TASKS = _int("MAX_CONCURRENT_TASKS", 3)
MAX_TARGETS_PER_TASK = _int("MAX_TARGETS_PER_TASK", 500)

CONTACT_SYNC_PAUSE = (_int("CONTACT_SYNC_PAUSE_MIN", 5), _int("CONTACT_SYNC_PAUSE_MAX", 12))
USER_PAUSE = (_int("USER_PAUSE_MIN", 35), _int("USER_PAUSE_MAX", 65))

