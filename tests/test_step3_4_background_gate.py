"""tests/test_step3_4_background_gate.py

docs/GUVENLIK_KAPISI_PLAN.md Adim 3.4 - agent_loop ve Brain Team onaylari
gercek kapidan (security_gate.authorize) ve cok-istekli depodan
(JarvisLive._approvals, MultiApprovalStore) gecer.

Eskiden:
  * Depoya araca degil bir SOZDE cagriya baglanmis kayit yaziliyordu
    (for_pending("agent_loop"/"brain_team", {task_id, ...})); kayit hangi
    aracin hangi argumanlarla calisacagini tasimiyordu.
  * agent_loop.approve_task onay kaniti istemiyordu (cagiran herkes
    calistirabiliyordu); karar tools_kopru.is_destructive'ten geliyordu.
  * Brain approve() adimi yeniden cozup calistiriyordu; onaylanan cagri ile
    calisan cagri karsilastirilmiyordu. Risk dusukse (classify_risk
    bilinmeyene "low") kapi hic sorulmadan calisiyordu.

Artik:
  * Istek depoya kind="agent_loop" / "brain_team" olarak GERCEK
    ResolvedCall (arac + normalize argumanlar, kaynak AGENT_LOOP /
    BRAIN_TEAM) ve parmak iziyle yazilir (TTL 600 sn).
  * Calistirmadan hemen once authorize(...) cagrilir: DENY -> calismaz;
    NEEDS_APPROVAL -> yalnizca depodaki KENDI request_id'li onayla ve
    onaylanan parmak izi calisacak cagrinin parmak iziyle birebir esitse.

STUB YOK: karar kodu (authorize, depo, is_destructive, _risk_of_step,
classify_risk) gercek. Gercek orkestrator, gercek executor_ai ve gercek
file_controller tmp HOME'da calisir. Yalnizca LLM cagrilari (security_ai
aciklamasi, auditor yargisi, agent_loop planner'i) sabit cevap doner ve
mesaj gonderme araci cagri kaydeden bir fonksiyondur.
IZOLASYON: HOME/JARVIS_HOME tmp_path; gorev/log dosyalari tmp.
"""
import importlib
import json
import logging
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from jarvis.main import JarvisLive
from jarvis import security_gate as sg
from jarvis.actions import agent_loop as al
from jarvis.actions import tools_kopru
import jarvis.core.brain_orchestrator as bo
import jarvis.core.message_bus as mb
from jarvis.brains.base_brain import BrainError

S, V = sg.Source, sg.Verdict


class _UI:
    muted = False

    def __init__(self):
        self.logs = []

    def write_log(self, text):
        self.logs.append(str(text))

    def __getattr__(self, _name):
        return lambda *a, **k: None


def _jl():
    obj = JarvisLive.__new__(JarvisLive)
    obj.ui = _UI()
    obj.session = None
    obj._loop = None
    obj._bg_tasks = None
    obj.spoken = []
    obj.speak = lambda text: obj.spoken.append(str(text))
    obj.speak_error = lambda *a, **k: None
    return obj


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    for d in ("Desktop", "Documents"):
        (h / d).mkdir(parents=True)
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("USERPROFILE", str(h))
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "jh"))
    monkeypatch.delenv("JARVIS_AUTO_DISCOVERY", raising=False)
    for var in list(os.environ):
        if var.startswith("XDG_") and var.endswith("_DIR"):
            monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    import jarvis.actions.file_controller as fc_mod
    importlib.reload(fc_mod)
    assert Path.home().resolve().is_relative_to(tmp_path.resolve())
    yield h
    monkeypatch.undo()
    importlib.reload(fc_mod)


# ══ agent_loop ══════════════════════════════════════════════════════════

_MSG = {"receiver": "Ali", "message_text": "selam", "platform": "whatsapp"}


