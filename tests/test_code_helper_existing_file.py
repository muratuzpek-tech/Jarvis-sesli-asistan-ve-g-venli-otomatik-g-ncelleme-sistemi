"""tests/test_code_helper_existing_file.py

code_helper'in edit ve optimize eylemlerindeki MEVCUT dosya dali (file_path)
iki acik tasiyordu:

1. Hedef politikasi yoktu: yeni dosya dali resolve_write_target'tan
   geciyordu ama file_path dali hicbir kontrol yapmadan ~/.bashrc,
   ~/.ssh/authorized_keys, ~/.config/autostart/ ya da ev dizini disindaki bir
   dosyayi okuyup (icerigi Gemini'ye gonderip) uzerine yazabiliyordu.
   Artik iki dal da file_controller ile AYNI politikadan gecer: yalnizca izinli
   klasorler, gizli bilesen yok, son bilesen symlink degil.
2. Onay kodu modele donuyordu: model onu ayni turda geri gonderince dosya
   kullanici onayi olmadan yaziliyordu. Ayrica onaydan sonraki cagri kodu
   YENIDEN uretiyordu, yani kaydedilen icerik kullanicinin gordugu onizleme
   degildi. Artik kod modele gosterilmez, modelin gonderdigi confirm_code yok
   sayilir, onay main.py'deki tek kullanimlik kullanici onayina baglidir ve
   kaydedilen icerik onizlenen icerigin aynisidir.

IZOLASYON: HOME/JARVIS_HOME tmp_path'e yonlendirilir, file_controller ve
code_helper yeniden yuklenir; Path.home() ve DESKTOP tmp_path altinda
dogrulanmadan test baslamaz. Gemini cagrisi sahte bir modelle degistirilir.
Oturum sonunda gercek ev dizinindeki hassas dosyalarin degismedigi kontrol
edilir.
"""
import asyncio
import hashlib
import importlib
import os
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

import jarvis.main as main_mod
from jarvis.main import JarvisLive


def _real_home() -> Path:
    try:
        import pwd
        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except Exception:
        return Path(os.environ.get("USERPROFILE") or os.path.expanduser("~"))


REAL_HOME = _real_home()


def _snapshot():
    snap = {}
    for rel in (".bashrc", ".profile", ".ssh/authorized_keys"):
        p = REAL_HOME / rel
        snap[rel] = hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None
    for rel in ("Desktop", "Documents", ".config/autostart"):
        p = REAL_HOME / rel
        snap[rel] = sorted(os.listdir(p)) if p.is_dir() else None
    return snap


@pytest.fixture(scope="module", autouse=True)
def real_home_untouched():
    before = _snapshot()
    yield
    assert _snapshot() == before, "Testler GERCEK ev dizinine dokundu!"


class _FakeModel:
    """Her cagrida FARKLI kod uretir - onaydan sonra yeniden uretilen degil,
    onizlenen icerigin kaydedildigini ayirt etmek icin."""

    def __init__(self):
        self.calls = 0
        self.fixed = False  # True: gercek bir model gibi ayni talimata ayni cikti

    def generate_content(self, _prompt):
        self.calls += 1
        n = 1 if self.fixed else self.calls
        return SimpleNamespace(text=f"print('surum {n}')\n")


