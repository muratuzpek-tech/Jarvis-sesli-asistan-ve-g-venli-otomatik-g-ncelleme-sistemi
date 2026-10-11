"""tests/test_file_controller_confirmation.py

file_controller'in move ve delete_all_files islemleri ilk cagrida bir
confirm_code uretip bunu sonuc metninde MODELE donuyordu. Model ayni turda
bu kodla tekrar cagirinca islem kullanici hic onay vermeden calisiyordu;
"kullanici onaylamadan bu kodu kullanma" yalnizca bir ricaydi.

Artik kod modele hic gosterilmez ve modelin gonderdigi confirm_code yok
sayilir. Onay, main.py'deki tek kullanimlik, arguman-bagli kullanici onayi
(_set_pending_dangerous / _consume_dangerous_confirmation) ile verilir; kodu
file_controller'a main.py kendisi iletir.

IZOLASYON: HOME tmp_path'e yonlendirilir, file_controller yeniden yuklenir,
Path.home() dogrulanmadan test baslamaz; cop kutusu tmp_path'e stub'lanir.
"""
import asyncio
import importlib
import os
import re
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

import jarvis.main as main_mod
from jarvis.main import JarvisLive


class _UI:
    muted = False

    def __getattr__(self, _name):
        return lambda *a, **k: None


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    for d in ("Desktop", "Documents", "Downloads"):
        (home / d).mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "jarvis_home"))
    for var in list(os.environ):
        if var.startswith("XDG_") and var.endswith("_DIR"):
            monkeypatch.delenv(var, raising=False)

    import jarvis.actions.file_controller as fc_mod
    fc = importlib.reload(fc_mod)
    assert Path.home().resolve().is_relative_to(tmp_path.resolve())
    assert all(r.resolve().is_relative_to(tmp_path.resolve()) for r in fc._SAFE_ROOTS)
    monkeypatch.setattr(main_mod, "file_controller", fc.file_controller)

    trash = tmp_path / "trash"
    trash.mkdir()
    monkeypatch.setattr(fc, "send2trash",
                        SimpleNamespace(send2trash=lambda p: shutil.move(str(p), str(trash / Path(p).name))),
                        raising=False)
    monkeypatch.setattr(fc, "_SEND2TRASH", True)

    (home / "Desktop" / "a.txt").write_text("A")
    for n in ("d1.txt", "d2.txt"):
        (home / "Downloads" / n).write_text(n)

    jl = JarvisLive.__new__(JarvisLive)
    jl.ui = _UI()
    jl.session = None
    jl._bg_tasks = None

    yield SimpleNamespace(jl=jl, home=home, trash=trash)

    monkeypatch.undo()
    importlib.reload(fc_mod)


def call(env, **args):
    resp = asyncio.run(env.jl._execute_tool(SimpleNamespace(name="file_controller", args=args, id="1")))
    return str(resp.response.get("result", resp.response))


def _code_in(text):
    m = re.search(r"confirm_code\s*=\s*'?([0-9a-f]+)", text)
    return m.group(1) if m else None


MOVE = dict(action="move", path="desktop", name="a.txt", destination="documents")
BULK = dict(action="delete_all_files", path="downloads")


# ── Model kodu kendi kendine kullanamaz ──

def test_preview_does_not_reveal_code_to_model(env):
    r = call(env, **MOVE)
    assert "ONAY" in r
    assert _code_in(r) is None
    assert "confirm_code" not in r
    assert (env.home / "Desktop" / "a.txt").exists()


def test_model_replaying_code_without_user_turn_does_not_move(env):
    r = call(env, **MOVE)
    code = _code_in(r) or "deadbeef"
    call(env, **MOVE, confirm_code=code)
    assert (env.home / "Desktop" / "a.txt").exists()
    assert not (env.home / "Documents" / "a.txt").exists()


def test_model_replaying_bulk_delete_code_does_not_delete(env):
    r = call(env, **BULK)
    code = _code_in(r) or "deadbeef"
    call(env, **BULK, confirm_code=code)
    assert sorted(p.name for p in (env.home / "Downloads").iterdir()) == ["d1.txt", "d2.txt"]
    assert not any(env.trash.iterdir())


def test_guessed_code_is_ignored(env):
    call(env, **MOVE)
    for guess in ("0000", "ffff", "1234"):
        call(env, **MOVE, confirm_code=guess)
    assert (env.home / "Desktop" / "a.txt").exists()


# ── Gercek kullanici onayi calisir ──

def test_user_confirmation_moves_with_same_args(env):
    call(env, **MOVE)
    env.jl._grant_dangerous_confirmation()  # kullanici bir sonraki turda "evet" dedi
    r = call(env, **MOVE)
    assert r.startswith("Moved"), r
    assert (env.home / "Documents" / "a.txt").read_text() == "A"


def test_user_confirmation_bulk_delete(env):
    call(env, **BULK)
    env.jl._grant_dangerous_confirmation()
    r = call(env, **BULK)
    assert r.startswith("Bulk delete complete"), r
    assert sorted(p.name for p in env.trash.iterdir()) == ["d1.txt", "d2.txt"]


def test_confirmation_is_single_use(env):
    (env.home / "Desktop" / "b.txt").write_text("B")
    call(env, **MOVE)
    env.jl._grant_dangerous_confirmation()
    assert call(env, **MOVE).startswith("Moved")
    other = dict(MOVE, name="b.txt")
    r = call(env, **other)
    assert not r.startswith("Moved")
    assert (env.home / "Desktop" / "b.txt").exists()


def test_confirmation_does_not_cover_different_args(env):
    (env.home / "Desktop" / "b.txt").write_text("B")
    call(env, **MOVE)
    env.jl._grant_dangerous_confirmation()
    r = call(env, **dict(MOVE, name="b.txt"))
    assert not r.startswith("Moved")
    assert (env.home / "Desktop" / "b.txt").exists()
    assert (env.home / "Desktop" / "a.txt").exists()


def test_unrelated_user_turn_cancels_pending(env):
    call(env, **MOVE)
    env.jl._set_pending_dangerous(None)  # kullanici baska bir sey soyledi
    env.jl._grant_dangerous_confirmation()
    assert not call(env, **MOVE).startswith("Moved")
    assert (env.home / "Desktop" / "a.txt").exists()


def test_confirmation_with_model_supplied_code_still_uses_real_code(env):
    call(env, **MOVE)
    env.jl._grant_dangerous_confirmation()
    r = call(env, **MOVE, confirm_code="0000")
    assert r.startswith("Moved"), r


# ── Sema modele confirm_code sunmaz ──

def test_file_controller_schema_has_no_confirm_code():
    decl = next(d for d in main_mod.TOOL_DECLARATIONS if d["name"] == "file_controller")
    assert "confirm_code" not in decl["parameters"]["properties"]
    assert "confirm_code" not in decl["description"]
