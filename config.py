import os
import json
from pathlib import Path
from dotenv import load_dotenv

# Silence Windows HF symlinks warning
os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"

# Automatically register NVIDIA CUDA & cuDNN DLLs installed in venv on Windows
def _setup_cuda_dlls():
    import sys
    if sys.platform != "win32":
        return
    site_packages = os.path.join(sys.prefix, "Lib", "site-packages")
    nvidia_dir = os.path.join(site_packages, "nvidia")
    if os.path.exists(nvidia_dir):
        for sub in ["cublas", "cudnn", "cuda_nvrtc"]:
            bin_dir = os.path.join(nvidia_dir, sub, "bin")
            if os.path.exists(bin_dir):
                try:
                    os.add_dll_directory(bin_dir)
                    os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
                except Exception:
                    pass

_setup_cuda_dlls()

# Base directories
BASE_DIR = Path(__file__).resolve().parent
TEMP_DIR = BASE_DIR / "temp"
TEMP_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
USERS_FILE = DATA_DIR / "allowed_users.json"

# Load .env file
load_dotenv(BASE_DIR / ".env")

# Telegram credentials
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
API_ID_RAW = os.getenv("API_ID", "").strip()
API_HASH = os.getenv("API_HASH", "").strip()

API_ID = int(API_ID_RAW) if API_ID_RAW.isdigit() else None

# Whisper settings
# Available models: tiny, base, small, medium, large-v3, turbo
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "small").strip().lower()

# Device: 'cuda' (GPU) or 'cpu'
DEVICE = os.getenv("DEVICE", "cuda").strip().lower()

# Compute type: 'float16' (for GPU), 'int8' (for CPU) or 'default'
COMPUTE_TYPE = os.getenv("COMPUTE_TYPE", "float16").strip().lower()

# Default language (None for automatic detection, or 'ru', 'en', etc.)
WHISPER_LANGUAGE = os.getenv("WHISPER_LANGUAGE", "").strip() or None

# Beam size for Whisper: 1 = Greedy Search (3x faster), 2 = balanced, 5 = highest precision
BEAM_SIZE = int(os.getenv("BEAM_SIZE", "1"))

# Max concurrent transcriptions on GPU
MAX_CONCURRENT_TASKS = int(os.getenv("MAX_CONCURRENT_TASKS", "1"))

# NVIDIA NIM AI Model settings (NVIDIA Nemotron 3.5 Bolt E4B content generation)
NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY", "").strip()
NVIDIA_MODEL = os.getenv("NVIDIA_MODEL", "nvidia/nemotron-3.5-bolt-e4b").strip()
NVIDIA_BASE_URL = os.getenv("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1").strip()

# Admin IDs (users with full rights to add/remove users and manage the bot)
DEFAULT_ADMIN_ID = 727532544
admin_raw = os.getenv("ADMIN_ID", str(DEFAULT_ADMIN_ID)).strip()
ADMIN_IDS: set[int] = set(int(u.strip()) for u in admin_raw.replace(" ", ",").split(",") if u.strip().isdigit())
ADMIN_IDS.add(DEFAULT_ADMIN_ID)

# Whitelist storage & management
def _save_allowed_users():
    """Атомарно сохраняет список разрешенных пользователей в JSON-файл."""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        data = {
            "allowed_users": sorted(ALLOWED_USERS),
            "admin_ids": sorted(ADMIN_IDS)
        }
        temp_file = USERS_FILE.with_suffix(".tmp")
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        temp_file.replace(USERS_FILE)
    except Exception as e:
        print(f"[Config Warning] Ошибка сохранения {USERS_FILE}: {e}", flush=True)


def _init_allowed_users() -> set[int]:
    """Загружает список разрешенных пользователей из .env и постоянного JSON-файла."""
    users: set[int] = set()

    # 1. Загрузка из .env
    allowed_users_raw = os.getenv("ALLOWED_USERS", "").strip()
    if allowed_users_raw:
        for u in allowed_users_raw.replace(" ", ",").split(","):
            if u.strip().isdigit():
                users.add(int(u.strip()))

    # 2. Объединение с постоянным файлом USERS_FILE
    if USERS_FILE.exists():
        try:
            with open(USERS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                file_users = data.get("allowed_users", [])
                for u in file_users:
                    if isinstance(u, int):
                        users.add(u)
                    elif isinstance(u, str) and u.isdigit():
                        users.add(int(u))
        except Exception as e:
            print(f"[Config Warning] Ошибка чтения {USERS_FILE}: {e}", flush=True)

    # 3. Администраторы всегда присутствуют в белом списке
    users.update(ADMIN_IDS)
    return users


ALLOWED_USERS: set[int] = _init_allowed_users()
_save_allowed_users()


def is_admin(user_id: int) -> bool:
    """Проверяет, является ли пользователь администратором."""
    return user_id in ADMIN_IDS


def is_user_allowed(user_id: int) -> bool:
    """Проверяет, разрешен ли доступ пользователю к боту."""
    if is_admin(user_id):
        return True
    if not ALLOWED_USERS:
        return True
    return user_id in ALLOWED_USERS


def add_allowed_user(user_id: int) -> tuple[bool, str]:
    """Добавляет пользователя в белый список с сохранением на диск."""
    if user_id in ALLOWED_USERS:
        return False, f"ℹ️ Пользователь `{user_id}` уже есть в белом списке."
    ALLOWED_USERS.add(user_id)
    _save_allowed_users()
    return True, f"✅ Пользователь `{user_id}` успешно добавлен в белый список!"


def remove_allowed_user(user_id: int) -> tuple[bool, str]:
    """Удаляет пользователя из белого списка с сохранением на диск."""
    if is_admin(user_id):
        return False, "⚠️ Нельзя удалить администратора бота."
    if user_id not in ALLOWED_USERS:
        return False, f"ℹ️ Пользователь `{user_id}` не найден в белом списке."
    ALLOWED_USERS.discard(user_id)
    _save_allowed_users()
    return True, f"✅ Пользователь `{user_id}` удален из белого списка."


def get_allowed_users_list() -> list[int]:
    """Возвращает отсортированный список всех разрешенных пользователей."""
    return sorted(ALLOWED_USERS)
