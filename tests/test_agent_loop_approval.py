"""tests/test_agent_loop_approval.py

Guvenlik bulgusu 1 ve 2:

1) agent_loop'un 'approve' eylemi Gemini aracina acikti: model onay bekleyen
   HER gorevi (send_message, file_controller silme, entegrasyon_uygula ...)
   kendi kendine onaylayabiliyordu. Artik approve Gemini semasinda yok; onay
   Brain Team ile ayni mekanizmadan gelir: bekleyen gorev main.py'deki tek
   kullanimlik, 60 sn TTL'li, parmak izi (gorev kimligi + arac + argumanlar)
   bagli yuvaya kaydedilir ve yalnizca kullanicinin gercek turundaki
   "evet"/"hayir" ile cozulur.

2) tools_kopru.is_destructive ALLOWED_TOOLS'taki her araca False donuyordu;
   entegrasyon_uygula ile eklenen (disaridan gelen) bir arac boylece her
   cagrida onaysiz calisiyordu. Artik yalnizca acikca salt-okunur isaretli
   araclar onaysiz calisir (fail-closed).

Bu testler onay kararini (is_destructive, _consume_dangerous_confirmation,
approve_task) STUB'LAMAZ. Yalnizca arac fonksiyonlari cagri kaydeden sahte
fonksiyonlarla degistirilir ve LLM planlayicisi (_decide_next_step) sabit bir
adim dondurur - ag/LLM cagrisi yok.

IZOLASYON: HOME/JARVIS_HOME tmp_path; agent_loop gorev/log dosyalari tmp;
oturum sonunda gercek MuratJARVIS dosyalarinin degismedigi dogrulanir.
"""
import hashlib
import importlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import jarvis.main as main_mod
from jarvis.main import JarvisLive
from jarvis.actions import agent_loop as al
from jarvis.actions import tools_kopru


def _real_home() -> Path:
    try:
        import pwd
        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except Exception:
        return Path(os.environ.get("USERPROFILE") or os.path.expanduser("~"))


REAL_HOME = _real_home()
REAL_MEMORY = REAL_HOME / ".local" / "share" / "MuratJARVIS" / "memory"
_GUARDED = [REAL_MEMORY / n for n in (
    "agent_tasks.json", "agent_loop_log.jsonl", "audit.log", "integration_log.jsonl",
)]


def _snapshot():
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None
            for p in _GUARDED}


@pytest.fixture(scope="module", autouse=True)
def real_data_untouched():
    before = _snapshot()
    yield
    assert _snapshot() == before, "Testler GERCEK MuratJARVIS verisine dokundu!"


class _UI:
    def __init__(self):
        self.logs = []
        self.muted = False

    def write_log(self, text):
        self.logs.append(str(text))

    def __getattr__(self, _name):
        return lambda *a, **k: None


class _Recorder:
    def __init__(self, result="kaydedildi"):
        self.calls = []
        self.result = result

    def __call__(self, parameters):
        self.calls.append(dict(parameters or {}))
        return self.result


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "Desktop").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "jh"))
    monkeypatch.delenv("JARVIS_AUTO_DISCOVERY", raising=False)
    for var in list(os.environ):
        if var.startswith("XDG_") and var.endswith("_DIR"):
            monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    import jarvis.actions.file_controller as fc_mod
    importlib.reload(fc_mod)
    from jarvis.paths import memory_dir
    tmp = tmp_path.resolve()
    assert Path.home().resolve().is_relative_to(tmp)
    assert memory_dir().resolve().is_relative_to(tmp)

    monkeypatch.setattr(al, "TASKS_PATH", tmp_path / "agent_tasks.json")
    monkeypatch.setattr(al, "LOG_PATH", tmp_path / "agent_loop_log.jsonl")
    monkeypatch.setattr(al, "_approval_hook", None, raising=False)

    send = _Recorder("mesaj gönderildi")
    monkeypatch.setitem(tools_kopru.ALLOWED_TOOLS, "send_message", send)

    jl = JarvisLive.__new__(JarvisLive)
    jl.ui = _UI()
    jl.session = None
    jl._loop = None
    spoken = []
    jl.speak = lambda text: spoken.append(str(text))
    # Sinif seviyesindeki onay durumu testler arasinda tasinmasin.
    jl._set_pending_dangerous(None)
    jl._agent_pending_task_id = None

    yield SimpleNamespace(jl=jl, home=home, send=send, spoken=spoken, tmp=tmp_path)

    monkeypatch.undo()
    importlib.reload(fc_mod)


def _awaiting(task_id, tool="send_message", parameters=None, created="2026-10-05T10:00:00"):
    return {
        "id": task_id, "goal": f"hedef {task_id}", "status": "awaiting_approval",
        "created_at": created, "updated_at": created, "history": [],
        "pending_action": {
            "tool": tool,
            "parameters": parameters if parameters is not None else
            {"receiver": "Ali", "message_text": f"selam {task_id}", "platform": "whatsapp"},
            "note": "test",
        },
    }


