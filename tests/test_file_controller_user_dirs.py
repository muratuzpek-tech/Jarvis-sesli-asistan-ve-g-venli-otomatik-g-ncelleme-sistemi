"""tests/test_file_controller_user_dirs.py

file_controller'in _get_desktop/_get_documents/... fonksiyonlari Linux'ta
yalnizca XDG_*_DIR ORTAM DEGISKENLERINE bakiyordu. Bunlar neredeyse hic ayarli
degildir; gercek konumlar ~/.config/user-dirs.dirs dosyasindadir. Turkce bir
sistemde Masaustu ~/Masaüstü, Belgeler ~/Belgeler'dir - Jarvis ise ~/Desktop,
~/Documents gibi (cogu zaman bos ya da kullanicinin gormedigi) klasorleri
kullaniyor, yazma politikasi da GERCEK klasorleri reddediyordu.

Artik ortam degiskeni yoksa user-dirs.dirs ayristirilir. $HOME'un kendisini,
ev dizininin bir atasini ya da gizli bir yolu gosteren girdiler yok sayilir
(aksi halde ev dizini koku ya da ~/.config yazilabilir hale gelirdi).

IZOLASYON: HOME tmp_path'e yonlendirilir, XDG_* degiskenleri temizlenir,
file_controller yeniden yuklenir; Path.home() dogrulanmadan test baslamaz.
"""
import importlib
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

LOCALIZED = {
    "XDG_DESKTOP_DIR": "Masaüstü",
    "XDG_DOWNLOAD_DIR": "İndirilenler",
    "XDG_DOCUMENTS_DIR": "Belgeler",
    "XDG_PICTURES_DIR": "Resimler",
    "XDG_MUSIC_DIR": "Müzik",
    "XDG_VIDEOS_DIR": "Videolar",
}
GETTERS = {
    "XDG_DESKTOP_DIR": "_get_desktop",
    "XDG_DOWNLOAD_DIR": "_get_downloads",
    "XDG_DOCUMENTS_DIR": "_get_documents",
    "XDG_PICTURES_DIR": "_get_pictures",
    "XDG_MUSIC_DIR": "_get_music",
    "XDG_VIDEOS_DIR": "_get_videos",
}
SHORTCUTS = {
    "XDG_DESKTOP_DIR": "desktop",
    "XDG_DOWNLOAD_DIR": "downloads",
    "XDG_DOCUMENTS_DIR": "documents",
    "XDG_PICTURES_DIR": "pictures",
    "XDG_MUSIC_DIR": "music",
    "XDG_VIDEOS_DIR": "videos",
}


def _write_user_dirs(home: Path, entries: dict, extra: str = "") -> None:
    cfg = home / ".config"
    cfg.mkdir(parents=True, exist_ok=True)
    lines = [
        "# This file is written by xdg-user-dirs-update",
        "# If you want to change or add directories, just edit the line you're",
        "# interested in. All local changes will be retained on the next run.",
    ]
    lines += [f'{k}="{v}"' for k, v in entries.items()]
    (cfg / "user-dirs.dirs").write_text("\n".join(lines) + "\n" + extra, encoding="utf-8")


@pytest.fixture
def env(tmp_path, monkeypatch):
    if os.name == "nt":
        pytest.skip("user-dirs.dirs yalnizca Linux'ta kullanilir")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "jarvis_home"))
    for var in list(os.environ):
        if var.startswith("XDG_") and (var.endswith("_DIR") or var == "XDG_CONFIG_HOME"):
            monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    import jarvis.actions.file_controller as fc_mod
    fc = importlib.reload(fc_mod)
    monkeypatch.setattr(fc, "_OS", "Linux")
    assert Path.home().resolve().is_relative_to(tmp_path.resolve())
    assert all(r.resolve().is_relative_to(tmp_path.resolve()) for r in fc._SAFE_ROOTS)

    # Hem yerellestirilmis GERCEK klasorler hem de Ingilizce adli "sahte"
    # klasorler diskte var - politika yalnizca gercek olanlari kabul etmeli.
    for name in list(LOCALIZED.values()) + ["Şablonlar", "Genel", "Desktop", "Documents",
                                             "Downloads", "Pictures", "Music", "Videos"]:
        (home / name).mkdir()
    _write_user_dirs(home, {**{k: f"$HOME/{v}" for k, v in LOCALIZED.items()},
                            "XDG_TEMPLATES_DIR": "$HOME/Şablonlar",
                            "XDG_PUBLICSHARE_DIR": "$HOME/Genel"})

    yield SimpleNamespace(fc=fc, home=home, tmp=tmp_path)

    monkeypatch.undo()
    importlib.reload(fc_mod)


# ── user-dirs.dirs okunuyor ──

@pytest.mark.parametrize("key", list(LOCALIZED))
def test_getters_read_user_dirs(env, key):
    assert getattr(env.fc, GETTERS[key])() == env.home / LOCALIZED[key]


@pytest.mark.parametrize("key", list(LOCALIZED))
def test_write_policy_accepts_real_localized_folders(env, key):
    target = env.home / LOCALIZED[key] / "not.txt"
    assert env.fc._is_safe_write_path(target) is True
    assert env.fc._is_safe_write_path(env.home / LOCALIZED[key] / "alt" / "x.txt") is True


@pytest.mark.parametrize("key", list(LOCALIZED))
def test_shortcuts_resolve_to_localized_folders(env, key):
    r = env.fc.file_controller(parameters={
        "action": "create_file", "path": SHORTCUTS[key], "name": "kisayol.txt", "content": "x"})
    assert r.startswith("File created"), r
    assert (env.home / LOCALIZED[key] / "kisayol.txt").read_text() == "x"


