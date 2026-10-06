"""tests/test_step3_contracts.py

docs/GUVENLIK_KAPISI_PLAN.md Adim 3.2 - SONRAKI parcalarin (3.3-3.8)
sozlesme testleri. Uygulama henuz yok; testler bilerek KIRMIZI ve
xfail(strict=True, raises=...) ile isaretli: beklenen sebeple kirmizi
kaldikca paket yesil, biri beklenmedik sekilde gecerse (XPASS) ya da baska
bir sebeple kirilirsa paket kirmizi olur. Ilgili parca uygulandiginda o
testin xfail isareti kaldirilir.

Kullanici kararlari (2026-10-06):
  * Sesli arac yolu (E1/E2) onayi 60 sn gecerli.
  * Arka plan (Brain Team / agent_loop) bekleyen onayi 10 dakika sonra duser
    ve kullaniciya bildirilir.
  * "evet" yalnizca EN SON duyurulan istegi onaylar; "hayir" yalnizca onu
    iptal eder; digerleri bekler ve sirayla yeniden sorulur.
  * Ilgisiz kullanici metni yalnizca sesli arac yolunun bekleyenini siler;
    Brain ve agent_loop onaylarina dokunmaz.
  * file_controller move/delete_all onay kodu hicbir LLM'e (agent_loop
    planner'i, Brain auditor'u, canli model) gitmez; kullanicinin onayi
    yeterlidir.

Beklenen API (3.3): security_gate.MultiApprovalStore(clock=...)
  request(call, *, background=False, task_id=None) -> request_id  (en son duyurulan olur)
  latest() -> request_id | None
  answer(confirmed: bool) -> request_id | None   (yalnizca en son duyurulana)
  consume(call) -> bool                            (parmak izi + TTL + tek kullanim)
  pending_ids() -> list[str]
  expire() -> list[kayit]                          (suresi dolan arka plan istekleri)
  sabitler: FOREGROUND_TTL_S == 60, BACKGROUND_TTL_S == 600

IZOLASYON: HOME/JARVIS_HOME tmp_path; gercek ag/LLM yok (security_ai ve
auditor bus mesajlari ve planner LLM'i yakalanir).
"""
import asyncio
import importlib
import json
import logging
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

import jarvis.main as main_mod
from jarvis.main import JarvisLive
from jarvis import security_gate as sg
from jarvis.actions import agent_loop as al
import jarvis.core.brain_orchestrator as bo

S = sg.Source

class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _store():
    clock = _Clock()
    return sg.MultiApprovalStore(clock=clock), clock


def _call(tool="send_message", **args):
    return sg.resolve(tool, args or {"receiver": "Ali"}, S.MODEL_LIVE)


# ── 1) Cok-istekli onay deposu (3.3) ──

def test_store_ttl_constants():
    assert sg.FOREGROUND_TTL_S == 60
    assert sg.BACKGROUND_TTL_S == 600


def test_store_yes_approves_only_latest_announced_and_others_wait():
    store, _ = _store()
    a, b = _call(receiver="A"), _call(receiver="B")
    rid_a = store.request(a)
    rid_b = store.request(b)
    assert store.latest() == rid_b
    assert store.answer(True) == rid_b
    assert store.consume(a) is False          # A'ya onay tasinmadi
    assert store.consume(b) is True
    assert rid_a in store.pending_ids()
    assert store.latest() == rid_a            # A sirayla yeniden sorulur


def test_store_no_cancels_only_latest():
    store, _ = _store()
    a, b = _call(receiver="A"), _call(receiver="B")
    rid_a = store.request(a)
    rid_b = store.request(b)
    assert store.answer(False) == rid_b
    assert rid_b not in store.pending_ids()
    assert rid_a in store.pending_ids()
    assert store.consume(b) is False


def test_store_grant_is_single_use_and_bound_to_args():
    store, _ = _store()
    b = _call(receiver="B")
    store.request(b)
    store.answer(True)
    assert store.consume(_call(receiver="Baskasi")) is False
    assert store.consume(b) is True
    assert store.consume(b) is False


def test_store_grant_does_not_move_to_a_newer_request():
    store, _ = _store()
    b, c = _call(receiver="B"), _call(receiver="C")
    store.request(b)
    store.answer(True)
    store.request(c)                          # onaydan sonra yeni istek
    assert store.consume(c) is False


def test_store_foreground_grant_expires_after_60s():
    store, clock = _store()
    b = _call(receiver="B")
    store.request(b)
    store.answer(True)
    clock.t += 61
    assert store.consume(b) is False


def test_store_foreground_grant_valid_within_60s():
    store, clock = _store()
    b = _call(receiver="B")
    store.request(b)
    store.answer(True)
    clock.t += 59
    assert store.consume(b) is True


