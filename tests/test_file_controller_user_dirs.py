"""Murat@goxs 2026-09-29: Türkçe Ubuntu'da İndirilenler klasörü bulunamıyordu
(Jarvis ~/Downloads'a bakıyordu)."""
from __future__ import annotations

from pathlib import Path

from jarvis.actions import file_controller as fc


def _home(tmp_path: Path, dirs_file: bool) -> Path:
    home = tmp_path / "murat"
    for name in ("İndirilenler", "Masaüstü", "Desktop"):
        (home / name).mkdir(parents=True)
    if dirs_file:
        cfg = home / ".config"
        cfg.mkdir()
        (cfg / "user-dirs.dirs").write_text(
            '# yorum\nXDG_DOWNLOAD_DIR="$HOME/İndirilenler"\nXDG_DESKTOP_DIR="$HOME/Desktop"\n'
            'XDG_MUSIC_DIR="$HOME/"\n', encoding="utf-8")
    return home


def test_user_dirs_file_is_used(tmp_path, monkeypatch):
    monkeypatch.setattr(fc, "_OS", "Linux")
    monkeypatch.delenv("XDG_DOWNLOAD_DIR", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    home = _home(tmp_path, dirs_file=True)
    assert fc._user_folder("downloads", home) == home / "İndirilenler"
    assert fc._user_folder("desktop", home) == home / "Desktop"
    # Ev klasörünün kendisine işaret eden kayıt yok sayılır.
    assert fc._user_folder("music", home) == home / "Music"


def test_turkish_folder_found_without_user_dirs_file(tmp_path, monkeypatch):
    monkeypatch.setattr(fc, "_OS", "Linux")
    monkeypatch.delenv("XDG_DOWNLOAD_DIR", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    home = _home(tmp_path, dirs_file=False)
    assert fc._user_folder("downloads", home) == home / "İndirilenler"
    assert fc._user_folder("desktop", home) == home / "Desktop"   # İngilizce varsa o


def test_turkish_shortcut_names_are_recognised():
    for word in ("İndirilenler", "indirilenler", "İNDİRİLENLER"):
        assert fc._normalize_shortcut(word) == "downloads", word
    assert fc._normalize_shortcut("Masaüstü") == "desktop"