def _status(task_id):
    return next(t for t in al._load_tasks() if t["id"] == task_id)["status"]


def _hook(env):
    al.set_approval_hook(env.jl.request_agent_loop_approval)


# ── Model approve cagiramiyor ──

def test_approve_is_not_in_gemini_tool_schema():
    decl = next(d for d in main_mod.TOOL_DECLARATIONS if d["name"] == "agent_loop")
    assert "approve" not in json.dumps(decl, ensure_ascii=False).casefold(), decl


@pytest.mark.parametrize("extra", [{}, {"confirm_code": "ABC123"}, {"confirmed": "yes"}])
def test_model_approve_with_task_id_does_not_execute(env, extra):
    al._save_tasks([_awaiting("aaaa1111")])
    _hook(env)
    out = al.agent_loop_tool({"action": "approve", "task_id": "aaaa1111", **extra})
    assert env.send.calls == [], out
    assert _status("aaaa1111") == "awaiting_approval"


# ── Onay yalnizca gercek kullanici turundan ──

def test_no_approval_without_user_turn(env):
    al._save_tasks([_awaiting("aaaa1111")])
    _hook(env)
    assert env.jl._pending_dangerous_action == "agent_loop"
    assert env.jl._agent_pending_task_id == "aaaa1111"
    pending = al._load_tasks()[0]["pending_action"]
    args = env.jl._agent_loop_fingerprint_args("aaaa1111", pending)
    # Kullanici "evet" demeden onay tuketilemez.
    assert env.jl._consume_dangerous_confirmation("agent_loop", args) is False
    al.agent_loop_tool({"action": "approve", "task_id": "aaaa1111"})
    assert env.send.calls == []
    assert _status("aaaa1111") == "awaiting_approval"


def test_user_yes_executes_exactly_once(env):
    al._save_tasks([_awaiting("aaaa1111")])
    _hook(env)
    assert env.jl._handle_agent_loop_reply("evet") is True
    assert env.send.calls == [{"receiver": "Ali", "message_text": "selam aaaa1111", "platform": "whatsapp"}]
    assert _status("aaaa1111") != "awaiting_approval"
    # Tek kullanimlik: ikinci "evet" hicbir seyi tekrar calistirmaz.
    env.jl._handle_agent_loop_reply("evet")
    assert len(env.send.calls) == 1


def test_user_no_denies_without_executing(env):
    al._save_tasks([_awaiting("aaaa1111")])
    _hook(env)
    assert env.jl._handle_agent_loop_reply("hayır") is True
    assert env.send.calls == []
    assert _status("aaaa1111") == "failed"
    assert env.jl._pending_dangerous_action is None


def test_unrelated_text_is_not_an_answer(env):
    al._save_tasks([_awaiting("aaaa1111")])
    _hook(env)
    assert env.jl._handle_agent_loop_reply("saat kaç") is False
    assert env.send.calls == []


def test_approval_request_shows_tool_args_and_target(env):
    al._save_tasks([
        _awaiting("aaaa1111"),
        _awaiting("bbbb2222", tool="file_controller",
                  parameters={"action": "delete", "path": "desktop", "name": "notlar.txt"},
                  created="2026-10-05T11:00:00"),
    ])
    _hook(env)
    text = "\n".join(env.spoken + env.jl.ui.logs)
    assert "send_message" in text and "Ali" in text
    # Icerik alanlari (message_text) onay metnine ham yazilmaz, yalnizca
    # uzunlugu gorunur (Adim 3.2b).
    assert "selam aaaa1111" not in text and "message_text=<14 karakter gizlendi>" in text
    assert "aaaa1111" in text
    low = text.casefold()
    assert "evet" in low and "hayır" in low

    env.jl._handle_agent_loop_reply("hayır")   # A cozuldu -> siradaki B sorulur
    text = "\n".join(env.spoken + env.jl.ui.logs)
    assert env.jl._agent_pending_task_id == "bbbb2222"
    assert "file_controller" in text and "delete" in text
    assert str((env.home / "Desktop" / "notlar.txt").resolve()) in text


# ── Farkli gorevin onayi baska gorevi calistirmiyor ──

def test_yes_runs_only_the_announced_task(env):
    al._save_tasks([_awaiting("aaaa1111"),
                    _awaiting("bbbb2222", created="2026-10-05T11:00:00")])
    _hook(env)
    assert env.jl._agent_pending_task_id == "aaaa1111"
    env.jl._handle_agent_loop_reply("evet")
    assert [c["message_text"] for c in env.send.calls] == ["selam aaaa1111"]
    assert _status("bbbb2222") == "awaiting_approval"
    # B ayrica soruluyor, A'nin onayi ona tasinmadi.
    assert env.jl._agent_pending_task_id == "bbbb2222"
    assert env.jl._dangerous_confirmation_granted is False