class _UI:
    muted = False

    def __getattr__(self, _name):
        return lambda *a, **k: None


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    for d in ("Desktop", "Documents", ".ssh", ".config/autostart"):
        (home / d).mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "jarvis_home"))
    for var in list(os.environ):
        if var.startswith("XDG_") and var.endswith("_DIR"):
            monkeypatch.delenv(var, raising=False)

    import jarvis.actions.file_controller as fc_mod
    import jarvis.actions.code_helper as ch
    fc = importlib.reload(fc_mod)
    # code_helper YENIDEN YUKLENMEZ (UnsafeWriteTarget sinifi diger test
    # dosyalarinin import ettigiyle ayni kalmali); Masaustu her cagrida
    # file_controller._get_desktop() ile cozulur.
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    tmp = tmp_path.resolve()
    assert Path.home().resolve().is_relative_to(tmp)
    assert fc._get_desktop().resolve().is_relative_to(tmp)
    assert all(r.resolve().is_relative_to(tmp) for r in fc._SAFE_ROOTS)

    model = _FakeModel()
    monkeypatch.setattr(ch, "_get_gemini", lambda *a, **k: model)
    monkeypatch.setattr(main_mod, "code_helper", ch.code_helper)
    monkeypatch.setattr(main_mod, "_JARVIS2_REGISTRY_AVAILABLE", False)

    (home / ".bashrc").write_text("ORIG_BASHRC\n")
    (home / ".ssh" / "authorized_keys").write_text("ORIG_KEY\n")
    (home / ".config" / "autostart" / "x.desktop").write_text("[Desktop Entry]\n")
    (home / "notes.py").write_text("print('home root')\n")
    (home / "Desktop" / "app.py").write_text("print('orijinal')\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x.py").write_text("print('disarida')\n")
    git_hook = home / "Documents" / "proj" / ".git" / "hooks" / "pre-commit"
    git_hook.parent.mkdir(parents=True)
    git_hook.write_text("#!/bin/sh\n")
    (home / "Desktop" / "link.py").symlink_to(home / ".bashrc")

    jl = JarvisLive.__new__(JarvisLive)
    jl.ui = _UI()
    jl.session = None
    jl._bg_tasks = None

    yield SimpleNamespace(ch=ch, jl=jl, home=home, outside=outside, model=model)

    monkeypatch.undo()
    importlib.reload(fc_mod)


def _code_in(text):
    m = re.search(r"confirm_code\s*=\s*'?([0-9a-f]+)", text)
    return m.group(1) if m else None


def _targets(env):
    h = env.home
    return {
        "bashrc": h / ".bashrc",
        "authorized_keys": h / ".ssh" / "authorized_keys",
        "autostart": h / ".config" / "autostart" / "x.desktop",
        "home_root": h / "notes.py",
        "outside_home": env.outside / "x.py",
        "git_hook": h / "Documents" / "proj" / ".git" / "hooks" / "pre-commit",
        "symlink_to_bashrc": h / "Desktop" / "link.py",
    }


TARGET_KEYS = ["bashrc", "authorized_keys", "autostart", "home_root",
               "outside_home", "git_hook", "symlink_to_bashrc"]


def _ch_call(env, **params):
    return env.ch.code_helper(parameters=params)


# ── 1) Hedef politikasi: mevcut dosya dali ──

@pytest.mark.parametrize("key", TARGET_KEYS)
@pytest.mark.parametrize("action", ["edit", "optimize"])
def test_existing_file_outside_policy_rejected(env, key, action):
    target = _targets(env)[key]
    before = target.read_bytes()
    params = dict(action=action, file_path=str(target), description="degistir")

    env.model.fixed = True
    first = _ch_call(env, **params)
    code = _code_in(first)
    if code:  # eski kod: model kodu geri gonderip yazdirabiliyordu
        _ch_call(env, **params, confirm_code=code)

    assert "Güvenlik" in first, first
    assert code is None
    assert target.read_bytes() == before
    assert (env.home / ".bashrc").read_text() == "ORIG_BASHRC\n"


@pytest.mark.parametrize("key", ["bashrc", "authorized_keys"])
def test_sensitive_file_content_is_not_sent_to_llm(env, key):
    _ch_call(env, action="edit", file_path=str(_targets(env)[key]), description="degistir")
    assert env.model.calls == 0


def test_new_and_existing_branch_share_one_policy(env):
    ch = env.ch
    for key in TARGET_KEYS:
        with pytest.raises(ch.UnsafeWriteTarget):
            ch.check_write_target(_targets(env)[key])
    assert ch.check_write_target(env.home / "Desktop" / "app.py") == env.home / "Desktop" / "app.py"
    with pytest.raises(ch.UnsafeWriteTarget):
        ch.resolve_write_target(str(env.home / "notes2.py"), "python")
    assert ch.resolve_write_target("yeni.py", "python") == env.home / "Desktop" / "yeni.py"


