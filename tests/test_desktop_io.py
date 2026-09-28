"""desktop_io testleri — gerçek ydotool/ekran olmadan, sahte çalıştırıcıyla."""
from __future__ import annotations

import importlib
import sys

import pytest

from jarvis.desktop_io.keys import UnknownKeyError, keycode
from jarvis.desktop_io.wayland import DesktopIOError, WaylandBackend, keyboard_layout


class FakeRunner:
    def __init__(self, clipboard: bytes | None = b"eski pano", fail: set[str] | None = None,
                 layout_out: bytes = b"[('xkb', 'tr')]"):
        self.calls: list[tuple[list[str], bytes | None]] = []
        self.clipboard = clipboard
        self.fail = fail or set()
        self.layout_out = layout_out

    def __call__(self, args, stdin=None):
        args = list(args)
        self.calls.append((args, stdin))
        if args[0] in self.fail:
            return 1, b"hata"
        if args[0] == "gsettings":
            return 0, self.layout_out
        if args[0] == "wl-paste":
            return (0, self.clipboard) if self.clipboard is not None else (1, b"No selection")
        if args[:2] == ["wl-copy", "--clear"]:
            self.clipboard = None
        elif args[0] == "wl-copy":
            self.clipboard = stdin
        return 0, b""

    def ydotool(self):
        return [a[1:] for a, _ in self.calls if a[0] == "ydotool"]


def backend(runner, layout=None):
    b = WaylandBackend(runner=runner, layout=layout)
    b.PAUSE = 0
    return b


# ── tuş kodları ──────────────────────────────────────────────────────────
def test_keycodes_cover_every_key_jarvis_uses():
    used = ["command", "ctrl", "win", "enter", "shift", "tab", "w", "v", "down", "alt", "z",
            "volumemute", "up", "s", "right", "left", "l", "f", "a", "x", "t", "super", "space",
            "r", "minus", "f11", "equal", "delete", "d", "c", "0", "y", "volumeup", "volumedown",
            "q", "print_screen", "pageup", "pagedown", "m", "i", "home", "f5", "f4", "escape",
            "esc", "end", "e", "bracketright", "bracketleft", "3"]
    for k in used:
        assert isinstance(keycode(k), int), k
    assert keycode("fn") is None
    assert keycode("A") == keycode("a") == 30
    assert keycode("ctrl") == 29 and keycode("win") == keycode("command") == 125
    with pytest.raises(UnknownKeyError):
        keycode("nosuchkey")


# ── klavye ───────────────────────────────────────────────────────────────
def test_hotkey_presses_in_order_and_releases_in_reverse():
    r = FakeRunner()
    backend(r).hotkey("ctrl", "shift", "t")
    assert r.ydotool() == [["key", "29:1", "42:1", "20:1", "20:0", "42:0", "29:0"]]


def test_press_repeats_and_ignores_fn():
    r = FakeRunner()
    b = backend(r)
    b.press("volumeup", presses=3)
    b.press("fn")
    assert r.ydotool() == [["key", "115:1", "115:0"]] * 3


def test_write_turkish_layout_uses_clipboard_and_restores_it():
    r = FakeRunner(clipboard=b"eski pano")
    backend(r, layout="tr").write("Şişli'de ığdır")
    copies = [stdin for a, stdin in r.calls if a == ["wl-copy"]]
    assert copies == ["Şişli'de ığdır".encode(), b"eski pano"]
    assert ["key", "29:1", "47:1", "47:0", "29:0"] in r.ydotool()  # Ctrl+V
    assert r.clipboard == b"eski pano"


def test_write_empty_clipboard_is_cleared_after_paste():
    r = FakeRunner(clipboard=None)
    backend(r, layout="tr").write("merhaba")
    assert r.clipboard is None
    assert (["wl-copy", "--clear"], None) in r.calls


def test_write_us_layout_ascii_types_directly():
    r = FakeRunner()
    backend(r, layout="us").write("hello; rm -rf /", interval=0.05)
    assert r.ydotool() == [["type", "--key-delay", "50", "--", "hello; rm -rf /"]]
    assert not any(a[0].startswith("wl-") for a, _ in r.calls)


