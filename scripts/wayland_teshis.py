#!/usr/bin/env python3
"""wayland_teshis.py — JARVIS'in masaüstü özelliklerinin BU makinede gerçekten
çalışıp çalışmadığını tek tek dener ve raporlar.

Güvenli: sudo istemez, hiçbir şey kurmaz/silmez, tıklamaz, yazı yazmaz.
Yaptığı tek "görünür" şeyler: bir test bildirimi göstermek ve fareyi 1 piksel
oynatıp geri almak. Panodaki mevcut içerik test sonrası geri yüklenir.

Kullanım (proje kökünden, JARVIS'in sanal ortamıyla):
    .venv/bin/python scripts/wayland_teshis.py
Rapor ayrıca .jarvis_kontrol/wayland_teshis.json dosyasına yazılır.
"""
from __future__ import annotations

import importlib
import json
import os
import platform
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

OK, WARN, FAIL, INFO = "✅", "⚠️", "❌", "ℹ️"
RESULTS: list[dict] = []


def record(group: str, name: str, status: str, detail: str = "") -> None:
    RESULTS.append({"group": group, "name": name, "status": status, "detail": detail})
    print(f"  {status} {name}" + (f" — {detail}" if detail else ""))


def run(args: list[str], timeout: float = 10) -> tuple[int | None, str]:
    """Komutu shell'siz çalıştırır; (çıkış kodu, çıktı) döner. Bulunamazsa (None, mesaj)."""
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
        return p.returncode, (p.stdout + p.stderr).strip()
    except FileNotFoundError:
        return None, "bulunamadı"
    except subprocess.TimeoutExpired:
        return None, f"{timeout:.0f} sn zaman aşımı"
    except OSError as e:
        return None, str(e)


def image_verdict(img) -> tuple[str, str]:
    """Ekran görüntüsünün gerçek içerik mi, yoksa siyah/tek renk mi olduğunu söyler."""
    try:
        from PIL import ImageStat
        small = img.convert("L").resize((64, 36))
        stat = ImageStat.Stat(small)
        mean, std = stat.mean[0], stat.stddev[0]
        size = f"{img.width}x{img.height}"
        if std < 2:
            return FAIL, f"{size}, görüntü TEK RENK (ort. parlaklık {mean:.0f}) — büyük ihtimalle boş/siyah"
        return OK, f"{size}, gerçek içerik var (kontrast {std:.0f})"
    except Exception as e:  # noqa: BLE001 - teşhis aracı, her hatayı raporlar
        return WARN, f"görüntü analiz edilemedi: {e}"


# ── 1. Oturum ────────────────────────────────────────────────────────────
def check_session() -> None:
    print("\n[1] Oturum")
    env = {k: os.environ.get(k, "") for k in
           ("XDG_SESSION_TYPE", "WAYLAND_DISPLAY", "DISPLAY", "XDG_CURRENT_DESKTOP")}
    session = env["XDG_SESSION_TYPE"] or "bilinmiyor"
    record("oturum", "Oturum türü", INFO, session)
    record("oturum", "WAYLAND_DISPLAY / DISPLAY", INFO,
           f"{env['WAYLAND_DISPLAY'] or '-'} / {env['DISPLAY'] or '-'}"
           + (" (XWayland var: eski X11 uygulamaları çalışabilir)" if env["DISPLAY"] and session == "wayland" else ""))
    record("oturum", "Masaüstü", INFO, env["XDG_CURRENT_DESKTOP"] or "-")
    _, gs = run(["gnome-shell", "--version"])
    record("oturum", "GNOME Shell", INFO, gs)
    record("oturum", "Sistem", INFO, f"{platform.platform()} | Python {platform.python_version()}")
    _, groups = run(["id", "-nG"])
    record("oturum", "Kullanıcı grupları", INFO, groups)


# ── 2. Araçlar ───────────────────────────────────────────────────────────
TOOLS = {
    "ydotool": "Wayland'de klavye/fare (önerilen)",
    "ydotoold": "ydotool arka plan servisi",
    "wl-copy": "Wayland pano",
    "wl-paste": "Wayland pano",
    "xclip": "X11 pano",
    "wmctrl": "X11 pencere yönetimi",
    "xdotool": "X11 klavye/fare/pencere",
    "notify-send": "bildirim",
    "playerctl": "medya kontrolü",
    "pactl": "ses seviyesi",
    "brightnessctl": "parlaklık",
    "gnome-screenshot": "ekran görüntüsü",
    "gdbus": "D-Bus çağrıları (portal, GNOME)",
    "gtk-launch": "uygulama açma",
    "xdg-open": "dosya/URL açma",
    "tesseract": "ekrandan yazı okuma (OCR)",
    "go": "Go derleyici",
    "golangci-lint": "Go denetleyici",
}


