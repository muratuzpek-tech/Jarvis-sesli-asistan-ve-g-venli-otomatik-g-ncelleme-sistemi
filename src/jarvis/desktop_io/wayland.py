"""Wayland arka ucu: klavye/fare = ydotool, pano = wl-clipboard, ekran = portal.

Komutlar shell'siz (liste argümanlı) ve zaman aşımlı çalışır. Çalıştırıcı
(runner) enjekte edilebilir; testler gerçek ydotool olmadan komutları doğrular.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable, Sequence

from jarvis.desktop_io.keys import keycode

Runner = Callable[[Sequence[str], "bytes | None"], "tuple[int, bytes]"]

# ydotool 1.x "click" kodları: alt 4 bit = düğme, 0x40 = bas, 0x80 = bırak.
_BUTTONS = {"left": 0, "right": 1, "middle": 2}
_DOWN, _UP = 0x40, 0x80

# pyautogui.scroll(): Windows'ta tipik değerler 120'nin katları (bir "çentik").
_WHEEL_UNIT = 120


def _default_runner(args: Sequence[str], stdin: bytes | None = None) -> tuple[int, bytes]:
    env = dict(os.environ)
    if "YDOTOOL_SOCKET" not in env:
        sock = os.path.join(env.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"), ".ydotool_socket")
        if os.path.exists(sock):
            env["YDOTOOL_SOCKET"] = sock
    p = subprocess.run(list(args), input=stdin, capture_output=True, timeout=10, env=env, check=False)
    return p.returncode, p.stdout + p.stderr


class DesktopIOError(RuntimeError):
    pass


def keyboard_layout(runner: Runner | None = None) -> str:
    """GNOME'un İLK giriş kaynağının düzen kodu (ör. 'tr', 'us'); bilinmiyorsa ''."""
    run = runner or _default_runner
    try:
        code, out = run(["gsettings", "get", "org.gnome.desktop.input-sources", "sources"], None)
    except (OSError, subprocess.SubprocessError):
        return ""
    m = re.search(r"\('xkb',\s*'([^']+)'\)", out.decode(errors="replace")) if code == 0 else None
    return m.group(1).split("+")[0] if m else ""


