"""tests/conftest.py — Jarvis 2.0 test isolation

Sadece yeni regression testlerini çalıştır (test_registry, test_security, ...)
Eski test_brain_*.py dosyaları JarvisLive mock gerektirir → ayrı koleksiyon.

GERÇEK DENETİM KAYITLARI KORUMASI: test paketi kullanıcının gerçek denetim
kaydını DEĞİŞTİREMEZ. Kanonik kayıt (plan Adım 3.1) gerçek
JARVIS_HOME/memory/audit.log'dur; JARVIS_HOME tanımlı değilse
~/.local/share/MuratJARVIS/memory/audit.log (ya da $XDG_DATA_HOME). Eski
~/.jarvis/audit.log'a artık yazılmaz; yine de korunur (değişirse hata).
Her testten önce/sonra dosyaların sha256'sı karşılaştırılır (değiştiren test
hata alır); oturum sonunda da, test dışı (ör. toplama sırasındaki)
yazmalara karşı, bir kez daha karşılaştırılır. Gerçek ev dizini ortam
değişkeninden değil kullanıcı veritabanından alınır; gerçek JARVIS_HOME /
XDG_DATA_HOME, izolasyon ortamı değiştirmeden ÖNCE (conftest import anında)
okunur.
"""
import hashlib
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

collect_ignore = [
    "test_brain_health_route.py",
    "test_brain_intent_priority.py",
    "test_brain_start_route.py",
    "test_brain_status_priority.py",
    "test_brain_*.py",
]


def _real_home() -> Path:
    try:
        import pwd
        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except Exception:  # Windows
        return Path(os.environ.get("USERPROFILE") or os.path.expanduser("~"))


def real_audit_logs() -> tuple[Path, ...]:
    """Kullanıcının GERÇEK denetim kayıtları: kanonik JARVIS_HOME/memory/
    audit.log (jarvis.paths.data_dir ile aynı öncelik) + eski konumlar."""
    home = _real_home()
    out = [home / ".jarvis" / "audit.log",
           home / ".local" / "share" / "MuratJARVIS" / "memory" / "audit.log"]
    env_home = os.environ.get("JARVIS_HOME", "").strip()
    if env_home:
        out.append(Path(env_home).expanduser() / "memory" / "audit.log")
    xdg = os.environ.get("XDG_DATA_HOME", "").strip()
    if xdg:
        out.append(Path(xdg) / "MuratJARVIS" / "memory" / "audit.log")
    return tuple(dict.fromkeys(out))


# conftest import anında = izolasyon (pytest_configure) ortamı değiştirmeden önce.
REAL_AUDIT_LOGS = real_audit_logs()


def _audit_hashes() -> dict[str, str | None]:
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None
            for p in REAL_AUDIT_LOGS}


def _changed(before: dict, after: dict) -> list[str]:
    return [path for path in before if before[path] != after[path]]


@pytest.fixture(autouse=True)
def _real_audit_logs_untouched():
    before = _audit_hashes()
    yield
    changed = _changed(before, _audit_hashes())
    assert not changed, f"Test GERCEK denetim kaydina yazdi: {changed}"


# ── Depo icindeki proje yedekleri ─────────────────────────────────────────
#
# JarvisBackupTool.for_jarvis() yedekleri HOME/JARVIS_HOME'dan BAGIMSIZ olarak
# depodaki src/jarvis_yedekler/ klasorune yazar (backups_root_for(
# jarvis_project_root())). HOME izolasyonu bunu engellemez; bir test gercek
# executor uzerinden backup_create calistirirsa depoda yedek birakir. Her
# testten once/sonra (ve oturum sonunda) bu klasorun alt ogeleri
# karsilastirilir; yeni bir oge olusursa test basarisiz olur. Yol cagri
# aninda ayni fonksiyonlardan hesaplanir (tek kaynak: backup_tool).

def _project_backups_root() -> Path | None:
    try:
        from jarvis.backup_tool import backups_root_for, jarvis_project_root
        return backups_root_for(jarvis_project_root())
    except Exception:
        return None


def _project_backups(root: Path | None) -> frozenset[str]:
    if root is None or not root.is_dir():
        return frozenset()
    return frozenset(p.name for p in root.iterdir())


