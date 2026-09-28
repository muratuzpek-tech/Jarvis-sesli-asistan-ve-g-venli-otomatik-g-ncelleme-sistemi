"""Canlı öz-test: .venv/bin/python -m jarvis.desktop_io

Güvenli: yazı yazmaz, tıklamaz. Yalnızca (1) hangi arka ucun seçildiğini
söyler, (2) ekran boyutunu okur, (3) fareyi 1 piksel oynatıp geri alır,
(4) Shift tuşuna basıp bırakır, (5) ekran görüntüsü alıp içeriğini ölçer
(ilk seferde GNOME izin penceresi çıkabilir — 'İzin ver' de).
"""
from __future__ import annotations

import sys
import time


def main() -> int:
    try:
        from jarvis.desktop_io import backend_name, gui
    except ImportError as e:
        print(f"❌ Arka uç yok: {e}")
        return 1
    print(f"ℹ️ Arka uç: {backend_name()}")
    failures = 0

    def step(name, fn):
        nonlocal failures
        try:
            detail = fn()
            print(f"✅ {name}" + (f" — {detail}" if detail else ""))
        except Exception as e:  # noqa: BLE001 - öz-test her hatayı raporlar
            failures += 1
            print(f"❌ {name} — {type(e).__name__}: {e}")

    step("Ekran boyutu", lambda: "x".join(map(str, gui.size())))

    def mouse():
        gui.moveRel(1, 0)
        gui.moveRel(-1, 0)
        return "1 piksel sağa ve geri"
    step("Fare", mouse)

    def shift():
        gui.press("shift")
        return "Shift basıldı/bırakıldı"
    step("Klavye", shift)

    def shot():
        from PIL import ImageStat

        t0 = time.time()
        img = gui.screenshot()
        std = ImageStat.Stat(img.convert("L").resize((64, 36))).stddev[0]
        if std < 2:
            raise RuntimeError(f"{img.width}x{img.height} ama görüntü tek renk (siyah/boş)")
        return f"{img.width}x{img.height}, gerçek içerik (kontrast {std:.0f}), {time.time() - t0:.1f} sn"
    step("Ekran görüntüsü", shot)

    print("\nSonuç:", "hepsi çalışıyor" if not failures else f"{failures} adım başarısız")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