def check_tools() -> None:
    print("\n[2] Komut satırı araçları")
    for tool, why in TOOLS.items():
        path = shutil.which(tool)
        record("araclar", tool, OK if path else WARN, f"{why}" + ("" if path else " — KURULU DEĞİL"))
    if shutil.which("tesseract"):
        _, langs = run(["tesseract", "--list-langs"])
        record("araclar", "tesseract Türkçe", OK if "\ntur" in "\n" + langs else WARN,
               "tur dil paketi " + ("var" if "\ntur" in "\n" + langs else "yok (tesseract-ocr-tur)"))


# ── 3. Python kütüphaneleri ──────────────────────────────────────────────
def check_python_libs() -> dict:
    print(f"\n[3] Python kütüphaneleri (bu Python: {sys.executable})")
    mods = {}
    for name in ("pyautogui", "mss", "pyperclip", "pygetwindow", "sounddevice", "PyQt6", "playwright", "PIL"):
        try:
            mods[name] = importlib.import_module(name)
            record("python", name, OK, getattr(mods[name], "__version__", ""))
        except Exception as e:  # noqa: BLE001
            record("python", name, FAIL if name in ("pyautogui", "mss", "pyperclip") else WARN,
                   f"{type(e).__name__}: {str(e)[:120]}")
    return mods


# ── 4. Pano ──────────────────────────────────────────────────────────────
def check_clipboard(mods: dict) -> None:
    print("\n[4] Pano")
    pc = mods.get("pyperclip")
    if pc is None:
        record("pano", "pyperclip kopyala/yapıştır", FAIL, "pyperclip yüklenemedi")
        return
    token = "jarvis-test-" + secrets.token_hex(4)
    try:
        old = pc.paste()
    except Exception:  # noqa: BLE001
        old = None
    try:
        pc.copy(token)
        time.sleep(0.3)
        got = pc.paste()
        record("pano", "pyperclip kopyala/yapıştır", OK if got == token else FAIL,
               "gidip geldi" if got == token else f"geri okunan: {got[:40]!r}")
    except Exception as e:  # noqa: BLE001
        record("pano", "pyperclip kopyala/yapıştır", FAIL, f"{type(e).__name__}: {str(e)[:150]}")
    finally:
        if old is not None:
            try:
                pc.copy(old)
            except Exception:  # noqa: BLE001
                pass


# ── 5. Ekran görüntüsü ───────────────────────────────────────────────────
def check_screenshots(mods: dict) -> None:
    print("\n[5] Ekran görüntüsü")
    pag = mods.get("pyautogui")
    if pag is not None:
        try:
            v = image_verdict(pag.screenshot())
            record("ekran", "pyautogui.screenshot()", *v)
        except Exception as e:  # noqa: BLE001
            record("ekran", "pyautogui.screenshot()", FAIL, f"{type(e).__name__}: {str(e)[:150]}")
    mss_mod = mods.get("mss")
    if mss_mod is not None:
        try:
            from PIL import Image
            with mss_mod.mss() as s:
                shot = s.grab(s.monitors[1])
                img = Image.frombytes("RGB", shot.size, shot.rgb)
            record("ekran", "mss", *image_verdict(img))
        except Exception as e:  # noqa: BLE001
            record("ekran", "mss", FAIL, f"{type(e).__name__}: {str(e)[:150]}")
    if shutil.which("gnome-screenshot"):
        out = Path(tempfile.gettempdir()) / f"jarvis_teshis_{os.getpid()}.png"
        code, text = run(["gnome-screenshot", "-f", str(out)], timeout=15)
        if out.is_file():
            try:
                from PIL import Image
                with Image.open(out) as img:
                    record("ekran", "gnome-screenshot", *image_verdict(img))
            finally:
                out.unlink(missing_ok=True)
        else:
            record("ekran", "gnome-screenshot", FAIL, f"dosya oluşmadı (kod {code}): {text[:150]}")
    # Portal: yalnızca VAR MI diye bakılır; gerçek çekim izin penceresi açabileceği için denenmez.
    code, text = run(["gdbus", "introspect", "--session", "--dest", "org.freedesktop.portal.Desktop",
                      "--object-path", "/org/freedesktop/portal/desktop"])
    has = code == 0 and "org.freedesktop.portal.Screenshot" in text
    record("ekran", "xdg-desktop-portal Screenshot arayüzü", OK if has else WARN,
           "mevcut (Wayland'de önerilen yol)" if has else f"bulunamadı: {text[:120]}")


