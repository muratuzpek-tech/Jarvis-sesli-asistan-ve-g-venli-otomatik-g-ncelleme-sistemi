"""desktop_io — JARVIS'in klavye/fare/ekran erişimi için tek giriş noktası.

NEDEN: JARVIS bu işleri 9 dosyada, 100'den fazla yerde doğrudan pyautogui ile
yapıyordu. pyautogui yalnızca X11/Windows/macOS'ta çalışır; Ubuntu 26.04
(GNOME 50, yalnızca Wayland) üzerinde yüklenemiyor bile ("Authorization
required ... :0"), mss ise siyah görüntü döndürüyor.

KULLANIM — pyautogui ile AYNI arayüz, çağıran kod değişmez:

    try:
        from jarvis.desktop_io import gui as pyautogui
    except Exception:
        pyautogui = None

Arka uç seçimi (JARVIS_DESKTOP_BACKEND=pyautogui|wayland ile zorlanabilir):
  * Linux + Wayland oturumu + ydotool kurulu → WaylandBackend
      klavye/fare: ydotool · metin: düzen 'us' değilse pano+Ctrl+V ·
      ekran görüntüsü: xdg-desktop-portal
  * diğer her durum → gerçek pyautogui (Windows/macOS/X11 davranışı AYNEN korunur)
Uygun bir arka uç yoksa içe aktarma ImportError verir; çağıranların mevcut
"pyautogui = None" yedek yolu çalışmaya devam eder.
"""
from __future__ import annotations

import os
import shutil
import sys

__all__ = ["gui", "backend_name", "is_wayland_session", "select_backend"]


def is_wayland_session() -> bool:
    return sys.platform.startswith("linux") and (
        os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland" or bool(os.environ.get("WAYLAND_DISPLAY"))
    )


def select_backend():
    forced = os.environ.get("JARVIS_DESKTOP_BACKEND", "").strip().lower()
    if forced not in ("", "pyautogui", "wayland"):
        raise ImportError(f"JARVIS_DESKTOP_BACKEND geçersiz: {forced!r}")
    if forced == "wayland" or (not forced and is_wayland_session()):
        if shutil.which("ydotool"):
            from jarvis.desktop_io.wayland import WaylandBackend

            backend = WaylandBackend()
            backend.FAILSAFE = False  # Wayland'de fare konumu okunamadığından köşe-kaçışı yok
            return backend
        if forced == "wayland":
            raise ImportError("Wayland arka ucu için ydotool gerekli (scripts/wayland_kurulum.sh).")
    try:
        import pyautogui
    except Exception as e:  # DisplayConnectionError gibi ImportError olmayan hatalar da
        raise ImportError(f"pyautogui kullanılamıyor: {type(e).__name__}: {e}") from e
    return pyautogui


_GUI = None


def __getattr__(name: str):
    """`gui` TEMBEL seçilir (PEP 562): alt modüller (keys, wayland, portal)
    arka uç olmayan makinelerde de içe aktarılabilsin diye. Uygun arka uç
    yoksa `from jarvis.desktop_io import gui` ImportError verir."""
    global _GUI
    if name == "gui":
        if _GUI is None:
            _GUI = select_backend()
        return _GUI
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def backend_name() -> str:
    return getattr(__getattr__("gui"), "name", "pyautogui")
