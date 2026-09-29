"""Üretilen programlar için bubblewrap güvenlik kafesi (Linux).

NEDEN: dev_agent'ın yazdığı kod şimdiye kadar kullanıcının kendi hesabıyla,
ev klasörüne, SSH anahtarlarına, .env dosyalarına ve JARVIS'in API
anahtarlarına (ortam değişkenleri) tam erişimle çalışıyordu. Model kötü niyetli
olmasa bile yanlış bir `shutil.rmtree` ya da yanlış yol her şeyi silebilir.

Kafesin içinde program yalnızca şunları görür:
  • /usr, /etc ve Python çalışma ortamı  → salt okunur
  • kendi proje klasörü                  → yazılabilir (çıktılar buraya)
  • komut satırındaki girdi yolları       → salt okunur
  • /tmp                                  → boş, geçici, dışarıdan ayrı
  • ağ                                    → açık (kazıma görevleri için)
Ev klasörü, diğer projeler, SSH ajanı, D-Bus, Docker soketi görünmez. Ortam
değişkenleri SIFIRDAN kurulur (API anahtarları aktarılmaz). Ayrı PID ad alanı:
program dışarıdaki süreçleri göremez/öldüremez; kafes kapanınca içindeki TÜM
süreçler de kapanır (--die-with-parent + PID ad alanı).

JARVIS_SANDBOX ortam değişkeni:
  auto      (varsayılan) bubblewrap çalışıyorsa kullan; yoksa UYARI ver, kafessiz çalıştır
  required  kafes çalışmıyorsa programı HİÇ çalıştırma
  off       kafesi kapat (yalnızca hata ayıklama için)
"""
from __future__ import annotations

import functools
import os
import shutil
import subprocess
import sys
from pathlib import Path

MODES = ("auto", "required", "off")
SAFE_ENV_KEYS = ("LANG", "LC_ALL", "LC_CTYPE", "LANGUAGE", "TZ", "TERM")
_SYSTEM_LINKS = ("/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32")


def mode() -> str:
    m = (os.environ.get("JARVIS_SANDBOX", "") or "auto").strip().lower()
    return m if m in MODES else "auto"


def bwrap_path() -> str | None:
    if not sys.platform.startswith("linux"):
        return None
    return shutil.which("bwrap")


@functools.lru_cache(maxsize=1)
def sandbox_works() -> tuple[bool, str]:
    """Kafes bu makinede gerçekten kurulabiliyor mu? (Ubuntu 24.04+ AppArmor
    ayrıcalıksız kullanıcı ad alanlarını kısıtlayabilir.) Bir kez denenir."""
    bw = bwrap_path()
    if not bw:
        return False, ("bubblewrap kurulu değil (Linux'ta: sudo apt install bubblewrap)"
                       if sys.platform.startswith("linux") else "bubblewrap yalnızca Linux'ta var")
    probe = [bw, *_base_args(network=False), "--", "/bin/true"]
    try:
        r = subprocess.run(probe, capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"bubblewrap çalıştırılamadı: {e}"
    if r.returncode != 0:
        return False, f"bubblewrap kafes kuramadı: {(r.stderr or r.stdout).strip()[:300]}"
    return True, "bubblewrap"


def _base_args(network: bool = True) -> list[str]:
    args = ["--unshare-all", "--die-with-parent", "--new-session"]
    if network:
        args.append("--share-net")
    args += ["--ro-bind", "/usr", "/usr"]
    for p in _SYSTEM_LINKS:
        path = Path(p)
        if path.is_symlink():
            args += ["--symlink", os.readlink(p), p]
        elif path.is_dir():
            args += ["--ro-bind", p, p]
    args += ["--ro-bind", "/etc", "/etc", "--proc", "/proc", "--dev", "/dev",
             "--tmpfs", "/tmp", "--tmpfs", "/run", "--dir", "/tmp/home"]
    # DNS: /etc/resolv.conf çoğu Ubuntu'da systemd-resolved'a bağlantıdır.
    for p in ("/run/systemd/resolve",):
        if Path(p).is_dir():
            args += ["--ro-bind", p, p]
    return args


def _ro(src: Path, dest: str) -> list[str]:
    return ["--ro-bind", str(src), dest]


def _python_runtime_paths() -> list[Path]:
    """Kafesin içinde aynı Python'un (ve kurulu paketlerin) çalışması için."""
    paths = {Path(sys.prefix), Path(sys.base_prefix), Path(os.path.realpath(sys.executable)).parent.parent}
    return sorted(p for p in paths if p.exists() and not str(p).startswith("/usr"))


def _playwright_browsers() -> Path | None:
    p = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "") or Path.home() / ".cache" / "ms-playwright")
    return p if p.is_dir() else None


