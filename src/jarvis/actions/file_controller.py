import os
import re
import shutil
import platform
import secrets
import time
import heapq
import tempfile
import errno
from pathlib import Path
from datetime import datetime

try:
    import send2trash
    _SEND2TRASH = True
except ImportError:
    _SEND2TRASH = False

_OS = platform.system()  # "Windows" | "Darwin" | "Linux"

_SAFE_ROOTS: list[Path] = [
    Path.home(),
]

# --- Windows "bilinen klasör" cozumu (OneDrive Klasor Tasima dahil) --------
#
# GERCEK YASANAN HATA: Path.home() / "Desktop" gibi saf birlestirme, OneDrive
# "Klasor Tasima" (Known Folder Move) aktifse YANLIS bir yola isaret ediyor.
# OneDrive KFM acildiginda kullanicinin GERCEK Masaustu/Belgeler/Indirilenler/
# Resimler/Muzik/Videolar klasorleri "C:\Users\<ad>\OneDrive\Masaustu" gibi bir
# yere tasinir; eski "C:\Users\<ad>\Desktop" ya bos kalir ya da hic yoktur.
# Jarvis eskiden hep bu ESKI, kullanicinin ARTIK GORMEDIGI klasoru kullaniyordu
# - dosya olusturma/silme "basarili" donuyordu ama kullanicinin gercek
# masaustunde hicbir sey degismiyordu (kullanicinin gercekten yasadigi hata).
#
# Dogru cozum: Windows'un SHGetKnownFolderPath Shell API'sini cagirmak - bu,
# OneDrive/Grup Ilkesi/manuel tasima dahil HER TURLU yonlendirmeyi dogru
# sekilde takip eder (registry/env-var parse etmekten cok daha guvenilir).
# Windows disinda ya da API basarisiz olursa eski Path.home()/<isim> davranisina
# duser - hicbir platformda regresyona yol acmaz.

_FOLDERID_GUIDS: dict[str, str] = {
    "desktop":   "B4BFCC3A-DB2C-424C-B029-7FE99A87C641",
    "documents": "FDD39AD0-238F-46AF-ADB4-6C85480369C7",
    "downloads": "374DE290-123F-4565-9164-39C4925E467B",
    "pictures":  "33E28130-4E1E-4676-835A-98395C3BC3BB",
    "music":     "4BD8D571-6D19-48D3-BE97-422220080E43",
    "videos":    "18989B1D-99B5-455B-841C-AB7C74E4DDFC",
}


def _known_folder_path(key: str) -> "Path | None":
    """Windows Shell API ile GERCEK bilinen klasor yolunu doner (OneDrive
    yonlendirmesi dahil). Windows disinda, API yoksa veya sonuc gecersizse
    None doner - cagiran taraf eski davranisa (Path.home()/<isim>) duser."""
    if _OS != "Windows":
        return None
    guid_str = _FOLDERID_GUIDS.get(key)
    if not guid_str:
        return None
    try:
        import ctypes
        from uuid import UUID

        class _GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", ctypes.c_ulong),
                ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort),
                ("Data4", ctypes.c_ubyte * 8),
            ]

        u = UUID(guid_str)
        time_low, time_mid, time_hi_version, clock_seq_hi_variant, clock_seq_low, node = u.fields
        guid = _GUID()
        guid.Data1 = time_low
        guid.Data2 = time_mid
        guid.Data3 = time_hi_version
        guid.Data4[0] = clock_seq_hi_variant
        guid.Data4[1] = clock_seq_low
        for i, b in enumerate(node.to_bytes(6, "big")):
            guid.Data4[2 + i] = b

        buf = ctypes.c_wchar_p()
        hresult = ctypes.windll.shell32.SHGetKnownFolderPath(
            ctypes.byref(guid), 0, 0, ctypes.byref(buf)
        )
        if hresult != 0 or not buf.value:
            return None
        path_str = buf.value
        try:
            ctypes.windll.ole32.CoTaskMemFree(buf)
        except Exception:
            pass
        p = Path(path_str)
        return p if p.is_dir() else None
    except Exception:
        return None

# Onaysiz calisan move_file/copy_file, yanlis anlasilan bir sesli komuttan
# dolayi dosyalarin yanlislikla tasinmasina yol acabiliyordu (gercek olay).
# Iki adimli onay: ilk cagri hicbir dosyaya dokunmaz, sadece bir kod uretir.
# Gercek islem SADECE dogru kodla, AYRI bir cagriyla gerceklesir.
_pending_file_ops: dict[str, tuple[str, Path, Path]] = {}
_pending_bulk_deletes: dict[str, tuple[Path, tuple[Path, ...]]] = {}

def _is_safe_path(target: Path) -> bool:
    """Verilen path _SAFE_ROOTS içinde mi? Değilse işlemi reddet."""
    try:
        resolved = target.resolve()
        return any(
            resolved == root.resolve() or resolved.is_relative_to(root.resolve())
            for root in _SAFE_ROOTS
        )
    except Exception:
        return False

# Linux'ta "İndirilenler", "Masaüstü" gibi YERELLEŞTİRİLMİŞ klasör adları
# ~/.config/user-dirs.dirs dosyasında durur; XDG_*_DIR ortam değişkenleri
# normalde TANIMLI DEĞİLDİR. Murat@goxs 2026-09-29: "İndirilenler klasörümde
# ne var" → Jarvis /home/murat/Downloads'a baktı, "klasör bulunamadı" dedi.
_XDG_KEYS = {
    "desktop": "XDG_DESKTOP_DIR", "downloads": "XDG_DOWNLOAD_DIR",
    "documents": "XDG_DOCUMENTS_DIR", "pictures": "XDG_PICTURES_DIR",
    "music": "XDG_MUSIC_DIR", "videos": "XDG_VIDEOS_DIR",
}
_ENGLISH_NAMES = {
    "desktop": "Desktop", "downloads": "Downloads", "documents": "Documents",
    "pictures": "Pictures", "music": "Music", "videos": "Videos",
}
# user-dirs.dirs yoksa denenecek yaygın yerel adlar (önce Türkçe).
_LOCAL_NAMES = {
    "desktop": ("Masaüstü",), "downloads": ("İndirilenler",), "documents": ("Belgeler",),
    "pictures": ("Resimler",), "music": ("Müzik",), "videos": ("Videolar",),
}


def _xdg_user_dir(key: str, home: "Path | None" = None) -> "Path | None":
    home = home or Path.home()
    var = _XDG_KEYS[key]
    env = os.environ.get(var, "")
    if env and Path(env).is_dir():
        return Path(env)
    cfg = Path(os.environ.get("XDG_CONFIG_HOME", "") or home / ".config") / "user-dirs.dirs"
    try:
        for line in cfg.read_text(encoding="utf-8").splitlines():
            m = re.match(rf'\s*{var}\s*=\s*"([^"]*)"', line)
            if m:
                value = m.group(1).replace("$HOME", str(home))
                p = Path(value).expanduser()
                # Ev klasörünün kendisine işaret ediyorsa (kullanıcı klasörü kapatmış) yok say.
                if p.is_dir() and p != home:
                    return p
    except OSError:
        pass
    return None


