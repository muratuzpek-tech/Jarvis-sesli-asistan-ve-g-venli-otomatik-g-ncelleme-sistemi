"""tests/test_secret_at_rest.py

Adim 3.2c - hassas degerler diske HAM yazilmaz.

1) core/message_bus._log her istegi payload'iyla logs/message_bus.jsonl'e
   yaziyordu: vault_encrypt'in password'u diske ham dusuyordu. Artik
   payload (ve yanittaki sonuc) core/audit_log.mask_params ile maskelenir.

2) tasks/brain_tasks.json: plan adimlari, bekleyen adim (payload.pending_step)
   ve gecmis, adimin parameters'indaki parolayi ham tutuyordu. Artik gizli
   anahtarli degerler diske "***" olarak yazilir; gercek deger yalnizca
   surec belleginde kalir ve gorev okunurken geri konur - onaydan sonra adim
   gercek parolayla yeniden cozulur. Bellekte deger yoksa (ör. yeniden
   baslatma) adim "***" ile CALISTIRILMAZ, gorev acik bir hatayla duser.

Akis gercek: gercek MessageBus (loglama dahil), gercek executor_ai ve
guvenli_kasa sifrelemesi tmp dosyada calisir. Yalnizca LLM cagrilari
(security_ai aciklamasi, auditor yargisi) sabit cevap doner ve
planner'in cozumleyicisi adimin kendi parameters'ini vault_encrypt'e
yonlendirir (bugun planner ciktisini vault'a baglayan bir yol yok).

IZOLASYON: HOME/JARVIS_HOME tmp_path; gorev deposu ve bus logu tmp.
"""
import json
import logging
import os
from types import SimpleNamespace

import pytest

from jarvis.main import JarvisLive
import jarvis.core.brain_orchestrator as bo
import jarvis.core.message_bus as mb
import jarvis.core.task_manager as tm
from jarvis.brains.base_brain import BrainError

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


@pytest.fixture
def env(tmp_path, monkeypatch):
    home, jhome = tmp_path / "home", tmp_path / "jarvis_home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("JARVIS_HOME", str(jhome))
    saved = {}
    for name in ("jarvis.task_manager", "jarvis.watchdog", "jarvis.task_results"):
        lg = logging.getLogger(name)
        saved[name] = (lg.handlers[:], lg.level)
        lg.handlers = [logging.FileHandler(tmp_path / f"{name}.log", encoding="utf-8")]
        lg.setLevel(logging.INFO)
    import jarvis.core.watchdog as wd
    monkeypatch.setattr(wd, "LOGS_DIR", tmp_path)
    bus_log = tmp_path / "logs" / "message_bus.jsonl"
    monkeypatch.setattr(mb, "LOGS_DIR", bus_log.parent)
    monkeypatch.setattr(mb, "BUS_LOG_PATH", bus_log)

    from jarvis.brains.security_ai import SecurityAI
    from jarvis.brains.auditor_ai import AuditorAI

    def _no_llm(self, prompt):
        raise BrainError("test: LLM yok")

    monkeypatch.setattr(SecurityAI, "call_llm_json", _no_llm)
    monkeypatch.setattr(AuditorAI, "call_llm_json",
                        lambda self, prompt: {"passed": True, "reason": "test"})

    o = bo.BrainOrchestrator()
    tasks_path = tmp_path / "tasks" / "brain_tasks.json"
    o.tasks = tm.TaskManager(path=tasks_path)
    # Planner'in vault adimini executor'a yonlendiren cozumleyici: adimin
    # KENDI (diskten okunan) parameters'ini kullanir.
    o._resolve_action_with_file_modification = (
        lambda task, step, base_path=".": ("vault_encrypt", dict(step.get("parameters") or {})))
    jl = _jl()
    o._last_player = jl
    monkeypatch.setattr(bo, "get_orchestrator", lambda: o)

    doc = home / "belge.txt"
    doc.write_text("gizli belge içeriği", encoding="utf-8")
    assert str(tasks_path).startswith(str(tmp_path)) and os.environ["HOME"] == str(home)
    yield SimpleNamespace(orch=o, jl=jl, doc=doc, tasks_path=tasks_path, bus_log=bus_log)
    jl._set_pending_dangerous(None)
    for name, (handlers, level) in saved.items():
        lg = logging.getLogger(name)
        for h in lg.handlers:
            h.close()
        lg.handlers = handlers
        lg.setLevel(level)


