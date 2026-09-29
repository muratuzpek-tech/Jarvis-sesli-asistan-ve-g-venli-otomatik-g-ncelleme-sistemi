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
  required  (Linux'ta varsayılan) kafes kurulamıyorsa programı HİÇ çalıştırma
  auto      (Windows/macOS varsayılanı) kafes varsa kullan; yoksa UYARI ver, kafessiz çalıştır
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


def default_mode() -> str:
    """Linux'ta varsayılan ZORUNLU: kafes kurulamazsa üretilen kod çalışmaz
    (sessiz güvenlik düşüşü yok). Windows/macOS'ta bubblewrap olmadığından
    'auto' (açık uyarıyla kafessiz)."""
    return "required" if sys.platform.startswith("linux") else "auto"


def mode() -> str:
    m = (os.environ.get("JARVIS_SANDBOX", "") or default_mode()).strip().lower()
    return m if m in MODES else default_mode()


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
    # Deneme, gerçek görevdeki gibi AYNI Python yorumlayıcısıyla yapılır: yalnız
    # /bin/true denemek, yorumlayıcının kafeste bulunamadığı durumu gizliyordu.
    import tempfile
    with tempfile.TemporaryDirectory(prefix="jarvis_kafes_") as d:
        probe, env = build([sys.executable, "-c", "import json, ssl, sqlite3"], Path(d), network=False)
        try:
            r = subprocess.run(probe, env=env, capture_output=True, text=True, timeout=30, check=False)
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


def _symlink_hops(path: str, limit: int = 20) -> list[Path]:
    """Yolun kendisi ve izlediği her sembolik bağlantı adımı (ör. uv'nin
    '.venv/bin/python → …/cpython-3.12-linux…/bin/python3.12' takma adı →
    '…/cpython-3.12.14…'). Kafeste her adımın yolu var olmalı; yoksa
    'execvp: No such file or directory' (canlı test 2026-09-29, murat@goxs)."""
    hops, p = [], Path(path)
    for _ in range(limit):
        hops.append(p)
        if not p.is_symlink():
            break
        target = Path(os.readlink(p))
        p = target if target.is_absolute() else p.parent / target
    return hops


_SYSTEM_PREFIXES = ("/usr", "/etc", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32")


def _is_system(p: Path) -> bool:
    return any(str(p) == s or str(p).startswith(s + "/") for s in _SYSTEM_PREFIXES)


def _python_runtime_paths() -> list[Path]:
    """Kafese salt okunur bağlanan klasörler: YALNIZ sanal ortam ve Python'un
    gerçek kurulum kökü. Aradaki bağlantı adımları (uv'nin ~/.local/bin
    kısayolu, cpython-3.12-… takma adı) klasör olarak bağlanmaz; kafeste
    --symlink ile birebir yeniden kurulur (bkz. _python_runtime_symlinks).
    (murat@goxs 2026-09-29: kısayol yüzünden ~/.local'ın TAMAMI — anahtarlık,
    JARVIS ayarları — kafese görünür oluyordu.)"""
    home = Path.home()
    cands = {Path(sys.prefix), Path(os.path.realpath(sys.base_prefix)),
             Path(os.path.realpath(sys.executable)).parent.parent}
    roots = {p for p in cands if p.exists() and not _is_system(p) and p not in (Path("/"), home)}
    return sorted(p for p in roots if not any(o != p and o in p.parents for o in roots))


def _python_runtime_symlinks(roots: list[Path]) -> list[tuple[str, str]]:
    """(hedef, yol): yorumlayıcıya giden zincirde, bağlı köklerin DIŞINDA kalan
    her sembolik bağlantı (dosya ya da üst klasör)."""
    def inside_root(p: Path) -> bool:
        return any(p == r or r in p.parents for r in roots)

    links: list[tuple[str, str]] = []
    seen: set[str] = set()
    for hop in _symlink_hops(sys.executable):
        # Yolun kendisi ve üst klasörleri arasındaki bağlantılar (ör. takma ad klasörü).
        parts = hop.parts
        for i in range(2, len(parts) + 1):
            q = Path(*parts[:i])
            if str(q) in seen or _is_system(q) or inside_root(q):
                continue
            if q.is_symlink():
                seen.add(str(q))
                links.append((os.readlink(q), str(q)))
    return links


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
    roots = _python_runtime_paths()
    for p in roots:
        args += _ro(Path(os.path.realpath(p)), str(p))
    for target, link in _python_runtime_symlinks(roots):
        args += ["--symlink", target, link]
    browsers = _playwright_browsers()
    if browsers:
        args += _ro(Path(os.path.realpath(browsers)), str(browsers))
    for a in [*(extra_ro or []), *input_paths(argv, project_dir)]:
        args += _ro(Path(os.path.realpath(a)), a)
    # Proje klasörü verilen yolla (ör. ~/Desktop/JarvisProjects → /data bağlantısı) bağlanır.
    real_project = os.path.realpath(project)
    args += ["--bind", real_project, project]
    if real_project != project:
        # Canlı test 2026-09-29 (murat@goxs): kabul testi giriş dosyasını ve örnek
        # verileri .resolve() ile (/data/...) veriyordu; proje yalnız bağlantı
        # yoluyla bağlandığı için "can't open file" (çıkış 2) alınıyordu. Aynı
        # klasör gerçek yoluyla da bağlanır; kafese başka hiçbir şey açılmaz.
        args += ["--bind", real_project, real_project]
    args += ["--chdir", str(cwd or project)]

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