def test_store_background_request_expires_after_10_minutes_and_is_reported():
    store, clock = _store()
    bg = sg.resolve("send_message", {"receiver": "Ali"}, S.AGENT_LOOP)
    rid = store.request(bg, background=True, task_id="t1")
    clock.t += 599
    assert store.expire() == [] and rid in store.pending_ids()
    clock.t += 2
    expired = store.expire()
    assert [getattr(r, "task_id", None) for r in expired] == ["t1"]
    assert rid not in store.pending_ids()


# ── Ortak: canli oturum + sahte Brain orkestratoru ──

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


class _FakeTasks:
    def __init__(self, task):
        self.task = task

    def get(self, task_id):
        return self.task if task_id == self.task["id"] else None


class _FakeOrch:
    def __init__(self):
        self.approved = []
        self.step = {"agent": "executor_ai", "description": "Masaüstüne a.txt oluştur", "risk": "high"}
        self.tasks = _FakeTasks({"id": "t1", "status": "waiting_approval",
                                 "payload": {"pending_step": self.step}})

    def approve(self, task_id, **_kw):
        # main, depodaki kendi istegini tuketen onayin cagrisini verir (Adim 3.4).
        self.approved.append(task_id)
        return f"Onaylandı: {task_id}"

    def deny(self, task_id):
        return f"İptal: {task_id}"


@pytest.fixture
def live(monkeypatch):
    orch = _FakeOrch()
    monkeypatch.setattr(bo, "get_orchestrator", lambda: orch)
    sent = []
    monkeypatch.setattr(main_mod, "send_message", lambda **k: sent.append(k) or "gitti")
    jl = _jl()
    yield SimpleNamespace(jl=jl, orch=orch, sent=sent)
    jl._set_pending_dangerous(None)


def _e1(jl, name, args):
    resp = asyncio.run(jl._execute_tool(SimpleNamespace(name=name, args=args, id="1")))
    return str(resp.response.get("result", ""))


_MSG = {"receiver": "Ali", "message_text": "selam", "platform": "whatsapp"}


# ── 2) Yuva ezmesi (3.3-3.5) ──

def test_model_request_does_not_drop_pending_brain_approval(live):
    jl = live.jl
    jl.request_brain_team_approval("t1", live.orch.step, "Onay gerekiyor: a.txt")
    assert _e1(jl, "send_message", _MSG).startswith("CONFIRMATION_REQUIRED")
    # En son duyurulan send_message: "evet" onu onaylar.
    jl._on_text_command("evet")
    _e1(jl, "send_message", _MSG)
    assert len(live.sent) == 1
    assert live.orch.approved == []
    # Brain istegi kaybolmadi; ikinci "evet" onu onaylar.
    jl._on_text_command("evet")
    assert live.orch.approved == ["t1"]


def test_brain_announcement_does_not_drop_pending_model_request(live):
    jl = live.jl
    assert _e1(jl, "send_message", _MSG).startswith("CONFIRMATION_REQUIRED")
    jl.request_brain_team_approval("t1", live.orch.step, "Onay gerekiyor: a.txt")
    jl._on_text_command("evet")                 # en son duyurulan: Brain
    assert live.orch.approved == ["t1"]
    assert live.sent == []
    jl._on_text_command("evet")                 # sirada bekleyen: send_message
    _e1(jl, "send_message", _MSG)
    assert len(live.sent) == 1


def test_no_cancels_only_the_latest_announced_request(live):
    jl = live.jl
    jl.request_brain_team_approval("t1", live.orch.step, "Onay gerekiyor: a.txt")
    _e1(jl, "send_message", _MSG)
    jl._on_text_command("hayır")                # yalnizca send_message iptal
    _e1(jl, "send_message", _MSG)
    assert live.sent == []
    jl._on_text_command("evet")                 # Brain hala bekliyor
    assert live.orch.approved == ["t1"]


def test_unrelated_text_does_not_drop_background_approval(live):
    jl = live.jl
    jl.request_brain_team_approval("t1", live.orch.step, "Onay gerekiyor: a.txt")
    jl._on_text_command("saat kaç")
    jl._on_text_command("evet")
    assert live.orch.approved == ["t1"]


# ── 3) Onay kodu LLM'lere sizmaz (3.6 / 3.7) ──

_CODE_RE = re.compile(r"confirm_code|onay kodu", re.I)


