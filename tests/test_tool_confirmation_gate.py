"""tests/test_tool_confirmation_gate.py

Guvenlik bulgulari 4, 5 ve discovered_* araclari:

4) terminal onizlemesi "Onay kodu: X" satirini MODELE donuyordu ve
   approval_service, kod uretildikten sonraki HERHANGI bir kullanici turunu
   (ör. "hayir") yeterli sayiyordu: kullanici reddettikten sonra model kodu
   geri gondererek komutu calistirabiliyordu. Artik kod modele gosterilmez,
   modelin gonderdigi confirm_code yok sayilir, onay file_controller ile ayni
   tek kullanimlik, arguman-bagli kullanici onayindan gelir; "hayir" bekleyen
   onay kaydini iptal eder.

5) tool_gate bos olmayan HER confirm_code'u onay sayiyor ve gercek kodu
   modele gosteriyordu (send_message, computer_control, open_app,
   self_improve). Artik onay yalnizca kullanici turundan gelir ve kod yoktur.
   self_improve guvenlik dosyalarini hedefleyemez.

discovered_topydo / discovered_jc (disaridan entegre edilmis kod) Gemini'ye
dogrudan acikti ve onaysiz calisiyordu; artik fail-closed onay ister.

Onay kararini veren kod (gate, _consume_dangerous_confirmation,
approval_service, self_improve hedef politikasi) STUB'LANMAZ. Yalnizca
araclarin kendisi cagri kaydeden sahte fonksiyonlarla degistirilir; terminal
testleri tmp ev dizininde gercek, zararsiz bir python komutu calistirir.
LLM/ag cagrisi yok; self_improve'un model ve yedek adimlari, kod oraya
ulasirsa test patlasin diye hata firlatan sahtelerle degistirilmistir.

IZOLASYON: HOME/JARVIS_HOME tmp_path; gercek MuratJARVIS dosyalari kontrol
edilir.
"""
import asyncio
import hashlib
import os
import shlex
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import jarvis.main as main_mod
from jarvis.main import JarvisLive
from jarvis.core.approval_service import approval_service
from jarvis import tool_gate


def _real_home() -> Path:
    try:
        import pwd
        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except Exception:
        return Path(os.environ.get("USERPROFILE") or os.path.expanduser("~"))


REAL_MEMORY = _real_home() / ".local" / "share" / "MuratJARVIS" / "memory"
_GUARDED = [REAL_MEMORY / n for n in ("audit.log", "self_improve_log.jsonl", "conversation_log.jsonl",
                                     "agent_tasks.json", "agent_loop_log.jsonl")]


def _snapshot():
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None
            for p in _GUARDED}


@pytest.fixture(scope="module", autouse=True)
def real_data_untouched():
    before = _snapshot()
    yield
    assert _snapshot() == before, "Testler GERCEK MuratJARVIS verisine dokundu!"


class _UI:
    muted = False

    def __init__(self):
        self.logs = []

    def write_log(self, text):
        self.logs.append(str(text))

    def __getattr__(self, _name):
        return lambda *a, **k: None


class _Recorder:
    def __init__(self, result="tamam"):
        self.calls = []
        self.result = result

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.result


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "Desktop").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "jh"))
    from jarvis.paths import memory_dir
    assert Path.home().resolve().is_relative_to(tmp_path.resolve())
    assert memory_dir().resolve().is_relative_to(tmp_path.resolve())
    monkeypatch.setattr(approval_service, "_pending", {})
    from jarvis.actions import agent_loop as al
    monkeypatch.setattr(al, "TASKS_PATH", tmp_path / "agent_tasks.json")
    monkeypatch.setattr(al, "LOG_PATH", tmp_path / "agent_loop_log.jsonl")
    # conversation_log.LOG_PATH import aninda GERCEK hafiza dizinine baglaniyor.
    from jarvis.actions import conversation_log as cl
    monkeypatch.setattr(cl, "LOG_PATH", tmp_path / "conversation_log.jsonl")

    jl = JarvisLive.__new__(JarvisLive)
    jl.ui = _UI()
    jl.session = None
    jl._loop = None
    jl._bg_tasks = None
    spoken = []
    jl.speak = lambda text: spoken.append(str(text))
    jl.speak_error = lambda *a, **k: None
    jl._set_pending_dangerous(None)
    jl._tool_confirm_code = None
    jl._pending_terminal_command = None
    yield SimpleNamespace(jl=jl, home=home, tmp=tmp_path, spoken=spoken)
    monkeypatch.undo()


