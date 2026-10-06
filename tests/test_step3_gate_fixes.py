"""tests/test_step3_gate_fixes.py

docs/GUVENLIK_KAPISI_PLAN.md Adim 3.0:

1) brain_orchestrator.brain_team_tool'da action="approve" dali vardi: cagiran
   herkes bekleyen Brain adimini kullanici onayi OLMADAN calistirabiliyordu
   (bugun modelden erisilemiyordu ama atil bir bypass'ti). Dal kalkti.

2) "brain_team" EFFECTS'te kayitli bir sozde-arac oldugu icin, model sesli
   arac yolundan (E1) brain_team cagirinca kapi NEEDS_APPROVAL donuyor ve tek
   onay yuvasini modelin parmak iziyle EZIYORDU. Bekleyen Brain onayi bir daha
   onaylanamiyordu. Artik MODEL_LIVE kaynagindan yalnizca modelin gercekten
   erisebildigi araclar (TOOL_DECLARATIONS / _execute_tool dallari / registry)
   kabul edilir; digerleri DENY ve yuvaya dokunulmaz.

3) agent_loop_tool(retry) modele aciktI: kullanicinin iptal ettigi bir gorevi
   model yeniden kuyruga alabiliyordu. Artik retry, ayni task_id ile
   kullanicinin gercek turundaki onaya baglidir.

Karar kodu stub'lanmaz. HOME/JARVIS_HOME tmp_path (conftest + fixture).
"""
import asyncio
from types import SimpleNamespace

import pytest

from jarvis.main import JarvisLive
from jarvis import security_gate as sg
from jarvis.actions import agent_loop as al
import jarvis.core.brain_orchestrator as bo


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
    obj._set_pending_dangerous(None)
    obj._tool_confirm_code = None
    obj._brain_pending_task_id = None
    obj._agent_pending_task_id = None
    return obj


def _call(jl, name, args):
    resp = asyncio.run(jl._execute_tool(SimpleNamespace(name=name, args=args, id="1")))
    return str(resp.response.get("result", resp.response.get("output", "")))


class _FakeTasks:
    def __init__(self, task):
        self.task = task

    def get(self, task_id):
        return self.task if task_id == self.task["id"] else None


class _FakeOrch:
    def __init__(self):
        self.approved, self.denied = [], []
        self.step = {"agent": "executor_ai", "description": "Masaüstüne a.txt oluştur",
                     "risk": "high"}
        self.tasks = _FakeTasks({"id": "t1", "status": "waiting_approval",
                                 "payload": {"pending_step": self.step}})

    def approve(self, task_id, **_kw):
        # main, depodaki kendi istegini tuketen onayin cagrisini verir (Adim 3.4).
        self.approved.append(task_id)
        return f"Onaylandı: {task_id}"

    def deny(self, task_id):
        self.denied.append(task_id)
        return f"İptal: {task_id}"


@pytest.fixture
def brain(monkeypatch):
    orch = _FakeOrch()
    monkeypatch.setattr(bo, "get_orchestrator", lambda: orch)
    jl = _jl()
    jl.request_brain_team_approval("t1", orch.step, "Onay gerekiyor: a.txt")
    yield SimpleNamespace(jl=jl, orch=orch)
    jl._set_pending_dangerous(None)


# ── 1) brain_team_tool approve dali yok ──

def test_brain_team_tool_cannot_approve(brain):
    out = bo.brain_team_tool({"action": "approve", "task_id": "t1"})
    assert brain.orch.approved == []
    assert "Bilinmeyen action" in out
    assert "start/status/deny/health/agents" in out


# ── 2) Model brain_team cagirinca bekleyen Brain onayi ezilmez ──

