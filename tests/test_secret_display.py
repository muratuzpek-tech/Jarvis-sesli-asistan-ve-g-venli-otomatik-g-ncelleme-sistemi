"""tests/test_secret_display.py

Adim 3.2b:

1) Onay metinleri hassas argumanlari HAM yaziyordu:
   * Brain: _describe_pending_step dosya disi araclarda hedefi "k=v" diye
     (ilk 60 karakter) yaziyordu -> vault_encrypt'in password'u onay
     isteginde, speak() ile modele, ekran loguna ve task_results.log'a
     gidiyordu.
   * agent_loop: describe_pending_action argumanlari ilk 120 karakterle yaziyordu.
   * security_gate._approval_texts: params[:40] kesmesi kisa bir parolayi
     aynen gosteriyordu.
   Artik gizli anahtarli argumanlar (password, parola, token, secret, key,
   api_key, confirm_code ...) "***", icerik alanlari (content, text,
   message_text ...) yalnizca uzunluklariyla gorunur.

2) Bir adim calistiktan sonra _finish_step hata verirse audit'e hem
   "executed" hem "execution_failed" dusuyordu. Artik tek tutarli dizi:
   "executed" ardindan "post_failure" (ayni task_id ve fingerprint).
   Onayli adimda: "approved_executed" ardindan "post_failure".

IZOLASYON: HOME/JARVIS_HOME tmp_path; security_ai ve auditor bus mesajlari
yakalanir (LLM yok); sifreleme tmp dosya uzerinde gercek calisir.
"""
import asyncio
import json
import logging
from types import SimpleNamespace

import pytest

from jarvis.main import JarvisLive
from jarvis import security_gate as sg
from jarvis.actions import agent_loop as al
import jarvis.core.brain_orchestrator as bo
import jarvis.core.message_bus as mb

SECRET = "S3cret!pw"


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


def _audit_rows():
    from jarvis.paths import memory_dir
    p = memory_dir() / "audit.log"
    if not p.is_file():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


@pytest.fixture
def brain(tmp_path, monkeypatch):
    saved = {}
    for name in ("jarvis.task_manager", "jarvis.watchdog", "jarvis.task_results"):
        lg = logging.getLogger(name)
        saved[name] = (lg.handlers[:], lg.level)
        lg.handlers = [logging.FileHandler(tmp_path / f"{name}.log", encoding="utf-8")]
        lg.setLevel(logging.INFO)
    import jarvis.core.watchdog as wd
    monkeypatch.setattr(wd, "LOGS_DIR", tmp_path)
    monkeypatch.setattr(mb, "BUS_LOG_PATH", tmp_path / "message_bus.jsonl")
    from jarvis.core.task_manager import TaskManager
    from jarvis.brains.security_ai import SecurityAI
    o = bo.BrainOrchestrator()
    o.tasks = TaskManager(path=tmp_path / "brain_tasks.json")
    auditor = []
    real_send = o.bus.send

    def _send(frm, to, desc, payload=None):
        if to == "security_ai":
            risk = SecurityAI.classify_risk(payload.get("tool"), payload.get("action"))
            return {"status": "completed", "result": {"risk": risk, "reason": "test"}}
        if to == "auditor_ai":
            auditor.append(json.dumps(payload, ensure_ascii=False, default=str))
            return {"status": "completed", "result": {"passed": True, "reason": "test"}}
        return real_send(frm, to, desc, payload=payload)

    o.bus.send = _send
    jl = _jl()
    o._last_player = jl
    monkeypatch.setattr(bo, "get_orchestrator", lambda: o)
    yield SimpleNamespace(orch=o, jl=jl, auditor=auditor, tmp=tmp_path)
    jl._set_pending_dangerous(None)
    for name, (handlers, level) in saved.items():
        lg = logging.getLogger(name)
        for h in lg.handlers:
            h.close()
        lg.handlers = handlers
        lg.setLevel(level)


def _plan(desc):
    return {"goal": desc, "step_index": 0, "history": [], "audit_retries": 0,
            "plan": [{"order": 1, "description": desc, "agent": "executor_ai", "operation": "execute"}]}


# ── 1) Hassas argumanlar onay metinlerinde gorunmez ──

def test_brain_vault_password_never_appears_in_any_user_or_model_text(brain):
    o, jl = brain.orch, brain.jl
    doc = brain.tmp / "belge.txt"
    doc.write_text("gizli belge", encoding="utf-8")
    # Onay metni, risk ve yurutme ayni kok cozumleyiciden gecer.
    o._resolve_action_with_file_modification = lambda task, step, base_path=".": (
        "vault_encrypt", {"source": str(doc), "password": SECRET})
    t = o.tasks.create(name="Belgeyi şifrele", agent="executor_ai", payload=_plan("Belgeyi şifrele"))
    o._tick()
    assert o.tasks.get(t["id"])["status"] == "waiting_approval"
    assert jl._brain_pending_task_id == t["id"]       # onay istegi canli oturuma gitti
    jl._on_text_command("evet")                        # kullanicinin gercek onayi
    assert brain.auditor, "adim yurutulmedi / auditor'a gitmedi"

    task_results = (brain.tmp / "jarvis.task_results.log").read_text(encoding="utf-8")
    texts = {
        "speak (model)": "\n".join(jl.spoken),
        "ekran logu": "\n".join(jl.ui.logs),
        "task_results.log": task_results,
        "auditor": "\n".join(brain.auditor),
        "durum": o.list_status(),
        "audit.log": json.dumps(_audit_rows(), ensure_ascii=False),
    }
    for where, text in texts.items():
        assert SECRET not in text, where
    assert "password=***" in texts["speak (model)"]


