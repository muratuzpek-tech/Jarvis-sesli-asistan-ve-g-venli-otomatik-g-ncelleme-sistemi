"""JARVIS merkezi logging — tek dosyada tum loglar."""
import logging
import sys
from pathlib import Path

_LOG_DIR = Path.home() / ".jarvis" / "logs"
_LOG_DIR.mkdir(parents=True, exist_ok=True)

# Ana log dosyasi (her seviye)
_MAIN_LOG = _LOG_DIR / "jarvis.log"

# Formatter: timestamp + level + module + message
_fmt = logging.Formatter(
    "%(asctime)s | %(levelname)-7s | %(name)-25s | %(message)s",
    datefmt="%H:%M:%S"
)

def get_logger(name: str) -> logging.Logger:
    """Modul icin logger olustur — orn: get_logger("dev_agent")"""
    logger = logging.getLogger(f"jarvis.{name}")
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)

    # Console handler: sadece WARNING+ (spam'i engelle)
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.WARNING)
    ch.setFormatter(_fmt)

    # File handler: her seviye
    fh = logging.FileHandler(_MAIN_LOG, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(_fmt)

    logger.addHandler(ch)
    logger.addHandler(fh)
    logger.propagate = False
    return logger


def set_console_level(level: str) -> None:
    """Konsol log seviyesini degistir: DEBUG, INFO, WARNING, ERROR"""
    lvl = getattr(logging, level.upper(), logging.WARNING)
    root = logging.getLogger("jarvis")
    for h in root.handlers:
        if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
            h.setLevel(lvl)
