"""
Configures logging: console + rotating file in logs/.
Call setup_logging() once at the start of main.py.
"""
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from config.settings import LOGS_DIR


def setup_logging(level: str = "INFO") -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOGS_DIR / "pipeline.log"

    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    handlers = [
        logging.StreamHandler(sys.stdout),
        RotatingFileHandler(log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"),
    ]

    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO),
                        format=fmt, datefmt=datefmt, handlers=handlers)
    logging.getLogger("msal").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