def _user_folder(key: str, home: "Path | None" = None) -> Path:
    home = home or Path.home()
    if _OS == "Linux":
        p = _xdg_user_dir(key, home)
        if p:
            return p
    else:
        kf = _known_folder_path(key)
        if kf:
            return kf
    english = home / _ENGLISH_NAMES[key]
    if not english.is_dir():
        for name in _LOCAL_NAMES[key]:
            if (home / name).is_dir():
                return home / name
    return english


def _get_desktop() -> Path:
    return _user_folder("desktop")


def _get_downloads() -> Path:
    return _user_folder("downloads")


def _get_documents() -> Path:
    return _user_folder("documents")


def _get_pictures() -> Path:
    return _user_folder("pictures")


def _get_music() -> Path:
    return _user_folder("music")


def _get_videos() -> Path:
    return _user_folder("videos")


_SHORTCUT_DIRS: tuple[str, ...] = (
    "desktop", "downloads", "documents", "pictures", "music", "videos", "home",
)
# Model bazen Turkce klasor adlarini da kullanabiliyor - Ingilizce kisayola
# esleyip ayni mantigi calistirmak icin.
_SHORTCUT_TR_ALIASES: dict[str, str] = {
    "masaüstü": "desktop", "masaustu": "desktop",
    "indirilenler": "downloads",
    "belgeler": "documents",
    "resimler": "pictures",
    "müzik": "music", "muzik": "music",
    "videolar": "videos",
    "ev": "home",
}


def _normalize_shortcut(word: str) -> str:
    # 'İndirilenler'.lower() → 'i̇ndirilenler' (i + birleşik nokta): eşleşmiyordu.
    w = word.strip().lower().replace("i\u0307", "i")
    return _SHORTCUT_TR_ALIASES.get(w, w)


def _normalize_path_name(path: str, name: str) -> tuple[str, str]:
    """Model bazen path='desktop', name='dosya.txt' diye AYRI parametre
    gonderiyor, bazen (ozellikle sesli komutlarda) hepsini tek bir path
    icine ('desktop/dosya.txt' veya 'masaüstü/dosya.txt') sikistirip name'i
    bos birakiyor. Ikinci durumda _resolve_path bunu bilinen bir kisayol
    olarak tanimadigi icin (tam string eslesmesi ariyor), proje klasorune
    gore ANLAMSIZ bir relatif yola donusuyor ve dosya hep 'bulunamadi'
    cikiyordu - kullanicinin gercekten yasadigi hata buydu.

    name bosken path icinde bir ayirici varsa ve ilk parca bilinen bir
    kisayolsa, path'i kisayol + kalan kismi isim olarak boluyoruz."""
    if name:
        return path, name
    raw = path.strip()
    for sep in ("/", "\\"):
        if sep in raw:
            head, _, rest = raw.partition(sep)
            key = _normalize_shortcut(head)
            if key in _SHORTCUT_DIRS and rest:
                return key, rest
    return path, name


def _normalize_file_name(name: str) -> str:
    """Sesli komutlarda dosya adına eklenen Türkçe fiil eklerini ayır.

    Örnek: ``JARVIS_TEST_HEALTH.txt dosyasını sil`` ->
    ``JARVIS_TEST_HEALTH.txt``. Yalnızca bilinen komut son ekleri kaldırılır;
    dosya adının içindeki kelimelere dokunulmaz.
    """
    value = str(name or "").strip().strip('"\'`')
    suffixes = (
        " dosyasını sil", " dosyayı sil", " dosyasini sil",
        " dosyayi sil", " dosyasını kaldır", " dosyayı kaldır",
        " dosyasini kaldir", " dosyayi kaldir", " dosyasını siler misin",
        " dosyayı siler misin", " dosyasını sil lütfen", " dosyayı sil lütfen",
    )
    lowered = value.casefold()
    for suffix in suffixes:
        if lowered.endswith(suffix.casefold()):
            return value[: -len(suffix)].strip().strip('"\'`')
    return value


def _resolve_path(raw: str) -> Path:
    shortcuts: dict[str, Path] = {
        "desktop":   _get_desktop(),
        "downloads": _get_downloads(),
        "documents": _get_documents(),
        "pictures":  _get_pictures(),
        "music":     _get_music(),
        "videos":    _get_videos(),
        "home":      Path.home(),
    }
    lower = _normalize_shortcut(raw)
    if lower in shortcuts:
        return shortcuts[lower]
    return Path(raw).expanduser()

def _resolve_target_name(base: Path, name: str) -> tuple[Path | None, str | None]:
    """'name' base icinde TAM ismiyle yoksa, UZANTISIZ isim eslesmesine bakar
    (ör. kullanici 'murat12345' der, gercek dosya 'murat12345.txt'dir).

    Sesli/yazili komutlarda kullanicilar neredeyse hicbir zaman dosya
    uzantisini soylemez/yazmaz - eskiden bu yuzden tam olarak var olan bir
    dosya bile 'Not found' donuyordu ve Jarvis 'boyle bir dosya yok' gibi
    yanlis bir cevap veriyordu (kullanicinin gercekten yasadigi hata).

    Tek eslesme varsa onu kullanir; birden fazla eslesme varsa (ör. hem
    'murat12345.txt' hem 'murat12345.docx' varsa) hangisini kastettigini
    netlestirmesi icin acikca bir hata mesaji doner - sessizce rastgele
    birini SILMEZ/TASIMAZ."""
    target = base / name
    if target.exists() or not base.is_dir():
        return target, None

    name_lower = name.strip().lower()
    stem_lower = Path(name).stem.lower()
    matches = []
    try:
        for item in base.iterdir():
            if item.name.lower() == name_lower or item.stem.lower() == stem_lower:
                matches.append(item)
    except Exception:
        return target, None

    if len(matches) == 1:
        return matches[0], None
    if len(matches) > 1:
        names = ", ".join(m.name for m in matches)
        return None, f"Birden fazla eşleşme bulundu, hangisini kastettiğini netleştir: {names}"
    return target, None


def _format_size(b: int) -> str:
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} TB"

def _safe_trash(target: Path) -> str:

    if not _SEND2TRASH:
        return (
            "send2trash is not installed. "
            "Run: pip install send2trash — "
            "Permanent deletion is disabled for safety."
        )
    send2trash.send2trash(str(target))
    return f"Moved to Trash: {target.name}"


# --- Dosya kilidi / paylaşım ihlali sınıflandırma ve yeniden deneme -------
#
# GERCEK RISK (denetim bulgusu F-03): Windows'ta OneDrive tarafindan
# senkronize edilen ya da baska bir uygulamada acik olan bir dosyaya
# yazma/tasima/silme denendiginde PermissionError/OSError (WinError 32 -
# ERROR_SHARING_VIOLATION, WinError 33 - ERROR_LOCK_VIOLATION) firlar.
# Eskiden butun mutasyon fonksiyonlari bunu sadece genel
# "except Exception as e" ile yakalayip ham hata metnini donduruyordu -
# kullaniciya ne yapmasi gerektigini soylemiyordu ve kisa bir an icin
# kilitli olan (ör. antivirus taramasi, indeksleyici) dosyalarda hicbir
# yeniden deneme yapilmiyordu. Asagidaki yardimcilar TEK, ORTAK bir
# siniflandirma/yeniden-deneme politikasi saglar.
_WIN_ACCESS_DENIED = 5           # ERROR_ACCESS_DENIED
_WIN_SHARING_VIOLATION = 32      # ERROR_SHARING_VIOLATION
_WIN_LOCK_VIOLATION = 33         # ERROR_LOCK_VIOLATION
_WIN_FILENAME_EXCED_RANGE = 206  # ERROR_FILENAME_EXCED_RANGE (yol/isim cok uzun)