def test_brain_describe_pending_step_masks_secrets_and_content(brain):
    o = brain.orch
    o._resolve_action_with_file_modification = lambda task, step, base_path=".": ("vault_encrypt", {
        "source": "/tmp/a.txt", "password": SECRET, "api_key": "AIzaKEY", "parola": "Parola1",
        "confirm_code": "abc123", "content": "uzun gizli içerik", "text": "metin içeriği"})
    info = o._describe_pending_step({"payload": {}}, {"agent": "executor_ai", "description": "x"})
    target = info["target"]
    for raw in (SECRET, "AIzaKEY", "Parola1", "abc123", "uzun gizli içerik", "metin içeriği"):
        assert raw not in target, raw
    assert "password=***" in target and "source=/tmp/a.txt" in target


def test_agent_loop_approval_message_masks_secrets_and_content():
    task = {"id": "t1", "goal": "g", "pending_action": {"tool": "send_message", "parameters": {
        "receiver": "Ali", "message_text": "çok özel mesaj", "token": "tok-" + SECRET,
        "secret": SECRET, "key": "k-" + SECRET}}}
    text = al.describe_pending_action(task)
    assert SECRET not in text and "çok özel mesaj" not in text
    assert "receiver=Ali" in text and "token=***" in text
    assert "message_text=<14 karakter gizlendi>" in text


def test_gate_approval_texts_mask_short_secrets_and_content():
    d = sg.authorize("send_message", {"receiver": "Ali", "message_text": "kısa", "password": "pw1"},
                     sg.Source.MODEL_LIVE)
    assert d.verdict is sg.Verdict.NEEDS_APPROVAL
    for text in (d.model_message, d.user_prompt):
        assert "pw1" not in text and "=kısa" not in text
        assert "password=***" in text and "receiver=Ali" in text


def test_e1_confirmation_text_to_model_has_no_secret():
    jl = _jl()
    resp = asyncio.run(jl._execute_tool(SimpleNamespace(
        name="send_message", args={"receiver": "Ali", "message_text": "selam", "token": SECRET}, id="1")))
    out = str(resp.response.get("result"))
    assert out.startswith("CONFIRMATION_REQUIRED") and SECRET not in out
    jl._set_pending_dangerous(None)


# ── 2) _finish_step hatasinda tek tutarli olay dizisi ──

def _events(task_id):
    return [r for r in _audit_rows() if r.get("source") == "brain_team" and r.get("task_id") == task_id]


def _boom(*_a, **_k):
    raise RuntimeError("denetim çöktü")


def test_post_failure_after_unapproved_execution_is_a_single_sequence(brain):
    o = brain.orch
    o._risk_of_step = lambda step, task=None: ("low", "test")
    o._execute_step = lambda task, step: {"status": "completed", "result": "yapıldı"}
    o._finish_step = _boom
    t = o.tasks.create(name="x", agent="executor_ai", payload=_plan("Masaüstüne a.txt oluştur"))
    o._tick()
    rows = _events(t["id"])
    assert [r["action"] for r in rows] == ["executed", "post_failure"]
    assert rows[0]["fingerprint"] == rows[1]["fingerprint"]
    assert rows[1]["executed"] is True and "denetim çöktü" in rows[1]["result"]
    # Eski davranis korunur: adim basarisiz sayilir, gorev ilerler.
    payload = o.tasks.get(t["id"])["payload"]
    assert payload["failed_steps"] == ["Masaüstüne a.txt oluştur"] and payload["step_index"] == 1


def test_execution_error_is_a_single_execution_failed_event(brain):
    o = brain.orch
    o._risk_of_step = lambda step, task=None: ("low", "test")
    o._execute_step = _boom
    t = o.tasks.create(name="x", agent="executor_ai", payload=_plan("Masaüstüne a.txt oluştur"))
    o._tick()
    assert [r["action"] for r in _events(t["id"])] == ["execution_failed"]


def test_post_failure_after_approved_execution_is_a_single_sequence(brain):
    o = brain.orch
    o._risk_of_step = lambda step, task=None: ("high", "test")
    o._execute_step = lambda task, step: {"status": "completed", "result": "yapıldı"}
    o._finish_step = _boom
    t = o.tasks.create(name="x", agent="executor_ai", payload=_plan("Masaüstüne a.txt oluştur"))
    o._tick()
    task = o.tasks.get(t["id"])
    # main.py'nin kullanici-turu yolu gibi: onaylanan cagri verilir (Adim 3.4).
    out = o.approve(t["id"], approved_call=o._approval_call(task, task["payload"]["pending_step"]))
    rows = _events(t["id"])
    assert [r["action"] for r in rows] == ["approval_requested", "approved_executed", "post_failure"]
    assert len({r["fingerprint"] for r in rows}) == 1
    assert rows[1]["approved"] is True and rows[2]["approved"] is True
    assert "hata" in out.lower()
    assert o.tasks.get(t["id"])["status"] == "failed"
