import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
_configured_data_dir = Path(os.getenv("APP_DATA_DIR", "data"))
DATA_DIR = (
    _configured_data_dir
    if _configured_data_dir.is_absolute()
    else PROJECT_ROOT / _configured_data_dir
).resolve()

DATABASE_FILE = DATA_DIR / "app_data.db"
CHECKPOINT_FILE = DATA_DIR / "langgraph_checkpoints.db"
STORAGE_DIR = DATA_DIR / "storage"
ATTACHMENT_DIR = STORAGE_DIR / "attachments"
SECRET_FILE = DATA_DIR / ".app_secret"


def ensure_runtime_directories() -> None:
    ATTACHMENT_DIR.mkdir(parents=True, exist_ok=True)


ensure_runtime_directories()