class WaylandBackend:
    name = "wayland-ydotool"

    def __init__(self, runner: Runner | None = None, layout: str | None = None) -> None:
        self._run = runner or _default_runner
        self._layout = layout
        self.PAUSE = 0.1

    # ── yardımcılar ──────────────────────────────────────────────────────
    def _ydotool(self, *args: str) -> None:
        code, out = self._run(["ydotool", *args], None)
        if code != 0:
            raise DesktopIOError(f"ydotool {' '.join(args)} başarısız: {out.decode(errors='replace')[:200]}")

    def _pause(self) -> None:
        if self.PAUSE:
            time.sleep(self.PAUSE)

    @property
    def layout(self) -> str:
        if self._layout is None:
            self._layout = keyboard_layout(self._run)
        return self._layout

    # ── klavye ───────────────────────────────────────────────────────────
    def hotkey(self, *keys: str, **_: object) -> None:
        codes = [c for c in (keycode(k) for k in keys) if c is not None]
        if not codes:
            return
        events = [f"{c}:1" for c in codes] + [f"{c}:0" for c in reversed(codes)]
        self._ydotool("key", *events)
        self._pause()

    def press(self, keys: str | Sequence[str], presses: int = 1, interval: float = 0.0, **_: object) -> None:
        names = [keys] if isinstance(keys, str) else list(keys)
        for i in range(max(1, int(presses))):
            for name in names:
                code = keycode(name)
                if code is not None:
                    self._ydotool("key", f"{code}:1", f"{code}:0")
            if interval and i < presses - 1:
                time.sleep(interval)
        self._pause()

    def keyDown(self, key: str) -> None:  # noqa: N802 - pyautogui adı
        code = keycode(key)
        if code is not None:
            self._ydotool("key", f"{code}:1")

    def keyUp(self, key: str) -> None:  # noqa: N802 - pyautogui adı
        code = keycode(key)
        if code is not None:
            self._ydotool("key", f"{code}:0")

    def write(self, text: str, interval: float = 0.0, **_: object) -> None:
        """Metni yazar.

        ydotool FİZİKSEL tuş kodu gönderir; Türkçe Q düzeninde 'i' tuşu 'ı'
        üretir, ş/ğ/ü gibi harfler hiç yazılamaz. Bu yüzden düzen 'us' değilse
        (veya metin ASCII dışı karakter içeriyorsa) metin panoya konup
        Ctrl+V ile yapıştırılır; panodaki önceki içerik geri yüklenir.
        """
        text = str(text)
        if not text:
            return
        if self.layout == "us" and text.isascii():
            args = ["type"]
            if interval:
                args += ["--key-delay", str(max(1, int(interval * 1000)))]
            self._ydotool(*args, "--", text)
        else:
            self._paste(text)
        self._pause()

    typewrite = write

    def _paste(self, text: str) -> None:
        if not shutil.which("wl-copy") and self._run is _default_runner:
            raise DesktopIOError("wl-clipboard kurulu değil (sudo apt install wl-clipboard)")
        code, previous = self._run(["wl-paste", "--no-newline"], None)
        had_previous = code == 0
        if self._run is not _default_runner:
            code, out = self._run(["wl-copy"], text.encode("utf-8"))
            if code != 0:
                raise DesktopIOError(f"wl-copy başarısız: {out.decode(errors='replace')[:200]}")
            time.sleep(0.05)
            self.hotkey("ctrl", "v")
            time.sleep(0.15)
            if had_previous:
                self._run(["wl-copy"], previous)
            else:
                self._run(["wl-copy", "--clear"], None)
            return

        # wl-copy deliberately stays alive while it owns the clipboard. Waiting
        # with subprocess.run() deadlocks before Ctrl+V can be sent, so keep the
        # owner process alive only until the target application consumes the paste.
        env = dict(os.environ)
        process = subprocess.Popen(
            ["wl-copy"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env=env,
        )
        try:
            if process.stdin is None:
                raise DesktopIOError("wl-copy stdin açılamadı")
            process.stdin.write(text.encode("utf-8"))
            process.stdin.close()
            time.sleep(0.05)
            if process.poll() not in (None, 0):
                error = process.stderr.read().decode(errors="replace") if process.stderr else ""
                raise DesktopIOError(f"wl-copy başarısız: {error[:200]}")
            self.hotkey("ctrl", "v")
            time.sleep(0.15)
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)

    # ── fare ─────────────────────────────────────────────────────────────
    def moveTo(self, x: float | None = None, y: float | None = None, duration: float = 0.0, **_: object) -> None:  # noqa: N802
        if x is None or y is None:
            return
        self._ydotool("mousemove", "--absolute", "-x", str(int(x)), "-y", str(int(y)))
        self._pause()

    def moveRel(self, xOffset: float = 0, yOffset: float = 0, duration: float = 0.0, **_: object) -> None:  # noqa: N802,N803
        self._ydotool("mousemove", "-x", str(int(xOffset)), "-y", str(int(yOffset)))
        self._pause()

    move = moveRel

    def click(self, x: float | None = None, y: float | None = None, clicks: int = 1,
              interval: float = 0.0, button: str = "left", **_: object) -> None:
        if x is not None and y is not None:
            self.moveTo(x, y)
        btn = _BUTTONS.get(str(button).lower(), 0)
        for i in range(max(1, int(clicks))):
            self._ydotool("click", hex(_DOWN | _UP | btn))
            if interval and i < clicks - 1:
                time.sleep(interval)
        self._pause()

    def doubleClick(self, x=None, y=None, **kw) -> None:  # noqa: N802
        self.click(x, y, clicks=2, interval=0.05, **kw)

    def rightClick(self, x=None, y=None, **kw) -> None:  # noqa: N802
        self.click(x, y, button="right", **kw)

    def dragTo(self, x: float, y: float, duration: float = 0.0, button: str = "left", **_: object) -> None:  # noqa: N802
        btn = _BUTTONS.get(str(button).lower(), 0)
        self._ydotool("click", hex(_DOWN | btn))
        try:
            self.moveTo(x, y)
        finally:
            self._ydotool("click", hex(_UP | btn))
        self._pause()

    def _wheel(self, dx: int, dy: int) -> None:
        self._ydotool("mousemove", "--wheel", "-x", str(dx), "-y", str(dy))

    @staticmethod
    def _notches(amount: float) -> int:
        n = max(1, min(50, round(abs(amount) / _WHEEL_UNIT))) if abs(amount) >= 1 else 0
        return n if amount > 0 else -n

    def scroll(self, clicks: float, x=None, y=None, **_: object) -> None:
        """Pozitif = yukarı (pyautogui ile aynı). Değerler 120'lik 'çentik'lere çevrilir."""
        if x is not None and y is not None:
            self.moveTo(x, y)
        n = self._notches(clicks)
        if n:
            self._wheel(0, n)
        self._pause()

    vscroll = scroll

    def hscroll(self, clicks: float, x=None, y=None, **_: object) -> None:
        if x is not None and y is not None:
            self.moveTo(x, y)
        n = self._notches(clicks)
        if n:
            self._wheel(n, 0)
        self._pause()

    # ── ekran ────────────────────────────────────────────────────────────
    def screenshot(self, imageFilename: str | None = None, region=None, **_: object):  # noqa: N803
        from jarvis.desktop_io.portal import screenshot_image

        img = screenshot_image(region)
        if imageFilename:
            img.save(imageFilename)
        return img

    def size(self) -> tuple[int, int]:
        """Birincil ekran boyutu. mss Wayland'de piksel veremese de monitör
        geometrisini doğru bildiriyor; o da olmazsa portal görüntüsünden ölçülür."""
        try:
            import mss

            factory = getattr(mss, "MSS", None) or mss.mss
            with factory() as s:
                mon = s.monitors[1]
                return int(mon["width"]), int(mon["height"])
        except Exception:  # noqa: BLE001
            img = self.screenshot()
            return img.width, img.height

    def position(self) -> tuple[int, int]:
        raise DesktopIOError("Wayland'de fare konumu okunamaz (güvenlik kısıtı).")
