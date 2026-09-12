import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")

# --- БЛОК POSTGRES ---
DATABASE_URL = os.getenv("DATABASE_URL", "")
if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL не задан. Добавь переменную DATABASE_URL в Railway.")
# --- КОНЕЦ БЛОКА ---

SEND_MIN_DELAY = int(os.getenv("SEND_MIN_DELAY", "40"))
SEND_MAX_DELAY = int(os.getenv("SEND_MAX_DELAY", "120"))
SPAMBOT_UNBLOCK_WAIT = int(os.getenv("SPAMBOT_UNBLOCK_WAIT", "25"))
ACCOUNT_COOLDOWN = int(os.getenv("ACCOUNT_COOLDOWN", "86400"))
USERS_PER_ACCOUNT = int(os.getenv("USERS_PER_ACCOUNT", "50"))
