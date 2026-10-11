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


def _setup_crash_logging() -> None:
    """Masaüstü kısayolu Terminal=false ile açıldığında print()/traceback
    çıktıları hiçbir yere yazılmıyordu; "1 dakikada kapanma" gibi
    sorunların nedeni görünmüyordu. Bu fonksiyon:
      * her başlatmayı ve normal çıkışı logs/jarvis_runtime.log'a yazar,
      * yakalanmamış Python hatalarını (ana ve arka plan thread'leri) yazar,
      * faulthandler ile yerel çökmeleri (segfault vb.) thread dökümüyle yazar,
      * terminal yoksa stdout/stderr'i de aynı dosyaya yönlendirir.
    Log'da "normal çıkış" ya da "shutdown_jarvis" satırı olmadan biten bir
    oturum = süreç dışarıdan öldürüldü (ör. OOM/SIGKILL)."""
    import atexit
    import faulthandler
    import threading
    import traceback
    from datetime import datetime

    try:
        from jarvis.paths import logs_dir

        log_path = logs_dir() / "jarvis_runtime.log"
        if log_path.exists() and log_path.stat().st_size > 5 * 1024 * 1024:
            log_path.replace(log_path.with_name("jarvis_runtime.log.1"))
        fh = open(log_path, "a", encoding="utf-8", buffering=1)
    except Exception:
        # DUZELTME (2026-09-28, testte bulundu): sadece OSError degil,
        # beklenmeyen HERHANGI bir hata (ör. ImportError) da bu kurulumu
        # bozabilir - fonksiyonun kendi amaci ('log kurulamazsa JARVIS
        # yine de acilmali') OSError disini de kapsar, yoksa bu guvenlik
        # agi tam tersine JARVIS'i cokertecek bir tek hata noktasi olur.
        return  # log kurulamazsa JARVIS yine de açılmalı

    def _stamp() -> str:
        return datetime.now().isoformat(timespec="seconds")

    fh.write(f"\n===== JARVIS başlatıldı {_stamp()} pid={os.getpid()} =====\n")
    faulthandler.enable(file=fh, all_threads=True)

    if not (sys.stderr is not None and sys.stderr.isatty()):
        sys.stdout = fh
        sys.stderr = fh

    def _excepthook(exc_type, exc, tb) -> None:
        fh.write(f"[{_stamp()}] YAKALANMAMIŞ HATA (ana thread):\n")
        traceback.print_exception(exc_type, exc, tb, file=fh)

    def _thread_excepthook(args) -> None:
        name = args.thread.name if args.thread else "?"
        fh.write(f"[{_stamp()}] YAKALANMAMIŞ HATA (thread={name}):\n")
        traceback.print_exception(args.exc_type, args.exc_value, args.exc_traceback, file=fh)

    sys.excepthook = _excepthook
    threading.excepthook = _thread_excepthook
    atexit.register(lambda: fh.write(f"===== normal çıkış {_stamp()} =====\n"))


def _run_ui_only() -> None:
    # Deliberately import only the UI. In particular, do not import jarvis.main:
    # it owns audio, Gemini, and the backend task loop.
    from jarvis.ui import JarvisUI

    ui = JarvisUI(_face_path(), backend_enabled=False)
    ui.root.mainloop()


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="jarvis", description="MuratJARVIS")
    parser.add_argument(
        "--ui-only",
        action="store_true",
        help="Yalnızca PyQt arayüzünü aç; backend, ses ve ağ başlatma.",
    )
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    _setup_crash_logging()
    if args.ui_only:
        _run_ui_only()
        return

    from jarvis.main import main as run

    run()


if __name__ == "__main__":
    main()
