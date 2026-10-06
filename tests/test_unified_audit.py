"""tests/test_unified_audit.py

docs/GUVENLIK_KAPISI_PLAN.md Adim 3.1 - tek denetim kaydi.

Eskiden uc ayri kayit vardi:
  * security_gate.audit -> tool_gate.audit_entry -> ~/.jarvis/audit.log
    (JARVIS_HOME'u yok sayiyordu; params MASKESIZ; "approved" aslinda
    "yurutuldu" demekti; "destructive" eski tool_gate tablosundan geliyordu),
  * core/audit_log.log_action -> JARVIS_HOME/memory/audit.log,
  * agent_loop / Brain onay-ret-yurutme olaylari hic audit'e yazilmiyordu.

Artik hepsi JARVIS_HOME/memory/audit.log'a core/audit_log.log_action
bicimiyle yazilir; kayitta source/verdict/fingerprint vardir, gizli ve icerik
alanlari maskelenir, "approved" yalnizca gercek kullanici onayinda True'dur,
yurutme ayrica "executed" alaniyla isaretlenir.

IZOLASYON: HOME/JARVIS_HOME tmp_path (conftest). Gercek ag/LLM yok; arac
fonksiyonlari cagri kaydeden sahte fonksiyonlardir, karar kodu stub'lanmaz.
"""
import asyncio
import json
import logging
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import jarvis.main as main_mod
from jarvis.main import JarvisLive
from jarvis import tool_gate
from jarvis.actions import agent_loop as al
from jarvis.actions import tools_kopru

_BASE_FIELDS = {"timestamp", "module", "action", "detail", "risk", "approval_required", "result"}


def _audit_path() -> Path:
    from jarvis.paths import memory_dir
    return memory_dir() / "audit.log"


def _rows() -> list[dict]:
    p = _audit_path()
    if not p.is_file():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


def _raw() -> str:
    p = _audit_path()
    return p.read_text(encoding="utf-8") if p.is_file() else ""


class _UI:
    muted = False

    def __init__(self):
        self.logs = []

    def write_log(self, text):
        self.logs.append(str(text))

    def __getattr__(self, _name):
        return lambda *a, **k: None


@pytest.fixture
def jl():
    obj = JarvisLive.__new__(JarvisLive)
    obj.ui = _UI()
    obj.session = None
    obj._loop = None
    obj._bg_tasks = None
    obj.spoken = []
    obj.speak = lambda text: obj.spoken.append(str(text))
    obj.speak_error = lambda *a, **k: None
    obj._set_pending_dangerous(None)
    obj._tool_confirm_code = None
    obj._brain_pending_task_id = None
    obj._agent_pending_task_id = None
    yield obj
    obj._set_pending_dangerous(None)


def _e1(jl, name, args):
    return asyncio.run(jl._execute_tool(SimpleNamespace(name=name, args=args, id="1")))


# ── Konum ve bicim ──

def test_gate_audit_goes_to_jarvis_home_not_dot_jarvis(jl, monkeypatch):
    monkeypatch.setattr(main_mod, "weather_action", lambda **k: "güneşli")
    _e1(jl, "weather_report", {"city": "Ankara"})
    assert _audit_path() == Path(os.environ["JARVIS_HOME"]) / "memory" / "audit.log"
    assert _audit_path().is_file()
    assert not (Path.home() / ".jarvis" / "audit.log").exists()
    row = next(r for r in _rows() if r.get("tool") == "weather_report")
    assert _BASE_FIELDS <= set(row)
    assert row["source"] == "model_live"
    assert row["verdict"] == "allow"
    assert row["executed"] is True
    assert row["approved"] is False          # kullanici onayi yok
    assert row["fingerprint"] and "Ankara" not in row["fingerprint"]
    assert "destructive" not in row


def test_approved_true_only_after_real_user_approval(jl, monkeypatch):
    sent = []
    monkeypatch.setattr(main_mod, "send_message", lambda **k: sent.append(k) or "gitti")
    args = {"receiver": "Ali", "message_text": "çok gizli mesaj", "platform": "whatsapp"}
    _e1(jl, "send_message", args)
    jl._on_text_command("evet")
    _e1(jl, "send_message", args)
    assert len(sent) == 1
    rows = [r for r in _rows() if r.get("tool") == "send_message"]
    pending = [r for r in rows if not r["executed"]]
    done = [r for r in rows if r["executed"]]
    assert pending and pending[0]["verdict"] == "needs_approval" and pending[0]["approved"] is False
    assert pending[0]["approval_required"] is True
    assert done and done[0]["approved"] is True
    assert "çok gizli mesaj" not in _raw()
    assert done[0]["params"]["receiver"] == "Ali"