def _vault_task(env):
    step = {"order": 1, "description": "Belgeyi şifrele", "agent": "executor_ai",
            "operation": "execute", "parameters": {"source": str(env.doc), "password": SECRET}}
    return env.orch.tasks.create(name="Belgeyi şifrele", agent="executor_ai", payload={
        "goal": "Belgeyi şifrele", "plan": [step], "step_index": 0,
        "history": [], "audit_retries": 0})["id"]


def _on_disk(env) -> dict[str, str]:
    return {"brain_tasks.json": env.tasks_path.read_text(encoding="utf-8"),
            "message_bus.jsonl": env.bus_log.read_text(encoding="utf-8") if env.bus_log.is_file() else ""}


def test_vault_password_never_written_to_disk_and_encryption_still_runs(env):
    o, jl = env.orch, env.jl
    tid = _vault_task(env)
    for where, text in _on_disk(env).items():
        assert SECRET not in text, f"olusturma sonrasi {where}"

    o._tick()                                           # HIGH risk -> onay istegi
    assert o.tasks.get(tid)["status"] == "waiting_approval"
    assert jl._brain_pending_task_id == tid
    for where, text in _on_disk(env).items():
        assert SECRET not in text, f"onay istegi sonrasi {where}"
    assert '"password": "***"' in _on_disk(env)["brain_tasks.json"]

    jl._on_text_command("evet")                         # kullanicinin gercek onayi
    for where, text in _on_disk(env).items():
        assert SECRET not in text, f"onay/yurutme sonrasi {where}"
    assert env.bus_log.is_file() and '"vault_encrypt"' in _on_disk(env)["message_bus.jsonl"]

    # Sifreleme gercekten GERCEK parolayla calisti ("***" ile degil).
    encrypted = env.doc.with_name(env.doc.name + ".encrypted")
    assert encrypted.is_file()
    from jarvis.guvenli_kasa import decrypt_file
    env.doc.unlink()
    assert decrypt_file(encrypted, SECRET).read_text(encoding="utf-8") == "gizli belge içeriği"
    o._tick()                                           # approve() gorevi pending'e birakir
    assert o.tasks.get(tid)["status"] == "completed"
    for where, text in _on_disk(env).items():
        assert SECRET not in text, f"tamamlanma sonrasi {where}"


def test_in_memory_task_view_keeps_real_value_for_execution(env):
    tid = _vault_task(env)
    step = env.orch.tasks.get(tid)["payload"]["plan"][0]
    assert step["parameters"]["password"] == SECRET    # bellekte gercek deger


def test_lost_secret_fails_closed_instead_of_running_with_mask(env):
    o, jl = env.orch, env.jl
    tid = _vault_task(env)
    tm._SECRETS.clear()                                 # surec yeniden baslatildi
    o._tick()                                           # gorev maskeli adimla yeniden soruluyor
    assert o.tasks.get(tid)["status"] == "waiting_approval"
    jl._on_text_command("evet")
    assert not env.doc.with_name(env.doc.name + ".encrypted").exists()
    task = o.tasks.get(tid)
    assert task["status"] == "failed"
    assert "bellekte" in str(task.get("error")).lower()


def test_non_secret_fields_are_stored_unchanged(env):
    tid = env.orch.tasks.create(name="x", agent="executor_ai", payload={
        "goal": "g", "file_modification": {"action": "create_file", "name": "a.txt", "content": "içerik"},
        "history": [{"step": {"description": "d"}, "audit": {"passed": True, "exit_code": 0}}]})["id"]
    raw = json.loads(env.tasks_path.read_text(encoding="utf-8"))
    payload = next(t for t in raw if t["id"] == tid)["payload"]
    assert payload["file_modification"]["content"] == "içerik"
    assert payload["history"][0]["audit"] == {"passed": True, "exit_code": 0}