@pytest.fixture
def loop(home, tmp_path, monkeypatch):
    monkeypatch.setattr(al, "TASKS_PATH", tmp_path / "agent_tasks.json")
    monkeypatch.setattr(al, "LOG_PATH", tmp_path / "agent_loop_log.jsonl")
    sent = []
    monkeypatch.setitem(tools_kopru.ALLOWED_TOOLS, "send_message",
                        lambda p: sent.append(dict(p)) or "mesaj gönderildi")
    jl = _jl()
    al.set_approval_hook(jl.request_agent_loop_approval)
    yield SimpleNamespace(jl=jl, sent=sent)
    al._approval_hook = None


def _plan_step(monkeypatch, tool, params):
    monkeypatch.setattr(al, "_decide_next_step",
                        lambda task: {"tool": tool, "parameters": dict(params), "done": False, "note": "n"})
    al.add_task("hedef")
    al._tick()
    return al._load_tasks()[-1]


def test_agent_loop_request_is_stored_as_real_resolved_call(loop, monkeypatch):
    task = _plan_step(monkeypatch, "send_message", _MSG)
    assert task["status"] == "awaiting_approval"
    recs = loop.jl._approvals.records(sg.KIND_AGENT_LOOP)
    assert len(recs) == 1
    rec = recs[0]
    assert rec.call.tool == "send_message"
    assert rec.call.source is S.AGENT_LOOP
    assert rec.call.params == _MSG
    assert rec.fingerprint == sg.fingerprint("send_message", _MSG)
    assert rec.task_id == task["id"] and rec.background and rec.ttl == 600
    # Model request_id'yi gormez: kullaniciya/modele giden metinde yok.
    assert rec.request_id not in "\n".join(loop.jl.spoken + loop.jl.ui.logs)


def test_agent_loop_approve_task_without_store_grant_does_not_run(loop, monkeypatch):
    task = _plan_step(monkeypatch, "send_message", _MSG)
    out = al.approve_task(task["id"], expected_action=task["pending_action"])
    assert loop.sent == [], out
    assert al._load_tasks()[-1]["status"] == "awaiting_approval"


def test_agent_loop_approve_task_with_foreign_fingerprint_does_not_run(loop, monkeypatch):
    task = _plan_step(monkeypatch, "send_message", _MSG)
    other = sg.Grant(sg.fingerprint("send_message", {**_MSG, "receiver": "Veli"}))
    al.approve_task(task["id"], expected_action=task["pending_action"], grant=other)
    assert loop.sent == []


def test_agent_loop_user_yes_runs_once_through_own_request(loop, monkeypatch):
    task = _plan_step(monkeypatch, "send_message", _MSG)
    loop.jl._on_text_command("evet")
    assert loop.sent == [_MSG]
    assert loop.jl._approvals.records(sg.KIND_AGENT_LOOP) == []
    assert next(t for t in al._load_tasks() if t["id"] == task["id"])["status"] == "pending"


def test_agent_loop_gate_deny_never_asks_and_never_runs(loop, monkeypatch):
    ran = []
    monkeypatch.setitem(tools_kopru.ALLOWED_TOOLS, "zz_kayitsiz_arac", lambda p: ran.append(p) or "x")
    task = _plan_step(monkeypatch, "zz_kayitsiz_arac", {"a": 1})
    assert task["status"] == "failed"
    assert loop.jl._approvals.records() == []
    assert ran == []


def test_agent_loop_approve_task_respects_gate_deny_even_with_grant(loop, monkeypatch):
    ran = []
    monkeypatch.setitem(tools_kopru.ALLOWED_TOOLS, "zz_kayitsiz_arac", lambda p: ran.append(p) or "x")
    pending = {"tool": "zz_kayitsiz_arac", "parameters": {"a": 1}, "note": "n"}
    al._save_tasks([{"id": "z1", "goal": "g", "status": "awaiting_approval",
                     "created_at": "2026-10-06T10:00:00", "updated_at": "2026-10-06T10:00:00",
                     "history": [], "pending_action": pending}])
    al.approve_task("z1", expected_action=pending,
                    grant=sg.Grant(sg.fingerprint("zz_kayitsiz_arac", {"a": 1})))
    assert ran == []