def _win_error_code(exc: BaseException) -> "int | None":
    return getattr(exc, "winerror", None)


def _is_retryable_lock_error(exc: BaseException) -> bool:
    if not isinstance(exc, OSError):
        return False
    if _win_error_code(exc) in (_WIN_SHARING_VIOLATION, _WIN_LOCK_VIOLATION):
        return True
    # Linux/macOS: ETXTBSY/EBUSY gecici olabilir (ör. calisir durumdaki dosya).
    return exc.errno in (getattr(errno, "ETXTBSY", None), getattr(errno, "EBUSY", None))


def _classify_os_error(exc: BaseException, target: "Path | None" = None) -> str:
    """OS hatasini kullaniciya anlamli, eylem onerir bir mesaja cevirir
    (F-03, F-09 denetim bulgularinin ortak cozumu)."""
    label = target.name if isinstance(target, Path) else (str(target) if target else "")
    code = _win_error_code(exc)
    if isinstance(exc, PermissionError) or code == _WIN_ACCESS_DENIED:
        return (f"İzin reddedildi: {label or exc}. Dosya salt-okunur olabilir "
                f"ya da başka bir uygulama/OneDrive tarafından kilitli olabilir.")
    if code in (_WIN_SHARING_VIOLATION, _WIN_LOCK_VIOLATION):
        return (f"Dosya başka bir uygulama veya OneDrive tarafından kullanılıyor: "
                f"{label or exc}. İlgili programı/senkronizasyonu kapatıp tekrar deneyin.")
    if code == _WIN_FILENAME_EXCED_RANGE or (
        isinstance(exc, OSError) and exc.errno == getattr(errno, "ENAMETOOLONG", None)
    ):
        return (f"Dosya yolu çok uzun (Windows ~260 karakter sınırı): {label or exc}. "
                f"Daha kısa bir ad veya daha sığ bir klasör kullanın.")
    if isinstance(exc, FileNotFoundError):
        return f"Bulunamadı: {label or exc}"
    return f"{label + ': ' if label else ''}{exc}"


def _with_lock_retry(fn, target: "Path | None" = None, attempts: int = 3, base_delay: float = 0.2):
    """Kilit/paylaşım ihlali hatalarinda kisa, sinirli exponential backoff ile
    yeniden dener. Yalnizca IDEMPOTENT/tekrar-guvenli islemler (write/mkdir/
    delete/rename) icin kullanilir - move/copy icin ayri, kismi-durum
    temizleyen bir saricisi vardir (bkz. _with_lock_retry_move_or_copy)."""
    last_exc: BaseException | None = None
    for attempt in range(attempts):
        try:
            return fn()
        except OSError as exc:
            last_exc = exc
            if not _is_retryable_lock_error(exc) or attempt == attempts - 1:
                raise RuntimeError(_classify_os_error(exc, target)) from exc
            time.sleep(base_delay * (2 ** attempt))
    raise RuntimeError(_classify_os_error(last_exc, target))


def _with_lock_retry_move_or_copy(fn, dst: Path, target: "Path | None" = None,
                                   attempts: int = 3, base_delay: float = 0.2):
    """move/copy icin: her denemeden once, ONCEKI denemeden kalmis olabilecek
    KISMI hedef dosyayi temizler (F-03 denetim bulgusu: 'taşıma/kopyalama
    için yarım işlem kontrolü'). Boylece kullanici hicbir zaman 'var ama
    bozuk/yarim' bir dosyayla bas basa kalmaz."""
    last_exc: BaseException | None = None
    for attempt in range(attempts):
        try:
            return fn()
        except OSError as exc:
            last_exc = exc
            try:
                if dst.exists() and dst.is_file():
                    dst.unlink()
            except Exception:
                pass
            if not _is_retryable_lock_error(exc) or attempt == attempts - 1:
                raise RuntimeError(_classify_os_error(exc, target)) from exc
            time.sleep(base_delay * (2 ** attempt))
    raise RuntimeError(_classify_os_error(last_exc, target))


# --- Sinirli, sembolik-link-donguse KARSI KORUMALI dosya gezici -----------
#
# GERCEK RISK (denetim bulgulari F-04/F-05): Path.rglob("*") TUM agaci
# sinirsizca gezip (ozellikle Home/Downloads/disk gibi buyuk agaclarda)
# zaman/bellek riski yaratiyordu ve sembolik link dongulerine karsi acik
# bir sozlesmesi yoktu. Asagidaki gezici; klasor SAYISINI, toplam taranan
# dosya sayisini, derinligi ve gecen sureyi sinirlar, sinira ulasilirsa
# bunu `state["partial"]=True` ile ACIKCA bildirir (sessizce eksik sonuc
# DONMEZ) ve sembolik link KLASORLERINI hicbir zaman takip etmez.
_WALK_MAX_DIRS = 500
_WALK_MAX_FILES_SCANNED = 200_000
_WALK_MAX_DEPTH = 20
_WALK_TIME_BUDGET_S = 10.0


def _iter_files_bounded(root: Path, state: "dict | None" = None,
                         max_dirs: int = _WALK_MAX_DIRS,
                         max_files_scanned: int = _WALK_MAX_FILES_SCANNED,
                         max_depth: int = _WALK_MAX_DEPTH,
                         time_budget_s: float = _WALK_TIME_BUDGET_S):
    if state is None:
        state = {}
    state["partial"] = False
    start = time.monotonic()
    dirs_scanned = 0
    files_scanned = 0
    stack: list[tuple[Path, int]] = [(root, 0)]

    while stack:
        if time.monotonic() - start > time_budget_s:
            state["partial"] = True
            return
        current, depth = stack.pop()
        dirs_scanned += 1
        if dirs_scanned > max_dirs:
            state["partial"] = True
            return
        try:
            with os.scandir(current) as it:
                entries = list(it)
        except (PermissionError, OSError):
            continue

        for entry in entries:
            try:
                is_symlink = entry.is_symlink()
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                continue
            if is_dir:
                if is_symlink:
                    # Sembolik link klasorlerini TAKIP ETME - dongu koruma
                    # sozlesmesi burada ACIK, platforma bagli degil.
                    continue
                if depth + 1 <= max_depth:
                    stack.append((Path(entry.path), depth + 1))
                continue

            files_scanned += 1
            if files_scanned > max_files_scanned:
                state["partial"] = True
                return
            try:
                if entry.is_file(follow_symlinks=False):
                    yield Path(entry.path)
            except OSError:
                continue


_LIST_MAX_ITEMS = 500
_LIST_MAX_CHARS = 8000