def test_save_memory_value_is_masked(jl, monkeypatch):
    monkeypatch.setattr(main_mod, "update_memory", lambda *a, **k: None)
    _e1(jl, "save_memory", {"category": "notes", "key": "doğum günü", "value": "annemin adresi X sokak"})
    assert "annemin adresi" not in _raw()
    assert any(r.get("tool") == "save_memory" and r["executed"] for r in _rows())


# ── Maskeleme ──

def test_secret_and_content_params_are_masked_and_codes_redacted():
    sg_params = {"password": "p4ssw0rd!", "api_key": "AIzaSECRET", "token": "tok-123",
                 "content": "dosyanın tüm içeriği", "message_text": "merhaba gizli",
                 "value": "bellek değeri", "new_text": "yeni metin", "path": "desktop"}
    tool_gate.audit_entry("file_controller", sg_params,
                          "ONAY GEREKLİ ... confirm_code='d7d0beef' ve Onay kodu: Zq9-xY", approved=False)
    raw = _raw()
    for secret in ("p4ssw0rd!", "AIzaSECRET", "tok-123", "dosyanın tüm içeriği",
                   "merhaba gizli", "bellek değeri", "yeni metin", "d7d0beef", "Zq9-xY"):
        assert secret not in raw, secret
    row = _rows()[-1]
    assert row["params"]["path"] == "desktop"
    assert row["approved"] is False and "destructive" not in row


def test_tool_gate_audit_entry_has_no_destructive_table_dependency(monkeypatch):
    monkeypatch.setattr(tool_gate, "is_destructive",
                        lambda *_: (_ for _ in ()).throw(AssertionError("kullanilmamali")))
    tool_gate.audit_entry("send_message", {"receiver": "a"}, "ok", approved=True)
    assert _rows()[-1]["approved"] is True


# ── agent_loop olaylari ──

@pytest.fixture
def loop_env(tmp_path, monkeypatch, jl):
    monkeypatch.setattr(al, "TASKS_PATH", tmp_path / "agent_tasks.json")
    monkeypatch.setattr(al, "LOG_PATH", tmp_path / "agent_loop_log.jsonl")
    monkeypatch.setattr(al, "_approval_hook", None, raising=False)
    calls = []
    monkeypatch.setitem(tools_kopru.ALLOWED_TOOLS, "send_message",
                        lambda p: calls.append(dict(p)) or "mesaj gönderildi")
    monkeypatch.setitem(tools_kopru.ALLOWED_TOOLS, "web_search", lambda p: "sonuç: 3 kayıt")
    al.set_approval_hook(jl.request_agent_loop_approval)
    yield SimpleNamespace(jl=jl, calls=calls)
    al._approval_hook = None


def _run_step(monkeypatch, step):
    monkeypatch.setattr(al, "_decide_next_step", lambda task: step)
    al.add_task("test hedefi")
    al._tick()
    return al._load_tasks()[-1]["id"]


def _loop_rows(event=None):
    return [r for r in _rows() if r.get("source") == "agent_loop"
            and (event is None or r["action"] == event)]


def test_agent_loop_request_approve_and_execute_are_audited(loop_env, monkeypatch):
    tid = _run_step(monkeypatch, {"tool": "send_message", "done": False, "note": "n",
                                  "parameters": {"receiver": "Ali", "message_text": "gizli içerik"}})
    req = _loop_rows("approval_requested")
    assert req and req[-1]["tool"] == "send_message" and req[-1]["verdict"] == "needs_approval"
    assert req[-1]["task_id"] == tid and req[-1]["executed"] is False
    loop_env.jl._handle_agent_loop_reply("evet")
    assert loop_env.calls
    done = _loop_rows("approved_executed")
    assert done and done[-1]["approved"] is True and done[-1]["executed"] is True
    assert "gizli içerik" not in _raw()