def test_optimize_new_file_branch_still_works(env):
    r = _ch_call(env, action="optimize", code="x=1", output_path="opt.py")
    assert "Saved to" in r, r
    assert (env.home / "Desktop" / "opt.py").read_text() == "print('surum 1')"


def test_edit_in_allowed_folder_returns_preview(env):
    r = _ch_call(env, action="edit", file_path=str(env.home / "Desktop" / "app.py"), description="degistir")
    assert "ONAY" in r
    assert (env.home / "Desktop" / "app.py").read_text() == "print('orijinal')\n"


# ── 2) Onay: main.py uzerinden, gercek kullanici turuna bagli ──

def _main_call(env, **args):
    resp = asyncio.run(env.jl._execute_tool(SimpleNamespace(name="code_helper", args=args, id="1")))
    return str(resp.response.get("result", resp.response))



@pytest.fixture
def edit_args(env):
    return dict(action="edit", file_path=str(env.home / "Desktop" / "app.py"), description="degistir")


@pytest.fixture
def optimize_args(env):
    return dict(action="optimize", file_path=str(env.home / "Desktop" / "app.py"))


@pytest.mark.parametrize("which", ["edit_args", "optimize_args"])
def test_preview_does_not_reveal_code_to_model(env, which, request):
    args = request.getfixturevalue(which)
    r = _main_call(env, **args)
    assert "ONAY" in r
    assert "confirm_code" not in r
    assert _code_in(r) is None
    assert "surum 1" in r  # onizleme hala gorunur


@pytest.mark.parametrize("which", ["edit_args", "optimize_args"])
def test_model_replaying_code_without_user_turn_does_not_write(env, which, request):
    env.model.fixed = True
    args = request.getfixturevalue(which)
    r = _main_call(env, **args)
    _main_call(env, **args, confirm_code=_code_in(r) or "deadbe")
    assert (env.home / "Desktop" / "app.py").read_text() == "print('orijinal')\n"


@pytest.mark.parametrize("which", ["edit_args", "optimize_args"])
def test_user_confirmation_saves_previewed_content(env, which, request):
    args = request.getfixturevalue(which)
    _main_call(env, **args)
    env.jl._grant_dangerous_confirmation()  # kullanici bir sonraki turda "evet" dedi
    r = _main_call(env, **args)
    assert "Saved to" in r, r
    # Kaydedilen icerik kullanicinin gordugu onizleme; yeniden uretilmedi.
    assert (env.home / "Desktop" / "app.py").read_text() == "print('surum 1')"
    assert env.model.calls == 1


def test_confirmation_does_not_cover_different_args(env, edit_args):
    (env.home / "Desktop" / "b.py").write_text("print('b')\n")
    _main_call(env, **edit_args)
    env.jl._grant_dangerous_confirmation()
    _main_call(env, **dict(edit_args, file_path=str(env.home / "Desktop" / "b.py")))
    assert (env.home / "Desktop" / "b.py").read_text() == "print('b')\n"
    assert (env.home / "Desktop" / "app.py").read_text() == "print('orijinal')\n"


def test_confirmation_is_single_use(env, edit_args):
    _main_call(env, **edit_args)
    env.jl._grant_dangerous_confirmation()
    assert "Saved to" in _main_call(env, **edit_args)
    (env.home / "Desktop" / "app.py").write_text("print('elle')\n")
    r = _main_call(env, **edit_args)
    assert "Saved to" not in r
    assert (env.home / "Desktop" / "app.py").read_text() == "print('elle')\n"


def test_guessed_code_is_ignored(env, edit_args):
    env.model.fixed = True
    _main_call(env, **edit_args)
    for guess in ("000000", "ffffff", "abcdef"):
        _main_call(env, **edit_args, confirm_code=guess)
    assert (env.home / "Desktop" / "app.py").read_text() == "print('orijinal')\n"


def test_code_helper_schema_has_no_confirm_code():
    decl = next(d for d in main_mod.TOOL_DECLARATIONS if d["name"] == "code_helper")
    assert "confirm_code" not in decl["parameters"]["properties"]
