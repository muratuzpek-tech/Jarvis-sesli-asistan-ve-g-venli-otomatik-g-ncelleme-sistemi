"""Tek kaynak: kod nerede, KULLANICI VERİSİ nerede.

Kod dizini salt-okunur kabul edilir; tüm çalışma verileri, sırlar, loglar,
görevler, eklentiler ve üretim çalışma alanları kullanıcı veri dizinine gider.
``JARVIS_HOME`` test ve portable kurulumlar için açık bir izolasyon sınırıdır.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "MuratJARVIS"


def package_dir() -> Path:
    """Kodun bulunduğu dizin (salt okunur kabul edilir)."""
    return Path(__file__).resolve().parent


def project_root() -> Path:
    """Depo kökü (kurulu pakette paket dizininin iki üstü: src/jarvis -> repo)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return package_dir().parent.parent


def assets_dir() -> Path:
    """Pakete gömülü görseller/ikonlar."""
    return package_dir() / "assets"


def asset(name: str) -> Path:
    return assets_dir() / name


def _platform_data_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
    return Path(base) / APP_NAME


def data_dir() -> Path:
    env = os.environ.get("JARVIS_HOME", "").strip()
    if env:
        return Path(env).expanduser()
    if os.environ.get("JARVIS_USE_LEGACY_DATA", "").strip() == "1":
        legacy = project_root() / "memory"
        if legacy.is_dir():
            return project_root()
    return _platform_data_dir()


def _sub(name: str) -> Path:
    path = data_dir() / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def memory_dir() -> Path:
    return _sub("memory")


def logs_dir() -> Path:
    return _sub("logs")


def tasks_dir() -> Path:
    return _sub("tasks")


def config_dir() -> Path:
    return _sub("config")


def certs_dir() -> Path:
    return _sub("config/certs")


def plugins_dir() -> Path:
    return _sub("plugins")


def dev_projects_dir() -> Path:
    """Workspace for generated projects; never place these under package code."""
    return _sub("dev_agent/projects")


def dev_cache_dir() -> Path:
    """Non-secret temporary/cache artifacts for development tools."""
    return _sub("dev_agent/cache")


def sandbox_dir() -> Path:
    """User-data sandbox root for self-improvement experiments."""
    return _sub("sandbox")
