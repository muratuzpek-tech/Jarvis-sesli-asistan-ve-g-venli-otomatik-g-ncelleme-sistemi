"""tests/test_terminal_tilde.py

terminal_tool komutlari shell=False ile calistirir; "~" kabuk tarafindan
genisletilmiyordu. `touch ~/x` ev dizinine degil, calisma klasorunde
"~" adli bir klasorun icine yaziyordu; onizleme de kullaniciya "~/x"
gosteriyordu. Yol denetimleri genisletilmemis metne bakiyordu:
`cat ~/../etc/passwd` ev dizini disini okudugu halde salt-okunur sayilip
onaysiz calisiyordu.

Artik "~" ve "~/..." ile baslayan her token onizlemeden ve calistirmadan
ONCE os.path.expanduser ile genisletilir; yol denetimleri genisletilmis yol
uzerinden yapilir. Ayni kural security_gate'in terminal etki hesabinda da
gecerlidir (kapi ile arac ayni komutu ayni sekilde siniflandirir).

IZOLASYON: HOME ve JARVIS_HOME tmp_path; gercek ~/.jarvis/audit.log ve
MuratJARVIS audit.log'un degismedigi kontrol edilir. Gercek bir `touch`
yalnizca tmp ev dizininde calisir.
"""
import hashlib
import os
import shutil
from pathlib import Path

import pytest

from jarvis.actions import terminal_tool as tt
from jarvis.core.approval_service import approval_service


def _real_home() -> Path:
    try:
        import pwd
        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except Exception:
        return Path(os.environ.get("USERPROFILE") or os.path.expanduser("~"))


_GUARDED = [_real_home() / ".jarvis" / "audit.log",
            _real_home() / ".local" / "share" / "MuratJARVIS" / "memory" / "audit.log"]


def _snapshot():
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None
            for p in _GUARDED}


@pytest.fixture(scope="module", autouse=True)
def real_audit_untouched():
    before = _snapshot()
    yield
    assert _snapshot() == before, "Testler GERCEK audit.log'a yazdi!"


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / "work" / "~").mkdir(parents=True)   # eski hata burada "~/x" olustururdu
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("USERPROFILE", str(h))
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "jh"))
    from jarvis.paths import memory_dir
    assert Path.home().resolve() == h.resolve()
    assert memory_dir().resolve().is_relative_to(tmp_path.resolve())
    monkeypatch.setattr(approval_service, "_pending", {})
    return h


def _code(text: str) -> str:
    return next(line.split(": ", 1)[1] for line in text.splitlines() if line.startswith("Onay kodu:"))


@pytest.mark.skipif(shutil.which("touch") is None, reason="touch yok")
def test_touch_tilde_writes_into_home_not_cwd(home):
    cwd = home / "work"
    cmd = {"command": "touch ~/x", "cwd": str(cwd)}
    preview = tt.terminal_tool(cmd)
    assert preview.startswith("ONAY GEREKLİ"), preview
    approval_service.mark_user_turn()          # kullanicinin sonraki turu
    out = tt.terminal_tool({**cmd, "confirm_code": _code(preview)})
    assert out.startswith("exit_code: 0"), out
    assert (home / "x").is_file()
    assert not (cwd / "~" / "x").exists()


def test_preview_shows_the_expanded_path(home):
    preview = tt.terminal_tool({"command": "touch ~/x ~", "cwd": str(home / "work")})
    assert preview.startswith("ONAY GEREKLİ"), preview
    first = preview.splitlines()[1]
    assert first.startswith("Komut: ")
    assert f"{home}/x" in first and str(home) in first
    assert "~" not in first


def test_bare_tilde_and_tilde_slash_are_expanded_others_untouched(home):
    argv = tt._expand_home_tokens(["ls", "~", "~/a/b", "x~", "~kullanici/y", "--opt=~/z", "a/~/b"])
    assert argv == ["ls", str(home), f"{home}/a/b", "x~", "~kullanici/y", "--opt=~/z", "a/~/b"]


@pytest.mark.parametrize("command", ["cat ~/../etc/passwd", "head ~/../../etc/shadow",
                                     "grep root ~/../etc/passwd", "ls ~/.."])
def test_tilde_path_escaping_home_is_not_readonly(home, command):
    import shlex
    assert tt._is_readonly(shlex.split(command)) is False
    assert tt._is_readonly(tt._expand_home_tokens(shlex.split(command))) is False


def test_escaping_read_requires_approval_and_reads_nothing(home):
    outside = home.parent / "disari.txt"
    outside.write_text("GIZLI-ICERIK")
    out = tt.terminal_tool({"command": "cat ~/../disari.txt"})
    assert out.startswith("ONAY GEREKLİ"), out
    assert "GIZLI-ICERIK" not in out
    assert f"{home}/../disari.txt" in out


def test_tilde_path_inside_home_stays_readonly(home):
    (home / "notlar.txt").write_text("merhaba")
    assert tt._is_readonly(["cat", "~/notlar.txt"]) is True
    out = tt.terminal_tool({"command": "cat ~/notlar.txt"})
    assert out.startswith("exit_code: 0") and "merhaba" in out


def test_gate_and_tool_classify_the_same_command_identically(home):
    from jarvis import security_gate as sg
    for command in ("cat ~/../etc/passwd", "cat ~/notlar.txt", "touch ~/x", "ls ~"):
        tool_ro = tt._is_readonly(tt._expand_home_tokens(command.split()))
        gate_ro = sg.spec_for("terminal").effect_of({"command": command}) is sg.Effect.READ
        assert tool_ro == gate_ro, command