# ── 6. Klavye / fare ─────────────────────────────────────────────────────
def check_input(mods: dict) -> None:
    print("\n[6] Klavye / fare")
    pag = mods.get("pyautogui")
    if pag is not None:
        try:
            before = pag.position()
            pag.moveRel(1, 0, duration=0)
            time.sleep(0.2)
            moved = pag.position()
            pag.moveRel(-1, 0, duration=0)
            ok = moved != before
            record("girdi", "pyautogui fare hareketi", OK if ok else FAIL,
                   f"{before} → {moved}" + ("" if ok else " (hareket etmedi — Wayland kısıtı)"))
        except Exception as e:  # noqa: BLE001
            record("girdi", "pyautogui fare hareketi", FAIL, f"{type(e).__name__}: {str(e)[:150]}")
    uinput = Path("/dev/uinput")
    if uinput.exists():
        rw = os.access(uinput, os.R_OK | os.W_OK)
        record("girdi", "/dev/uinput erişimi (ydotool için)", OK if rw else WARN,
               "okunup yazılabilir" if rw else "izin yok — ydotool için bir kerelik udev kuralı gerekir")
    else:
        record("girdi", "/dev/uinput", FAIL, "yok (uinput çekirdek modülü yüklenmemiş)")
    code, _ = run(["pgrep", "-x", "ydotoold"])
    record("girdi", "ydotoold servisi", OK if code == 0 else WARN, "çalışıyor" if code == 0 else "çalışmıyor")


# ── 7. Pencereler ────────────────────────────────────────────────────────
def check_windows() -> None:
    print("\n[7] Pencere yönetimi")
    if shutil.which("wmctrl"):
        code, text = run(["wmctrl", "-l"])
        n = len([ln for ln in text.splitlines() if ln.strip()]) if code == 0 else 0
        record("pencere", "wmctrl -l", OK if n else WARN,
               f"{n} pencere görüyor (yalnızca X11/XWayland pencereleri)" if code == 0 else text[:120])
    code, text = run(["gdbus", "call", "--session", "--dest", "org.gnome.Shell",
                      "--object-path", "/org/gnome/Shell/Extensions/Windows",
                      "--method", "org.gnome.Shell.Extensions.Windows.List"])
    record("pencere", "GNOME 'Window Calls' eklentisi", OK if code == 0 else WARN,
           "kurulu, tüm pencereler listelenebilir" if code == 0 else "kurulu değil (Wayland'de pencere listesi için önerilen)")


# ── 8. Ses / medya / parlaklık / bildirim ────────────────────────────────
def check_system() -> None:
    print("\n[8] Ses, medya, parlaklık, bildirim")
    code, text = run(["pactl", "info"])
    server = next((ln.split(":", 1)[1].strip() for ln in text.splitlines() if ln.startswith(("Server Name", "Sunucu Adı"))), text[:80])
    record("sistem", "pactl (ses sunucusu)", OK if code == 0 else FAIL, server)
    code, text = run(["pactl", "get-sink-volume", "@DEFAULT_SINK@"])
    record("sistem", "ses seviyesi okuma", OK if code == 0 else FAIL, text.splitlines()[0][:100] if text else "")
    code, text = run(["playerctl", "-l"])
    record("sistem", "playerctl", OK if code is not None else WARN,
           (text.replace("\n", ", ") or "komut çalışıyor (şu an çalan oynatıcı yok)") if code is not None else text)
    code, text = run(["brightnessctl", "-m", "info"])
    record("sistem", "brightnessctl okuma", OK if code == 0 else WARN, text[:100] or "ekran parlaklık aygıtı yok (harici monitör olabilir)")
    code, text = run(["notify-send", "-a", "JARVIS", "JARVIS teşhis", "Bu bildirim görünüyorsa bildirimler çalışıyor."])
    record("sistem", "notify-send", OK if code == 0 else FAIL, "gönderildi — ekranda gördün mü?" if code == 0 else text[:120])


def main() -> int:
    print("JARVIS Wayland teşhisi —", time.strftime("%Y-%m-%d %H:%M"))
    check_session()
    check_tools()
    mods = check_python_libs()
    check_clipboard(mods)
    check_screenshots(mods)
    check_input(mods)
    check_windows()
    check_system()

    root = Path(__file__).resolve().parent.parent
    out_dir = root / ".jarvis_kontrol"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / "wayland_teshis.json"
    out.write_text(json.dumps(RESULTS, ensure_ascii=False, indent=2), encoding="utf-8")

    fails = sum(r["status"] == FAIL for r in RESULTS)
    warns = sum(r["status"] == WARN for r in RESULTS)
    print(f"\nÖzet: {fails} başarısız, {warns} uyarı. Rapor: {out.relative_to(root)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