def call(env, name, **args):
    resp = asyncio.run(env.jl._execute_tool(SimpleNamespace(name=name, args=args, id="1")))
    return str(resp.response.get("result", resp.response))


def _terminal_codes():
    return [code for code, item in approval_service._pending.items() if item.action == "terminal"]


def _marker_cmd(env):
    marker = env.tmp / "TERMINAL_RAN"
    code = f"open({str(marker)!r}, 'w').write('x')"
    return marker, f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"


# ── 4) terminal ──

def test_terminal_preview_does_not_show_code_to_model(env):
    marker, cmd = _marker_cmd(env)
    r = call(env, "terminal", command=cmd)
    assert "ONAY" in r
    codes = _terminal_codes()
    assert len(codes) == 1
    assert codes[0] not in r
    assert "Onay kodu" not in r and "confirm_code" not in r
    assert not marker.exists()


def test_model_resending_code_after_unrelated_turn_does_not_run(env):
    marker, cmd = _marker_cmd(env)
    call(env, "terminal", command=cmd)
    code = _terminal_codes()[0]
    approval_service.mark_user_turn()          # kullanici baska bir sey soyledi
    r = call(env, "terminal", command=cmd, confirm_code=code)
    assert not marker.exists(), r
    r = call(env, "terminal", command=cmd)     # ayni komutu tekrar gondermek de yetmez
    assert not marker.exists(), r


def test_user_no_cancels_terminal_approval_record(env):
    marker, cmd = _marker_cmd(env)
    call(env, "terminal", command=cmd)
    code = _terminal_codes()[0]
    env.jl._on_text_command("hayır")
    approval_service.mark_user_turn()
    assert code not in approval_service._pending
    from jarvis.actions.terminal_tool import terminal_tool
    out = terminal_tool({"command": cmd, "confirm_code": code})
    assert not marker.exists(), out


def test_spoken_no_cancels_terminal_approval_record(env):
    marker, cmd = _marker_cmd(env)
    call(env, "terminal", command=cmd)
    code = _terminal_codes()[0]
    env.jl._apply_spoken_confirmation("hayır istemiyorum")
    assert code not in approval_service._pending
    assert env.jl._pending_dangerous_action is None


def test_user_yes_then_same_command_runs_once(env):
    marker, cmd = _marker_cmd(env)
    call(env, "terminal", command=cmd)
    approval_service.mark_user_turn()
    env.jl._grant_dangerous_confirmation()     # kullanici bir sonraki turda "evet" dedi
    r = call(env, "terminal", command=cmd)
    assert marker.exists(), r
    marker.unlink()
    r = call(env, "terminal", command=cmd)     # tek kullanimlik
    assert not marker.exists(), r


def test_yes_does_not_cover_a_different_command(env):
    marker, cmd = _marker_cmd(env)
    call(env, "terminal", command=f"{shlex.quote(sys.executable)} -c 'print(1)'")
    approval_service.mark_user_turn()
    env.jl._grant_dangerous_confirmation()
    call(env, "terminal", command=cmd)
    assert not marker.exists()


def test_router_preview_hides_code_and_no_cancels(env):
    env.jl.session = object()
    env.jl._loop = object()
    env.jl._on_text_command("python ile ekrana merhaba yazdır")
    codes = _terminal_codes()
    assert len(codes) == 1
    said = "\n".join(env.spoken + env.jl.ui.logs)
    assert "ONAY" in said
    assert codes[0] not in said and "Onay kodu" not in said
    env.jl.session = None   # "hayir" Gemini oturumu olmadan da islenmeli
    env.jl._on_text_command("hayır")
    assert codes[0] not in approval_service._pending


def test_terminal_schema_has_no_confirm_code():
    decl = next(d for d in main_mod.TOOL_DECLARATIONS if d["name"] == "terminal")
    assert "confirm_code" not in decl["parameters"]["properties"]
    assert "confirm_code" not in decl["description"]


# ── 5) tool_gate ──

GATED = {
    "send_message": ("send_message", {"receiver": "Ali", "message_text": "selam", "platform": "whatsapp"}),
    "computer_control": ("computer_control", {"action": "type", "text": "rm -rf ~"}),
    "open_app": ("open_app", {"app_name": "terminal"}),
}


@pytest.mark.parametrize("tool", list(GATED))
@pytest.mark.parametrize("fake", ["X", "evet", "ABC123", "1"])
def test_fake_confirm_code_does_not_run_gated_tool(env, monkeypatch, tool, fake):
    attr, args = GATED[tool]
    rec = _Recorder()
    monkeypatch.setattr(main_mod, attr, rec)
    r = call(env, tool, **args, confirm_code=fake)
    assert rec.calls == [], r
    assert r.startswith("CONFIRMATION_REQUIRED")