def test_changed_pending_step_is_not_executed(env):
    al._save_tasks([_awaiting("aaaa1111")])
    _hook(env)
    tasks = al._load_tasks()
    tasks[0]["pending_action"]["parameters"]["receiver"] = "Başkası"
    al._save_tasks(tasks)
    env.jl._handle_agent_loop_reply("evet")
    assert env.send.calls == []
    assert _status("aaaa1111") == "awaiting_approval"


def test_announcement_does_not_overwrite_other_pending_confirmation(env):
    env.jl._set_pending_dangerous("agentic_code", "agentic_code:{}")
    al._save_tasks([_awaiting("aaaa1111")])
    _hook(env)
    assert env.jl._pending_dangerous_action == "agentic_code"
    assert env.jl._handle_agent_loop_reply("evet") is False
    assert env.send.calls == []


def test_background_step_is_announced_and_waits(env, monkeypatch):
    _hook(env)
    monkeypatch.setattr(al, "_decide_next_step", lambda task: {
        "tool": "send_message", "note": "mesaj at",
        "parameters": {"receiver": "Ali", "message_text": "arka plan", "platform": "whatsapp"}})
    al.add_task("Ali'ye mesaj at")
    al._tick()
    task = al._load_tasks()[0]
    assert task["status"] == "awaiting_approval"
    assert env.send.calls == []
    assert env.jl._agent_pending_task_id == task["id"]
    env.jl._handle_agent_loop_reply("evet")
    assert env.send.calls == [{"receiver": "Ali", "message_text": "arka plan", "platform": "whatsapp"}]


# ── Entegre edilmis arac onaysiz calismiyor, salt-okunur arac calisiyor ──

def test_integrated_tool_is_destructive_and_cannot_run_unapproved(env, monkeypatch):
    fake = _Recorder()
    monkeypatch.setitem(tools_kopru.ALLOWED_TOOLS, "discovered_fake", fake)
    assert tools_kopru.is_destructive("discovered_fake", {}) is True
    with pytest.raises(PermissionError):
        tools_kopru.call_tool("discovered_fake", {})
    assert fake.calls == []


def test_integrated_tool_waits_for_approval_in_agent_loop(env, monkeypatch):
    fake = _Recorder()
    monkeypatch.setitem(tools_kopru.ALLOWED_TOOLS, "discovered_fake", fake)
    monkeypatch.setattr(al, "_decide_next_step", lambda task: {
        "tool": "discovered_fake", "parameters": {"x": "1"}, "note": "dene"})
    al.add_task("yeni aracı dene")
    al._tick()
    assert al._load_tasks()[0]["status"] == "awaiting_approval"
    assert fake.calls == []


def test_readonly_tool_still_runs_without_approval(env, monkeypatch):
    search = _Recorder("sonuç")
    monkeypatch.setitem(tools_kopru.ALLOWED_TOOLS, "web_search", search)
    monkeypatch.setattr(al, "_decide_next_step", lambda task: {
        "tool": "web_search", "parameters": {"query": "hava"}, "note": "ara"})
    al.add_task("hava durumunu araştır")
    al._tick()
    assert search.calls == [{"query": "hava"}]
    assert al._load_tasks()[0]["status"] != "awaiting_approval"


def test_readonly_file_action_still_runs(env):
    assert tools_kopru.is_destructive("file_controller", {"action": "list"}) is False
    out = tools_kopru.call_tool("file_controller", {"action": "list", "path": "desktop"})
    assert isinstance(out, str)


@pytest.mark.parametrize("tool,params", [
    ("discovered_jc", {"command": "ls", "data": ""}),
    ("discovered_topydo", {"action": "add", "task": "x"}),
    ("system_scan_and_repair", {}),
    ("reminder", {"date": "2026-10-06", "time": "10:00", "message": "x"}),
    ("computer_settings", {"action": "type_text", "value": "rm -rf ~"}),
    ("computer_settings", {"description": "bilgisayarı kapat"}),
    ("tamamen_bilinmeyen_arac", {}),
])
def test_non_readonly_tools_are_destructive(tool, params):
    assert tools_kopru.is_destructive(tool, params) is True


@pytest.mark.parametrize("tool,params", [
    ("web_search", {"query": "x"}),
    ("weather_report", {"city": "Ankara"}),
    ("system_status", {}),
    ("github_arama", {"query": "x"}),
    ("windows_system", {"command_name": "process_list"}),
    ("computer_settings", {"action": "volume_up"}),
])
def test_explicitly_safe_tools_are_not_destructive(tool, params):
    assert tools_kopru.is_destructive(tool, params) is False
