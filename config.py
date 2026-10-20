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

DB_PATH = Path(os.getenv("DB_PATH", "data/invite.db"))
if not DB_PATH.is_absolute():
    DB_PATH = BASE_DIR / DB_PATH
SESSIONS_DIR = Path(os.getenv("SESSIONS_DIR", "sessions"))
if not SESSIONS_DIR.is_absolute():
    SESSIONS_DIR = BASE_DIR / SESSIONS_DIR

DAILY_INVITE_LIMIT = _int("DAILY_INVITE_LIMIT", 40)
MAX_CONCURRENT_TASKS = _int("MAX_CONCURRENT_TASKS", 3)
MAX_TARGETS_PER_TASK = _int("MAX_TARGETS_PER_TASK", 500)

CONTACT_SYNC_PAUSE = (_int("CONTACT_SYNC_PAUSE_MIN", 5), _int("CONTACT_SYNC_PAUSE_MAX", 12))
USER_PAUSE = (_int("USER_PAUSE_MIN", 35), _int("USER_PAUSE_MAX", 65))

