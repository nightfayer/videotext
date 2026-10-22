import os
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

# NVIDIA NIM AI Model settings (Nemotron reasoning & content generation)
NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY", "").strip()
NVIDIA_MODEL = os.getenv("NVIDIA_MODEL", "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning").strip()
NVIDIA_BASE_URL = os.getenv("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1").strip()

# Whitelist of allowed Telegram user IDs (empty set = public bot)
allowed_users_raw = os.getenv("ALLOWED_USERS", "").strip()
ALLOWED_USERS = set(int(u.strip()) for u in allowed_users_raw.split(",") if u.strip().isdigit())