def list_files(path: str = "desktop", show_hidden: bool = False) -> str:
    try:
        target = _resolve_path(path)
        if not _is_safe_path(target):
            return f"Access denied: {target}"
        if not target.exists():
            return f"Path not found: {target}"
        if not target.is_dir():
            return f"Not a directory: {target}"

        try:
            entries = sorted(target.iterdir())
        except PermissionError:
            return f"Permission denied: {path}"

        # DUZELTME (denetim bulgusu F-05): eskiden max_items/karakter
        # butcesi yoktu - on binlerce ogeli bir klasor (Downloads gibi)
        # tum listeyi tek seferde Gemini/Ollama istemine tasiyabiliyordu.
        # Ayrica TEK bir kilitli/erisilemeyen ogenin stat() hatasi butun
        # listelemeyi durduruyordu (per-item izolasyon yoktu).
        items: list[str] = []
        total_count = 0
        truncated_by_count = False
        # Türlere göre sayım KODLA yapılır (Murat@goxs 2026-09-29: 11 .py dosyasını
        # sesli model "10" diye saydı, .sh dosyasını unuttu, toplamı "7" dedi).
        kinds: dict[str, int] = {}
        for item in entries:
            if not show_hidden and item.name.startswith("."):
                continue
            total_count += 1
            kind = "klasör" if item.is_dir() else (item.suffix.lower() or "(uzantısız)")
            kinds[kind] = kinds.get(kind, 0) + 1
            if len(items) >= _LIST_MAX_ITEMS:
                truncated_by_count = True
                continue
            if item.is_dir():
                items.append(f"📁 {item.name}/")
            else:
                try:
                    size = _format_size(item.stat().st_size)
                    items.append(f"📄 {item.name} ({size})")
                except OSError:
                    items.append(f"📄 {item.name} (boyut alınamadı — erişim hatası)")

        if not items:
            return f"Directory is empty: {target.name}/"

        body = "\n".join(items)
        if len(body) > _LIST_MAX_CHARS:
            body = body[:_LIST_MAX_CHARS] + f"\n... [çıktı {_LIST_MAX_CHARS} karakterde kesildi]"

        header = f"Contents of {target.name}/ ({total_count} items"
        if truncated_by_count:
            header += f", ilk {_LIST_MAX_ITEMS} tanesi gösteriliyor"
        header += "):"
        summary = ", ".join(f"{k}: {n}" for k, n in sorted(kinds.items(), key=lambda kv: (-kv[1], kv[0])))
        return (header + "\n" + body + f"\n\nTÜRLERE GÖRE SAYIM (kesin, kodla sayıldı — "
                f"kullanıcıya bu sayıları aynen söyle): {summary}. Toplam: {total_count}.")

    except PermissionError:
        return f"Permission denied: {path}"
    except Exception as e:
        return f"Error listing files: {e}"


def create_file(path: str, name: str = "", content: str = "") -> str:
    try:
        # DUZELTME (kullanici onayli, 2026-09-15, "bos hedef guvenligi"):
        # name bos gelirse eskiden target=base (klasorun KENDISI) oluyordu -
        # write_text() bir klasore yazmaya calisip IsADirectoryError
        # firlatiyordu (yakalaniyordu ama mesaj cig bir Python hatasiydi ve
        # kok neden gizleniyordu). Simdi net, erken bir hata donuluyor -
        # base/target hic hesaplanmiyor, diske HICBIR dokunma olmuyor.
        if not name:
            return "Could not create file: dosya adı belirtilmedi (isim boş olduğu için işlem güvenlik amacıyla durduruldu, hedef klasörün kendisine dokunulmadı)."
        base   = _resolve_path(path)
        target = base / name
        if not _is_safe_path(target):
            return f"Access denied: {target}"

        def _do_write():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

        _with_lock_retry(_do_write, target)
        # DUZELTME (kullanici onayli analiz raporu, 2026-09-15): write_text()
        # exception firlatmadi diye BASARILI SAYMA - "agent 'yaptim' dedi"
        # ile "gercekten oldu" ARASINDAKI fark tam burada aciliyordu (canli
        # testte gorulen sahte basari). Dosyayi TEKRAR OKUYUP gercekten
        # diskte oldugunu ve icerigin GERCEKTEN yazildigi gibi oldugunu
        # dogrula - sadece bu gecerse basari mesaji don.
        if not target.is_file():
            return f"Could not create file: dosya yazıldıktan sonra diskte bulunamadı ({target})"
        if target.read_text(encoding="utf-8") != content:
            return f"Could not create file: yazılan içerik doğrulanamadı ({target})"
        return f"File created: {target.name}"
    except RuntimeError as e:
        return f"Could not create file: {e}"
    except Exception as e:
        return f"Could not create file: {e}"


def create_folder(path: str, name: str = "") -> str:
    try:
        # DUZELTME (kullanici onayli, 2026-09-15, "bos hedef guvenligi"): name
        # bos gelirse eskiden target=base oluyordu; base COGU ZAMAN zaten
        # var olan bir klasor (ör. Desktop) oldugu icin mkdir(exist_ok=True)
        # SESSIZCE hicbir sey yapmadan basariyla donuyor ve "Folder created:
        # <base>" gibi TAM BIR SAHTE BASARI mesaji uretiyordu - hicbir yeni
        # klasor olusmadigi halde. Simdi erken, acik bir hata donuluyor.
        if not name:
            return "Could not create folder: klasör adı belirtilmedi (isim boş olduğu için işlem güvenlik amacıyla durduruldu, mevcut klasöre dokunulmadı)."
        base   = _resolve_path(path)
        target = base / name
        if not _is_safe_path(target):
            return f"Access denied: {target}"

        _with_lock_retry(lambda: target.mkdir(parents=True, exist_ok=True), target)
        # DUZELTME (aynı rapor): mkdir() exception firlatmadi diye BASARILI
        # SAYMA - klasorun GERCEKTEN diskte var oldugunu tekrar kontrol et.
        if not target.is_dir():
            return f"Could not create folder: klasör oluşturulduktan sonra diskte bulunamadı ({target})"
        return f"Folder created: {target.name}"
    except RuntimeError as e:
        return f"Could not create folder: {e}"
    except Exception as e:
        return f"Could not create folder: {e}"


def delete_file(path: str, name: str = "") -> str:
    try:
        base = _resolve_path(path)
        if name:
            target, ambiguous = _resolve_target_name(base, name)
            if ambiguous:
                return ambiguous
        else:
            target = base
        if not _is_safe_path(target):
            return f"Access denied: {target}"
        if not target.exists():
            return f"Not found: {target.name}"

        # Güvenli dizin kontrolü — kritik kullanıcı klasörlerini koru
        protected = {
            _get_desktop(), _get_downloads(), _get_documents(),
            _get_pictures(), _get_music(), _get_videos(), Path.home()
        }
        if target.resolve() in {p.resolve() for p in protected}:
            return f"Protected directory, cannot delete: {target.name}"

        return _with_lock_retry(lambda: _safe_trash(target), target)

    except PermissionError:
        return f"Permission denied: {path}"
    except RuntimeError as e:
        return f"Could not delete: {e}"
    except Exception as e:
        return f"Could not delete: {e}"