_PARITY = [
    ("web_search", {"query": "x"}), ("weather_report", {"city": "x"}), ("system_status", {}),
    ("github_arama", {"query": "x"}), ("windows_system", {"command_name": "cpu_load"}),
    ("github_arac_bul_ve_degerlendir", {"query": "x"}),
    ("computer_settings", {"action": "volume_up"}), ("computer_settings", {"action": "brightness"}),
    ("computer_settings", {"action": "shutdown"}), ("computer_settings", {"action": ""}),
    ("file_controller", {"action": "list"}), ("file_controller", {"action": "read"}),
    ("file_controller", {"action": "delete"}), ("file_controller", {"action": "trash"}),
    ("reminder", {"message": "x"}), ("send_message", _MSG), ("system_scan_and_repair", {}),
    ("entegrasyon_uygula", {"source_name": "a/b"}), ("discovery_register", {"name": "x"}),
    ("discovered_jc", {}), ("discovered_topydo", {"action": "list"}),
]


@pytest.mark.parametrize("tool,params", _PARITY, ids=[f"{t}-{p.get('action', '')}" for t, p in _PARITY])
def test_agent_loop_gate_policy_matches_bridge_policy(tool, params):
    """Kapinin AGENT_LOOP karari bugunku onaysiz-calisma kumesini korur:
    yalnizca is_destructive=False olanlar ALLOW."""
    allowed = sg.authorize(tool, params, S.AGENT_LOOP).verdict is V.ALLOW
    assert allowed == (not tools_kopru.is_destructive(tool, params))


# ══ Brain Team ══════════════════════════════════════════════════════════

@pytest.fixture
def brain(home, tmp_path, monkeypatch):
    saved = {}
    for name in ("jarvis.task_manager", "jarvis.watchdog", "jarvis.task_results"):
        lg = logging.getLogger(name)
        saved[name] = (lg.handlers[:], lg.level)
        lg.handlers = [logging.FileHandler(tmp_path / f"{name}.log", encoding="utf-8")]
        lg.setLevel(logging.INFO)
    import jarvis.core.watchdog as wd
    monkeypatch.setattr(wd, "LOGS_DIR", tmp_path)
    monkeypatch.setattr(mb, "LOGS_DIR", tmp_path)
    monkeypatch.setattr(mb, "BUS_LOG_PATH", tmp_path / "message_bus.jsonl")
    from jarvis.brains.security_ai import SecurityAI
    from jarvis.brains.auditor_ai import AuditorAI

    def _no_llm(self, prompt):
        raise BrainError("test: LLM yok")

    monkeypatch.setattr(SecurityAI, "call_llm_json", _no_llm)
    monkeypatch.setattr(AuditorAI, "call_llm_json", lambda self, p: {"passed": True, "reason": "t"})
    from jarvis.core.task_manager import TaskManager
    o = bo.BrainOrchestrator()
    o.tasks = TaskManager(path=tmp_path / "brain_tasks.json")
    sent_to = []
    real_send = o.bus.send

    def _send(frm, to, desc, payload=None, **kw):
        sent_to.append((to, json.loads(json.dumps(payload or {}, default=str))))
        return real_send(frm, to, desc, payload=payload, **kw)

    o.bus.send = _send
    jl = _jl()
    o._last_player = jl
    monkeypatch.setattr(bo, "get_orchestrator", lambda: o)
    yield SimpleNamespace(orch=o, jl=jl, home=home, sent_to=sent_to)
    for name, (handlers, level) in saved.items():
        lg = logging.getLogger(name)
        for h in lg.handlers:
            h.close()
        lg.handlers = handlers
        lg.setLevel(level)


def _brain_task(o, file_mod, desc="Masaüstüne a.txt oluştur"):
    return o.tasks.create(name=desc, agent="executor_ai", payload={
        "goal": desc, "step_index": 0, "history": [], "audit_retries": 0,
        "plan": [{"order": 1, "description": desc, "agent": "executor_ai", "operation": "execute"}],
        "file_modification": file_mod})["id"]


_CREATE = {"action": "create_file", "path": "desktop", "name": "a.txt", "content": "merhaba"}


def _executor_calls(b):
    return [p for to, p in b.sent_to if to == "executor_ai"]