def test_write_us_layout_non_ascii_falls_back_to_paste():
    r = FakeRunner()
    backend(r, layout="us").write("çay")
    assert any(a == ["wl-copy"] and s == "çay".encode() for a, s in r.calls)


def test_keyboard_layout_detection():
    assert keyboard_layout(FakeRunner(layout_out=b"[('xkb', 'tr'), ('xkb', 'us')]")) == "tr"
    assert keyboard_layout(FakeRunner(layout_out=b"[('xkb', 'us+intl')]")) == "us"
    assert keyboard_layout(FakeRunner(fail={"gsettings"})) == ""


# ── fare ─────────────────────────────────────────────────────────────────
def test_click_move_scroll_drag_commands():
    r = FakeRunner()
    b = backend(r)
    b.click(100, 200)
    b.click(button="right", clicks=2)
    b.scroll(500)
    b.scroll(-120)
    b.hscroll(240)
    b.dragTo(10, 20)
    assert r.ydotool() == [
        ["mousemove", "--absolute", "-x", "100", "-y", "200"],
        ["click", "0xc0"],
        ["click", "0xc1"], ["click", "0xc1"],
        ["mousemove", "--wheel", "-x", "0", "-y", "4"],
        ["mousemove", "--wheel", "-x", "0", "-y", "-1"],
        ["mousemove", "--wheel", "-x", "2", "-y", "0"],
        ["click", "0x40"],
        ["mousemove", "--absolute", "-x", "10", "-y", "20"],
        ["click", "0x80"],
    ]


def test_drag_releases_button_even_if_move_fails():
    calls = []

    def runner(args, stdin=None):
        calls.append(list(args))
        return (1, b"boom") if "--absolute" in args else (0, b"")

    b = backend(runner)
    with pytest.raises(DesktopIOError):
        b.dragTo(1, 2)
    assert calls[-1] == ["ydotool", "click", "0x80"]


def test_ydotool_failure_raises_clear_error():
    b = backend(FakeRunner(fail={"ydotool"}))
    with pytest.raises(DesktopIOError, match="ydotool key"):
        b.press("enter")


# ── arka uç seçimi ───────────────────────────────────────────────────────
def _reload(monkeypatch, env, which):
    for k in ("XDG_SESSION_TYPE", "WAYLAND_DISPLAY", "JARVIS_DESKTOP_BACKEND"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr("shutil.which", which)
    monkeypatch.setattr(sys, "platform", "linux")
    sys.modules.pop("jarvis.desktop_io", None)
    return importlib.import_module("jarvis.desktop_io")


def test_selects_wayland_backend_when_ydotool_present(monkeypatch):
    mod = _reload(monkeypatch, {"XDG_SESSION_TYPE": "wayland"}, lambda n: "/usr/bin/" + n)
    assert mod.backend_name() == "wayland-ydotool"
    mod.gui.PAUSE = 0.05  # pyautogui gibi öznitelik ataması desteklenir
    assert mod.gui.FAILSAFE is False


def test_falls_back_to_pyautogui_and_raises_importerror_when_unusable(monkeypatch):
    fake = type(sys)("pyautogui")
    monkeypatch.setitem(sys.modules, "pyautogui", fake)
    mod = _reload(monkeypatch, {"XDG_SESSION_TYPE": "x11"}, lambda n: None)
    assert mod.gui is fake
    # Alt modüller, arka uç olmasa bile içe aktarılabilir olmalı.
    importlib.import_module("jarvis.desktop_io.portal")

    class Boom(Exception):
        pass

    def broken_import(name, *a, **k):
        if name == "pyautogui":
            raise Boom("Can't connect to display")
        return real_import(name, *a, **k)

    import builtins
    real_import = builtins.__import__
    monkeypatch.delitem(sys.modules, "pyautogui")
    monkeypatch.setattr(builtins, "__import__", broken_import)
    mod = _reload(monkeypatch, {"XDG_SESSION_TYPE": "wayland"}, lambda n: None)
    with pytest.raises(ImportError, match="pyautogui kullanılamıyor"):
        from jarvis.desktop_io import gui  # noqa: F401
    # Çağıranların kalıbı: hata -> pyautogui = None
    try:
        from jarvis.desktop_io import gui as pyautogui
    except Exception:
        pyautogui = None
    assert pyautogui is None and mod is not None