def delete_all_files(path: str = "downloads", confirm_code: str = "") -> str:
    """Move direct files in a safe folder to Trash, never the folder/subfolders.

    First call is preview-only and returns a short-lived confirmation code.
    The second call must provide that exact code.
    """
    try:
        base = _resolve_path(path)
        if not _is_safe_path(base):
            return f"Access denied: {base}"
        if not base.is_dir():
            return f"Not a directory: {base}"
        protected = {p.resolve() for p in (
            _get_desktop(), _get_downloads(), _get_documents(),
            _get_pictures(), _get_music(), _get_videos(), Path.home()
        )}
        if base.resolve() in protected and base.resolve() == Path.home().resolve():
            return "Protected directory, bulk deletion is not allowed here."
        files = tuple(item for item in base.iterdir() if item.is_file() and not item.name.startswith("."))
        if not confirm_code:
            code = secrets.token_hex(3)
            _pending_bulk_deletes[code] = (base, files)
            return (f"ONAY GEREKLİ: {len(files)} dosya '{base}' içinde bulundu. "
                    f"Alt klasörler ve klasörün kendisi korunacak. Onaydan sonra "
                    f"dosyalar Çöp Kutusu'na taşınacak. confirm_code={code}")
        pending = _pending_bulk_deletes.pop(confirm_code, None)
        if pending is None or pending[0] != base:
            return "Onay kodu geçersiz veya süresi dolmuş. Önce önizleme isteyin."
        moved, failed = 0, []
        for item in pending[1]:
            try:
                _with_lock_retry(lambda item=item: _safe_trash(item), item)
                moved += 1
            except Exception as exc:
                failed.append(f"{item.name}: {exc}")
        result = f"Bulk delete complete: {moved}/{len(pending[1])} files moved to Trash."
        if failed:
            result += " Failed: " + "; ".join(failed[:5])
        return result
    except Exception as e:
        return f"Could not bulk delete: {e}"


def move_file(path: str, name: str = "", destination: str = "", confirm_code: str = "") -> str:
    try:
        base = _resolve_path(path)
        if name:
            src, ambiguous = _resolve_target_name(base, name)
            if ambiguous:
                return ambiguous
        else:
            src = base
        dst    = _resolve_path(destination) if destination else None

        if not src.exists():
            return f"Source not found: {src.name}"
        if dst is None:
            return "No destination specified."
        if not _is_safe_path(src):
            return f"Access denied (source): {src}"
        if not _is_safe_path(dst):
            return f"Access denied (destination): {dst}"

        if dst.is_dir():
            dst = dst / src.name

        if not confirm_code:
            code = secrets.token_hex(2)
            _pending_file_ops[code] = ("move", src, dst)
            return (
                f"ONAY GEREKLİ (henüz taşınmadı): '{src}' -> '{dst}'. "
                f"Kullanıcıya bu taşımayı tarif et ve kullanıcı SESLİ/YAZILI olarak "
                f"açıkça onaylarsa (kullanıcının bir sonraki mesajında), move_file'i "
                f"aynı path/name/destination ile ve confirm_code='{code}' parametresiyle "
                f"TEKRAR çağır. Kullanıcı onaylamadan bu kodu kendi kendine kullanma."
            )

        pending = _pending_file_ops.pop(confirm_code, None)
        if pending is None or pending[0] != "move" or pending[1] != src or pending[2] != dst:
            return "Onay kodu geçersiz veya süresi dolmuş. Önce confirm_code vermeden çağırıp yeni kod alın."

        def _do_move():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))

        _with_lock_retry_move_or_copy(_do_move, dst, src)
        return f"Moved: {src.name} → {dst.parent.name}/"

    except RuntimeError as e:
        return f"Could not move: {e}"
    except Exception as e:
        return f"Could not move: {e}"


def copy_file(path: str, name: str = "", destination: str = "") -> str:
    try:
        base = _resolve_path(path)
        if name:
            src, ambiguous = _resolve_target_name(base, name)
            if ambiguous:
                return ambiguous
        else:
            src = base
        dst  = _resolve_path(destination) if destination else None

        if not src.exists():
            return f"Source not found: {src.name}"
        if dst is None:
            return "No destination specified."
        if not _is_safe_path(src):
            return f"Access denied (source): {src}"
        if not _is_safe_path(dst):
            return f"Access denied (destination): {dst}"

        if dst.is_dir():
            dst = dst / src.name

        def _do_copy():
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.is_dir():
                shutil.copytree(str(src), str(dst))
            else:
                shutil.copy2(str(src), str(dst))

        _with_lock_retry_move_or_copy(_do_copy, dst, src)
        return f"Copied: {src.name} → {dst.parent.name}/"

    except RuntimeError as e:
        return f"Could not copy: {e}"
    except Exception as e:
        return f"Could not copy: {e}"


def rename_file(path: str, name: str = "", new_name: str = "") -> str:
    try:
        base     = _resolve_path(path)
        target   = (base / name) if name else base
        if not _is_safe_path(target):
            return f"Access denied: {target}"
        if not target.exists():
            return f"Not found: {target.name}"
        if not new_name:
            return "No new name provided."

        new_path = target.parent / new_name
        if new_path.exists():
            return f"A file named '{new_name}' already exists here."

        _with_lock_retry(lambda: target.rename(new_path), target)
        return f"Renamed: {target.name} → {new_name}"

    except RuntimeError as e:
        return f"Could not rename: {e}"
    except Exception as e:
        return f"Could not rename: {e}"


# DUZELTME (denetim bulgusu F-10): errors="ignore" sessizce byte KAYBINA yol
# aciyordu (bozuk/uyumsuz kodlamali dosyalarda kullaniciya HABER VERMEDEN
# karakterler dusuyordu). Simdi bilinen kodlamalar sirayla STRICT denenir;
# hicbiri tutmazsa son care olarak replace kullanilir AMA bu ACIKCA
# kullaniciya bildirilir - sessiz veri kaybi yok.
_TEXT_ENCODING_FALLBACKS: tuple[str, ...] = ("utf-8", "utf-8-sig", "cp1254", "latin-1")


def read_file(path: str, name: str = "", max_chars: int = 4000) -> str:
    try:
        base   = _resolve_path(path)
        target = (base / name) if name else base
        if not _is_safe_path(target):
            return f"Access denied: {target}"
        if not target.exists():
            return f"File not found: {target.name}"
        if not target.is_file():
            return f"Not a file: {target.name}"

        raw = target.read_bytes()
        content = None
        used_encoding = None
        for enc in _TEXT_ENCODING_FALLBACKS:
            try:
                content = raw.decode(enc)
                used_encoding = enc
                break
            except UnicodeDecodeError:
                continue

        note = ""
        if content is None:
            content = raw.decode("utf-8", errors="replace")
            note = "\n\n[Uyarı: dosya kodlaması tanınamadı, bazı karakterler '�' ile değiştirildi.]"
        elif used_encoding != "utf-8":
            note = f"\n\n[Not: dosya UTF-8 değil, '{used_encoding}' olarak okundu.]"

        if len(content) > max_chars:
            content = content[:max_chars] + f"\n\n[Truncated — {len(content)} total chars]"
        return content + note

    except Exception as e:
        return f"Could not read file: {e}"


