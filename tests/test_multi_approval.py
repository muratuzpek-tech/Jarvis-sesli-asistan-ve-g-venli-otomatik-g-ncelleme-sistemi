"""tests/test_multi_approval.py

Adim 3.3: tek onay yuvasi yerine cok-istekli onay deposu
(security_gate.MultiApprovalStore). tests/test_step3_contracts.py'deki
sozlesme testlerinin KAPSAMADIGI durumlar:

1) agent_loop(retry) (modelin sesli arac cagrisi) ile arka plan agent_loop
   onayi ayni "agent_loop" adini tasiyordu; retry istegi arka plan yuvasini
   eziyor, kullanicinin "evet"i yanlis parmak iziyle tuketilip ikisini de
   dusuruyordu. Artik ayri request_id ve tur (voice / agent_loop).
2) Model "discovered_*" adli uydurma bir arac cagirinca fail-closed spec
   yuzunden NEEDS_APPROVAL alip bekleyen Brain onayini eziyordu. Modelin
   arac listesinde olmayan discovered_* artik DENY; depoya dokunmaz.
3) Sesli kismi transkript ayni tur icinde tekrar tekrar isleniyor
   ("hayır", "hayır istemiyorum"): "hayir" tur basinda duyurulmus TEK istegi
   iptal eder; ayni turun tamamlanmasi siradaki Brain istegini reddetmez.
4) Cevaplanan istekten sonra sirada bekleyen istek kullaniciya yeniden
   sorulur.
5) Kullanicinin reddettigi cagri model tarafindan hemen yeniden istenirse
   kuyrugun basina gecmez; kullanicinin yeni (ret olmayan) turundan sonra
   yeniden sorulabilir.
6) Ilgisiz metin TUM sesli arac isteklerini siler, arka plan isteklerine
   dokunmaz.

IZOLASYON: HOME/JARVIS_HOME tmp_path; ag/LLM yok.
"""
import asyncio
from types import SimpleNamespace

import pytest

import jarvis.main as main_mod
from jarvis.main import JarvisLive
from jarvis import security_gate as sg
from jarvis.actions import agent_loop as al
from jarvis.actions import tools_kopru
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


class _FakeTasks:
    def __init__(self, task):
        self.task = task

    def get(self, task_id):
        return self.task if task_id == self.task["id"] else None


class _FakeOrch:
    def __init__(self):
        self.approved, self.denied = [], []
        self.step = {"agent": "executor_ai", "description": "Masaüstüne a.txt oluştur", "risk": "high"}
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
def live(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "jh"))
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


def _msg(receiver="Ali"):
    return {"receiver": receiver, "message_text": "selam", "platform": "whatsapp"}


def _announce_brain(live):
    live.jl.request_brain_team_approval("t1", live.orch.step, "Onay gerekiyor: a.txt")


# ── 1) agent_loop(retry) ve arka plan agent_loop onayi ayri istekler ──

@pytest.fixture
def loop_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "jh"))
    monkeypatch.setattr(al, "TASKS_PATH", tmp_path / "agent_tasks.json")
    monkeypatch.setattr(al, "LOG_PATH", tmp_path / "agent_loop_log.jsonl")
    monkeypatch.setattr(al, "_approval_hook", None, raising=False)
    sent = []
    monkeypatch.setitem(tools_kopru.ALLOWED_TOOLS, "send_message",
                        lambda parameters: sent.append(dict(parameters)) or "gitti")
    pending = {"tool": "send_message", "parameters": {"receiver": "Ali", "message_text": "arka plan"},
               "note": "mesaj"}
    al._save_tasks([
        {"id": "c1", "goal": "iptal edilen", "status": "cancelled", "created_at": "2026-10-06T10:00:00",
         "updated_at": "2026-10-06T10:00:00", "history": [], "pending_action": None},
        {"id": "a1", "goal": "Ali'ye yaz", "status": "awaiting_approval", "created_at": "2026-10-06T10:00:00",
         "updated_at": "2026-10-06T10:00:00", "history": [], "pending_action": pending},
    ])
    jl = _jl()
    yield SimpleNamespace(jl=jl, sent=sent)
    jl._set_pending_dangerous(None)


def _status(tid):
    return next(t for t in al._load_tasks() if t["id"] == tid)["status"]


def test_model_retry_and_background_agent_loop_approval_are_separate_requests(loop_env):
    jl = loop_env.jl
    al.set_approval_hook(jl.request_agent_loop_approval)       # a1 duyurulur
    try:
        assert jl._agent_pending_task_id == "a1"
        assert _e1(jl, "agent_loop", {"action": "retry", "task_id": "c1"}).startswith("CONFIRMATION_REQUIRED")
        ids = jl._approvals.pending_ids()
        assert len(ids) == 2 and len(set(ids)) == 2
        # En son duyurulan: retry. "evet" yalnizca onu onaylar.
        jl._on_text_command("evet")
        assert loop_env.sent == [] and _status("a1") == "awaiting_approval"
        assert _e1(jl, "agent_loop", {"action": "retry", "task_id": "c1"}).startswith("'c1'")
        assert _status("c1") == "pending"
        # Arka plan istegi kaybolmadi; sirada o var.
        assert jl._agent_pending_task_id == "a1"
        jl._on_text_command("evet")
        assert loop_env.sent == [{"receiver": "Ali", "message_text": "arka plan"}]
    finally:
        al._approval_hook = None