def test_brain_request_is_stored_as_real_resolved_call(brain):
    o, jl = brain.orch, brain.jl
    tid = _brain_task(o, _CREATE)
    o._tick()
    assert o.tasks.get(tid)["status"] == "waiting_approval"
    recs = jl._approvals.records(sg.KIND_BRAIN_TEAM)
    assert len(recs) == 1
    rec = recs[0]
    expected = sg.resolve("file_controller", {**_CREATE}, S.BRAIN_TEAM)
    assert rec.call.tool == "file_controller" and rec.call.source is S.BRAIN_TEAM
    assert rec.call.params["action"] == "create_file" and rec.call.params["name"] == "a.txt"
    assert rec.fingerprint == expected.fingerprint
    assert rec.task_id == tid and rec.background and rec.ttl == 600
    assert rec.request_id not in "\n".join(jl.spoken + jl.ui.logs)


def test_brain_user_yes_runs_the_approved_call(brain):
    o, jl = brain.orch, brain.jl
    tid = _brain_task(o, _CREATE)
    o._tick()
    jl._on_text_command("evet")
    assert (brain.home / "Desktop" / "a.txt").read_text(encoding="utf-8") == "merhaba"
    assert len(_executor_calls(brain)) == 1
    assert o.tasks.get(tid)["status"] in ("pending", "completed")


def test_brain_approve_with_mismatched_fingerprint_does_not_run(brain):
    o = brain.orch
    tid = _brain_task(o, _CREATE)
    o._tick()
    other = sg.resolve("file_controller", {**_CREATE, "name": "baska.txt"}, S.BRAIN_TEAM)
    o.approve(tid, approved_call=other)
    assert _executor_calls(brain) == []
    assert not (brain.home / "Desktop" / "a.txt").exists()


def test_brain_call_changed_after_announcement_is_not_run(brain):
    """Duyurudan sonra gorevin cozulen cagrisi degisirse (plan adimi ayni
    kalsa bile) kullanicinin 'evet'i yeni cagriyi calistirmaz."""
    o, jl = brain.orch, brain.jl
    tid = _brain_task(o, _CREATE)
    o._tick()
    task = o.tasks.get(tid)
    task["payload"]["file_modification"] = {**_CREATE, "name": "baska.txt"}
    o.tasks.update(tid, payload=task["payload"])
    jl._on_text_command("evet")
    assert _executor_calls(brain) == []
    assert not (brain.home / "Desktop" / "baska.txt").exists()
    assert not (brain.home / "Desktop" / "a.txt").exists()


def test_brain_gate_deny_blocks_execution_even_when_risk_is_low(brain):
    o = brain.orch
    o._resolve_action_with_file_modification = lambda task, step, base_path=".": ("zz_kayitsiz_arac", {})
    tid = _brain_task(o, None, desc="Bir şey yap")
    o._tick()
    assert _executor_calls(brain) == []
    assert o.tasks.get(tid)["status"] != "waiting_approval"


def test_brain_gate_needs_approval_waits_even_when_classify_risk_is_not_high(brain, monkeypatch):
    """backup_create classify_risk'te 'medium' (onaysiz calisiyordu); kapi
    MUTATE der -> kullanici onayi beklenir."""
    # Gercek yedek araci depoya (src/jarvis_yedekler) yazar; regresyonda bile
    # depoya dokunulmasin diye arac fonksiyonu cagri kaydedicisidir.
    from jarvis.brains import executor_ai
    backups = []
    monkeypatch.setitem(executor_ai._ALLOWED_ACTIONS, "backup_create",
                        lambda params: backups.append(params) or "yedek alındı")
    o = brain.orch
    o._resolve_action_with_file_modification = lambda task, step, base_path=".": ("backup_create", {})
    tid = _brain_task(o, None, desc="Projenin yedeğini al")
    o._tick()
    assert _executor_calls(brain) == [] and backups == []
    assert o.tasks.get(tid)["status"] == "waiting_approval"


def test_brain_readonly_step_runs_without_approval(brain):
    o = brain.orch
    tid = _brain_task(o, {"action": "list", "path": "desktop"}, desc="Masaüstüne bak")
    o._tick()
    assert len(_executor_calls(brain)) == 1
    assert o.tasks.get(tid)["status"] != "waiting_approval"
