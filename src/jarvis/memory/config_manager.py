import json
import os
from pathlib import Path

from jarvis.core.secure_config import api_keys_path
from jarvis.paths import config_dir, project_root


def get_base_dir() -> Path:
    return project_root()

BASE_DIR    = get_base_dir()
CONFIG_DIR  = config_dir()
CONFIG_FILE = api_keys_path(for_write=True)

def ensure_config_dir() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

def config_exists() -> bool:
    return CONFIG_FILE.exists()

def save_api_keys(gemini_api_key: str) -> None:
    """Anahtari kullanici veri dizinine yazar (kurulu paketin icine DEGIL)."""
    ensure_config_dir()

    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}

    data["gemini_api_key"] = gemini_api_key.strip()

    CONFIG_FILE.write_text(
        json.dumps(data, indent=2),
        encoding="utf-8"
    )
    # DUZELTME (2026-09-28, CodeQL: duz metin hassas veri saklama bulgusu):
    # dosya API anahtari iceriyor - izinlerin sistemin umask'ina birakilmasi
    # yerine acikca 0600 (yalnizca sahibi okur/yazar) olarak zorlaniyor.
    try:
        os.chmod(CONFIG_FILE, 0o600)
    except OSError:
        pass

def load_api_keys() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"❌ Failed to load api_keys.json: {e}")
        return {}

def get_gemini_key() -> str | None:
    return load_api_keys().get("gemini_api_key")

def is_configured() -> bool:
    key = get_gemini_key()
    return bool(key and len(key) > 15)