def write_file(path: str, name: str = "", content: str = "",
               append: bool = False) -> str:
    try:
        # DUZELTME (kullanici onayli, 2026-09-15, "bos hedef guvenligi"):
        # name bos gelirse eskiden target=base oluyordu - open(base, "w")
        # base bir klasor oldugu icin IsADirectoryError firlatiyordu
        # (yakalaniyordu ama cig Python hata mesaji kok nedeni gizliyordu;
        # canli E2E testte bu YUZDEN "Dosyaya yaz" adimi CWD'nin kendisine
        # yazmaya calisip patlamisti). Simdi net, erken bir hata donuluyor.
        if not name:
            return "Could not write file: dosya adı belirtilmedi (isim boş olduğu için işlem güvenlik amacıyla durduruldu, hedef klasörün kendisine yazılmadı)."
        base   = _resolve_path(path)
        target = base / name
        if not _is_safe_path(target):
            return f"Access denied: {target}"

        def _do_write():
            target.parent.mkdir(parents=True, exist_ok=True)
            mode = "a" if append else "w"
            with open(target, mode, encoding="utf-8") as f:
                f.write(content)

        _with_lock_retry(_do_write, target)
        # DUZELTME (kullanici onayli analiz raporu, 2026-09-15): yazma
        # exception firlatmadi diye BASARILI SAYMA - dosyayi TEKRAR OKUYUP
        # icerigin GERCEKTEN orada oldugunu dogrula. append=True'da tam
        # esitlik degil, SONDA bulunma kontrol edilir (dosyada onceden baska
        # icerik olabilir).
        if not target.is_file():
            return f"Could not write file: dosya yazıldıktan sonra diskte bulunamadı ({target})"
        actual = target.read_text(encoding="utf-8")
        ok = actual.endswith(content) if append else (actual == content)
        if not ok:
            return f"Could not write file: yazılan içerik doğrulanamadı ({target})"
        action = "Appended to" if append else "Written to"
        return f"{action}: {target.name}"
    except RuntimeError as e:
        return f"Could not write file: {e}"
    except Exception as e:
        return f"Could not write file: {e}"


def find_replace_in_file(path: str, name: str = "", old_text: str = "", new_text: str = "") -> str:
    # DUZELTME (canli testte bulundu): "X yerine Y yaz" gorevleri ASLA
    # basarili olamiyordu, boyle bir bul-degistir islevi hic yoktu.
    # write_file() ile AYNI guvenlik/dogrulama desenini kullanir.
    try:
        if not name:
            return "Could not edit file: dosya adı belirtilmedi (isim boş olduğu için işlem güvenlik amacıyla durduruldu)."
        if not old_text:
            return "Could not edit file: değiştirilecek metin (old_text) boş olamaz."
        base = _resolve_path(path)
        target = base / name
        if not _is_safe_path(target):
            return f"Access denied: {target}"
        if not target.is_file():
            return f"File not found: {target.name}"

        original = target.read_text(encoding="utf-8")
        if old_text not in original:
            return f"Could not edit file: '{old_text}' dosyada bulunamadı ({target.name})."
        count = original.count(old_text)
        updated = original.replace(old_text, new_text)

        def _do_write():
            with open(target, "w", encoding="utf-8") as f:
                f.write(updated)

        _with_lock_retry(_do_write, target)

        actual = target.read_text(encoding="utf-8")
        if actual != updated:
            return f"Could not edit file: değişiklik doğrulanamadı ({target})"
        return f"Edited: {target.name} ({count} yer değiştirildi)"
    except Exception as e:
        return f"Could not edit file: {e}"


def find_files(name: str = "", extension: str = "",
               path: str = "home", max_results: int = 20) -> str:
    try:
        search_path = _resolve_path(path)
        if not _is_safe_path(search_path):
            return f"Access denied: {search_path}"
        if not search_path.exists():
            return f"Search path not found: {path}"

        # DUZELTME (denetim bulgusu F-04/F-05 ile ayni kok neden, sembolik
        # link degerlendirmesi): rglob() yerine sinirli/dongu-korumali
        # _iter_files_bounded kullanilir - max_dirs/zaman siniri asilirsa
        # bu ACIKCA "partial" olarak isaretlenir.
        results: list[str] = []
        state: dict = {}
        for item in _iter_files_bounded(search_path, state):
            if extension and item.suffix.lower() != extension.lower():
                continue
            if name and name.lower() not in item.name.lower():
                continue
            try:
                size = _format_size(item.stat().st_size)
            except OSError:
                continue
            results.append(f"📄 {item.name} ({size}) — {item.parent}")
            if len(results) >= max_results:
                break

        if not results:
            query = name or extension or "files"
            suffix = " [tarama sınırına ulaşıldı, sonuç kısmi olabilir]" if state.get("partial") else ""
            return f"No {query} found in {search_path.name}/{suffix}"

        header = f"Found {len(results)} file(s):"
        if state.get("partial") and len(results) < max_results:
            header += " [tarama sınırına ulaşıldı, sonuç kısmi olabilir]"
        return header + "\n" + "\n".join(results)

    except Exception as e:
        return f"Search error: {e}"


def get_largest_files(path: str = "downloads", count: int = 10) -> str:
    count = min(max(int(count), 1), 50)  # maksimum 50
    try:
        search_path = _resolve_path(path)
        if not _is_safe_path(search_path):
            return f"Access denied: {search_path}"
        if not search_path.exists():
            return f"Path not found: {path}"

        # DUZELTME (denetim bulgusu F-04): eskiden TUM dosyalar (size, path)
        # olarak belleğe alinip SONRA siralaniyordu - buyuk Home/Downloads/
        # disk taramalarinda zaman/bellek riski (rglob() sinirsizdi ve
        # `count<=50` cikti sinirlamasi TARAMA maliyetini sinirlamiyordu).
        # Simdi sabit boyutlu (count buyuklugunde) bir min-heap ile SADECE
        # top-N bellekte tutulur, tarama da _iter_files_bounded ile sinirli.
        heap: list[tuple[int, str]] = []
        state: dict = {}
        for item in _iter_files_bounded(search_path, state):
            try:
                size = item.stat().st_size
            except OSError:
                continue
            entry = (size, str(item))
            if len(heap) < count:
                heapq.heappush(heap, entry)
            elif size > heap[0][0]:
                heapq.heapreplace(heap, entry)

        if not heap:
            return "No files found."

        top = sorted(heap, reverse=True)
        lines = [f"Top {len(top)} largest files in {search_path.name}/:"]
        for size, p in top:
            pth = Path(p)
            lines.append(f"  {_format_size(size):>10}  {pth.name}  ({pth.parent})")
        if state.get("partial"):
            lines.append("[Not: tarama sınırına ulaşıldığı için sonuç kısmi olabilir.]")

        return "\n".join(lines)

    except Exception as e:
        return f"Error: {e}"


