"""pyautogui tuş adlarını Linux evdev tuş kodlarına çevirir (ydotool 1.x 'key' komutu için).

Kodlar linux/input-event-codes.h ile birebir aynıdır. ydotool FİZİKSEL tuş
kodu gönderir; harfin ne üreteceğini etkin klavye düzeni belirler (bkz.
wayland.py'deki write() notu).
"""
from __future__ import annotations

_LETTERS = {
    "q": 16, "w": 17, "e": 18, "r": 19, "t": 20, "y": 21, "u": 22, "i": 23, "o": 24, "p": 25,
    "a": 30, "s": 31, "d": 32, "f": 33, "g": 34, "h": 35, "j": 36, "k": 37, "l": 38,
    "z": 44, "x": 45, "c": 46, "v": 47, "b": 48, "n": 49, "m": 50,
}
_DIGITS = {"1": 2, "2": 3, "3": 4, "4": 5, "5": 6, "6": 7, "7": 8, "8": 9, "9": 10, "0": 11}
_FKEYS = {f"f{i}": 58 + i for i in range(1, 11)} | {"f11": 87, "f12": 88} | {f"f{i}": 170 + i for i in range(13, 25)}

_NAMED = {
    # değiştiriciler
    "ctrl": 29, "ctrlleft": 29, "control": 29, "ctrlright": 97,
    "shift": 42, "shiftleft": 42, "shiftright": 54,
    "alt": 56, "altleft": 56, "option": 56, "altright": 100, "altgr": 100,
    # Linux'ta "win"/"super"/"command" aynı Super (Meta) tuşudur.
    "win": 125, "winleft": 125, "super": 125, "command": 125, "cmd": 125, "meta": 125, "winright": 126,
    # düzen / gezinme
    "enter": 28, "return": 28, "tab": 15, "space": 57, "backspace": 14,
    "esc": 1, "escape": 1, "delete": 111, "del": 111, "insert": 110,
    "home": 102, "end": 107, "pageup": 104, "pgup": 104, "pagedown": 109, "pgdn": 109,
    "up": 103, "down": 108, "left": 105, "right": 106,
    "capslock": 58, "numlock": 69, "scrolllock": 70, "pause": 119,
    "printscreen": 99, "print_screen": 99, "prtsc": 99, "prtscr": 99, "apps": 127, "menu": 127,
    # noktalama (ABD fiziksel konumları)
    "minus": 12, "-": 12, "equal": 13, "=": 13, "plus": 13,
    "bracketleft": 26, "[": 26, "bracketright": 27, "]": 27,
    "semicolon": 39, ";": 39, "apostrophe": 40, "'": 40, "grave": 41, "`": 41,
    "backslash": 43, "\\": 43, "comma": 51, ",": 51, "period": 52, ".": 52, "slash": 53, "/": 53,
    # medya
    "volumemute": 113, "volumedown": 114, "volumeup": 115,
    "playpause": 164, "nexttrack": 163, "prevtrack": 165, "stop": 166,
}

KEYCODES: dict[str, int] = {**_LETTERS, **_DIGITS, **_FKEYS, **_NAMED}

# Linux'ta karşılığı olmayan, sessizce yok sayılacak tuşlar (ör. dizüstü "fn"
# donanımda işlenir, işletim sistemine hiç ulaşmaz).
IGNORED = frozenset({"fn"})


class UnknownKeyError(ValueError):
    pass


def keycode(name: str) -> int | None:
    """Tuş adının evdev kodunu döndürür; yok sayılan tuşlar için None."""
    key = str(name).strip().lower()
    if key in IGNORED:
        return None
    if len(name) == 1 and name.isupper() and name.lower() in _LETTERS:
        key = name.lower()
    try:
        return KEYCODES[key]
    except KeyError:
        raise UnknownKeyError(f"Bilinmeyen tuş: {name!r}") from None