def test_model_cannot_reach_internal_pseudo_tools():
    S, V = sg.Source, sg.Verdict
    for name in ("brain_team", "coder_ai", "backup_rollback", "vault_decrypt",
                 "entegrasyon_uygula", "discovery_register", "file_delete"):
        assert sg.authorize(name, {}, S.MODEL_LIVE).verdict is V.DENY, name
    # Modelin gercek araclari etkilenmez.
    assert sg.authorize("weather_report", {"city": "x"}, S.MODEL_LIVE).verdict is V.ALLOW
    assert sg.authorize("send_message", {"receiver": "a"}, S.MODEL_LIVE).verdict is V.NEEDS_APPROVAL
    assert sg.authorize("agentic_code", {"description": "x"}, S.MODEL_LIVE).verdict is V.NEEDS_APPROVAL
    # Ayni araclar kendi yollarinda hala bilinir (DENY degil).
    assert sg.authorize("coder_ai", {"operation": "analyze"}, S.BRAIN_TEAM).verdict is not V.DENY
    assert sg.authorize("entegrasyon_uygula", {}, S.AGENT_LOOP).verdict is not V.DENY


def test_model_brain_team_call_keeps_pending_brain_slot(brain):
    jl = brain.jl
    before = (jl._pending_dangerous_action, jl._pending_dangerous_fingerprint)
    for args in ({"action": "approve", "task_id": "t1"},
                 {"action": "approve", "task_id": "t1", "confirm_code": "evet"},
                 {"action": "start", "goal": "x"}):
        out = _call(jl, "brain_team", args)
        assert out.startswith("BLOCKED"), out
        assert (jl._pending_dangerous_action, jl._pending_dangerous_fingerprint) == before
    assert brain.orch.approved == []
    # Kullanicinin gercek "evet"i hala duyurulan Brain adimini onaylar.
    jl._on_text_command("evet")
    assert brain.orch.approved == ["t1"]


# ── 3) agent_loop retry kullanici onayina bagli ──

@pytest.fixture
def loop_env(tmp_path, monkeypatch):
    monkeypatch.setattr(al, "TASKS_PATH", tmp_path / "agent_tasks.json")
    monkeypatch.setattr(al, "LOG_PATH", tmp_path / "agent_loop_log.jsonl")
    monkeypatch.setattr(al, "_approval_hook", None, raising=False)
    al._save_tasks([
        {"id": "c1", "goal": "iptal edilen", "status": "cancelled", "created_at": "2026-10-06T10:00:00",
         "updated_at": "2026-10-06T10:00:00", "history": [], "pending_action": None},
        {"id": "c2", "goal": "baska", "status": "failed", "created_at": "2026-10-06T10:00:00",
         "updated_at": "2026-10-06T10:00:00", "history": [], "pending_action": None},
    ])
    jl = _jl()
    yield jl
    jl._set_pending_dangerous(None)


def _st(tid):
    return next(t for t in al._load_tasks() if t["id"] == tid)["status"]


def test_model_retry_without_user_turn_does_not_requeue(loop_env):
    out = _call(loop_env, "agent_loop", {"action": "retry", "task_id": "c1"})
    assert out.startswith("CONFIRMATION_REQUIRED"), out
    assert _st("c1") == "cancelled"
    _call(loop_env, "agent_loop", {"action": "retry", "task_id": "c1"})
    assert _st("c1") == "cancelled"


def test_model_retry_runs_after_real_user_yes_bound_to_task(loop_env):
    jl = loop_env
    _call(jl, "agent_loop", {"action": "retry", "task_id": "c1"})
    jl._on_text_command("evet")                      # kullanicinin gercek turu
    # Onay c1'e bagli: model baska gorevi canlandiramaz.
    assert _call(jl, "agent_loop", {"action": "retry", "task_id": "c2"}).startswith("CONFIRMATION_REQUIRED")
    assert _st("c2") == "failed"


def test_model_retry_requeues_once_after_user_yes(loop_env):
    jl = loop_env
    _call(jl, "agent_loop", {"action": "retry", "task_id": "c1"})
    jl._on_text_command("evet")
    _call(jl, "agent_loop", {"action": "retry", "task_id": "c1"})
    assert _st("c1") == "pending"


def test_model_list_and_cancel_still_work_without_approval(loop_env):
    assert "görev" in _call(loop_env, "agent_loop", {"action": "list"})
    _call(loop_env, "agent_loop", {"action": "cancel", "task_id": "c2"})
    assert _st("c2") == "cancelled"