@pytest.fixture(autouse=True)
def _no_project_backups_in_repo():
    root = _project_backups_root()
    before = _project_backups(root)
    yield
    created = sorted(_project_backups(root) - before)
    assert not created, (
        f"Test depoya proje yedegi yazdi ({root}): {created}. "
        "Yedek araci HOME'dan bagimsiz olarak depoya yazar; testte arac fonksiyonunu "
        "kayit tutan bir fonksiyonla degistirin ya da jarvis_project_root'u tmp'ye "
        "yonlendirin.")


# ── Kullanici dizinlerinin izolasyonu ─────────────────────────────────────
#
# Iki katman:
#  1) Oturum: pytest_configure (moduller TOPLANMADAN/import edilmeden once)
#     HOME/USERPROFILE/JARVIS_HOME'u gecici bir klasore cevirir, XDG
#     degiskenlerini kaldirir. Boylece import aninda hesaplanan yollar
#     (ör. conversation_log.LOG_PATH) ve testten sonra yazmaya devam eden arka
#     plan thread'leri de gercek dizine degil gecici klasore yazar.
#  2) Test: her test kendi bos HOME/JARVIS_HOME'unu alir (autouse fixture);
#     testin kendi monkeypatch.setenv'i bunun ustune yazabilir.

_ISOLATED_ENV_KEYS = ("HOME", "USERPROFILE", "JARVIS_HOME")
# Derleme onbellekleri HOME'dan turetilir; her teste bos HOME verildigi icin
# Go testleri her seferinde std kutuphaneyi sifirdan derliyordu (paket suresi
# ~2 kat). Oturum boyunca PAYLASILAN ama gercek ev dizini disinda bir yer.
_SHARED_CACHE_KEYS = ("GOCACHE", "GOPATH", "GOMODCACHE")


def _xdg_keys():
    # Kullanici veri/ayar klasorleri (XDG_DESKTOP_DIR, XDG_DATA_HOME ...).
    # XDG_RUNTIME_DIR bir veri klasoru degil, ses sunucusu gibi oturum
    # soketlerinin yeri: kaldirilirsa jarvis.main import'unda PortAudio
    # PulseAudio'ya baglanamaz.
    return [k for k in os.environ
            if k in ("XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME")
            or (k.startswith("XDG_") and k.endswith("_DIR") and k != "XDG_RUNTIME_DIR")]


def pytest_configure(config):
    import tempfile
    base = Path(tempfile.mkdtemp(prefix="jarvis-tests-"))
    (base / "home").mkdir()
    saved = {k: os.environ.get(k)
             for k in list(_ISOLATED_ENV_KEYS) + list(_SHARED_CACHE_KEYS) + _xdg_keys()}
    config._jarvis_isolation = (base, saved)
    os.environ["HOME"] = str(base / "home")
    os.environ["USERPROFILE"] = str(base / "home")
    os.environ["JARVIS_HOME"] = str(base / "jarvis_home")
    os.environ["GOCACHE"] = str(base / "cache" / "go-build")
    os.environ["GOPATH"] = str(base / "go")
    os.environ["GOMODCACHE"] = str(base / "go" / "pkg" / "mod")
    for key in _xdg_keys():
        del os.environ[key]


def pytest_unconfigure(config):
    import shutil
    base, saved = getattr(config, "_jarvis_isolation", (None, {}))
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    if base is not None:
        shutil.rmtree(base, ignore_errors=True)


@pytest.fixture(autouse=True)
def _isolated_user_dirs(tmp_path_factory, monkeypatch):
    base = tmp_path_factory.mktemp("isolated-user")
    (base / "home").mkdir()
    monkeypatch.setenv("HOME", str(base / "home"))
    monkeypatch.setenv("USERPROFILE", str(base / "home"))
    monkeypatch.setenv("JARVIS_HOME", str(base / "jarvis_home"))
    for key in _xdg_keys():
        monkeypatch.delenv(key, raising=False)
    yield


def pytest_sessionstart(session):
    session.config._real_audit_hashes = _audit_hashes()
    root = _project_backups_root()
    session.config._project_backups = (root, _project_backups(root))


def pytest_sessionfinish(session, exitstatus):
    root, backups_before = getattr(session.config, "_project_backups", (None, frozenset()))
    created = sorted(_project_backups(root) - backups_before)
    if created:
        print(f"\nHATA: test paketi depoya proje yedegi yazdi ({root}): {created}")
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
    before = getattr(session.config, "_real_audit_hashes", None)
    if before is None:
        return
    changed = _changed(before, _audit_hashes())
    if changed:
        print(f"\nHATA: test paketi GERCEK denetim kayitlarini degistirdi: {changed}")
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