@pytest.fixture
def files(tmp_path, monkeypatch):
    home = tmp_path / "home"
    for d in ("Desktop", "Documents"):
        (home / d).mkdir(parents=True)
    (home / "Desktop" / "a.txt").write_text("x", encoding="utf-8")
    (home / "Desktop" / "b.txt").write_text("y", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "jh"))
    for var in list(os.environ):
        if var.startswith("XDG_") and var.endswith("_DIR"):
            monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    import jarvis.actions.file_controller as fc_mod
    importlib.reload(fc_mod)
    assert Path.home().resolve().is_relative_to(tmp_path.resolve())
    yield SimpleNamespace(home=home, fc=fc_mod)
    monkeypatch.undo()
    importlib.reload(fc_mod)


_FC_STEPS = [
    ({"action": "move", "path": "desktop", "name": "a.txt", "destination": "documents"},
     lambda h: (h / "Documents" / "a.txt").is_file() and not (h / "Desktop" / "a.txt").exists()),
    ({"action": "delete_all_files", "path": "desktop"},
     lambda h: not any((h / "Desktop").iterdir())),
]


@pytest.mark.parametrize("params,done", _FC_STEPS, ids=["move", "delete_all"])
@pytest.mark.xfail(strict=True, raises=AssertionError,
                   reason="3.6: file_controller onizleme kodu agent_loop gecmisine/planner'a gidiyor")
def test_agent_loop_file_code_never_reaches_planner(files, tmp_path, monkeypatch, params, done):
    monkeypatch.setattr(al, "TASKS_PATH", tmp_path / "agent_tasks.json")
    monkeypatch.setattr(al, "LOG_PATH", tmp_path / "agent_loop_log.jsonl")
    monkeypatch.setattr(al, "_approval_hook", None, raising=False)
    # Cop kutusu yerine dogrudan sil (tmp HOME; gercek cop kutusuna dokunulmaz).
    monkeypatch.setattr(files.fc, "_safe_trash", lambda p: Path(p).unlink() or f"silindi: {p}")
    pending = {"tool": "file_controller", "parameters": dict(params), "note": "kullanıcı istedi"}
    al._save_tasks([{"id": "f1", "goal": "masaüstündeki dosyayı taşı", "status": "awaiting_approval",
                     "created_at": "2026-10-06T10:00:00", "updated_at": "2026-10-06T10:00:00",
                     "history": [], "pending_action": pending}])
    out = al.approve_task("f1", expected_action=json.loads(json.dumps(pending)))
    prompts = []
    import jarvis.actions.local_llm as llm
    monkeypatch.setattr(llm, "generate_with_fallback",
                        lambda *a, **k: prompts.append(k.get("prompt_for_ollama", "")) or '{"done": true}')
    task = al._load_tasks()[0]
    al._process_task(task, [])
    seen = json.dumps(task.get("history", []), ensure_ascii=False) + out + "".join(prompts)
    assert not _CODE_RE.search(seen), seen[:400]
    assert done(files.home)                   # kullanicinin tek onayi yeterli


@pytest.fixture
def brain(files, tmp_path, monkeypatch):
    saved = {}
    for name in ("jarvis.task_manager", "jarvis.watchdog", "jarvis.task_results"):
        lg = logging.getLogger(name)
        saved[name] = (lg.handlers[:], lg.level)
        lg.handlers = [logging.FileHandler(tmp_path / f"{name}.log", encoding="utf-8")]
    import jarvis.core.watchdog as wd
    monkeypatch.setattr(wd, "LOGS_DIR", tmp_path)
    from jarvis.core.task_manager import TaskManager
    from jarvis.brains.security_ai import SecurityAI
    o = bo.BrainOrchestrator()
    o.tasks = TaskManager(path=tmp_path / "brain_tasks.json")
    o._last_player = None
    seen = {"auditor": [], "spoken": []}
    real_send = o.bus.send

    def _send(frm, to, desc, payload=None):
        if to == "security_ai":            # LLM'siz deterministik siniflandirma
            risk = SecurityAI.classify_risk(payload.get("tool"), payload.get("action"))
            return {"status": "completed", "result": {"risk": risk, "reason": "test"}}
        if to == "auditor_ai":
            seen["auditor"].append(json.dumps(payload, ensure_ascii=False, default=str))
            return {"status": "completed", "result": {"passed": True, "reason": "test"}}
        return real_send(frm, to, desc, payload=payload)

    o.bus.send = _send
    yield SimpleNamespace(orch=o, seen=seen, home=files.home)
    for name, (handlers, level) in saved.items():
        lg = logging.getLogger(name)
        for h in lg.handlers:
            h.close()
        lg.handlers = handlers
        lg.setLevel(level)


@pytest.mark.xfail(strict=True, raises=AssertionError,
                   reason="3.7: Brain move onizleme kodu auditor'a gidiyor, tasima hic tamamlanmiyor")