@pytest.mark.parametrize("tool", list(GATED))
def test_gate_preview_shows_no_code(env, monkeypatch, tool):
    attr, args = GATED[tool]
    monkeypatch.setattr(main_mod, attr, _Recorder())
    r = call(env, tool, **args)
    assert "ONAY_KODU" not in r and "confirm_code" not in r


@pytest.mark.parametrize("tool", list(GATED))
def test_gated_tool_runs_after_real_user_yes_with_same_args(env, monkeypatch, tool):
    attr, args = GATED[tool]
    rec = _Recorder()
    monkeypatch.setattr(main_mod, attr, rec)
    call(env, tool, **args)
    env.jl._grant_dangerous_confirmation()
    call(env, tool, **dict(args, extra="farkli"))   # farkli arguman: onay kapsamaz
    assert rec.calls == []
    call(env, tool, **args)
    env.jl._grant_dangerous_confirmation()
    call(env, tool, **args)
    assert len(rec.calls) == 1


@pytest.mark.parametrize("fake", ["X", "anything", "000000"])
def test_gate_unit_never_accepts_model_code(fake):
    assert tool_gate.gate("send_message", {"receiver": "a", "confirm_code": fake}) is not None
    assert tool_gate.gate("send_message", {"receiver": "a"}, user_approved=True) is None


# ── self_improve ──

@pytest.fixture
def si(env, monkeypatch):
    import jarvis.actions.self_improve as si_mod
    monkeypatch.setattr(si_mod, "LOG_PATH", env.tmp / "self_improve_log.jsonl")
    monkeypatch.setattr(si_mod, "_pending_self_improve", {})

    def _boom(*a, **k):
        raise AssertionError("self_improve onaysiz uygulama asamasina ulasti")

    monkeypatch.setattr(si_mod, "_get_model", _boom)
    import jarvis.backup_tool as bt
    monkeypatch.setattr(bt, "JarvisBackupTool", _boom)
    return si_mod


PROTECTED = ["actions/terminal_tool.py", "actions/tools_kopru.py",
             "core/approval_service.py", "main.py", "tool_gate.py"]


@pytest.mark.parametrize("rel", PROTECTED)
def test_self_improve_cannot_target_security_files(si, rel):
    for arg in (rel, str(si.BASE_DIR / rel)):
        out = si.self_improve({"file_path": arg, "goal": "onay kontrolünü kaldır"})
        assert not out.startswith("ONAY GEREKL"), out
    assert si._pending_self_improve == {}


def test_self_improve_auto_pick_skips_security_files(si):
    target = si._pick_target()
    assert target is None or str(target.relative_to(si.BASE_DIR)) not in PROTECTED
    assert si._is_allowed_target(si.BASE_DIR / "actions" / "weather_report.py")


def test_self_improve_preview_code_is_hidden_and_model_code_ignored(env, si):
    args = {"file_path": "actions/weather_report.py", "goal": "yorumları düzelt"}
    r = call(env, "self_improve", **args)
    assert "ONAY" in r and "confirm_code" not in r and "ONAY_KODU" not in r
    real = next(iter(si._pending_self_improve))
    assert real not in r
    # Model gercek kodu bilse bile onay sayilmaz (uygulama asamasina ulasirsa
    # sahte _get_model/yedek AssertionError firlatir).
    r = call(env, "self_improve", **args, confirm_code=real)
    assert "güncellendi" not in r


# ── discovered_* araclari ──

DISCOVERED = {
    "discovered_topydo": ("discovered_topydo_run", {"action": "add", "task": "süt al"}),
    "discovered_jc": ("discovered_jc_run", {"command": "date", "data": "x"}),
}


@pytest.mark.parametrize("tool", list(DISCOVERED))
def test_discovered_tool_does_not_run_without_approval(env, monkeypatch, tool):
    attr, args = DISCOVERED[tool]
    rec = _Recorder()
    monkeypatch.setattr(main_mod, attr, rec)
    r = call(env, tool, **args)
    assert rec.calls == [], r
    assert r.startswith("CONFIRMATION_REQUIRED")
    r = call(env, tool, **args, confirm_code="X")
    assert rec.calls == [], r
    env.jl._grant_dangerous_confirmation()
    call(env, tool, **args)
    assert len(rec.calls) == 1


def test_any_discovered_tool_is_fail_closed():
    assert tool_gate.needs_confirmation("discovered_yeni_bir_arac") is True