# ── 2) Uydurma discovered_* bekleyen onayi etkilemez ──

def test_model_invented_discovered_tool_is_denied_and_keeps_pending_brain(live):
    jl = live.jl
    _announce_brain(live)
    before = jl._approvals.pending_ids()
    out = _e1(jl, "discovered_uydurma", {"x": "1"})
    assert out.startswith("BLOCKED"), out
    assert jl._approvals.pending_ids() == before
    jl._on_text_command("evet")
    assert live.orch.approved == ["t1"]


def test_real_discovered_tools_still_need_approval():
    for name in ("discovered_topydo", "discovered_jc"):
        d = sg.authorize(name, {}, sg.Source.MODEL_LIVE)
        assert d.verdict is sg.Verdict.NEEDS_APPROVAL, name
    assert sg.authorize("discovered_uydurma", {}, sg.Source.MODEL_LIVE).verdict is sg.Verdict.DENY
    # agent_loop yolunda entegre edilen discovered_* araclar fail-closed kalir.
    assert sg.authorize("discovered_yeni", {}, sg.Source.AGENT_LOOP).verdict is sg.Verdict.NEEDS_APPROVAL


# ── 3) Kismi sesli transkript: "hayir" tur basindaki tek istegi iptal eder ──

def test_spoken_no_cancels_one_request_per_turn_and_keeps_brain(live):
    jl = live.jl
    _announce_brain(live)
    assert _e1(jl, "send_message", _msg()).startswith("CONFIRMATION_REQUIRED")
    jl._apply_spoken_confirmation("hayır")
    jl._apply_spoken_confirmation("hayır istemiyorum")       # ayni turun devami
    assert jl._pending_dangerous_action == "brain_team"
    handler = jl._voice_turn_reply_handler("hayır istemiyorum")  # tur tamamlandi
    assert handler is None
    assert live.orch.denied == [] and jl._brain_pending_task_id == "t1"
    # Sonraki turdaki "evet" Brain'i onaylar.
    jl._apply_spoken_confirmation("evet")
    handler = jl._voice_turn_reply_handler("evet")
    assert handler is not None
    handler()
    assert live.orch.approved == ["t1"]


def test_spoken_yes_for_voice_request_does_not_approve_queued_brain(live):
    jl = live.jl
    _announce_brain(live)
    _e1(jl, "send_message", _msg())
    jl._apply_spoken_confirmation("evet")
    handler = jl._voice_turn_reply_handler("evet")
    if handler is not None:
        handler()
    assert live.orch.approved == []
    _e1(jl, "send_message", _msg())
    assert len(live.sent) == 1


# ── 4) Siradaki istek yeniden sorulur ──

def test_queued_request_is_asked_again_after_the_latest_is_answered(live):
    jl = live.jl
    _e1(jl, "send_message", _msg())
    _announce_brain(live)
    jl.spoken.clear()
    jl._on_text_command("evet")                     # Brain onaylandi
    assert live.orch.approved == ["t1"]
    asked = "\n".join(jl.spoken)
    assert "[ONAY_SIRADA]" in asked and "send_message" in asked
    assert "selam" not in asked                     # icerik maskeli (3.2b)


# ── 5) Reddedilen cagri hemen yeniden istenemez ──

def test_model_cannot_requeue_a_just_rejected_call_until_next_user_turn(live):
    jl = live.jl
    _e1(jl, "send_message", _msg())
    jl._on_text_command("hayır")
    out = _e1(jl, "send_message", _msg())
    assert not out.startswith("CONFIRMATION_REQUIRED") and "reddetti" in out, out
    assert jl._approvals.pending_ids() == []
    jl._on_text_command("aslında gönder")           # kullanicinin yeni turu
    assert _e1(jl, "send_message", _msg()).startswith("CONFIRMATION_REQUIRED")
    jl._on_text_command("evet")
    _e1(jl, "send_message", _msg())
    assert len(live.sent) == 1


# ── 6) Ilgisiz metin yalnizca sesli arac isteklerini siler ──

def test_unrelated_text_drops_every_voice_request_but_keeps_brain(live):
    jl = live.jl
    _announce_brain(live)
    _e1(jl, "send_message", _msg("A"))
    _e1(jl, "send_message", _msg("B"))
    assert len(jl._approvals.pending_ids()) == 3
    jl._on_text_command("saat kaç")
    assert len(jl._approvals.pending_ids()) == 1
    assert jl._pending_dangerous_action == "brain_team"
    jl._on_text_command("evet")
    assert live.orch.approved == ["t1"]
    assert live.sent == []


def test_store_request_ids_are_distinct_for_same_fingerprint_in_different_kinds():
    store = sg.MultiApprovalStore(clock=lambda: 0.0)
    voice = sg.resolve("send_message", {"receiver": "Ali"}, sg.Source.MODEL_LIVE)
    rid_v = store.request(voice)
    rid_b = store.request(voice, background=True, task_id="t9", kind=sg.KIND_AGENT_LOOP)
    assert rid_v != rid_b and store.pending_ids() == [rid_v, rid_b]
    assert store.get(rid_v).kind == sg.KIND_VOICE and store.get(rid_b).kind == sg.KIND_AGENT_LOOP