def get_disk_usage(path: str = "home") -> str:
    try:
        target = _resolve_path(path)
        # DUZELTME (denetim bulgusu F-06): eskiden _is_safe_path() hic
        # cagirilmiyordu - /proc, /root, C:\Program Files gibi herhangi bir
        # mutlak yol icin disk kullanimi sorgulanabiliyordu. Diger tum
        # file_controller eylemleriyle AYNI politika burada da uygulanir.
        if not _is_safe_path(target):
            return f"Access denied: {target}"
        if not target.exists():
            return f"Path not found: {path}"
        usage  = shutil.disk_usage(target)
        pct    = usage.used / usage.total * 100
        return (
            f"Disk usage ({target}):\n"
            f"  Total : {_format_size(usage.total)}\n"
            f"  Used  : {_format_size(usage.used)} ({pct:.1f}%)\n"
            f"  Free  : {_format_size(usage.free)}"
        )
    except PermissionError:
        return f"Permission denied: {path}"
    except FileNotFoundError:
        return f"Path not found: {path}"
    except OSError as e:
        return f"Could not get disk usage: {e}"
    except Exception as e:
        return f"Could not get disk usage: {e}"


def organize_desktop() -> str:
    type_map = {
        "Images":    {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".svg", ".ico", ".heic"},
        "Documents": {".pdf", ".doc", ".docx", ".txt", ".xls", ".xlsx",
                      ".ppt", ".pptx", ".csv", ".odt", ".ods", ".odp"},
        "Videos":    {".mp4", ".avi", ".mkv", ".mov", ".wmv", ".flv", ".webm", ".m4v"},
        "Music":     {".mp3", ".wav", ".flac", ".aac", ".ogg", ".wma", ".m4a"},
        "Archives":  {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz"},
        "Code":      {".py", ".js", ".ts", ".html", ".css", ".json", ".xml",
                      ".cpp", ".java", ".cs", ".go", ".rs", ".sh"},
    }

    desktop = _get_desktop()
    moved, skipped, failed = [], [], []

    try:
        try:
            entries = list(desktop.iterdir())
        except PermissionError:
            return f"Permission denied: {desktop}"

        for item in entries:
            # DUZELTME (denetim bulgusu F-08 ile ayni kok neden - masaustu
            # islemlerinde tekil hata izolasyonu): eskiden bu dongude disari
            # sizan HERHANGI bir istisna (ör. kilitli/OneDrive senkron
            # dosyasinda shutil.move hatasi) TUM organize islemini yarida
            # kesiyordu. Simdi HER OGE ayri try/except ile isleniyor;
            # basarili/atlanan/basarisiz sayimlar ayri tutuluyor.
            try:
                # Klasörlere, gizli dosyalara ve organize klasörlerine dokunma
                if item.is_dir() or item.name.startswith("."):
                    continue
                if item.name in {k for k in type_map}:
                    continue

                ext        = item.suffix.lower()
                target_dir = desktop / "Others"
                for folder, exts in type_map.items():
                    if ext in exts:
                        target_dir = desktop / folder
                        break

                target_dir.mkdir(exist_ok=True)
                new_path = target_dir / item.name

                if new_path.exists():
                    skipped.append(item.name)
                    continue

                _with_lock_retry_move_or_copy(
                    lambda item=item, new_path=new_path: shutil.move(str(item), str(new_path)),
                    new_path, item,
                )
                moved.append(f"{item.name} → {target_dir.name}/")
            except Exception as item_exc:
                failed.append(f"{item.name}: {item_exc}")
                continue

        result = f"Desktop organized: {len(moved)} files moved."
        if moved:
            preview = moved[:8]
            result += "\n" + "\n".join(preview)
            if len(moved) > 8:
                result += f"\n... and {len(moved) - 8} more."
        if skipped:
            result += f"\n{len(skipped)} file(s) skipped (name conflict)."
        if failed:
            result += f"\n{len(failed)} file(s) failed:\n" + "\n".join(failed[:5])
            if len(failed) > 5:
                result += f"\n... and {len(failed) - 5} more failures."
        return result

    except Exception as e:
        return f"Could not organize desktop: {e}"


# --- ZIP guvenligi: zip-slip + archive-bomb korumasi ----------------------
#
# GERCEK RISK (denetim bulgusu F-07): zip-slip (yol traversal) korumasi
# vardi ama kaynak TUKETIMI (archive-bomb: kucuk sikistirilmis dosyanin
# devasa/cok sayida uye acmasi) hic sinirlanmiyordu. Asagidaki sabitler
# uye sayisini, tekil ve toplam acilmis boyutu sinirlar; ayrica sembolik
# link UYELERI reddedilir ve cikarma GECICI bir staging dizininde yapilip
# yalnizca TAMAMI basarili olursa hedefe tasinir (yarim cikarma, kullanicinin
# gercek hedef klasorune KARISMAZ).
_ZIP_MAX_MEMBERS = 20_000
_ZIP_MAX_TOTAL_UNCOMPRESSED = 2 * 1024 ** 3   # 2 GB
_ZIP_MAX_SINGLE_FILE = 1 * 1024 ** 3           # 1 GB
_ZIP_FREE_SPACE_MARGIN = 1.2                    # cikan veri x1.2 kadar bos alan iste


def _zip_member_is_symlink(info) -> bool:
    # Unix'te olusturulmus zip'lerde ust 16 bit dosya modunu tasir;
    # S_IFLNK == 0xA000. Windows'ta olusturulan zip'lerde bu bit anlamsizdir
    # (external_attr farkli kullanilir), bu yuzden yalnizca UNIX kaynakli
    # (create_system == 3) girdilerde kontrol edilir.
    if getattr(info, "create_system", 0) != 3:
        return False
    mode = (info.external_attr >> 16) & 0xFFFF
    return (mode & 0xF000) == 0xA000


def extract_archive(path: str, name: str = "", destination: str = "") -> str:
    """Bir .zip dosyasini GUVENLI sekilde acar — discovery.py'nin karantina
    icin kullandigi AYNI zip-slip korumasi (bir zip icindeki '../../...'
    gibi yollarin hedef klasor disina cikmasini engeller) ARTI archive-bomb
    (asiri buyuk/asiri cok sayida uye) korumasi.

    GERCEK YASANAN SORUN: file_controller'da 'extract' hic yoktu, bu yuzden
    bir kullanici istegi dev_agent'a ('kendi kodunu yaz ve calistir')
    dusuyordu - dev_agent'in ELLE yazdigi extraction kodu bu korumaya sahip
    DEGILDI ve calismadi (bkz. Jarvis_Improvement_Automation denemesi).
    Artik zip acmak icin GUVENLI, test edilmis TEK yol budur."""
    import zipfile

    base = _resolve_path(path)
    if name:
        src, ambiguous = _resolve_target_name(base, name)
        if ambiguous:
            return ambiguous
    else:
        src = base

    if not src.exists():
        return f"Not found: {src.name if name else src}"
    if not _is_safe_path(src):
        return f"Access denied (source): {src}"
    if not src.is_file() or src.suffix.lower() != ".zip":
        return f"Not a .zip file: {src.name}"

    # Hedef verilmezse, zip'in bulundugu klasorde zip ile AYNI isimde bir
    # alt klasore acilir - ne cikarildigi/nereye cikarildigi hep belli olur,
    # rastgele bir yere dagilmaz.
    dest = _resolve_path(destination) if destination else (src.parent / src.stem)
    if not _is_safe_path(dest):
        return f"Access denied (destination): {dest}"

    try:
        with zipfile.ZipFile(src) as zf:
            infolist = zf.infolist()

            if len(infolist) > _ZIP_MAX_MEMBERS:
                return (f"Güvenlik sınırı: zip {len(infolist)} öğe içeriyor "
                        f"(sınır {_ZIP_MAX_MEMBERS}), açma iptal edildi (archive-bomb koruması).")

            total_uncompressed = 0
            for member in infolist:
                if member.file_size > _ZIP_MAX_SINGLE_FILE:
                    return (f"Güvenlik sınırı: '{member.filename}' açılmış hâliyle "
                            f"{_format_size(member.file_size)} (sınır {_format_size(_ZIP_MAX_SINGLE_FILE)}), "
                            f"açma iptal edildi.")
                total_uncompressed += member.file_size
                if total_uncompressed > _ZIP_MAX_TOTAL_UNCOMPRESSED:
                    return (f"Güvenlik sınırı: toplam açılmış boyut "
                            f"{_format_size(_ZIP_MAX_TOTAL_UNCOMPRESSED)} sınırını aşıyor, "
                            f"açma iptal edildi (archive-bomb koruması).")
                if _zip_member_is_symlink(member):
                    return f"Güvensiz zip içeriği tespit edildi (symlink üye), açma iptal edildi: {member.filename}"

            try:
                probe = dest
                while not probe.exists():
                    probe = probe.parent
                free_space = shutil.disk_usage(str(probe)).free
                if total_uncompressed * _ZIP_FREE_SPACE_MARGIN > free_space:
                    return (f"Yetersiz disk alanı: açmak için ~{_format_size(total_uncompressed)} gerekiyor, "
                            f"{_format_size(free_space)} boş alan var. Açma iptal edildi.")
            except OSError:
                pass  # disk kullanim kontrolu basarisiz olsa bile devam et - kritik degil

            dest.mkdir(parents=True, exist_ok=True)
            # Once GECICI bir staging dizinine ac; sadece TUM uyeler basariyla
            # ve guvenli sekilde cikartildiktan sonra hedefe TASI. Boylece
            # yarim/bozuk bir cikarma kullanicinin gercek hedef klasorune
            # KARISMAZ.
            with tempfile.TemporaryDirectory(dir=str(dest.parent), prefix=".jarvis_extract_") as staging:
                staging_path = Path(staging)
                staging_resolved = staging_path.resolve()
                for member in infolist:
                    member_path = (staging_path / member.filename).resolve()
                    try:
                        member_path.relative_to(staging_resolved)
                    except ValueError:
                        return f"Güvensiz zip içeriği tespit edildi (zip-slip), açma iptal edildi: {member.filename}"
                zf.extractall(staging_path)

                moved = 0
                for item in staging_path.rglob("*"):
                    if item.is_dir():
                        continue
                    rel = item.relative_to(staging_path)
                    target_item = dest / rel
                    target_item.parent.mkdir(parents=True, exist_ok=True)
                    if target_item.exists():
                        target_item.unlink()
                    shutil.move(str(item), str(target_item))
                    moved += 1

        return f"Extracted: {src.name} → {dest} ({moved} dosya)"
    except zipfile.BadZipFile:
        return f"Bozuk ya da geçerli olmayan zip dosyası: {src.name}"
    except Exception as e:
        return f"Could not extract: {e}"


def get_file_info(path: str, name: str = "") -> str:
    try:
        base   = _resolve_path(path)
        target = (base / name) if name else base
        if not _is_safe_path(target):
            return f"Access denied: {target}"
        if not target.exists():
            return f"Not found: {target.name}"

        stat = target.stat()
        info = {
            "Name":      target.name,
            "Type":      "Folder" if target.is_dir() else "File",
            "Size":      _format_size(stat.st_size),
            "Location":  str(target.parent),
            "Created":   datetime.fromtimestamp(stat.st_ctime).strftime("%Y-%m-%d %H:%M"),
            "Modified":  datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
            "Extension": target.suffix or "—",
        }
        return "\n".join(f"  {k}: {v}" for k, v in info.items())

    except Exception as e:
        return f"Could not get file info: {e}"

def file_controller(
    parameters: dict = None,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    params = parameters or {}
    action = str(params.get("action", "")).lower().strip()
    # Sesli model bazen şema içindeki "delete" yerine doğal dil aliası
    # gönderebiliyor. Güvenli silme yolu yine yalnızca delete_file üzerinden
    # geçtiği için bu aliaslar davranışı genişletmez; sadece doğru dispatcher
    # dalına ulaşmayı sağlar.
    action = {
        "sil": "delete",
        "sil_file": "delete",
        "delete_file": "delete",
        "remove": "delete",
        "trash": "delete",
        "delete_all": "delete_all_files",
        "delete_all_files": "delete_all_files",
        "bulk_delete": "delete_all_files",
        "tümünü sil": "delete_all_files",
        "tumunu sil": "delete_all_files",
        "oluştur": "create_file",
        "olustur": "create_file",
    }.get(action, action)
    path   = params.get("path", "desktop")
    name   = _normalize_file_name(params.get("name", ""))
    path, name = _normalize_path_name(path, name)

    if player:
        player.write_log(f"[file] {action} {path}/{name}" if name else f"[file] {action} {path}")

    try:
        if action == "list":
            return list_files(path)

        elif action == "create_file":
            return create_file(path, name=name, content=params.get("content", ""))

        elif action == "create_folder":
            return create_folder(path, name=name)

        elif action == "delete":
            return delete_file(path, name=name)
        elif action == "delete_all_files":
            return delete_all_files(path=path, confirm_code=params.get("confirm_code", ""))

        elif action == "move":
            return move_file(path, name=name, destination=params.get("destination", ""),
                              confirm_code=params.get("confirm_code", ""))

        elif action == "copy":
            return copy_file(path, name=name, destination=params.get("destination", ""))

        elif action == "rename":
            return rename_file(path, name=name, new_name=params.get("new_name", ""))

        elif action == "read":
            return read_file(path, name=name)

        elif action == "write":
            return write_file(
                path, name=name,
                content=params.get("content", ""),
                append=params.get("append", False)
            )

        elif action == "find_replace":
            return find_replace_in_file(
                path, name=name,
                old_text=params.get("old_text", ""),
                new_text=params.get("new_text", ""),
            )

        elif action == "find":
            return find_files(
                name=name or params.get("name", ""),
                extension=params.get("extension", ""),
                path=path,
                max_results=min(int(params.get("max_results", 20)), 50),
            )

        elif action == "largest":
            return get_largest_files(
                path=path,
                count=int(params.get("count", 10)),
            )

        elif action == "disk_usage":
            return get_disk_usage(path)

        elif action == "organize_desktop":
            return organize_desktop()

        elif action == "info":
            return get_file_info(path, name=name)

        elif action == "extract":
            return extract_archive(path, name=name, destination=params.get("destination", ""))

        else:
            return f"Unknown action: '{action}'"

    except Exception as e:
        return f"File controller error ({action}): {e}"
