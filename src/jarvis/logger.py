"""Lightweight structured logging — all Jarvis modules use this."""
import logging
import sys
from pathlib import Path

_LOG_DIR = Path(__file__).resolve().parent.parent.parent / "logs"
_LOG_DIR.mkdir(exist_ok=True)

def get_logger(name: str) -> logging.Logger:
    """Return a named logger writing to logs/<name>.log + console."""
    log = logging.getLogger(f"jarvis.{name}")
    if log.handlers:  # already configured
        return log
    log.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s [%(levelname)-7s] %(name)s: %(message)s", datefmt="%H:%M:%S")
    fh = logging.FileHandler(_LOG_DIR / f"{name}.log", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    ch = logging.StreamHandler(sys.stderr)
    ch.setLevel(logging.WARNING)  # console: only warn+
    ch.setFormatter(fmt)
    log.addHandler(fh)
    log.addHandler(ch)
    return log
