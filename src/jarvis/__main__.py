"""`python -m jarvis` / `jarvis` komutunun giriş noktası.

`--ui-only`, gerçek backend, ağ, ses ve API anahtarı başlatmadan yalnızca
arayüzü açan bir paketleme/yerel UI tanılama yoludur. Normal başlatma yolu
mevcut `jarvis.main.main` API'sini korur.
"""
from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence


def _face_path() -> str:
    from jarvis.paths import asset

    return os.environ.get("JARVIS_FACE", "").strip() or str(asset("jarvis_icon.png"))


def _run_ui_only() -> None:
    # Deliberately import only the UI. In particular, do not import jarvis.main:
    # it owns audio, Gemini, and the backend task loop.
    from jarvis.ui import JarvisUI

    ui = JarvisUI(_face_path())
    ui.root.mainloop()


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="jarvis", description="MuratJARVIS")
    parser.add_argument(
        "--ui-only",
        action="store_true",
        help="Yalnızca PyQt arayüzünü aç; backend, ses ve ağ başlatma.",
    )
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    if args.ui_only:
        _run_ui_only()
        return

    from jarvis.main import main as run

    run()


if __name__ == "__main__":
    main()