def test_agent_loop_deny_is_audited(loop_env, monkeypatch):
    tid = _run_step(monkeypatch, {"tool": "send_message", "done": False, "note": "n",
                                  "parameters": {"receiver": "Ali", "message_text": "x"}})
    loop_env.jl._handle_agent_loop_reply("hayır")
    rows = _loop_rows("denied")
    assert rows and rows[-1]["task_id"] == tid and rows[-1]["approved"] is False


def test_agent_loop_unapproved_readonly_step_is_audited_as_executed(loop_env, monkeypatch):
    _run_step(monkeypatch, {"tool": "web_search", "done": False, "note": "n",
                            "parameters": {"query": "hava"}})
    rows = _loop_rows("executed")
    assert rows and rows[-1]["tool"] == "web_search"
    assert rows[-1]["executed"] is True and rows[-1]["approved"] is False
    assert rows[-1]["verdict"] == "allow"


# ── Brain Team olaylari ──

@pytest.fixture
def orch(tmp_path, monkeypatch):
    saved = {}
    for name in ("jarvis.task_manager", "jarvis.watchdog", "jarvis.task_results"):
        lg = logging.getLogger(name)
        saved[name] = (lg.handlers[:], lg.level)
        lg.handlers = [logging.FileHandler(tmp_path / f"{name}.log", encoding="utf-8")]
    import jarvis.core.watchdog as wd
    monkeypatch.setattr(wd, "LOGS_DIR", tmp_path)
    import jarvis.core.brain_orchestrator as bo
    from jarvis.core.task_manager import TaskManager
    o = bo.BrainOrchestrator()
    o.tasks = TaskManager(path=tmp_path / "brain_tasks.json")
    o._last_player = None
    executed = []
    o._execute_step = lambda task, step: executed.append(step) or {"status": "completed", "result": "yazıldı"}
    o._finish_step = lambda task, step, result: None
    o.executed = executed
    yield o
    for name, (handlers, level) in saved.items():
        lg = logging.getLogger(name)
        for h in lg.handlers:
            h.close()
        lg.handlers = handlers
        lg.setLevel(level)


def _brain_task(o, risk):
    o._risk_of_step = lambda step, task=None: (risk, f"test: {risk}")
    t = o.tasks.create(name="[FILE_MODIFICATION] a.txt", agent="executor_ai", payload={
        "goal": "Masaüstüne a.txt oluştur",
        "plan": [{"order": 1, "description": "Masaüstüne a.txt oluştur", "agent": "executor_ai",
                  "operation": "execute"}],
        "step_index": 0, "history": [], "audit_retries": 0,
        "file_modification": {"action": "create_file", "path": "desktop", "name": "a.txt",
                              "content": "gizli dosya içeriği"},
    })
    o._tick()
    return t["id"]


def _brain_rows(event):
    return [r for r in _rows() if r.get("source") == "brain_team" and r["action"] == event]


def test_brain_request_and_approve_are_audited(orch):
    tid = _brain_task(orch, "high")
    req = _brain_rows("approval_requested")
    assert req and req[-1]["task_id"] == tid and req[-1]["verdict"] == "needs_approval"
    assert req[-1]["executed"] is False and req[-1]["approved"] is False
    orch.approve(tid)
    assert orch.executed
    done = _brain_rows("approved_executed")
    assert done and done[-1]["task_id"] == tid and done[-1]["approved"] is True
    assert "gizli dosya içeriği" not in _raw()


def test_brain_deny_is_audited(orch):
    tid = _brain_task(orch, "high")
    orch.deny(tid)
    rows = _brain_rows("denied")
    assert rows and rows[-1]["task_id"] == tid


def test_brain_unapproved_low_risk_step_is_audited_as_executed(orch):
    tid = _brain_task(orch, "low")
    assert orch.executed
    rows = _brain_rows("executed")
    assert rows and rows[-1]["task_id"] == tid
    assert rows[-1]["executed"] is True and rows[-1]["approved"] is False


def test_brain_audit_masks_resolved_secret_params(orch):
    orch._resolve_step_call = lambda task, step: ("vault_encrypt",
                                                  {"source": "/tmp/a.txt", "password": "S3cret!pw"})
    tid = _brain_task(orch, "high")
    orch.approve(tid)
    assert "S3cret!pw" not in _raw()
    row = _brain_rows("approval_requested")[-1]
    assert row["tool"] == "vault_encrypt" and row["params"]["password"] == "***"