def test_brain_file_move_code_never_reaches_auditor(brain):
    o = brain.orch
    t = o.tasks.create(name="[FILE_MODIFICATION] taşı", agent="executor_ai", payload={
        "goal": "Masaüstündeki a.txt'yi belgelere taşı",
        "plan": [{"order": 1, "description": "Masaüstündeki a.txt'yi belgelere taşı",
                  "agent": "executor_ai", "operation": "execute"}],
        "step_index": 0, "history": [], "audit_retries": 0,
        "file_modification": {"action": "move", "path": "desktop", "name": "a.txt",
                              "destination": "documents"},
    })
    o._tick()
    assert o.tasks.get(t["id"])["status"] == "waiting_approval"
    o.approve(t["id"])                          # kullanicinin gercek onayi (main.py yolu)
    assert brain.seen["auditor"]
    assert not any(_CODE_RE.search(p) for p in brain.seen["auditor"]), brain.seen["auditor"]
    assert (brain.home / "Documents" / "a.txt").is_file()


# ── 4) waiting_approval zaman asimi (3.8) ──
#
# Sozlesme: onay isteginin yasi gorevin updated_at'inden (bekleme durumuna
# gecis ani) olculur. 10 dk dolunca gorev duser (failed/cancelled, hata
# metninde "zaman aşımı"), kullaniciya bildirilir ve audit'e
# "approval_expired" yazilir.

def _ago(minutes):
    return (datetime.now() - timedelta(minutes=minutes)).isoformat()


def _audit_events(source):
    from jarvis.paths import memory_dir
    p = memory_dir() / "audit.log"
    rows = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()] \
        if p.is_file() else []
    return [r["action"] for r in rows if r.get("source") == source]


def _age_brain_task(o, tid, minutes):
    tasks = o.tasks._load()
    for t in tasks:
        if t["id"] == tid:
            t["updated_at"] = _ago(minutes)
    o.tasks._save(tasks)


@pytest.mark.xfail(strict=True, raises=AssertionError,
                   reason="3.8: Brain waiting_approval icin zaman asimi yok")
def test_brain_waiting_approval_expires_after_10_minutes_and_user_is_told(brain):
    o = brain.orch
    player = _jl()
    o._last_player = player
    o._risk_of_step = lambda step, task=None: ("high", "test")
    t = o.tasks.create(name="x", agent="executor_ai", payload={
        "goal": "Masaüstüne a.txt oluştur",
        "plan": [{"order": 1, "description": "Masaüstüne a.txt oluştur", "agent": "executor_ai",
                  "operation": "execute"}],
        "step_index": 0, "history": [], "audit_retries": 0})
    o._tick()
    _age_brain_task(o, t["id"], 9)
    o._tick()
    assert o.tasks.get(t["id"])["status"] == "waiting_approval"
    _age_brain_task(o, t["id"], 11)
    o._tick()
    task = o.tasks.get(t["id"])
    assert task["status"] in ("failed", "cancelled")
    assert "zaman aşımı" in str(task.get("error") or task.get("result") or "").lower()
    assert any("zaman aşımı" in s.lower() for s in player.spoken + player.ui.logs)
    assert "approval_expired" in _audit_events("brain_team")


@pytest.mark.xfail(strict=True, raises=AssertionError,
                   reason="3.8: agent_loop awaiting_approval icin zaman asimi yok")
def test_agent_loop_awaiting_approval_expires_after_10_minutes_and_user_is_told(tmp_path, monkeypatch):
    monkeypatch.setattr(al, "TASKS_PATH", tmp_path / "agent_tasks.json")
    monkeypatch.setattr(al, "LOG_PATH", tmp_path / "agent_loop_log.jsonl")
    jl = _jl()
    al.set_approval_hook(jl.request_agent_loop_approval)
    try:
        pending = {"tool": "send_message", "parameters": {"receiver": "Ali"}, "note": "n"}
        al._save_tasks([
            {"id": "fresh", "goal": "g", "status": "awaiting_approval", "created_at": _ago(9),
             "updated_at": _ago(9), "history": [], "pending_action": dict(pending)},
            {"id": "old", "goal": "g", "status": "awaiting_approval", "created_at": _ago(11),
             "updated_at": _ago(11), "history": [], "pending_action": dict(pending)},
        ])
        al._tick()
        status = {t["id"]: t for t in al._load_tasks()}
        assert status["fresh"]["status"] == "awaiting_approval"
        assert status["old"]["status"] in ("failed", "cancelled")
        assert "zaman aşımı" in json.dumps(status["old"], ensure_ascii=False).lower()
        assert any("zaman aşımı" in s.lower() for s in jl.spoken + jl.ui.logs)
        assert "approval_expired" in _audit_events("agent_loop")
    finally:
        al._approval_hook = None
