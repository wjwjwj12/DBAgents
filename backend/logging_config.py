import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from runtime_paths import DATA_DIR


def close_file_log_handlers() -> None:
    targets = [logging.getLogger(), logging.getLogger("uvicorn.error"), logging.getLogger("uvicorn.access")]
    previous = []
    for target in targets:
        for handler in list(target.handlers):
            if getattr(handler, "_ai_ppt_file_handler", False):
                target.removeHandler(handler)
                previous.append(handler)
    for handler in set(previous):
        handler.close()


def configure_logging(log_dir: Path | None = None) -> Path:
    directory = log_dir or DATA_DIR / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    log_file = directory / "app.log"
    level = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s [pid=%(process)d] %(message)s"
    )
    root = logging.getLogger()
    root.setLevel(level)

    targets = [root, logging.getLogger("uvicorn.error"), logging.getLogger("uvicorn.access")]
    close_file_log_handlers()

    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=int(os.getenv("LOG_MAX_BYTES", str(10 * 1024 * 1024))),
        backupCount=int(os.getenv("LOG_BACKUP_COUNT", "5")),
        encoding="utf-8",
        delay=True,
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    file_handler._ai_ppt_file_handler = True
    root.addHandler(file_handler)
    for target in targets[1:]:
        if not target.propagate:
            target.addHandler(file_handler)

    if not any(getattr(handler, "_ai_ppt_console_handler", False) for handler in root.handlers):
        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.setLevel(level)
        console_handler.setFormatter(formatter)
        console_handler._ai_ppt_console_handler = True
        root.addHandler(console_handler)
    return log_file