@pytest.mark.parametrize("rel", [
    "Documents/x.txt", "Downloads/x.txt",                     # gercek olmayan Ingilizce adlar
    "Pictures/x.txt", "Music/x.txt", "Videos/x.txt",
    "Şablonlar/x.txt", "Genel/x.txt",                         # user-dirs'te ama izinli degil
    "x.txt", ".config/user-dirs.dirs", ".config/autostart/x.desktop",
    "Belgeler/.gizli/x.txt",
])
def test_write_policy_rejects_everything_else(env, rel):
    assert env.fc._is_safe_write_path(env.home / rel) is False


def test_user_dirs_file_itself_cannot_be_rewritten(env):
    r = env.fc.file_controller(parameters={
        "action": "write", "path": "home", "name": ".config/user-dirs.dirs",
        "content": 'XDG_DOCUMENTS_DIR="$HOME"\n'})
    assert "Access denied" in r
    assert "Belgeler" in (env.home / ".config" / "user-dirs.dirs").read_text()


# ── ~/Desktop: gercek masaustu baska yerde olsa da izinli kalir ──

def test_english_desktop_stays_writable(env):
    assert env.fc._get_desktop() == env.home / "Masaüstü"
    assert env.fc._is_safe_write_path(env.home / "Desktop" / "x.txt") is True
    assert env.fc._is_safe_write_path(env.home / "Desktop" / "alt" / "y.py") is True
    assert env.fc._is_safe_write_path(env.home / "Desktop" / ".gizli" / "x") is False
    r = env.fc.file_controller(parameters={
        "action": "create_file", "path": str(env.home / "Desktop"), "name": "eski.txt", "content": "e"})
    assert r.startswith("File created"), r


def test_relative_default_goes_to_real_desktop_not_english(env):
    r = env.fc.file_controller(parameters={
        "action": "create_file", "path": "desktop", "name": "yeni.txt", "content": "y"})
    assert r.startswith("File created"), r
    assert (env.home / "Masaüstü" / "yeni.txt").exists()
    assert not (env.home / "Desktop" / "yeni.txt").exists()


# ── Guvensiz girdiler yok sayilir ──

@pytest.mark.parametrize("value", [
    "$HOME", "$HOME/", '$HOME/.config', "$HOME/.config/autostart",
    "/", "$HOME/..", "$HOME/Belgeler/../.ssh",
])
def test_unsafe_entries_are_ignored(env, value):
    (env.home / ".config" / "autostart").mkdir(parents=True, exist_ok=True)
    (env.home / ".ssh").mkdir(exist_ok=True)
    _write_user_dirs(env.home, {"XDG_DOCUMENTS_DIR": value})
    docs = env.fc._get_documents()
    assert docs == env.home / "Documents"            # varsayilana dusuldu
    assert env.fc._is_safe_write_path(env.home / "x.txt") is False
    assert env.fc._is_safe_write_path(env.home / ".config" / "autostart" / "x.desktop") is False
    assert env.fc._is_safe_write_path(env.home / ".ssh" / "authorized_keys") is False


def test_missing_directory_falls_back(env):
    _write_user_dirs(env.home, {"XDG_DOCUMENTS_DIR": "$HOME/Yok"})
    assert env.fc._get_documents() == env.home / "Documents"


def test_absolute_path_entry_is_supported(env):
    data = env.tmp / "data" / "Belgelerim"
    data.mkdir(parents=True)
    _write_user_dirs(env.home, {"XDG_DOCUMENTS_DIR": str(data)})
    assert env.fc._get_documents() == data
    assert env.fc._is_safe_write_path(data / "x.txt") is True


def test_braced_home_and_comments(env):
    _write_user_dirs(env.home, {}, extra='  # yorum\nXDG_DOCUMENTS_DIR="${HOME}/Belgeler"\nbozuk satir\n')
    assert env.fc._get_documents() == env.home / "Belgeler"


def test_missing_user_dirs_file_keeps_old_default(env):
    (env.home / ".config" / "user-dirs.dirs").unlink()
    assert env.fc._get_desktop() == env.home / "Desktop"
    assert env.fc._is_safe_write_path(env.home / "Desktop" / "x.txt") is True


def test_env_var_still_takes_precedence(env, monkeypatch):
    other = env.home / "BaskaBelgeler"
    other.mkdir()
    monkeypatch.setenv("XDG_DOCUMENTS_DIR", str(other))
    assert env.fc._get_documents() == other


def test_xdg_config_home_is_respected(env, monkeypatch):
    alt = env.tmp / "altcfg"
    alt.mkdir()
    (alt / "user-dirs.dirs").write_text('XDG_DOCUMENTS_DIR="$HOME/Resimler"\n')
    monkeypatch.setenv("XDG_CONFIG_HOME", str(alt))
    assert env.fc._get_documents() == env.home / "Resimler"


# ── code_helper'in varsayilan hedefi gercek Masaustu ──

def test_code_helper_default_target_is_real_desktop(env):
    from jarvis.actions import code_helper as ch
    assert ch.resolve_write_target("hesap.py", "python") == env.home / "Masaüstü" / "hesap.py"
    assert ch.resolve_write_target("", "python") == env.home / "Masaüstü" / "jarvis_code.py"
    assert not (env.home / "Desktop" / "hesap.py").exists()


def test_code_helper_english_desktop_still_allowed(env):
    from jarvis.actions import code_helper as ch
    hedef = env.home / "Desktop" / "script.py"
    assert ch.resolve_write_target(str(hedef), "python") == hedef
