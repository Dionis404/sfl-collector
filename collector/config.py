"""Настройки sfl-collector из переменных окружения."""
import os

try:  # локальный запуск: подхватываем .env, в Docker переменные приходят из env
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


def _bool(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


# --- PostgreSQL (postgres-main, БД sfl) ---
DB_HOST = os.environ.get("DB_HOST", "postgres-main")
DB_PORT = int(os.environ.get("DB_PORT", "5432"))
DB_NAME = os.environ.get("DB_NAME", "sfl")
DB_USER = os.environ.get("DB_USER", "goblin")
DB_PASSWORD = os.environ.get("DB_PASSWORD")

# --- SFL Community API ---
SFL_API_BASE = os.environ.get("SFL_API_BASE", "https://api.sunflower-land.com")
SFL_DUMP_BASE = os.environ.get("SFL_DUMP_BASE", "https://community.sunflower-land.com")
SFL_API_KEY = os.environ.get("SFL_API_KEY", "").strip() or None
SFL_PROXY = os.environ.get("SFL_PROXY", "").strip() or None

# Лимит community API — по IP, ~1 запрос / 5 с (при перегрузе 1 / 10 с).
# Все сервисы на VPS делят его, поэтому берём с запасом.
API_MIN_INTERVAL_SEC = float(os.environ.get("API_MIN_INTERVAL_SEC", "10"))
API_MAX_RETRIES = int(os.environ.get("API_MAX_RETRIES", "4"))
API_RETRY_BASE_SEC = float(os.environ.get("API_RETRY_BASE_SEC", "10"))
API_TIMEOUT_SEC = float(os.environ.get("API_TIMEOUT_SEC", "60"))

# --- Лидерборд тикетов ---
TICKETS_ENABLED = _bool("TICKETS_ENABLED", True)
TICKETS_INTERVAL_SEC = int(os.environ.get("TICKETS_INTERVAL_SEC", "3600"))
# Отслеживать места фермеров бота (таблица farmers из goblin-bot)
TICKETS_TRACK_FARMERS = _bool("TICKETS_TRACK_FARMERS", True)

# --- Быстрый цикл по топу (торговец, задания, баунти, билетные доставки) ---
TOP_FARMS_ENABLED = _bool("TOP_FARMS_ENABLED", True)
TOP_FARMS_INTERVAL_SEC = int(os.environ.get("TOP_FARMS_INTERVAL_SEC", "3600"))
TOP_FARMS_N = int(os.environ.get("TOP_FARMS_N", "100"))
# Пещера с торговцем есть не у всех (у топа лидерборда её может не быть вовсе).
# Если в топе торговца не нашлось — берём его с этих ферм (через запятую).
MERCHANT_SOURCE_FARM_IDS = [
    int(x) for x in os.environ.get("MERCHANT_SOURCE_FARM_IDS", "62559,1").split(",") if x.strip()
]

# --- Проверки предметов у топа ---
ITEM_CHECKS_ENABLED = _bool("ITEM_CHECKS_ENABLED", True)
ITEM_CHECKS_POLL_SEC = int(os.environ.get("ITEM_CHECKS_POLL_SEC", "120"))
ITEM_CHECKS_TOP_N = int(os.environ.get("ITEM_CHECKS_TOP_N", "200"))
# Через сколько после конца аукциона делать проверки владения (часы, через запятую)
ITEM_CHECKS_AUCTION_DELAYS_H = [
    float(x) for x in os.environ.get("ITEM_CHECKS_AUCTION_DELAYS_H", "1,24").split(",") if x.strip()
]
# Аукционы старше этого окна автоматически не проверяем
ITEM_CHECKS_AUCTION_WINDOW_H = float(os.environ.get("ITEM_CHECKS_AUCTION_WINDOW_H", "72"))
# Ручные проверки (из админки сайта) — не чаще раза в N секунд
ITEM_CHECKS_MANUAL_COOLDOWN_SEC = int(os.environ.get("ITEM_CHECKS_MANUAL_COOLDOWN_SEC", "600"))

# --- Ночной дамп ---
DUMP_ENABLED = _bool("DUMP_ENABLED", True)
DUMP_POLL_SEC = int(os.environ.get("DUMP_POLL_SEC", "1800"))
DUMP_FILE_KIND = os.environ.get("DUMP_FILE_KIND", "active")  # active | all
DUMP_TOP_N = int(os.environ.get("DUMP_TOP_N", "500"))  # полная проверка предметов по топу
# Предметы аукционов за последние N дней перепроверяются по каждому новому дампу
DUMP_AUCTION_DAYS = int(os.environ.get("DUMP_AUCTION_DAYS", "14"))

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

# --- API для сайта (api/main.py) ---
# Секрет для POST /admin/*; пустой = админские эндпоинты выключены
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "").strip() or None
API_MAX_TOP_N = int(os.environ.get("API_MAX_TOP_N", "500"))