def input_paths(argv: list[str], project_dir: Path) -> list[str]:
    """Komut satırındaki, proje dışında var olan yollar (salt okunur bağlanır)."""
    proj = Path(os.path.realpath(project_dir))
    found = []
    for a in argv[1:]:
        if not a.startswith("/"):
            continue
        p = Path(a)
        try:
            if not p.exists():
                continue
            real = Path(os.path.realpath(p))
        except OSError:
            continue
        if real == proj or proj in real.parents:
            continue
        found.append(a)
    return found


def build(argv: list[str], project_dir: Path, *, network: bool = True, gui: bool = False,
          extra_ro: list[str] | None = None, cwd: Path | None = None) -> tuple[list[str], dict[str, str]]:
    """(kafesli komut, ortam). argv[0] kafesin içinde de aynı yolla bulunur."""
    bw = bwrap_path() or "bwrap"
    project = str(project_dir)
    args = [bw, *_base_args(network)]
    for p in _python_runtime_paths():
        args += _ro(p, str(p))
    browsers = _playwright_browsers()
    if browsers:
        args += _ro(Path(os.path.realpath(browsers)), str(browsers))
    for a in [*(extra_ro or []), *input_paths(argv, project_dir)]:
        args += _ro(Path(os.path.realpath(a)), a)
    # Proje klasörü verilen yolla (ör. ~/Desktop/JarvisProjects → /data bağlantısı) bağlanır.
    args += ["--bind", os.path.realpath(project), project, "--chdir", str(cwd or project)]

    env = {k: os.environ[k] for k in SAFE_ENV_KEYS if k in os.environ}
    venv_bin = str(Path(sys.executable).parent)
    env.update({
        "PATH": f"{venv_bin}:/usr/local/bin:/usr/bin:/bin",
        "HOME": "/tmp/home", "TMPDIR": "/tmp", "XDG_CACHE_HOME": "/tmp/home/.cache",
        "MPLCONFIGDIR": "/tmp/home/.mpl", "MPLBACKEND": "Agg",
        "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8",
        "JARVIS_IN_SANDBOX": "1",
    })
    if browsers:
        env["PLAYWRIGHT_BROWSERS_PATH"] = str(browsers)
    if gui:
        # UYARI: X11 soketi açıksa program diğer pencerelerin tuş vuruşlarını
        # dinleyebilir; GUI görevleri için bilinen, kabul edilmiş bir zayıflık.
        env["MPLBACKEND"] = os.environ.get("MPLBACKEND", "TkAgg")
        if Path("/tmp/.X11-unix").is_dir():
            args += _ro(Path("/tmp/.X11-unix"), "/tmp/.X11-unix")
        for key in ("DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "XAUTHORITY", "QT_QPA_PLATFORM"):
            if key in os.environ:
                env[key] = os.environ[key]
        xauth = os.environ.get("XAUTHORITY")
        if xauth and Path(xauth).is_file():
            args += _ro(Path(xauth), xauth)
        rt, wl = os.environ.get("XDG_RUNTIME_DIR"), os.environ.get("WAYLAND_DISPLAY")
        if rt and wl and Path(rt, wl).exists():
            args += ["--bind", str(Path(rt, wl)), str(Path(rt, wl))]
    return [*args, "--", *argv], env


def wrap(argv: list[str], project_dir: Path, *, gui: bool = False, cwd: Path | None = None,
         log=print) -> tuple[list[str], dict | None, str]:
    """Çalıştırılacak (komut, ortam, durum). ortam None → normal ortam (kafessiz).
    durum: 'kafes', 'kafessiz' ya da 'REFUSED: …' (required modda kafes yoksa)."""
    m = mode()
    if m == "off":
        return argv, None, "kafessiz"
    ok, why = sandbox_works()
    if ok:
        cmd, env = build(argv, project_dir, gui=gui, cwd=cwd)
        return cmd, env, "kafes"
    if m == "required":
        return argv, None, f"REFUSED: güvenlik kafesi zorunlu (JARVIS_SANDBOX=required) ama kurulamadı: {why}"
    _warn_once(log, why)
    return argv, None, "kafessiz"


_warned = False


def _warn_once(log, why: str) -> None:
    global _warned
    if not _warned:
        _warned = True
        log(f"⚠️ Güvenlik kafesi YOK — üretilen program kendi hesabınla, tam erişimle çalışacak ({why}).")


_GUI_IMPORT = ("import tkinter", "from tkinter", "import PyQt", "from PyQt", "import pygame", "from PySide",
               "import PySide", "import kivy", "import wx")


def project_uses_gui(project_dir: Path) -> bool:
    for p in Path(project_dir).rglob("*.py"):
        if ".jarvis" in p.parts:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if any(k in text for k in _GUI_IMPORT):
            return True
    return False
